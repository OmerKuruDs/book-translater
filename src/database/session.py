"""Engine, PRAGMA and transaction-mode setup, open/close/archive, schema versioning.

Design doc 03, sections 6 and 7.

Transaction discipline
----------------------
pysqlite's implicit deferred ``BEGIN`` is disabled on every pooled connection
(``isolation_level = None``); the engine-level ``begin`` event then emits an
explicit ``BEGIN IMMEDIATE`` when the connection carries the execution option
``sqlite_txn_mode == "IMMEDIATE"`` and a plain ``BEGIN`` otherwise. Repositories
obtain sessions through :meth:`Database.write_session` (IMMEDIATE) and
:meth:`Database.read_session` (DEFERRED). ``COMMIT``/``ROLLBACK`` stay SQLAlchemy's.

Migration rules (section 7)
---------------------------
* ``MIGRATIONS`` is keyed by the *from* version; every step ``n -> n+1`` runs in
  one transaction together with the final ``schema_meta`` version bump.
* Idempotent DDL only (``CREATE TABLE IF NOT EXISTS``, ``CREATE INDEX IF NOT EXISTS``);
  column additions via ``ALTER TABLE ... ADD COLUMN`` with defaults; table rebuilds
  follow SQLite's 12-step procedure with ``PRAGMA foreign_keys=OFF`` issued on the
  connection *before* ``BEGIN``.
* ``CURRENT_SCHEMA_VERSION`` is bumped in the same commit as the migration function.
* A file is backed up to ``state-archive/<ts>-pre-migrate-v<n>/`` before migrating.
* Never migrate under ``query_only``: read-only opens accept every version in
  ``READ_ONLY_ACCEPTED_VERSIONS`` as is (``Database.schema_version`` tells the caller
  what it got, doc 07 D18) and report ``STATE_SCHEMA_INCOMPATIBLE`` otherwise.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple, Union

from sqlalchemy import Connection, Engine, create_engine, event, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool
from sqlalchemy.schema import CreateIndex, CreateTable

from ..domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, Result
from . import models as m

MEMORY = ":memory:"
DB_FILE_NAME = "translation_state.db"
ARCHIVE_DIR_NAME = "state-archive"
SQLITE_MIN_VERSION = (3, 8, 0)

#: Schema versions a read-only open accepts without migrating (doc 07, B15 / D18).
#: Only ``status`` opens read-only, and it reads ``overlay_blocks`` through aggregates
#: alone - a version listed here must therefore survive every *aggregate* query, not a
#: whole-row load (a v3 file has no ``redact_bbox`` column).
READ_ONLY_ACCEPTED_VERSIONS: Tuple[int, ...] = (1, 2, 3)

log = logging.getLogger("book_translator.database")

TXN_MODE_OPTION = "sqlite_txn_mode"
TXN_IMMEDIATE = "IMMEDIATE"
TXN_DEFERRED = "DEFERRED"

PathLike = Union[str, Path]


def utcnow() -> datetime:
    """The one clock helper: aware UTC ``datetime`` with microsecond precision."""
    return datetime.now(timezone.utc)


def archive_stamp(now: Optional[datetime] = None) -> str:
    """Directory stamp for ``state-archive/<YYYYMMDDTHHMMSSZ>``."""
    value = now if now is not None else utcnow()
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def is_memory(path: PathLike) -> bool:
    return str(path) == MEMORY


# --------------------------------------------------------------------------- #
# Engine and hooks (section 6)
# --------------------------------------------------------------------------- #


def make_engine(path_or_memory: PathLike, read_only: bool = False) -> Engine:
    """Create the engine with the PRAGMA ``connect`` hook and the transaction-mode ``begin`` hook.

    File databases use a small ``QueuePool``; ``check_same_thread=False`` is required
    because repository calls run in ``asyncio.to_thread`` workers (serialized by the
    orchestrator lock). ``:memory:`` uses a ``StaticPool`` (one shared connection).
    """
    if is_memory(path_or_memory):
        engine = create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    else:
        abs_path = Path(path_or_memory).resolve()
        engine = create_engine(
            f"sqlite:///{abs_path.as_posix()}",
            connect_args={"check_same_thread": False},
            poolclass=QueuePool,
            pool_size=2,
            max_overflow=3,
        )
    _install_hooks(engine, read_only)
    return engine


def _install_hooks(engine: Engine, read_only: bool) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, connection_record: Any) -> None:
        # Disable pysqlite's implicit BEGIN; transactions are started by _on_begin below.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA foreign_keys=ON")
            if read_only:
                cursor.execute("PRAGMA query_only=1")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        mode = conn.get_execution_options().get(TXN_MODE_OPTION)
        conn.exec_driver_sql("BEGIN IMMEDIATE" if mode == TXN_IMMEDIATE else "BEGIN")


# --------------------------------------------------------------------------- #
# Database handle
# --------------------------------------------------------------------------- #


class Database:
    """An opened state database: engine plus the two session context managers.

    ``write_session()`` starts a ``BEGIN IMMEDIATE`` transaction and commits on
    normal exit (rolls back on exception, or skips the commit if the caller
    already rolled back). ``read_session()`` starts a deferred ``BEGIN`` and
    always rolls back.
    """

    def __init__(
        self, engine: Engine, path: Optional[Path], read_only: bool, schema_version: int
    ) -> None:
        self.engine = engine
        self.path = path
        self.read_only = read_only
        self.schema_version = schema_version
        self._factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def write_session(self) -> Iterator[Session]:
        session = self._factory()
        try:
            session.connection(execution_options={TXN_MODE_OPTION: TXN_IMMEDIATE})
            yield session
            if session.in_transaction():
                session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        session = self._factory()
        try:
            session.connection(execution_options={TXN_MODE_OPTION: TXN_DEFERRED})
            yield session
        finally:
            session.rollback()
            session.close()


# --------------------------------------------------------------------------- #
# Schema versioning and migrations (section 7)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MigrationContext:
    tool_version: str
    now: datetime


MigrationStep = Callable[[Connection, MigrationContext], None]


def _create_current(conn: Connection, ctx: MigrationContext) -> None:
    """0 -> current: full DDL from the models plus the ``schema_meta`` row.

    A new file is created at ``CURRENT_SCHEMA_VERSION`` in one step; ``_run_migrations``
    loops ``from..to-1`` and therefore never runs the later upgrade steps on it.
    """
    m.Base.metadata.create_all(conn)
    conn.execute(
        insert(m.SchemaMeta).values(
            id=1,
            schema_version=m.CURRENT_SCHEMA_VERSION,
            created_by_tool_version=ctx.tool_version,
            created_at=ctx.now,
        )
    )


# ``ALTER TABLE ... ADD COLUMN`` has no Core construct; the column definitions below must
# match ``models.py`` (types, defaults, constraint names) -- acceptance check O-19 compares
# ``PRAGMA table_info`` of a migrated file with a fresh ``create_all`` file.
_V2_ADD_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    (
        "jobs",
        "overlay_status",
        "VARCHAR(20) NOT NULL DEFAULT 'NONE' CONSTRAINT ck_jobs_overlay_status CHECK "
        "(overlay_status IN ('NONE', 'OVERLAY_EXTRACTED', 'OVERLAY_TRANSLATING', "
        "'OVERLAY_PAUSED', 'OVERLAY_TRANSLATED', 'OVERLAY_EXPORTED'))",
    ),
    (
        "jobs",
        "overlay_sha256",
        "VARCHAR(64) CONSTRAINT ck_jobs_overlay_sha256_len CHECK "
        "(overlay_sha256 IS NULL OR length(overlay_sha256) = 64) "
        "CONSTRAINT ck_jobs_overlay_binding CHECK "
        "((overlay_status = 'NONE') = (overlay_sha256 IS NULL))",
    ),
    (
        "jobs",
        "overlay_translate_headers",
        "BOOLEAN NOT NULL DEFAULT 0 CONSTRAINT ck_jobs_overlay_translate_headers CHECK "
        "(overlay_translate_headers IN (0, 1))",
    ),
    (
        "jobs",
        "overlay_keep_figure_text",
        "BOOLEAN NOT NULL DEFAULT 0 CONSTRAINT ck_jobs_overlay_keep_figure_text CHECK "
        "(overlay_keep_figure_text IN (0, 1))",
    ),
    ("jobs", "overlay_tool_version", "VARCHAR(40)"),
    (
        "jobs",
        "overlay_unit_count",
        "INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_jobs_overlay_unit_count CHECK "
        "(overlay_unit_count >= 0)",
    ),
    (
        "runs",
        "mode",
        "VARCHAR(7) NOT NULL DEFAULT 'reflow' CONSTRAINT ck_runs_mode CHECK "
        "(mode IN ('reflow', 'overlay'))",
    ),
)


def _existing_columns(conn: Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").all()}


def _upgrade_v1_to_v2(conn: Connection, ctx: MigrationContext) -> None:
    """1 -> 2 (doc 07, section 8.2): two tables, four indexes, seven ``ADD COLUMN``.

    Additive only, no data rewrite. The table/index DDL is generated from the ``Table``
    objects with ``IF NOT EXISTS``; each ``ADD COLUMN`` is guarded by ``PRAGMA table_info``
    so that a re-run after a manual repair is harmless. The version bump is emitted by
    ``_run_migrations`` as the last statement of the same transaction.
    """
    dialect = conn.dialect
    tables = m.Base.metadata.tables
    for table in (tables["overlay_pages"], tables["overlay_blocks"]):
        conn.exec_driver_sql(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)))
        for index in sorted(table.indexes, key=lambda i: str(i.name)):
            conn.exec_driver_sql(
                str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect))
            )
    present: Dict[str, set[str]] = {}
    for table_name, column, definition in _V2_ADD_COLUMNS:
        if table_name not in present:
            present[table_name] = _existing_columns(conn, table_name)
        if column in present[table_name]:
            continue
        conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}")
        present[table_name].add(column)


def _upgrade_v2_to_v3(conn: Connection, ctx: MigrationContext) -> None:
    """2 -> 3: ``overlay_blocks.keep_reason`` accepts ``"math"``.

    A display equation is kept as one unit (``keep_reason="math"``) so the redaction of the
    paragraph around it cannot wipe it off the page. SQLite stores a CHECK constraint in the
    table DDL, so widening the allowed set means rebuilding the table: create it under a
    temporary name from the current metadata, copy every row of the shared columns, drop the
    old table and rename. Indexes are recreated from the metadata afterwards. Rows are
    preserved; no value is rewritten.

    The copied column list is the *intersection* of the old table and today's metadata, and
    it is read from the renamed table rather than assumed. A later migration may have added
    a column (v4 added ``redact_bbox``), and naming one the old table does not have would
    not raise here: SQLite reads an unknown double-quoted identifier as a string literal, so
    ``SELECT "redact_bbox"`` would quietly write the text ``redact_bbox`` into every row.
    Columns added after v3 are nullable, so leaving them out of the copy is correct - the
    migration that introduces them fills them in.
    """
    dialect = conn.dialect
    table = m.Base.metadata.tables["overlay_blocks"]
    if "keep_reason" not in _existing_columns(conn, "overlay_blocks"):
        return  # a v2 file that never held the overlay tables: nothing to widen
    conn.exec_driver_sql("ALTER TABLE overlay_blocks RENAME TO overlay_blocks_v2")
    existing = _existing_columns(conn, "overlay_blocks_v2")
    shared = [c.name for c in table.columns if c.name in existing]
    columns = ", ".join(f'"{name}"' for name in shared)
    conn.exec_driver_sql(str(CreateTable(table).compile(dialect=dialect)))
    conn.exec_driver_sql(
        f"INSERT INTO overlay_blocks ({columns}) SELECT {columns} FROM overlay_blocks_v2"
    )
    conn.exec_driver_sql("DROP TABLE overlay_blocks_v2")
    for index in sorted(table.indexes, key=lambda i: str(i.name)):
        conn.exec_driver_sql(str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect)))


_V4_ADD_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    ("overlay_blocks", "redact_bbox", "TEXT"),
)


def _upgrade_v3_to_v4(conn: Connection, ctx: MigrationContext) -> None:
    """3 -> 4: ``overlay_blocks.redact_bbox``, the rect a unit's source glyphs are cleared
    from when it differs from the placement rect.

    A unit whose rect was cut back against a *painted* neighbour used to be redacted over
    the cut rect, which left the source glyphs of the removed strip on the page beside the
    translation. The new column carries the wider redaction rect; ``NULL`` keeps the old
    meaning ("same as the placement rect"), so no row is rewritten. Additive
    ``ALTER TABLE ... ADD COLUMN``, guarded by ``PRAGMA table_info`` like the 1 -> 2 step;
    a v3 file that never held the overlay tables is skipped.
    """
    present: Dict[str, set[str]] = {}
    for table_name, column, definition in _V4_ADD_COLUMNS:
        if table_name not in present:
            present[table_name] = _existing_columns(conn, table_name)
        if not present[table_name] or column in present[table_name]:
            continue  # table absent (nothing to extend) or column already there
        conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column} {definition}")
        present[table_name].add(column)


MIGRATIONS: Dict[int, MigrationStep] = {
    0: _create_current,
    1: _upgrade_v1_to_v2,
    2: _upgrade_v2_to_v3,
    3: _upgrade_v3_to_v4,
}


def _run_migrations(
    engine: Engine, from_version: int, to_version: int, ctx: MigrationContext
) -> None:
    """Run every step ``from_version .. to_version-1`` and bump the version in one transaction."""
    with engine.connect() as conn:
        conn = conn.execution_options(**{TXN_MODE_OPTION: TXN_IMMEDIATE})
        with conn.begin():
            for version in range(from_version, to_version):
                # BUG-04: a schema upgrade used to leave no trace but the archive folder
                log.info(
                    "state schema upgrade %d -> %d", version, version + 1,
                    extra={"event": "schema_migrated", "from_version": version,
                           "to_version": version + 1},
                )
                MIGRATIONS[version](conn, ctx)
            if from_version >= 1:
                conn.execute(
                    update(m.SchemaMeta)
                    .where(m.SchemaMeta.id == 1)
                    .values(
                        schema_version=to_version,
                        upgraded_by_tool_version=ctx.tool_version,
                        upgraded_at=ctx.now,
                    )
                )


def read_schema_version(engine: Engine) -> Result[int]:
    """``SELECT schema_version FROM schema_meta WHERE id = 1``.

    A missing table or row yields ``STATE_CORRUPT``.
    """
    with engine.connect() as conn:
        table = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
        ).first()
        if table is None:
            return _err(ErrorCode.STATE_CORRUPT, "state database has no schema_meta table")
        version = conn.execute(
            select(m.SchemaMeta.schema_version).where(m.SchemaMeta.id == 1)
        ).scalar_one_or_none()
        conn.rollback()
    if version is None:
        return _err(ErrorCode.STATE_CORRUPT, "schema_meta has no version row")
    return Ok(int(version))


# --------------------------------------------------------------------------- #
# open / close / archive
# --------------------------------------------------------------------------- #


def open_database(
    path: PathLike, *, read_only: bool = False, tool_version: str
) -> Result[Database]:
    """Open (and create or migrate) the state database at ``path`` (or ``":memory:"``).

    1. ``read_only`` and file missing -> ``STATE_NOT_FOUND`` (USER).
    2. SQLite library floor check -> ``SQLITE_UNSUPPORTED`` (USER).
    3. Missing / empty file (not read-only) -> create the current schema in one transaction.
    4. Version check: equal -> proceed; older -> migrate (backup first); newer ->
       ``STATE_SCHEMA_INCOMPATIBLE``; missing table/row -> ``STATE_CORRUPT``. A read-only
       open never migrates: versions in ``READ_ONLY_ACCEPTED_VERSIONS`` are accepted as
       they are (``Database.schema_version``), older ones are refused.
    """
    memory = is_memory(path)
    file_path: Optional[Path] = None if memory else Path(path)
    if file_path is not None and read_only and not file_path.exists():
        return _err(ErrorCode.STATE_NOT_FOUND, f"state database not found: {file_path}")
    if memory and read_only:
        return _err(ErrorCode.STATE_NOT_FOUND, "an in-memory database cannot be opened read-only")
    if sqlite3.sqlite_version_info < SQLITE_MIN_VERSION:
        return _err(
            ErrorCode.SQLITE_UNSUPPORTED,
            f"SQLite {sqlite3.sqlite_version} is too old; 3.8.0 or newer is required",
        )

    needs_create = (
        memory or file_path is None or (not file_path.exists() or file_path.stat().st_size == 0)
    )
    ctx = MigrationContext(tool_version=tool_version, now=utcnow())
    engine = make_engine(path, read_only=read_only)
    try:
        if needs_create:
            _run_migrations(engine, 0, m.CURRENT_SCHEMA_VERSION, ctx)

        version_result = read_schema_version(engine)
        if isinstance(version_result, Err):
            engine.dispose()
            return version_result
        version = version_result.value

        if version > m.CURRENT_SCHEMA_VERSION:
            engine.dispose()
            return _err(
                ErrorCode.STATE_SCHEMA_INCOMPATIBLE,
                f"state created by a newer tool version (schema v{version}, "
                f"this tool supports v{m.CURRENT_SCHEMA_VERSION})",
            )
        if version < m.CURRENT_SCHEMA_VERSION and read_only:
            if version in READ_ONLY_ACCEPTED_VERSIONS:
                return Ok(Database(engine, file_path, read_only, version))
            engine.dispose()
            return _err(
                ErrorCode.STATE_SCHEMA_INCOMPATIBLE,
                f"state schema v{version} is older than v{m.CURRENT_SCHEMA_VERSION}; "
                "run translate once to migrate",
            )
        if version < m.CURRENT_SCHEMA_VERSION:
            if file_path is None:
                engine.dispose()
                return _err(
                    ErrorCode.STATE_SCHEMA_INCOMPATIBLE,
                    f"state schema v{version} is older than v{m.CURRENT_SCHEMA_VERSION}; "
                    "run translate once to migrate",
                )
            backup_dir = (
                file_path.parent
                / ARCHIVE_DIR_NAME
                / f"{archive_stamp(ctx.now)}-pre-migrate-v{version}"
            )
            backup = archive_database(file_path, backup_dir)
            if isinstance(backup, Err):
                engine.dispose()
                return backup
            try:
                _run_migrations(engine, version, m.CURRENT_SCHEMA_VERSION, ctx)
            except (SQLAlchemyError, sqlite3.Error, KeyError, ValueError) as exc:
                engine.dispose()
                return _err(
                    ErrorCode.STATE_SCHEMA_INCOMPATIBLE,
                    f"migration from schema v{version} failed; backup kept at {backup.value}",
                    cause=f"{type(exc).__name__}: {exc}",
                )
            verify = read_schema_version(engine)
            if isinstance(verify, Err) or verify.value != m.CURRENT_SCHEMA_VERSION:
                engine.dispose()
                return _err(
                    ErrorCode.STATE_SCHEMA_INCOMPATIBLE,
                    f"schema version verification failed after migration; backup at {backup.value}",
                )
            version = m.CURRENT_SCHEMA_VERSION

        return Ok(Database(engine, file_path, read_only, version))
    except (SQLAlchemyError, sqlite3.Error) as exc:
        engine.dispose()
        return _err(
            ErrorCode.STATE_CORRUPT,
            "state database could not be opened",
            cause=f"{type(exc).__name__}: {exc}",
        )


def close_database(db: Database) -> None:
    """Checkpoint the WAL (file databases) and dispose the engine."""
    if db.path is not None and not db.read_only:
        try:
            raw = db.engine.raw_connection()
            try:
                cursor = raw.cursor()
                cursor.execute("PRAGMA wal_checkpoint(PASSIVE)")
                cursor.close()
            finally:
                raw.close()
        except (SQLAlchemyError, sqlite3.Error):
            pass  # best effort; the last connection closing also checkpoints
    db.engine.dispose()


def archive_database(src: Path, dest_dir: Path) -> Result[Path]:
    """Copy ``src`` into ``dest_dir`` with SQLite's online backup API.

    The copy is consistent even while a ``-wal`` file exists (a plain file copy would drop it).
    """
    if not src.exists():
        return _err(ErrorCode.STATE_NOT_FOUND, f"state database not found: {src}")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / src.name
        source = sqlite3.connect(str(src))
        try:
            target = sqlite3.connect(str(dst))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except (OSError, sqlite3.Error) as exc:
        return _err(
            ErrorCode.STATE_CORRUPT,
            f"could not archive state database to {dest_dir}",
            cause=f"{type(exc).__name__}: {exc}",
        )
    return Ok(dst)


def _err(code: ErrorCode, message: str, *, cause: Optional[str] = None) -> Err:
    return Err(AppError(code=code, message=message, scope=ErrorScope.USER, cause=cause))
