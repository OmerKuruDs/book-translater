"""Acceptance checks O-1..O-17, O-20, O-21 of the DB design v1.1 (docs/07_db_design_v1_1.md, §11).

Mirrors ``tests/test_repository.py`` for the ``overlay_pages`` / ``overlay_blocks`` tables and
``OverlayBlockRepository``. Fixtures live here (``conftest.py`` is owned elsewhere).
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pytest
from sqlalchemy import Connection, Select, func, literal_column, select, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session

from book_translator.database import models as m
from book_translator.database import repository as repository_module
from book_translator.database.repository import (
    ChunkRepository,
    JobPatch,
    JobRecord,
    JobRepository,
    JobSpec,
    LeaseHolder,
    LeaseRepository,
    OverlayBlockRepository,
    UnitTotals,
    overlay_claim_select,
    page_progress_select,
    select_batch,
)
from book_translator.database.session import (
    Database,
    archive_database,
    close_database,
    open_database,
    utcnow,
)
from book_translator.domain.models import (
    ChunkStatus,
    GlossaryStrategy,
    TranslationResult,
    content_hash_of,
)
from book_translator.domain.overlay import (
    OverlayAlignment,
    OverlayBlock,
    OverlayBlockKind,
    OverlayPage,
    OverlayStyle,
    format_block_id,
)
from book_translator.domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, unwrap
from tests.conftest import (
    GLOSSARY_HASH,
    RUN_ID,
    TOOL_VERSION,
    capture_statements,
    make_chunks,
    raw_row_dict,
    raw_rows,
)
from tests.test_migration import build_v1_file

# --------------------------------------------------------------------------- #
# Fixtures (doc 07 §11 preamble)
# --------------------------------------------------------------------------- #

STYLE = OverlayStyle(font_size=10.0, bold=False, italic=False, family="serif", color="#000000")


def make_page(
    page: int,
    block_count: int,
    translatable_count: int,
    skip_reason: Optional[str] = None,
    *,
    rotation: int = 0,
    body_size: float = 10.0,
) -> OverlayPage:
    return OverlayPage(
        page=page,
        width=595.0,
        height=842.0,
        rotation=rotation,
        has_text_layer=skip_reason is None,
        block_count=block_count,
        translatable_count=translatable_count,
        skip_reason=skip_reason,
        body_size=body_size,
    )


def make_block(
    unit_id: int,
    page: int,
    index_on_page: int,
    text: Optional[str] = None,
    *,
    translate: bool = True,
    kind: OverlayBlockKind = OverlayBlockKind.BODY,
    keep_reason: Optional[str] = None,
    style: OverlayStyle = STYLE,
    alignment: OverlayAlignment = OverlayAlignment.LEFT,
    bbox: Tuple[float, float, float, float] = (72.0, 100.0, 300.0, 124.0),
    line_boxes: Sequence[Tuple[float, float, float, float]] = ((72.0, 100.0, 300.0, 112.0),),
    fragment: bool = False,
    over_image: bool = False,
    block_id: Optional[str] = None,
    char_count: Optional[int] = None,
    content_hash: Optional[str] = None,
    warnings: Sequence[str] = (),
) -> OverlayBlock:
    source = text if text is not None else f"Block {unit_id} on page {page}."
    return OverlayBlock(
        unit_id=unit_id,
        block_id=block_id if block_id is not None else format_block_id(page, index_on_page),
        page=page,
        index_on_page=index_on_page,
        kind=kind,
        bbox=bbox,
        line_boxes=tuple(line_boxes),
        style=style,
        alignment=alignment,
        source_text=source,
        content_hash=content_hash if content_hash is not None else content_hash_of(source),
        char_count=char_count if char_count is not None else len(source),
        translate=translate,
        keep_reason=keep_reason,
        fragment=fragment,
        over_image=over_image,
        warnings=tuple(warnings),
    )


def kept_block(unit_id: int, page: int, index_on_page: int, text: str = "42") -> OverlayBlock:
    return make_block(
        unit_id,
        page,
        index_on_page,
        text,
        translate=False,
        kind=OverlayBlockKind.PAGE_NUMBER,
        keep_reason="page_number",
    )


def make_overlay(pages: int = 3, per_page: int = 4) -> Tuple[List[OverlayPage], List[OverlayBlock]]:
    """``pages`` pages of ``per_page`` units each; the last unit of every page is a kept one."""
    page_rows: List[OverlayPage] = []
    units: List[OverlayBlock] = []
    unit_id = 0
    for page in range(1, pages + 1):
        for index in range(1, per_page + 1):
            unit_id += 1
            if index == per_page:
                units.append(kept_block(unit_id, page, index))
            else:
                units.append(make_block(unit_id, page, index))
        page_rows.append(make_page(page, per_page, per_page - 1))
    return page_rows, units


def from_texts(
    pages_texts: Sequence[Sequence[str]],
) -> Tuple[List[OverlayPage], List[OverlayBlock]]:
    """Pages from explicit unit texts (a ``None``-free list per page); one kept unit per page."""
    page_rows: List[OverlayPage] = []
    units: List[OverlayBlock] = []
    unit_id = 0
    for page, texts in enumerate(pages_texts, start=1):
        for index, text in enumerate(texts, start=1):
            unit_id += 1
            units.append(make_block(unit_id, page, index, text))
        unit_id += 1
        units.append(kept_block(unit_id, page, len(texts) + 1))
        page_rows.append(make_page(page, len(texts) + 1, len(texts)))
    return page_rows, units


def _result(unit_id: int, text: str = "Çeviri metni.", **overrides: Any) -> TranslationResult:
    base: Dict[str, Any] = dict(
        chunk_id=unit_id,
        translated_text=text,
        provider="deepl",
        chars_sent=len(text),
        chars_billed=len(text) + 1,
        latency_ms=120,
        attempts=1,
        glossary_strategy=GlossaryStrategy.NATIVE,
        warnings=(),
        review_flag=False,
    )
    base.update(overrides)
    return TranslationResult(**base)


def _retryable(message: str = "429 too many requests") -> AppError:
    return AppError(
        code=ErrorCode.PROVIDER_RATE_LIMITED, message=message, scope=ErrorScope.CHUNK_RETRYABLE
    )


def _fatal(message: str = "400 bad request") -> AppError:
    return AppError(
        code=ErrorCode.PROVIDER_BAD_REQUEST, message=message, scope=ErrorScope.CHUNK_FATAL
    )


def _tick() -> None:
    """Windows' datetime.now() has ~15 ms granularity; ensure the next utcnow() differs."""
    time.sleep(0.02)


def _claim(repo: OverlayBlockRepository, job_id: str, limit: int) -> List[int]:
    return [u.unit_id for u in unwrap(repo.claim_pending(job_id, limit, utcnow(), repo.run_id))]


def _batch(
    repo: OverlayBlockRepository, job_id: str, max_units: int, min_chars: int, now: datetime
) -> List[int]:
    return [
        u.unit_id
        for u in unwrap(repo.claim_pending_batch(job_id, max_units, min_chars, now, repo.run_id))
    ]


def _status_map(db: Database) -> Dict[int, str]:
    return {
        int(i): str(s) for i, s in raw_rows(db, "SELECT id, status FROM overlay_blocks ORDER BY id")
    }


def _plan(db: Database, statement: Select[Any]) -> List[str]:
    sql = str(statement.compile(db.engine, compile_kwargs={"literal_binds": True}))
    return [str(row[3]) for row in raw_rows(db, "EXPLAIN QUERY PLAN " + sql)]


def _integrity_error(db: Database, sql: str, params: Tuple[Any, ...] = ()) -> str:
    """Run a raw write that must violate a CHECK/FK; return the error text."""
    with pytest.raises(sa_exc.IntegrityError) as excinfo:
        with db.engine.begin() as conn:
            conn.exec_driver_sql(sql, params)
    return str(excinfo.value)


@pytest.fixture
def overlay(mem_db: Database) -> OverlayBlockRepository:
    return OverlayBlockRepository(mem_db, run_id=RUN_ID, glossary_hash=GLOSSARY_HASH)


@pytest.fixture
def seeded_overlay(job: JobRecord, overlay: OverlayBlockRepository) -> JobRecord:
    """Job with 3 pages x 4 units (ids 1..12; units 4, 8, 12 are kept page numbers)."""
    pages, units = make_overlay(3, 4)
    assert unwrap(overlay.replace_all(job.id, units, pages=pages)) == 12
    return job


# --------------------------------------------------------------------------- #
# 1. Schema
# --------------------------------------------------------------------------- #


def test_01_schema_objects_and_runtime_parity(mem_db: Database) -> None:
    rows = raw_rows(mem_db, "SELECT type, name, sql FROM sqlite_master")
    tables = {name for kind, name, _ in rows if kind == "table"}
    assert tables == set(m.TABLE_NAMES) and len(m.TABLE_NAMES) == 7
    indexes = {name: sql for kind, name, sql in rows if kind == "index" and sql}
    assert set(m.INDEX_NAMES) == set(indexes) and len(m.INDEX_NAMES) == 7
    assert "WHERE status = 'PENDING'" in indexes["ix_overlay_blocks_pending_backoff"]
    assert "WHERE review_flag = 1" in indexes["ix_overlay_blocks_review"]
    assert "(job_id, page, status)" in indexes["ix_overlay_blocks_job_page_status"]
    assert "(job_id, status)" in indexes["ix_overlay_blocks_job_status"]

    meta = raw_rows(
        mem_db, "SELECT id, schema_version, created_by_tool_version, created_at FROM schema_meta"
    )
    assert len(meta) == 1
    row_id, version, tool, created = meta[0]
    assert (row_id, version, tool) == (1, m.CURRENT_SCHEMA_VERSION, TOOL_VERSION)
    assert len(created) == 27 and created.endswith("Z")
    # pinned on purpose: a bump must be a conscious decision (v3 widened keep_reason)
    assert mem_db.schema_version == m.CURRENT_SCHEMA_VERSION == 3

    ddl = {name: sql for kind, name, sql in rows if kind == "table"}
    for name in (
        "ck_overlay_blocks_translated_text_completed",
        "ck_overlay_blocks_next_attempt_pending_only",
        "ck_overlay_blocks_prefix_style",
        "ck_overlay_blocks_keep_reason",
        "ck_overlay_blocks_page_number_kept",
        "ck_overlay_blocks_bbox",
    ):
        assert name in ddl["overlay_blocks"], name
    for name in ("ck_jobs_overlay_binding", "ck_jobs_overlay_status"):
        assert name in ddl["jobs"], name
    assert "ck_runs_mode" in ddl["runs"]
    assert "ck_overlay_pages_skip" in ddl["overlay_pages"]

    # runtime group parity (B8)
    def runtime_info(table: str) -> Dict[str, Tuple[Any, ...]]:
        info = {
            str(r[1]): (str(r[2]), int(r[3]), r[4])
            for r in raw_rows(mem_db, f"PRAGMA table_info({table})")
        }
        return {c: info[c] for c in m.RUNTIME_STATE_COLUMNS}

    assert runtime_info("chunks") == runtime_info("overlay_blocks")
    assert len(m.RUNTIME_STATE_COLUMNS) == 20

    def checks(table: str) -> Dict[str, str]:
        found = re.findall(rf"CONSTRAINT (ck_{table}_\w+) CHECK \((.*)\)", ddl[table])
        group = {name for name in (c.name for c in m.runtime_state_constraints(table))}
        return {
            name.replace(f"ck_{table}_", "ck_T_"): expr for name, expr in found if name in group
        }

    chunk_checks = checks("chunks")
    assert len(chunk_checks) == len(m.runtime_state_constraints("chunks")) == 15
    assert chunk_checks == checks("overlay_blocks")


# --------------------------------------------------------------------------- #
# 2. PRAGMAs / read-only on a v1 file
# --------------------------------------------------------------------------- #


def test_02_read_only_v1_file(tmp_path: Path) -> None:
    path = build_v1_file(tmp_path / "translation_state.db")
    ro = unwrap(open_database(path, read_only=True, tool_version=TOOL_VERSION))
    try:
        assert ro.schema_version == 1 and ro.read_only
        assert raw_rows(ro, "PRAGMA query_only")[0][0] == 1
        with capture_statements(ro) as statements:
            job = unwrap(JobRepository(ro).get_single_job())
            assert job is not None
            assert unwrap(JobRepository(ro).get_job(job.id)) == job
            last = unwrap(JobRepository(ro).get_last_run(job.id))
            counts = unwrap(ChunkRepository(ro, RUN_ID, None).counts(job.id))
        assert job.overlay_status == "NONE" and job.overlay_sha256 is None
        assert last is not None and last.mode == "reflow"
        assert counts.total == 5 and counts.completed == 1
        assert not any("overlay" in s for s in statements), statements
        assert not any(re.search(r"\bmode\b", s) for s in statements), statements
    finally:
        close_database(ro)
    assert "overlay_pages" not in {
        r[0]
        for r in raw_rows(
            unwrap(open_database(path, read_only=True, tool_version=TOOL_VERSION)),
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    rw = unwrap(open_database(path, tool_version=TOOL_VERSION))
    try:
        assert rw.schema_version == m.CURRENT_SCHEMA_VERSION
    finally:
        close_database(rw)


# --------------------------------------------------------------------------- #
# 3. Transaction modes
# --------------------------------------------------------------------------- #


def test_03_transaction_modes(
    mem_db: Database, seeded_overlay: JobRecord, overlay: OverlayBlockRepository
) -> None:
    job_id = seeded_overlay.id
    ids = _claim(overlay, job_id, 3)
    unwrap(overlay.complete(job_id, ids[0], _result(ids[0])))
    pages, units = make_overlay(1, 2)

    writes: Dict[str, Callable[[], Any]] = {
        "requeue_processing": lambda: overlay.requeue_processing(job_id),
        "claim_pending": lambda: overlay.claim_pending(job_id, 2, utcnow(), RUN_ID),
        "claim_pending_batch": lambda: overlay.claim_pending_batch(job_id, 2, 10, utcnow(), RUN_ID),
        "complete": lambda: overlay.complete(job_id, 5, _result(5)),
        "reschedule": lambda: overlay.reschedule(job_id, 6, _retryable(), utcnow()),
        "fail": lambda: overlay.fail(job_id, 7, _fatal()),
        "release": lambda: overlay.release(job_id, [8]),
        "reset_failed": lambda: overlay.reset_failed(job_id),
        "replace_all": lambda: overlay.replace_all(job_id, units, pages=pages),
    }
    reads: Dict[str, Callable[[], Any]] = {
        "counts": lambda: overlay.counts(job_id),
        "earliest_next_attempt": lambda: overlay.earliest_next_attempt(job_id),
        "iter_ordered": lambda: list(overlay.iter_ordered(job_id)),
        "iter_page": lambda: list(overlay.iter_page(job_id, 2)),
        "pages": lambda: overlay.pages(job_id),
        "page_texts": lambda: overlay.page_texts(job_id, [1, 2]),
        "page_progress": lambda: overlay.page_progress(job_id),
        "totals": lambda: overlay.totals(job_id),
        "failed_ids": lambda: overlay.failed_ids(job_id),
        "review_ids": lambda: overlay.review_ids(job_id),
        "count_completed_with_other_glossary": lambda: overlay.count_completed_with_other_glossary(
            job_id, "x"
        ),
        "count_completed_by_run": lambda: overlay.count_completed_by_run(RUN_ID),
    }
    for name, call in reads.items():
        with capture_statements(mem_db) as statements:
            call()
        assert statements and statements[0] == "BEGIN", name
        assert "BEGIN IMMEDIATE" not in statements, name
        assert not any("RETURNING" in s for s in statements), name
    for name, call in writes.items():
        with capture_statements(mem_db) as statements:
            outcome = call()
        assert statements and statements[0] == "BEGIN IMMEDIATE", (name, statements[:2], outcome)
        assert not any("RETURNING" in s for s in statements), name
        # replace_all runs last on purpose: it refuses (COMPLETED row present) after BEGIN
        # IMMEDIATE.
    assert isinstance(outcome, Err) and outcome.error.code is ErrorCode.INTERNAL


# --------------------------------------------------------------------------- #
# 4. Batch claim semantics + plans
# --------------------------------------------------------------------------- #


def test_04_claim_pending_batch(
    mem_db: Database, job: JobRecord, overlay: OverlayBlockRepository
) -> None:
    # 3 pages x (200, 900, 50 chars) of translatable text + one kept unit per page
    pages, units = from_texts(
        [["a" * 100, "b" * 100], ["c" * 300, "d" * 300, "e" * 300], ["f" * 50]]
    )
    assert unwrap(overlay.replace_all(job.id, units, pages=pages)) == 9
    now = utcnow()
    assert _batch(overlay, job.id, 0, 1000, now) == []
    first = unwrap(overlay.claim_pending_batch(job.id, 50, 1000, now, RUN_ID))
    assert [u.unit_id for u in first] == [1, 2, 3, 4, 5, 6, 7]  # pages 1-2, boundary after >= 1000
    assert {u.page for u in first} == {1, 2}
    assert all(u.status is ChunkStatus.PROCESSING and u.next_attempt_at is None for u in first)
    for unit_id in (1, 3, 7):
        row = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (unit_id,))
        assert row["status"] == "PROCESSING" and row["next_attempt_at"] is None
        assert row["last_run_id"] == RUN_ID and row["claimed_at"] == m.format_timestamp(now)
    assert _batch(overlay, job.id, 50, 1000, now) == [8, 9]  # page 3
    assert _batch(overlay, job.id, 50, 1000, now) == []

    # max_units cuts mid-page; the next call continues the same page
    unwrap(overlay.requeue_processing(job.id))
    assert _batch(overlay, job.id, 2, 1000, now) == [1, 2]
    following = unwrap(overlay.claim_pending_batch(job.id, 2, 1000, now, RUN_ID))
    assert [u.unit_id for u in following] == [3, 4] and following[0].page == 1

    # min_chars = 0: exactly one page per call
    unwrap(overlay.requeue_processing(job.id))
    assert _batch(overlay, job.id, 50, 0, now) == [1, 2, 3]
    assert _batch(overlay, job.id, 50, 0, now) == [4, 5, 6, 7]
    assert _batch(overlay, job.id, 50, 0, now) == [8, 9]

    # kept units are claimed and count 0 chars: a kept-only page does not satisfy min_chars
    kept_pages = [make_page(1, 2, 0), make_page(2, 2, 1)]
    kept_units = [
        kept_block(1, 1, 1, "1"),
        kept_block(2, 1, 2, "i"),
        make_block(3, 2, 1, "x" * 10),
        kept_block(4, 2, 2, "2"),
    ]
    assert unwrap(overlay.replace_all(job.id, kept_units, pages=kept_pages)) == 4
    assert _batch(overlay, job.id, 50, 1, now) == [1, 2, 3, 4]
    unwrap(overlay.requeue_processing(job.id))
    assert _batch(overlay, job.id, 50, 0, now) == [1, 2]

    # backoff: future next_attempt_at skipped, past included (both claims)
    unwrap(overlay.requeue_processing(job.id))
    future = now + timedelta(minutes=5)
    past = now - timedelta(minutes=5)
    with mem_db.engine.begin() as conn:
        conn.execute(
            update(m.OverlayBlock).where(m.OverlayBlock.id == 1).values(next_attempt_at=future)
        )
        conn.execute(
            update(m.OverlayBlock).where(m.OverlayBlock.id == 2).values(next_attempt_at=past)
        )
    assert _batch(overlay, job.id, 50, 0, now) == [2]  # page 1: only unit 2 eligible
    assert _claim(overlay, job.id, 10) == [3, 4]
    assert _claim(overlay, job.id, 10) == []
    assert _batch(overlay, job.id, 50, 0, future) == [1]

    # plain-mode claim_pending behaves as v1 test 04 (no page rule)
    pages3, units3 = make_overlay(3, 4)
    assert unwrap(overlay.replace_all(job.id, units3, pages=pages3)) == 12
    with mem_db.engine.begin() as conn:
        conn.execute(
            update(m.OverlayBlock).where(m.OverlayBlock.id == 2).values(next_attempt_at=future)
        )
        conn.execute(
            update(m.OverlayBlock).where(m.OverlayBlock.id == 3).values(next_attempt_at=past)
        )
    assert unwrap(overlay.claim_pending(job.id, 0, now, RUN_ID)) == []
    plain = unwrap(overlay.claim_pending(job.id, 4, now, RUN_ID))
    assert [u.unit_id for u in plain] == [1, 3, 4, 5]  # spans pages 1 and 2
    assert [u.unit_id for u in unwrap(overlay.claim_pending(job.id, 100, now, RUN_ID))] == list(
        range(6, 13)
    )
    assert unwrap(overlay.claim_pending(job.id, 5, now, RUN_ID)) == []
    assert unwrap(overlay.claim_pending(job.id, 5, future, RUN_ID))[0].unit_id == 2

    # pure stop rule
    rows = [(1, 1, 100, True), (2, 1, 0, False), (3, 2, 900, True), (4, 3, 50, True)]
    assert select_batch(rows, 1000, True) == [1, 2, 3]
    assert select_batch(rows, 0, True) == [1, 2]
    assert select_batch(rows, 0, False) == [1, 2, 3, 4]
    assert select_batch([], 0, True) == []

    # plans
    plan = _plan(mem_db, overlay_claim_select(job.id, now, 50))
    assert any("USING INDEX ix_overlay_blocks_job_status" in step for step in plan), plan
    assert not any("ix_overlay_blocks_pending_backoff" in step for step in plan), plan
    assert not any("TEMP B-TREE" in step for step in plan), plan
    assert not any(step.startswith("SCAN overlay_blocks") for step in plan), plan
    min_plan = _plan(
        mem_db,
        select(func.min(m.OverlayBlock.next_attempt_at)).where(
            m.OverlayBlock.job_id == job.id,
            m.OverlayBlock.status == literal_column("'PENDING'"),
            m.OverlayBlock.next_attempt_at.is_not(None),
        ),
    )
    assert any("ix_overlay_blocks_pending_backoff" in step for step in min_plan), min_plan
    progress_plan = _plan(mem_db, page_progress_select(job.id))
    assert any(
        "COVERING INDEX ix_overlay_blocks_job_page_status" in step for step in progress_plan
    ), progress_plan
    assert not any(step.startswith("SCAN overlay_blocks") for step in progress_plan), progress_plan
    review_plan = _plan(
        mem_db,
        select(m.OverlayBlock.id)
        .where(m.OverlayBlock.job_id == job.id, m.OverlayBlock.review_flag == literal_column("1"))
        .order_by(m.OverlayBlock.id),
    )
    assert any("ix_overlay_blocks_review" in step for step in review_plan), review_plan


# --------------------------------------------------------------------------- #
# 5. Batch claim atomicity across connections + tripwire
# --------------------------------------------------------------------------- #


def test_05_batch_claim_atomic_across_connections(db_path: Path) -> None:
    db_a = unwrap(open_database(db_path, tool_version=TOOL_VERSION))
    db_b = unwrap(open_database(db_path, tool_version=TOOL_VERSION))
    try:
        job = unwrap(
            JobRepository(db_a).get_or_create_job(JobSpec("in.pdf", "c" * 64, TOOL_VERSION))
        )
        pages, units = make_overlay(2, 2)
        unwrap(
            OverlayBlockRepository(db_a, "aaaaaaaaaaaa", None).replace_all(
                job.id, units, pages=pages
            )
        )
        repo_b = OverlayBlockRepository(db_b, "bbbbbbbbbbbb", None)
        now = utcnow()
        outcome: Dict[str, Any] = {}

        def claim_on_b() -> None:
            started = time.perf_counter()
            outcome["result"] = repo_b.claim_pending_batch(job.id, 50, 0, now, "bbbbbbbbbbbb")
            outcome["elapsed"] = time.perf_counter() - started

        with db_a.write_session() as session:
            rows = session.execute(overlay_claim_select(job.id, now, 50)).all()
            a_ids = select_batch([(r[0], r[1], r[2], r[3]) for r in rows], 0, True)
            assert a_ids == [1, 2]
            worker = threading.Thread(target=claim_on_b)
            worker.start()
            worker.join(0.5)
            assert worker.is_alive(), (
                "engine B must block on BEGIN IMMEDIATE while A holds the lock"
            )
            session.execute(
                update(m.OverlayBlock)
                .where(
                    m.OverlayBlock.id.in_(a_ids),
                    m.OverlayBlock.status == literal_column("'PENDING'"),
                )
                .values(
                    status="PROCESSING", last_run_id="aaaaaaaaaaaa", claimed_at=now, updated_at=now
                )
            )
        worker.join(6)
        assert not worker.is_alive()
        b_ids = [u.unit_id for u in unwrap(outcome["result"])]
        assert b_ids == [3, 4]
        assert outcome["elapsed"] >= 0.4
    finally:
        close_database(db_a)
        close_database(db_b)


def test_05b_claim_tripwire_rolls_back(
    mem_db: Database,
    seeded_overlay: JobRecord,
    overlay: OverlayBlockRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def one_more(rows: Any, min_chars: int, page_rule: bool) -> List[int]:
        return select_batch(rows, min_chars, page_rule) + [999]  # never updated -> rowcount short

    monkeypatch.setattr(repository_module, "select_batch", one_more)
    broken = overlay.claim_pending_batch(seeded_overlay.id, 3, 0, utcnow(), RUN_ID)
    assert isinstance(broken, Err)
    assert broken.error.code is ErrorCode.INTERNAL and broken.error.scope is ErrorScope.JOB_FATAL
    assert "tripwire" in broken.error.message
    assert set(_status_map(mem_db).values()) == {"PENDING"}


# --------------------------------------------------------------------------- #
# 6. complete
# --------------------------------------------------------------------------- #


def test_06_complete(
    mem_db: Database, seeded_overlay: JobRecord, overlay: OverlayBlockRepository
) -> None:
    job_id = seeded_overlay.id
    ids = _claim(overlay, job_id, 4)
    assert ids == [1, 2, 3, 4]
    unwrap(overlay.fail(job_id, 2, _fatal()))

    assert (
        unwrap(overlay.complete(job_id, 1, _result(1, review_flag=True, warnings=("w1",)))) is True
    )
    row = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (1,))
    assert row["status"] == "COMPLETED" and row["translated_text"] == "Çeviri metni."
    assert row["last_error"] is None and row["last_error_code"] is None
    assert row["claimed_at"] is None and len(row["completed_at"]) == 27
    assert row["provider"] == "deepl" and row["glossary_hash"] == GLOSSARY_HASH
    assert row["glossary_strategy"] == "native" and row["review_flag"] == 1
    assert row["warnings"] == '["w1"]' and row["chars_sent"] == 13 and row["attempts"] == 1

    # kept unit (translate = 0): completed with the source text
    source = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (4,))["source_text"]
    assert unwrap(overlay.complete(job_id, 4, _result(4, source, chars_sent=0, attempts=0))) is True
    assert raw_row_dict(mem_db, "overlay_blocks", "id = ?", (4,))["translated_text"] == source

    before = {i: raw_row_dict(mem_db, "overlay_blocks", "id = ?", (i,)) for i in (1, 2, 5)}
    assert unwrap(overlay.complete(job_id, 1, _result(1))) is False  # COMPLETED
    assert unwrap(overlay.complete(job_id, 2, _result(2))) is False  # FAILED
    assert unwrap(overlay.complete(job_id, 5, _result(5))) is False  # PENDING
    assert unwrap(overlay.complete("0" * 36, 3, _result(3))) is False  # wrong job
    for unit_id, row_before in before.items():
        assert raw_row_dict(mem_db, "overlay_blocks", "id = ?", (unit_id,)) == row_before

    text = _integrity_error(mem_db, "UPDATE overlay_blocks SET status = 'COMPLETED' WHERE id = 5")
    assert "ck_overlay_blocks_translated_text_completed" in text
    text = _integrity_error(mem_db, "UPDATE overlay_blocks SET translated_text = 'x' WHERE id = 5")
    assert "ck_overlay_blocks_translated_text_completed" in text


# --------------------------------------------------------------------------- #
# 7. reschedule
# --------------------------------------------------------------------------- #


def test_07_reschedule(
    mem_db: Database, seeded_overlay: JobRecord, overlay: OverlayBlockRepository
) -> None:
    job_id = seeded_overlay.id
    assert unwrap(overlay.reschedule(job_id, 1, _retryable(), utcnow())) is False  # PENDING
    _claim(overlay, job_id, 1)
    before = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (1,))
    _tick()
    next_at = utcnow() + timedelta(seconds=30)
    assert unwrap(overlay.reschedule(job_id, 1, _retryable("rate limited"), next_at)) is True
    row = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (1,))
    assert row["status"] == "PENDING" and row["retry_count"] == 1 and row["claimed_at"] is None
    assert (
        row["last_error_code"] == ErrorCode.PROVIDER_RATE_LIMITED.value
        and row["last_error"] == "rate limited"
    )
    assert len(row["next_attempt_at"]) == 27 and row["updated_at"] != before["updated_at"]
    assert m.parse_timestamp(row["next_attempt_at"]) == next_at.replace(
        microsecond=next_at.microsecond
    )
    assert unwrap(overlay.earliest_next_attempt(job_id)) == next_at

    _claim(overlay, job_id, 1)  # unit 2 (unit 1 is waiting)
    naive = overlay.reschedule(job_id, 2, _retryable(), datetime(2026, 1, 1, 12, 0, 0))
    assert isinstance(naive, Err) and naive.error.code is ErrorCode.INTERNAL
    assert raw_row_dict(mem_db, "overlay_blocks", "id = ?", (2,))["status"] == "PROCESSING"


# --------------------------------------------------------------------------- #
# 8. fail
# --------------------------------------------------------------------------- #


def test_08_fail(
    mem_db: Database, seeded_overlay: JobRecord, overlay: OverlayBlockRepository
) -> None:
    job_id = seeded_overlay.id
    assert unwrap(overlay.fail(job_id, 1, _fatal())) is False
    _claim(overlay, job_id, 2)
    unwrap(overlay.reschedule(job_id, 1, _retryable(), utcnow()))
    _claim(overlay, job_id, 1)  # unit 1 again (past next_attempt_at)
    long_message = "x" * 5000
    assert unwrap(overlay.fail(job_id, 1, _fatal(long_message))) is True
    row = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (1,))
    assert row["status"] == "FAILED" and row["retry_count"] == 1
    assert len(row["last_error"]) == 2000 and row["last_error"].endswith("…")
    assert row["next_attempt_at"] is None and row["claimed_at"] is None
    text = _integrity_error(
        mem_db, "UPDATE overlay_blocks SET last_error = ? WHERE id = 2", ("y" * 2001,)
    )
    assert "ck_overlay_blocks_last_error_len" in text


# --------------------------------------------------------------------------- #
# 9. release
# --------------------------------------------------------------------------- #


def test_09_release(mem_db: Database, job: JobRecord, overlay: OverlayBlockRepository) -> None:
    pages = [make_page(1, 600, 600)]
    units = [make_block(i, 1, i) for i in range(1, 601)]
    assert unwrap(overlay.replace_all(job.id, units, pages=pages)) == 600
    claimed = _claim(overlay, job.id, 10)
    unwrap(overlay.complete(job.id, claimed[0], _result(claimed[0])))
    assert unwrap(overlay.release(job.id, [])) == 0
    with capture_statements(mem_db) as statements:
        assert unwrap(overlay.release(job.id, list(range(1, 601)))) == 9
    assert sum(1 for s in statements if s.startswith("UPDATE")) == 2
    statuses = _status_map(mem_db)
    assert statuses[1] == "COMPLETED" and all(statuses[i] == "PENDING" for i in range(2, 601))


# --------------------------------------------------------------------------- #
# 10. requeue_processing / reset_failed
# --------------------------------------------------------------------------- #


def test_10_requeue_and_reset(
    mem_db: Database, seeded_overlay: JobRecord, overlay: OverlayBlockRepository
) -> None:
    job_id = seeded_overlay.id
    ids = _claim(overlay, job_id, 4)
    unwrap(overlay.complete(job_id, 1, _result(1)))
    unwrap(overlay.fail(job_id, 2, _fatal()))
    completed_before = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (1,))
    processing_before = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (3,))
    _tick()
    assert unwrap(overlay.requeue_processing(job_id)) == 2  # 3, 4
    row = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (3,))
    assert row["status"] == "PENDING" and row["claimed_at"] is None
    assert row["updated_at"] != processing_before["updated_at"]
    assert row["last_run_id"] == RUN_ID  # untouched
    untouched = {k: v for k, v in row.items() if k not in ("status", "claimed_at", "updated_at")}
    assert untouched == {
        k: v
        for k, v in processing_before.items()
        if k not in ("status", "claimed_at", "updated_at")
    }
    failed_before = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (2,))
    _tick()
    assert unwrap(overlay.reset_failed(job_id)) == 1
    row = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (2,))
    assert row["status"] == "PENDING" and row["retry_count"] == 0 and row["last_error"] is None
    assert row["last_error_code"] is None and row["next_attempt_at"] is None
    changed = (
        "status",
        "retry_count",
        "next_attempt_at",
        "last_error",
        "last_error_code",
        "updated_at",
    )
    assert {k: v for k, v in row.items() if k not in changed} == {
        k: v for k, v in failed_before.items() if k not in changed
    }
    assert raw_row_dict(mem_db, "overlay_blocks", "id = ?", (1,)) == completed_before
    assert ids == [1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# 11. Backoff gate
# --------------------------------------------------------------------------- #


def test_11_backoff_gate(
    mem_db: Database, seeded_overlay: JobRecord, overlay: OverlayBlockRepository
) -> None:
    job_id = seeded_overlay.id
    assert unwrap(overlay.earliest_next_attempt(job_id)) is None
    now = utcnow()
    _claim(overlay, job_id, 2)
    later = now + timedelta(minutes=10)
    sooner = now + timedelta(minutes=2)
    unwrap(overlay.reschedule(job_id, 1, _retryable(), later))
    unwrap(overlay.reschedule(job_id, 2, _retryable(), sooner))
    assert unwrap(overlay.earliest_next_attempt(job_id)) == sooner
    assert 1 not in _batch(overlay, job_id, 50, 0, now) and 2 not in _claim(overlay, job_id, 50)
    assert _batch(overlay, job_id, 50, 0, sooner) == [2]
    assert _claim(overlay, job_id, 50) == []
    assert [u.unit_id for u in unwrap(overlay.claim_pending(job_id, 50, later, RUN_ID))] == [1]
    assert unwrap(overlay.earliest_next_attempt(job_id)) is None
    assert unwrap(overlay.counts(job_id, now)).waiting_backoff == 0


# --------------------------------------------------------------------------- #
# 12. Aggregates and page helpers
# --------------------------------------------------------------------------- #


def test_12_aggregates_and_pages(
    mem_db: Database, job: JobRecord, overlay: OverlayBlockRepository
) -> None:
    # page 1: 2 translatable + kept; page 2: 1 translatable + kept; page 3: kept only;
    # page 4: no text layer (skipped)
    pages = [
        make_page(1, 3, 2),
        make_page(2, 2, 1),
        make_page(3, 1, 0),
        make_page(4, 0, 0, "no_text_layer"),
    ]
    units = [
        make_block(1, 1, 1, "Alpha one."),
        make_block(2, 1, 2, "Alpha two."),
        kept_block(3, 1, 3),
        make_block(4, 2, 1, "Beta one."),
        kept_block(5, 2, 2),
        kept_block(6, 3, 1),
    ]
    assert unwrap(overlay.replace_all(job.id, units, pages=pages)) == 6
    now = utcnow()
    assert unwrap(overlay.page_progress(job.id)) == (0, 2)
    assert unwrap(overlay.page_texts(job.id, [2, 3])) == {2: ["Beta one."], 3: []}
    assert unwrap(overlay.page_texts(job.id, [1])) == {1: ["Alpha one.", "Alpha two."]}
    assert unwrap(overlay.page_texts(job.id, [])) == {}
    listed = unwrap(overlay.pages(job.id))
    assert [p.page for p in listed] == [1, 2, 3, 4]
    assert listed[3].skip_reason == "no_text_layer" and listed[3].has_text_layer is False
    assert listed[0] == pages[0]

    ids = _claim(overlay, job.id, 6)
    assert ids == [1, 2, 3, 4, 5, 6]
    unwrap(overlay.complete(job.id, 1, _result(1, review_flag=True)))
    unwrap(overlay.complete(job.id, 2, _result(2, "x" * 20)))
    assert unwrap(overlay.page_progress(job.id)) == (0, 2)  # kept unit 3 still PROCESSING
    unwrap(overlay.complete(job.id, 3, _result(3, "42", chars_sent=0, chars_billed=0, attempts=0)))
    assert unwrap(overlay.page_progress(job.id)) == (1, 2)
    unwrap(overlay.fail(job.id, 4, _fatal()))
    unwrap(overlay.reschedule(job.id, 5, _retryable(), now + timedelta(minutes=5)))
    unwrap(overlay.complete(job.id, 6, _result(6, "1", chars_sent=0, chars_billed=0, attempts=0)))
    assert unwrap(overlay.page_progress(job.id)) == (1, 2)  # page 3 has translatable_count 0

    counts = unwrap(overlay.counts(job.id, now))
    assert (counts.pending, counts.processing, counts.completed, counts.failed) == (1, 0, 4, 1)
    assert counts.total == 6 and counts.waiting_backoff == 1
    totals = unwrap(overlay.totals(job.id))
    assert isinstance(totals, UnitTotals)
    assert totals.total_chunks == 6 and totals.translatable_count == 3
    assert totals.total_chars == sum(u.char_count for u in units)
    assert totals.completed_chars == sum(u.char_count for u in units if u.unit_id in (1, 2, 3, 6))
    assert totals.chars_sent == 13 + 20 and totals.chars_billed == 14 + 21
    assert unwrap(overlay.failed_ids(job.id)) == [4]
    assert unwrap(overlay.review_ids(job.id)) == [1]
    assert unwrap(overlay.count_completed_with_other_glossary(job.id, GLOSSARY_HASH)) == 0
    assert unwrap(overlay.count_completed_with_other_glossary(job.id, "h" * 64)) == 2  # 1, 2 only
    assert unwrap(overlay.count_completed_by_run(RUN_ID)) == (4, 1)
    assert unwrap(overlay.count_completed_by_run("nope")) == (0, 0)

    unwrap(overlay.reset_failed(job.id))
    unwrap(overlay.claim_pending(job.id, 10, now + timedelta(minutes=6), RUN_ID))
    unwrap(overlay.complete(job.id, 4, _result(4)))
    unwrap(overlay.complete(job.id, 5, _result(5, "2", chars_sent=0, chars_billed=0, attempts=0)))
    assert unwrap(overlay.page_progress(job.id)) == (2, 2)

    empty_job = "0" * 36
    assert unwrap(overlay.counts(empty_job, now)).total == 0
    assert unwrap(overlay.totals(empty_job)).total_chunks == 0
    assert unwrap(overlay.page_progress(empty_job)) == (0, 0)
    assert unwrap(overlay.pages(empty_job)) == []


# --------------------------------------------------------------------------- #
# 13. iter_ordered / iter_page
# --------------------------------------------------------------------------- #


def test_13_iterators(mem_db: Database, job: JobRecord, overlay: OverlayBlockRepository) -> None:
    pages, units = make_overlay(2, 5)  # 10 units
    assert unwrap(overlay.replace_all(job.id, units, pages=pages)) == 10
    with capture_statements(mem_db) as statements:
        ids = [u.unit_id for u in overlay.iter_ordered(job.id, batch_size=3)]
    assert ids == list(range(1, 11))
    assert sum(1 for s in statements if s.lstrip().upper().startswith("SELECT")) == 4
    assert statements.count("BEGIN") == 1
    with pytest.raises(ValueError):
        next(overlay.iter_ordered(job.id, batch_size=0))

    with capture_statements(mem_db) as statements:
        page_two = list(overlay.iter_page(job.id, 2))
    assert [u.unit_id for u in page_two] == [6, 7, 8, 9, 10]
    assert all(u.page == 2 for u in page_two)
    assert statements.count("BEGIN") == 1 and "BEGIN IMMEDIATE" not in statements
    assert page_two[0].block_id == "p002-b001" and page_two[-1].kind is OverlayBlockKind.PAGE_NUMBER
    assert page_two[0] == units[5]  # full round trip of a fresh (PENDING) unit
    assert list(overlay.iter_page(job.id, 3)) == []
    assert list(overlay.iter_page("0" * 36, 1)) == []


# --------------------------------------------------------------------------- #
# 14. replace_all
# --------------------------------------------------------------------------- #


def test_14_replace_all(mem_db: Database, job: JobRecord, overlay: OverlayBlockRepository) -> None:
    pages, units = make_overlay(2, 3)
    page_one = [make_page(1, 1, 1)]

    def rejected(bad_units: Sequence[OverlayBlock], bad_pages: Sequence[OverlayPage]) -> None:
        with capture_statements(mem_db) as statements:
            outcome = overlay.replace_all(job.id, bad_units, pages=bad_pages)
        assert isinstance(outcome, Err) and outcome.error.code is ErrorCode.CHUNK_INTEGRITY
        assert outcome.error.scope is ErrorScope.JOB_FATAL
        assert statements == [], statements

    rejected([make_block(1, 1, 1), make_block(3, 1, 2)], [make_page(1, 2, 2)])  # ids not dense
    rejected([make_block(1, 1, 1), make_block(2, 1, 3)], [make_page(1, 2, 2)])  # index not dense
    rejected([make_block(1, 1, 2)], page_one)  # index must start at 1
    rejected([make_block(1, 2, 1)], page_one)  # page not in pages
    rejected([make_block(1, 1, 1)], [])  # pages=() with units
    rejected([make_block(1, 1, 1, char_count=3)], page_one)
    rejected([make_block(1, 1, 1, content_hash="0" * 64)], page_one)
    rejected([make_block(1, 1, 1, block_id="p001-b009")], page_one)
    rejected([make_block(1, 1, 1)], [make_page(1, 2, 1)])  # block_count mismatch
    rejected([make_block(1, 1, 1)], [make_page(1, 1, 0)])  # translatable_count mismatch
    rejected([make_block(1, 1, 1)], [make_page(1, 1, 1, "no_text_layer")])  # skipped with blocks
    rejected([make_block(1, 1, 1, translate=False)], [make_page(1, 1, 0)])  # keep_reason missing
    rejected([make_block(1, 1, 1, keep_reason="rotated")], page_one)  # translate with reason
    rejected(
        [make_block(1, 1, 1, kind=OverlayBlockKind.PAGE_NUMBER)], page_one
    )  # page numbers are never translated
    rejected([make_block(1, 1, 1), make_block(2, 2, 1)], [make_page(2, 1, 1), make_page(1, 1, 1)])
    rejected([make_block(1, 2, 1), make_block(2, 1, 1)], [make_page(1, 1, 1), make_page(2, 1, 1)])
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM overlay_blocks")[0][0] == 0
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM overlay_pages")[0][0] == 0
    assert unwrap(overlay.replace_all(job.id, [], pages=[])) == 0
    assert unwrap(overlay.replace_all(job.id, [], pages=[make_page(1, 0, 0, "no_text_layer")])) == 0

    # volume: 30 000 blocks + 1 000 pages
    big_pages = [make_page(p, 30, 29) for p in range(1, 1001)]
    big_units: List[OverlayBlock] = []
    for page in range(1, 1001):
        for index in range(1, 31):
            unit_id = (page - 1) * 30 + index
            if index == 30:
                big_units.append(kept_block(unit_id, page, index))
            else:
                big_units.append(make_block(unit_id, page, index))
    started = time.perf_counter()
    assert unwrap(overlay.replace_all(job.id, big_units, pages=big_pages)) == 30_000
    # CR-68: a guard against quadratic behaviour, not a benchmark - ~3 s on a developer
    # machine; the generous bound keeps a loaded CI runner from failing the suite.
    assert time.perf_counter() - started < 60.0
    assert raw_rows(mem_db, "SELECT MAX(id), COUNT(*) FROM overlay_blocks")[0] == (30_000, 30_000)
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM overlay_pages")[0][0] == 1000
    assert (
        raw_rows(
            mem_db,
            "SELECT COUNT(*) FROM overlay_pages p WHERE block_count <> "
            "(SELECT COUNT(*) FROM overlay_blocks b WHERE b.page = p.page) "
            "OR translatable_count <> (SELECT COUNT(*) FROM overlay_blocks b "
            "WHERE b.page = p.page AND b.translate = 1)",
        )[0][0]
        == 0
    )
    assert (
        raw_rows(
            mem_db, "SELECT COUNT(*) FROM overlay_blocks WHERE char_count <> length(source_text)"
        )[0][0]
        == 0
    )

    # a second call replaces both tables
    assert unwrap(overlay.replace_all(job.id, units, pages=pages)) == 6
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM overlay_pages")[0][0] == 2
    assert raw_rows(mem_db, "SELECT MAX(id), COUNT(*) FROM overlay_blocks")[0] == (6, 6)
    stored = raw_row_dict(mem_db, "overlay_blocks", "id = ?", (1,))
    assert stored["status"] == "PENDING" and stored["line_boxes"] == "[[72.0,100.0,300.0,112.0]]"
    assert stored["x1"] == 300.0 and stored["prefix_text"] is None
    # refused once a COMPLETED row exists
    _claim(overlay, job.id, 1)
    unwrap(overlay.complete(job.id, 1, _result(1)))
    refused = overlay.replace_all(job.id, units, pages=pages)
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.INTERNAL
    assert "COMPLETED" in refused.error.message
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM overlay_blocks")[0][0] == 6

    # DB CHECKs (raw writes that bypass the Python validation)
    check_cases: List[Tuple[str, str, Tuple[Any, ...]]] = [
        (
            "ck_overlay_blocks_page_number_kept",
            "UPDATE overlay_blocks SET translate = 1, keep_reason = NULL WHERE id = 3",
            (),
        ),
        (
            "ck_overlay_blocks_keep_reason",
            "UPDATE overlay_blocks SET keep_reason = 'rotated' WHERE id = 2",
            (),
        ),
        (
            "ck_overlay_blocks_prefix_style",
            "UPDATE overlay_blocks SET prefix_bold = 1 WHERE id = 2",
            (),
        ),
        ("ck_overlay_blocks_bbox", "UPDATE overlay_blocks SET x1 = x0 - 1 WHERE id = 2", ()),
        ("ck_overlay_blocks_color", "UPDATE overlay_blocks SET color = 'FFFFFF' WHERE id = 2", ()),
        ("ck_overlay_blocks_family", "UPDATE overlay_blocks SET family = 'comic' WHERE id = 2", ()),
        (
            "ck_overlay_blocks_alignment",
            "UPDATE overlay_blocks SET alignment = 'middle' WHERE id = 2",
            (),
        ),
        ("ck_overlay_blocks_kind", "UPDATE overlay_blocks SET kind = 'poem' WHERE id = 2", ()),
        ("ck_overlay_pages_rotation", "UPDATE overlay_pages SET rotation = 45 WHERE page = 1", ()),
        (
            "ck_overlay_pages_translatable_count",
            "UPDATE overlay_pages SET translatable_count = block_count + 1 WHERE page = 1",
            (),
        ),
        ("ck_overlay_pages_skip", "UPDATE overlay_pages SET has_text_layer = 0 WHERE page = 1", ()),
        (
            "ck_overlay_pages_skip_empty",
            "UPDATE overlay_pages SET skip_reason = 'no_text_layer', has_text_layer = 0 "
            "WHERE page = 1",
            (),
        ),
        (
            "ck_overlay_pages_skip_reason",
            "UPDATE overlay_pages SET skip_reason = 'excluded' WHERE page = 1",
            (),
        ),
    ]
    for constraint, sql, params in check_cases:
        assert constraint in _integrity_error(mem_db, sql, params), constraint
    fk_text = _integrity_error(
        mem_db,
        "INSERT INTO overlay_blocks (id, job_id, page, index_on_page, kind, translate, x0, y0, "
        "x1, y1, font_size, family, color, alignment, source_text, content_hash, char_count, "
        "created_at, updated_at) VALUES (99, ?, 99, 1, 'body', 1, 0, 0, 1, 1, 10, 'serif', "
        "'#000000', 'left', 't', ?, 1, ?, ?)",
        (job.id, "a" * 64, m.format_timestamp(utcnow()), m.format_timestamp(utcnow())),
    )
    assert "FOREIGN KEY" in fk_text
    # a prefix style round-trips
    styled = OverlayStyle(12.0, True, False, "sans", "#123abc", prefix_style=("1.", True, False))
    pages_s = [make_page(1, 1, 1)]
    unit_s = make_block(1, 1, 1, "Styled.", style=styled, alignment=OverlayAlignment.JUSTIFY)
    fresh = OverlayBlockRepository(mem_db, RUN_ID, None)
    with mem_db.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM overlay_blocks")
    assert unwrap(fresh.replace_all(job.id, [unit_s], pages=pages_s)) == 1
    assert list(fresh.iter_page(job.id, 1)) == [unit_s]


# --------------------------------------------------------------------------- #
# 15. Lease + both passes in one process
# --------------------------------------------------------------------------- #


def test_15_lease_and_both_claims(
    mem_db: Database, seeded_overlay: JobRecord, overlay: OverlayBlockRepository
) -> None:
    job_id = seeded_overlay.id
    leases = LeaseRepository(mem_db)
    holder = LeaseHolder(pid=10, host="host-a", run_id=RUN_ID)
    assert unwrap(leases.acquire(job_id, holder, 60)) is True
    chunks = ChunkRepository(mem_db, RUN_ID, GLOSSARY_HASH)
    assert unwrap(chunks.replace_all(job_id, make_chunks(4))) == 4
    now = utcnow()
    assert _batch(overlay, job_id, 50, 0, now) == [1, 2, 3, 4]
    assert [c.chunk_id for c in unwrap(chunks.claim_pending(job_id, 2, now, RUN_ID))] == [1, 2]
    assert unwrap(overlay.counts(job_id)).processing == 4
    assert unwrap(chunks.counts(job_id)).processing == 2
    unwrap(chunks.complete(job_id, 1, _result(1)))
    unwrap(overlay.complete(job_id, 1, _result(1)))
    assert unwrap(chunks.count_completed_by_run(RUN_ID)) == (1, 0)
    assert unwrap(overlay.count_completed_by_run(RUN_ID)) == (1, 0)
    assert unwrap(leases.heartbeat(job_id, holder)) is None
    unwrap(leases.release(job_id, holder))
    assert unwrap(leases.get(job_id)) is None


# --------------------------------------------------------------------------- #
# 16. Job / runs
# --------------------------------------------------------------------------- #


def test_16_job_and_runs(mem_db: Database, job: JobRecord, jobs: JobRepository) -> None:
    assert job.overlay_status == "NONE" and job.overlay_sha256 is None
    assert job.overlay_translate_headers is False and job.overlay_unit_count == 0
    half = jobs.update_job(job.id, JobPatch(overlay_status="OVERLAY_EXTRACTED"))
    assert isinstance(half, Err) and half.error.code is ErrorCode.INTERNAL
    assert "ck_jobs_overlay_binding" in half.error.message
    bogus = jobs.update_job(job.id, JobPatch(overlay_status="bogus", overlay_sha256="b" * 64))
    assert isinstance(bogus, Err) and "ck_jobs_overlay_status" in bogus.error.message
    short = jobs.update_job(
        job.id, JobPatch(overlay_status="OVERLAY_EXTRACTED", overlay_sha256="b")
    )
    assert isinstance(short, Err) and "ck_jobs_overlay_sha256_len" in short.error.message
    patch = JobPatch(
        overlay_status="OVERLAY_EXTRACTED",
        overlay_sha256="b" * 64,
        overlay_translate_headers=True,
        overlay_keep_figure_text=False,
        overlay_tool_version="0.2.0",
        overlay_unit_count=42,
    )
    unwrap(jobs.update_job(job.id, patch))
    loaded = unwrap(jobs.get_job(job.id))
    assert loaded is not None
    assert (
        loaded.overlay_status,
        loaded.overlay_sha256,
        loaded.overlay_translate_headers,
        loaded.overlay_keep_figure_text,
        loaded.overlay_tool_version,
        loaded.overlay_unit_count,
    ) == ("OVERLAY_EXTRACTED", "b" * 64, True, False, "0.2.0", 42)
    assert loaded.status == "CREATED"  # the reflow position is independent
    assert unwrap(jobs.get_single_job()) == loaded
    clear_half = jobs.update_job(job.id, JobPatch(overlay_sha256=None))
    assert isinstance(clear_half, Err) and "ck_jobs_overlay_binding" in clear_half.error.message
    negative = jobs.update_job(job.id, JobPatch(overlay_unit_count=-1))
    assert isinstance(negative, Err) and "ck_jobs_overlay_unit_count" in negative.error.message

    unwrap(jobs.start_run(job.id, "run000000001", "translate", TOOL_VERSION))
    assert raw_row_dict(mem_db, "runs", "id = ?", ("run000000001",))["mode"] == "reflow"
    unwrap(
        jobs.start_run(job.id, "run000000002", "translate", TOOL_VERSION, "deepl", mode="overlay")
    )
    assert raw_row_dict(mem_db, "runs", "id = ?", ("run000000002",))["mode"] == "overlay"
    bad_mode = jobs.start_run(job.id, "run000000003", "export", TOOL_VERSION, mode="x")
    assert isinstance(bad_mode, Err) and bad_mode.error.code is ErrorCode.INTERNAL
    assert "ck_runs_mode" in bad_mode.error.message
    last = unwrap(jobs.get_last_run(job.id))
    assert last is not None and last.id == "run000000002" and last.mode == "overlay"
    assert last.units_completed == 0 and last.units_failed == 0


# --------------------------------------------------------------------------- #
# 17. Versioning and archive
# --------------------------------------------------------------------------- #


def test_17_versioning_and_archive(db_path: Path, tmp_path: Path) -> None:
    db = unwrap(open_database(db_path, tool_version=TOOL_VERSION))
    job = unwrap(JobRepository(db).get_or_create_job(JobSpec("in.pdf", "e" * 64, TOOL_VERSION)))
    pages, units = make_overlay(2, 3)
    unwrap(OverlayBlockRepository(db, RUN_ID, None).replace_all(job.id, units, pages=pages))
    unwrap(ChunkRepository(db, RUN_ID, None).replace_all(job.id, make_chunks(7)))
    unwrap(JobRepository(db).start_run(job.id, RUN_ID, "translate", TOOL_VERSION, mode="overlay"))
    unwrap(LeaseRepository(db).acquire(job.id, LeaseHolder(1, "h", RUN_ID), 60))
    counts_before = {t: raw_rows(db, f"SELECT COUNT(*) FROM {t}")[0][0] for t in m.TABLE_NAMES}
    close_database(db)
    assert counts_before["overlay_blocks"] == 6 and counts_before["overlay_pages"] == 2

    archived = unwrap(archive_database(db_path, tmp_path / "state-archive" / "20260101T000000Z"))
    copy = unwrap(open_database(archived, tool_version=TOOL_VERSION))
    try:
        assert copy.schema_version == m.CURRENT_SCHEMA_VERSION
        assert {
            t: raw_rows(copy, f"SELECT COUNT(*) FROM {t}")[0][0] for t in m.TABLE_NAMES
        } == counts_before
    finally:
        close_database(copy)

    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE schema_meta SET schema_version = 99 WHERE id = 1")
    newer = open_database(db_path, tool_version=TOOL_VERSION)
    assert isinstance(newer, Err) and newer.error.code is ErrorCode.STATE_SCHEMA_INCOMPATIBLE
    newer_ro = open_database(db_path, read_only=True, tool_version=TOOL_VERSION)
    assert isinstance(newer_ro, Err) and newer_ro.error.code is ErrorCode.STATE_SCHEMA_INCOMPATIBLE

    corrupt_path = tmp_path / "corrupt.db"
    with sqlite3.connect(corrupt_path) as raw:
        raw.execute("CREATE TABLE something_else (x INTEGER)")
    corrupt = open_database(corrupt_path, tool_version=TOOL_VERSION)
    assert isinstance(corrupt, Err) and corrupt.error.code is ErrorCode.STATE_CORRUPT
    assert (unwrap(open_database(":memory:", tool_version=TOOL_VERSION)).schema_version
            == m.CURRENT_SCHEMA_VERSION)


# --------------------------------------------------------------------------- #
# 20. Boundary
# --------------------------------------------------------------------------- #


def test_20_boundary(
    mem_db: Database,
    seeded_overlay: JobRecord,
    overlay: OverlayBlockRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = seeded_overlay.id
    marker = "UNIQUE-OVERLAY-TEXT-MARKER"
    bad_style = OverlayStyle(10.0, False, False, "serif", "FFFFFF")  # CHECK, not Python-validated
    bad = OverlayBlockRepository(mem_db, RUN_ID, None).replace_all(
        job_id, [make_block(1, 1, 1, marker, style=bad_style)], pages=[make_page(1, 1, 1)]
    )
    assert isinstance(bad, Err) and bad.error.code is ErrorCode.INTERNAL
    assert bad.error.scope is ErrorScope.JOB_FATAL
    assert "ck_overlay_blocks_color" in bad.error.message
    assert marker not in bad.error.message and marker not in (bad.error.cause or "")
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM overlay_blocks")[0][0] == 12  # rolled back

    def locked(self: Session, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Session, "execute", locked)
    outcome = overlay.page_progress(job_id)
    assert isinstance(outcome, Err)
    assert (
        outcome.error.code is ErrorCode.STATE_LOCKED and outcome.error.scope is ErrorScope.JOB_FATAL
    )
    monkeypatch.undo()

    def locked_conn(self: Connection, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Connection, "execute", locked_conn)
    write = overlay.requeue_processing(job_id)
    assert isinstance(write, Err) and write.error.code is ErrorCode.STATE_LOCKED
    monkeypatch.undo()
    assert isinstance(overlay.counts(job_id), Ok)

    def corrupt(self: Session, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(Session, "execute", corrupt)
    broken = overlay.totals(job_id)
    assert isinstance(broken, Err) and broken.error.code is ErrorCode.STATE_CORRUPT
    monkeypatch.undo()

    with mem_db.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE overlay_blocks SET warnings = 'not json' WHERE id = 1")
    decoded = overlay.claim_pending_batch(job_id, 50, 0, utcnow(), RUN_ID)
    assert isinstance(decoded, Err) and decoded.error.code is ErrorCode.STATE_CORRUPT
    assert (
        raw_row_dict(mem_db, "overlay_blocks", "id = ?", (1,))["status"] == "PENDING"
    )  # rolled back


# --------------------------------------------------------------------------- #
# 21. JsonBoxes
# --------------------------------------------------------------------------- #


def test_21_json_boxes(mem_db: Database, job: JobRecord, overlay: OverlayBlockRepository) -> None:
    boxes = m.JsonBoxes()
    dialect = mem_db.engine.dialect
    assert boxes.process_bind_param(((1, 2, 3, 4),), dialect) == "[[1.0,2.0,3.0,4.0]]"
    assert boxes.process_bind_param([], dialect) == "[]"
    assert boxes.process_bind_param(None, dialect) is None
    assert boxes.process_result_value("[[1,2,3,4],[5.5,6,7,8]]", dialect) == (
        (1.0, 2.0, 3.0, 4.0),
        (5.5, 6.0, 7.0, 8.0),
    )
    assert boxes.process_result_value(None, dialect) is None
    for bad in (
        ((1, 2, 3),),
        "[[1,2,3,4]]",
        ((1, "2", 3, 4),),
        (("1234",),),
        5,
        ((1, 2, 3, True),),
    ):
        with pytest.raises(ValueError):
            boxes.process_bind_param(bad, dialect)  # type: ignore[arg-type]
    for stored in ("[[1,2]]", "not json", "{}", '[["a","b","c","d"]]', "[1,2,3,4]"):
        with pytest.raises(m.CorruptValueError):
            boxes.process_result_value(stored, dialect)

    pages = [make_page(1, 1, 1)]
    unit = make_block(1, 1, 1, line_boxes=((1, 2, 3, 4), (5, 6, 7, 8)))
    assert unwrap(overlay.replace_all(job.id, [unit], pages=pages)) == 1
    assert raw_row_dict(mem_db, "overlay_blocks", "id = ?", (1,))["line_boxes"] == (
        "[[1.0,2.0,3.0,4.0],[5.0,6.0,7.0,8.0]]"
    )
    assert list(overlay.iter_page(job.id, 1))[0].line_boxes == (
        (1.0, 2.0, 3.0, 4.0),
        (5.0, 6.0, 7.0, 8.0),
    )
    with mem_db.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE overlay_blocks SET line_boxes = '[[1,2]]' WHERE id = 1")
    with pytest.raises(m.CorruptValueError):
        list(overlay.iter_page(job.id, 1))
    corrupt = overlay.claim_pending(job.id, 1, utcnow(), RUN_ID)  # the boundary maps it
    assert isinstance(corrupt, Err) and corrupt.error.code is ErrorCode.STATE_CORRUPT
    three_tuple: Any = ((1, 2, 3),)
    bad_bind = OverlayBlockRepository(mem_db, RUN_ID, None).replace_all(
        job.id, [make_block(1, 1, 1, line_boxes=three_tuple)], pages=pages
    )
    assert isinstance(bad_bind, Err) and bad_bind.error.code is ErrorCode.INTERNAL
