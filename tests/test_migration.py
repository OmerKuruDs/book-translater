"""Acceptance checks O-18 and O-19 of the DB design v1.1 (docs/07_db_design_v1_1.md, section 11).

The v1 file is built from ``tests/fixtures/v1_schema.sql`` (the DDL snapshot frozen before
``models.py`` changed, section 8.6 / DQ1) and seeded with raw ``sqlite3``.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import Connection, Engine, event

from book_translator.database import models as m
from book_translator.database import session as session_module
from book_translator.database.repository import JobRepository, OverlayBlockRepository
from book_translator.database.session import (
    ARCHIVE_DIR_NAME,
    MigrationContext,
    close_database,
    open_database,
)
from book_translator.domain.result import Err, ErrorCode, unwrap
from tests.conftest import RUN_ID, TOOL_VERSION, capture_statements, raw_rows

FIXTURES = Path(__file__).resolve().parent / "fixtures"
V1_SCHEMA_SQL = FIXTURES / "v1_schema.sql"
SAMPLE_STATE_DB = (
    Path(__file__).resolve().parent.parent / "docs" / "test_doc_output" / ("translation_state.db")
)

JOB_ID = "11111111-2222-3333-4444-555555555555"
V1_TABLES: Tuple[str, ...] = ("jobs", "chunks", "runs", "lease")
V2_TABLES: Tuple[str, ...] = ("jobs", "runs", "overlay_pages", "overlay_blocks")
TS = "2026-09-01T10:00:00.000000Z"


def build_v1_file(path: Path, *, seed: bool = True) -> Path:
    """Create a schema-v1 state database at ``path`` from the frozen DDL snapshot."""
    script = V1_SCHEMA_SQL.read_text(encoding="utf-8")
    with sqlite3.connect(path) as raw:
        raw.executescript(script)
        if seed:
            _seed_v1(raw)
    return path


def _seed_v1(raw: sqlite3.Connection) -> None:
    """1 job, 5 chunks (one COMPLETED, one FAILED, one PENDING with backoff), 2 runs, 1 lease."""
    raw.execute(
        "INSERT INTO jobs (id, singleton, input_path, input_sha256, status, tool_version,"
        " warnings, created_at, updated_at)"
        " VALUES (?, 1, 'C:/books/in.pdf', ?, 'TRANSLATING', '0.1.0-v1', '[]', ?, ?)",
        (JOB_ID, "a" * 64, TS, TS),
    )
    chunk_rows: List[Tuple[Any, ...]] = []
    for chunk_id in range(1, 6):
        text = f"Paragraph {chunk_id} of the v1 book.\n\n"
        status = {1: "COMPLETED", 2: "FAILED", 3: "PENDING"}.get(chunk_id, "PENDING")
        chunk_rows.append(
            (
                chunk_id,
                JOB_ID,
                "TEXT",
                1,
                text,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
                len(text),
                '["Chapter 1"]',
                status,
                1 if chunk_id == 3 else 0,
                "2026-09-01T10:05:00.000000Z" if chunk_id == 3 else None,
                "PROVIDER_BAD_REQUEST" if chunk_id == 2 else None,
                "400 bad request" if chunk_id == 2 else None,
                "Çeviri 1.\n\n" if chunk_id == 1 else None,
                0,
                "[]",
                "deepl" if chunk_id == 1 else None,
                "native" if chunk_id == 1 else None,
                "g" * 64 if chunk_id == 1 else None,
                10 if chunk_id == 1 else 0,
                11 if chunk_id == 1 else 0,
                120 if chunk_id == 1 else None,
                1 if chunk_id in (1, 2) else 0,
                "run000000001" if chunk_id in (1, 2, 3) else None,
                None,
                TS if chunk_id == 1 else None,
                TS,
                TS,
            )
        )
    raw.executemany(
        "INSERT INTO chunks (id, job_id, kind, translatable, source_text, content_hash,"
        " char_count, heading_path, status, retry_count, next_attempt_at, last_error_code,"
        " last_error, translated_text, review_flag, warnings, provider, glossary_strategy,"
        " glossary_hash, chars_sent, chars_billed, latency_ms, attempts, last_run_id,"
        " claimed_at, completed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        chunk_rows,
    )
    raw.execute(
        "INSERT INTO runs (id, job_id, command, tool_version, provider,"
        " provider_switch_allowed, started_at, finished_at, outcome, exit_code,"
        " chunks_completed, chunks_failed, chars_sent, provider_calls, rate_limited_count)"
        " VALUES ('run000000001', ?, 'translate', '0.1.0-v1', 'deepl', 0, ?, ?, 'PARTIAL', 2,"
        " 1, 1, 10, 2, 0)",
        (JOB_ID, TS, "2026-09-01T10:10:00.000000Z"),
    )
    raw.execute(
        "INSERT INTO runs (id, job_id, command, tool_version, provider,"
        " provider_switch_allowed, started_at, finished_at, outcome, exit_code,"
        " chunks_completed, chunks_failed, chars_sent, provider_calls, rate_limited_count)"
        " VALUES ('run000000002', ?, 'translate', '0.1.0-v1', 'deepl', 1, ?, NULL, NULL, NULL,"
        " 0, 0, 0, 0, 0)",
        (JOB_ID, "2026-09-01T11:00:00.000000Z"),
    )
    raw.execute(
        "INSERT INTO lease (job_id, holder_pid, holder_host, run_id, acquired_at, heartbeat_at)"
        " VALUES (?, 4242, 'host-v1', 'run000000002', ?, ?)",
        (JOB_ID, "2026-09-01T11:00:00.000000Z", "2026-09-01T11:00:30.000000Z"),
    )


def _v1_columns(path: Path) -> Dict[str, List[str]]:
    with sqlite3.connect(path) as raw:
        return {
            table: [str(row[1]) for row in raw.execute(f"PRAGMA table_info({table})")]
            for table in V1_TABLES
        }


def canonical_dump(path: Path, columns: Dict[str, List[str]]) -> str:
    """sha256 of ``SELECT <v1 columns> FROM <table> ORDER BY rowid`` for the four v1 tables."""
    digest = hashlib.sha256()
    with sqlite3.connect(path) as raw:
        for table in V1_TABLES:
            cols = ", ".join(columns[table])
            for row in raw.execute(f"SELECT {cols} FROM {table} ORDER BY rowid"):
                digest.update(repr(row).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def schema_version_of(path: Path) -> int:
    with sqlite3.connect(path) as raw:
        return int(raw.execute("SELECT schema_version FROM schema_meta WHERE id = 1").fetchone()[0])


def table_names(path: Path) -> List[str]:
    with sqlite3.connect(path) as raw:
        return sorted(
            str(r[0]) for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        )


def _column_names(path: Path, table: str) -> List[str]:
    with sqlite3.connect(path) as raw:
        return [str(r[1]) for r in raw.execute(f"PRAGMA table_info({table})")]


def _table_info(path: Path, table: str) -> List[Tuple[Any, ...]]:
    with sqlite3.connect(path) as raw:
        return [tuple(r[1:6]) for r in raw.execute(f"PRAGMA table_info({table})")]


def _check_names(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as raw:
        sql = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()[0]
    return {token for token in sql.replace("(", " ").split() if token.startswith("ck_")}


def _index_shapes(path: Path, table: str) -> Dict[str, Tuple[Tuple[Any, ...], ...]]:
    with sqlite3.connect(path) as raw:
        shapes: Dict[str, Tuple[Tuple[Any, ...], ...]] = {}
        for row in raw.execute(f"PRAGMA index_list({table})"):
            name = str(row[1])
            info = tuple(tuple(r) for r in raw.execute(f"PRAGMA index_info({name})"))
            partial = raw.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
            ).fetchone()
            where = ""
            if partial and partial[0] and " WHERE " in partial[0]:
                where = partial[0].split(" WHERE ", 1)[1]
            shapes[name] = (info, (int(row[2]), int(row[4])), (where,))
        return shapes


@pytest.fixture
def v1_path(tmp_path: Path) -> Path:
    return build_v1_file(tmp_path / "translation_state.db")


def _v1_sources() -> List[Any]:
    """O-18 parametrizations: the DDL-snapshot fixture, plus the sample DB when it is a v1 file."""
    sources = [pytest.param("fixture", id="fixture")]
    if not SAMPLE_STATE_DB.exists() or SAMPLE_STATE_DB.stat().st_size == 0:
        reason = f"{SAMPLE_STATE_DB} is missing or 0 bytes (doc 07, V0)"
        sources.append(pytest.param("sample", id="sample", marks=pytest.mark.skip(reason=reason)))
        return sources
    try:
        version = schema_version_of(SAMPLE_STATE_DB)
    except sqlite3.Error as exc:
        reason = f"{SAMPLE_STATE_DB} is not a readable state database ({exc})"
        sources.append(pytest.param("sample", id="sample", marks=pytest.mark.skip(reason=reason)))
        return sources
    if version != 1:
        reason = f"{SAMPLE_STATE_DB} reports schema v{version}, not v1"
        sources.append(pytest.param("sample", id="sample", marks=pytest.mark.skip(reason=reason)))
        return sources
    sources.append(pytest.param("sample", id="sample"))
    return sources


# --------------------------------------------------------------------------- #
# 18. Migration round trip
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("source", _v1_sources())
def test_18_migration_round_trip(tmp_path: Path, source: str) -> None:
    path = tmp_path / "translation_state.db"
    if source == "fixture":
        build_v1_file(path)
    else:
        shutil.copy(SAMPLE_STATE_DB, path)
    assert schema_version_of(path) == 1
    columns_before = _v1_columns(path)
    dump_before = canonical_dump(path, columns_before)
    unmigrated = tmp_path / "unmigrated.db"
    shutil.copy(path, unmigrated)

    db = unwrap(open_database(path, tool_version=TOOL_VERSION))
    try:
        assert db.schema_version == m.CURRENT_SCHEMA_VERSION == 4
        assert schema_version_of(path) == m.CURRENT_SCHEMA_VERSION
        meta = raw_rows(
            db,
            "SELECT upgraded_by_tool_version, upgraded_at, created_by_tool_version"
            " FROM schema_meta",
        )[0]
        assert meta[0] == TOOL_VERSION and len(meta[1]) == 27 and meta[2] != TOOL_VERSION
        backups = sorted((tmp_path / ARCHIVE_DIR_NAME).glob("*-pre-migrate-v1"))
        assert len(backups) == 1
        backup_file = backups[0] / "translation_state.db"
        assert backup_file.exists() and schema_version_of(backup_file) == 1
        assert "overlay_pages" not in table_names(backup_file)

        # the v1 payload is byte-identical after the migration (doc 07, section 8.3)
        assert canonical_dump(path, columns_before) == dump_before
        assert "overlay_pages" in table_names(path) and "overlay_blocks" in table_names(path)
        assert raw_rows(db, "SELECT COUNT(*) FROM overlay_pages")[0][0] == 0
        assert raw_rows(db, "SELECT COUNT(*) FROM overlay_blocks")[0][0] == 0
        new_columns = raw_rows(
            db,
            "SELECT overlay_status, overlay_sha256, overlay_translate_headers,"
            " overlay_keep_figure_text, overlay_tool_version, overlay_unit_count FROM jobs",
        )
        assert new_columns and all(row == ("NONE", None, 0, 0, None, 0) for row in new_columns)
        modes = raw_rows(db, "SELECT mode FROM runs")
        assert modes and all(row == ("reflow",) for row in modes)

        job = unwrap(JobRepository(db).get_single_job())
        assert job is not None and job.overlay_status == "NONE" and job.overlay_unit_count == 0
        last = unwrap(JobRepository(db).get_last_run(job.id))
        assert last is not None and last.mode == "reflow"
        if source == "fixture":
            assert job.id == JOB_ID and last.id == "run000000002"
            assert raw_rows(db, "SELECT COUNT(*) FROM chunks")[0][0] == 5
            assert raw_rows(db, "SELECT COUNT(*) FROM lease")[0][0] == 1
    finally:
        close_database(db)

    # a second open is a no-op: no new backup, no DDL on the wire
    statements: List[str] = []

    def before(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", before)
    try:
        again = unwrap(open_database(path, tool_version=TOOL_VERSION))
        close_database(again)
    finally:
        event.remove(Engine, "before_cursor_execute", before)
    assert again.schema_version == m.CURRENT_SCHEMA_VERSION
    assert len(sorted((tmp_path / ARCHIVE_DIR_NAME).glob("*-pre-migrate-v1"))) == 1
    ddl = [s for s in statements if s.lstrip().upper().startswith(("CREATE", "ALTER"))]
    assert ddl == [], ddl

    # ``status`` on the unmigrated copy: read-only, still v1, never touches the v2 columns
    ro = unwrap(open_database(unmigrated, read_only=True, tool_version=TOOL_VERSION))
    try:
        assert ro.schema_version == 1
        with capture_statements(ro) as seen:
            ro_job = unwrap(JobRepository(ro).get_single_job())
            assert ro_job is not None and ro_job.overlay_status == "NONE"
            ro_last = unwrap(JobRepository(ro).get_last_run(ro_job.id))
            assert ro_last is not None and ro_last.mode == "reflow"
        assert not any("overlay" in s or "mode" in s for s in seen), seen
    finally:
        close_database(ro)
    assert schema_version_of(unmigrated) == 1


def test_18b_migration_failure_keeps_v1(
    v1_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = session_module.MIGRATIONS[1]

    def broken(conn: Connection, ctx: MigrationContext) -> None:
        from sqlalchemy.schema import CreateTable

        table = m.Base.metadata.tables["overlay_pages"]
        conn.exec_driver_sql(str(CreateTable(table, if_not_exists=True).compile(conn.engine)))
        raise sqlite3.OperationalError("simulated failure after the first CREATE TABLE")

    monkeypatch.setitem(session_module.MIGRATIONS, 1, broken)
    outcome = open_database(v1_path, tool_version=TOOL_VERSION)
    assert isinstance(outcome, Err) and outcome.error.code is ErrorCode.STATE_SCHEMA_INCOMPATIBLE
    backups = sorted((tmp_path / ARCHIVE_DIR_NAME).glob("*-pre-migrate-v1"))
    assert len(backups) == 1
    assert str(backups[0] / "translation_state.db") in outcome.error.message
    assert schema_version_of(v1_path) == 1
    assert not any(name.startswith("overlay_") for name in table_names(v1_path))

    # with the real step back in place the same file migrates
    monkeypatch.setitem(session_module.MIGRATIONS, 1, original)
    db = unwrap(open_database(v1_path, tool_version=TOOL_VERSION))
    close_database(db)
    assert db.schema_version == m.CURRENT_SCHEMA_VERSION


def test_18c_read_only_versions(v1_path: Path, tmp_path: Path) -> None:
    ro = unwrap(open_database(v1_path, read_only=True, tool_version=TOOL_VERSION))
    close_database(ro)
    assert ro.schema_version == 1 and schema_version_of(v1_path) == 1
    assert session_module.READ_ONLY_ACCEPTED_VERSIONS == (1, 2, 3)

    newer = tmp_path / "newer.db"
    build_v1_file(newer, seed=False)
    with sqlite3.connect(newer) as raw:
        raw.execute(
            "UPDATE schema_meta SET schema_version = ? WHERE id = 1",
            (m.CURRENT_SCHEMA_VERSION + 1,),
        )
    outcome = open_database(newer, read_only=True, tool_version=TOOL_VERSION)
    assert isinstance(outcome, Err) and outcome.error.code is ErrorCode.STATE_SCHEMA_INCOMPATIBLE
    assert "newer" in outcome.error.message


def test_18d_migration_is_idempotent_per_statement(v1_path: Path) -> None:
    """Running the step twice inside one transaction issues no second ADD COLUMN (PRAGMA guard)."""
    db = unwrap(open_database(v1_path, tool_version=TOOL_VERSION))
    try:
        with capture_statements(db) as statements:
            with db.engine.connect() as conn:
                conn = conn.execution_options(sqlite_txn_mode="IMMEDIATE")
                with conn.begin():
                    session_module.MIGRATIONS[1](
                        conn,
                        MigrationContext(tool_version=TOOL_VERSION, now=session_module.utcnow()),
                    )
        assert not any(s.startswith("ALTER TABLE") for s in statements), statements
        assert all("IF NOT EXISTS" in s for s in statements if s.startswith("CREATE")), statements
        assert raw_rows(db, "PRAGMA table_info(runs)")[-1][1] == "mode"
    finally:
        close_database(db)


# --------------------------------------------------------------------------- #
# 19. Fresh v2 == migrated v2
# --------------------------------------------------------------------------- #


def test_19_fresh_v2_equals_migrated_v2(tmp_path: Path) -> None:
    fresh_path = tmp_path / "fresh" / "translation_state.db"
    fresh_path.parent.mkdir()
    fresh = unwrap(open_database(fresh_path, tool_version=TOOL_VERSION))
    close_database(fresh)
    (tmp_path / "migrated").mkdir()
    migrated_path = build_v1_file(tmp_path / "migrated" / "translation_state.db", seed=False)
    migrated = unwrap(open_database(migrated_path, tool_version=TOOL_VERSION))
    close_database(migrated)
    assert (schema_version_of(fresh_path) == schema_version_of(migrated_path)
            == m.CURRENT_SCHEMA_VERSION)

    for table in V2_TABLES:
        assert _table_info(fresh_path, table) == _table_info(migrated_path, table), table
        assert _check_names(fresh_path, table) == _check_names(migrated_path, table), table
        assert _index_shapes(fresh_path, table) == _index_shapes(migrated_path, table), table
    assert table_names(fresh_path) == table_names(migrated_path)
    with sqlite3.connect(fresh_path) as raw:
        fresh_partials = dict(
            raw.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql LIKE '%WHERE%'"
            )
        )
    with sqlite3.connect(migrated_path) as raw:
        migrated_partials = dict(
            raw.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql LIKE '%WHERE%'"
            )
        )
    assert (
        set(fresh_partials)
        == set(migrated_partials)
        >= {
            "ix_overlay_blocks_pending_backoff",
            "ix_overlay_blocks_review",
        }
    )
    for name, sql in fresh_partials.items():
        assert sql.split(" WHERE ", 1)[1] == migrated_partials[name].split(" WHERE ", 1)[1], name


def test_20_v2_file_widens_keep_reason_to_math(tmp_path: Path) -> None:
    """2 -> 3: the ``keep_reason`` CHECK of an existing v2 file did not know ``"math"``.

    SQLite keeps a CHECK in the table DDL, so a file written before display-math bands
    existed rejects the new value until ``overlay_blocks`` is rebuilt. The migration must
    widen it *and* keep every row."""
    path = tmp_path / "translation_state.db"
    fresh = unwrap(open_database(path, tool_version=TOOL_VERSION))
    close_database(fresh)

    block_sql = (
        "INSERT INTO overlay_blocks (job_id, page, index_on_page, kind, translate,"
        " keep_reason, x0, y0, x1, y1, font_size, family, color, alignment, source_text,"
        " content_hash, char_count, status, created_at, updated_at)"
        " VALUES (?, 1, 1, 'body', 0, 'math', 0, 0, 1, 1, 10.0, 'serif', '#000000', 'left',"
        " 'x', ?, 1, 'PENDING', ?, ?)"
    )
    block_params = (JOB_ID, "h" * 64, TS, TS)

    # rewrite overlay_blocks with the pre-math CHECK and drop the file back to v2
    with sqlite3.connect(path) as raw:
        ddl = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'overlay_blocks'"
        ).fetchone()[0]
        old_ddl = ddl.replace(", 'math'", "").replace("'math', ", "")
        assert old_ddl != ddl, "the CHECK no longer spells 'math' - update this test"
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DROP TABLE overlay_blocks")
        raw.execute(old_ddl)
        raw.execute("UPDATE schema_meta SET schema_version = 2 WHERE id = 1")
        raw.execute(
            "INSERT INTO jobs (id, singleton, input_path, input_sha256, status, tool_version,"
            " warnings, created_at, updated_at)"
            " VALUES (?, 1, 'C:/books/in.pdf', ?, 'TRANSLATING', '0.1.0-v1', '[]', ?, ?)",
            (JOB_ID, "a" * 64, TS, TS),
        )
        raw.commit()
    with sqlite3.connect(path) as raw:
        with pytest.raises(sqlite3.IntegrityError):  # the point of the migration
            raw.execute(block_sql, block_params)

    db = unwrap(open_database(path, tool_version=TOOL_VERSION))
    close_database(db)
    assert schema_version_of(path) == m.CURRENT_SCHEMA_VERSION == 4
    with sqlite3.connect(path) as raw:
        raw.execute(block_sql, block_params)
        raw.commit()
        assert raw.execute("SELECT COUNT(*) FROM overlay_blocks").fetchone()[0] == 1


def test_21_v3_file_gains_the_redaction_rect_column(tmp_path: Path) -> None:
    """3 -> 4: ``overlay_blocks.redact_bbox``, added without rewriting a row.

    A v3 file has one rect per unit, used for placement *and* redaction, which left the
    source glyphs of a shortened block on the page. The upgrade appends one nullable TEXT
    column; ``NULL`` keeps the old meaning, so every existing row stays exactly as it is."""
    path = tmp_path / "translation_state.db"
    fresh = unwrap(open_database(path, tool_version=TOOL_VERSION))
    close_database(fresh)

    # drop the file back to v3: remove the column and the version bump
    with sqlite3.connect(path) as raw:
        raw.execute(
            "INSERT INTO jobs (id, singleton, input_path, input_sha256, status, tool_version,"
            " warnings, created_at, updated_at)"
            " VALUES (?, 1, 'C:/books/in.pdf', ?, 'TRANSLATING', '0.1.0-v1', '[]', ?, ?)",
            (JOB_ID, "a" * 64, TS, TS),
        )
        raw.execute(
            "INSERT INTO overlay_pages (page, job_id, width, height, rotation, has_text_layer,"
            " block_count, translatable_count, body_size, created_at)"
            " VALUES (1, ?, 595.0, 842.0, 0, 1, 1, 1, 10.0, ?)",
            (JOB_ID, TS),
        )
        raw.execute(
            "INSERT INTO overlay_blocks (id, job_id, page, index_on_page, kind, translate,"
            " x0, y0, x1, y1, font_size, family, color, alignment, source_text, content_hash,"
            " char_count, status, created_at, updated_at)"
            " VALUES (1, ?, 1, 1, 'body', 1, 10, 20, 30, 40, 10.0, 'serif', '#000000', 'left',"
            " 'Some source text', ?, 16, 'PENDING', ?, ?)",
            (JOB_ID, "h" * 64, TS, TS),
        )
        raw.execute("ALTER TABLE overlay_blocks DROP COLUMN redact_bbox")
        raw.execute("UPDATE schema_meta SET schema_version = 3 WHERE id = 1")
        raw.commit()
    assert "redact_bbox" not in _column_names(path, "overlay_blocks")

    db = unwrap(open_database(path, tool_version=TOOL_VERSION))
    close_database(db)
    assert schema_version_of(path) == m.CURRENT_SCHEMA_VERSION == 4
    with sqlite3.connect(path) as raw:
        assert "redact_bbox" in _column_names(path, "overlay_blocks")
        row = raw.execute(
            "SELECT id, source_text, x0, y0, x1, y1, redact_bbox FROM overlay_blocks"
        ).fetchall()
        assert row == [(1, "Some source text", 10.0, 20.0, 30.0, 40.0, None)]
        # the new column is writable and reads back through the repository layer
        raw.execute("UPDATE overlay_blocks SET redact_bbox = '[10,5,30,40]' WHERE id = 1")
        raw.commit()
    with_column = unwrap(open_database(path, tool_version=TOOL_VERSION))
    try:
        blocks = list(OverlayBlockRepository(with_column, RUN_ID, None).iter_page(JOB_ID, 1))
    finally:
        close_database(with_column)
    assert [b.redact_bbox for b in blocks] == [(10.0, 5.0, 30.0, 40.0)]


def test_21_migrating_a_v3_file_matches_a_fresh_one(tmp_path: Path) -> None:
    """The ``ADD COLUMN`` lands where ``create_all`` puts it (O-19 for the 3 -> 4 step)."""
    fresh_path = tmp_path / "fresh" / "translation_state.db"
    fresh_path.parent.mkdir()
    close_database(unwrap(open_database(fresh_path, tool_version=TOOL_VERSION)))
    migrated_path = tmp_path / "migrated" / "translation_state.db"
    migrated_path.parent.mkdir()
    close_database(unwrap(open_database(migrated_path, tool_version=TOOL_VERSION)))
    with sqlite3.connect(migrated_path) as raw:
        raw.execute("ALTER TABLE overlay_blocks DROP COLUMN redact_bbox")
        raw.execute("UPDATE schema_meta SET schema_version = 3 WHERE id = 1")
        raw.commit()
    close_database(unwrap(open_database(migrated_path, tool_version=TOOL_VERSION)))
    assert _table_info(fresh_path, "overlay_blocks") == _table_info(
        migrated_path, "overlay_blocks"
    )
