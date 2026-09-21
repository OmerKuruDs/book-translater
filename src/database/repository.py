"""Result-returning repositories over ``translation_state.db`` (DB design doc 03, section 5).

General contract
----------------
* One session, one transaction per call. Write methods run under
  ``BEGIN IMMEDIATE`` (``Database.write_session``), read methods under a deferred
  ``BEGIN`` (``Database.read_session``).
* Every state transition is a Core ``UPDATE ... WHERE <precondition>`` whose
  ``rowcount`` is checked; never ORM attribute assignment plus ``commit()``.
  Writes are executed on ``session.connection()`` (pure Core): the ORM-enabled
  ``Session.execute(update(...))`` path would append ``RETURNING`` for session
  synchronization, which the design forbids (SQLite floor 3.31).
* ``IN (...)`` lists are split into batches of 500 ids.
* Every public method is wrapped by :func:`boundary`, which maps exceptions to
  ``Err``: "database is locked" -> ``STATE_LOCKED``; other operational/database
  errors -> ``STATE_CORRUPT``; ``IntegrityError`` -> ``INTERNAL`` (message names the
  constraint, never row contents); ``ValueError`` from the type decorators ->
  ``INTERNAL``; undecodable stored values -> ``STATE_CORRUPT``.
* Records are frozen dataclasses (``JobRecord``, ``RunRecord``, ``LeaseRecord``) or
  the domain ``Chunk``; live ORM instances never leave this module.
* Status literals in hot predicates are SQL literals (``literal_column``) so that
  SQLite can use the partial indexes (section 4).

Deviations D1-D5 and additions A1-A5 of the design document are implemented.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
    cast,
)

from sqlalchemy import (
    ColumnClause,
    Select,
    case,
    delete,
    func,
    insert,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import CursorResult

from ..domain.models import (
    Chunk,
    ChunkKind,
    ChunkStatus,
    StatusCounts,
    TranslationResult,
    content_hash_of,
)
from ..domain.overlay import (
    OverlayAlignment,
    OverlayBlock,
    OverlayBlockKind,
    OverlayPage,
    OverlayStyle,
    format_block_id,
)
from ..domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, Result
from . import models as m
from .session import Database, utcnow

log = logging.getLogger(__name__)

IN_BATCH_SIZE = 500
LAST_ERROR_MAX_CHARS = 2000
_TRUNCATION_SUFFIX = "…"

# SQL literals (not bound parameters) so SQLite can prove the partial-index predicates.
_PENDING: ColumnClause[str] = literal_column("'PENDING'")
_PROCESSING: ColumnClause[str] = literal_column("'PROCESSING'")
_COMPLETED: ColumnClause[str] = literal_column("'COMPLETED'")
_FAILED: ColumnClause[str] = literal_column("'FAILED'")
_ONE: ColumnClause[int] = literal_column("1")


# --------------------------------------------------------------------------- #
# Record dataclasses
# --------------------------------------------------------------------------- #


class _Unset:
    """Sentinel type for ``JobPatch`` fields that are not part of the patch."""

    _instance: Optional["_Unset"] = None

    def __new__(cls) -> "_Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


@dataclass(frozen=True)
class JobSpec:
    """Input for ``JobRepository.get_or_create_job``."""

    input_path: str
    input_sha256: str
    tool_version: str


@dataclass(frozen=True)
class JobPatch:
    """Partial update for ``jobs``; only fields that are not ``UNSET`` are written."""

    status: Union[str, _Unset] = UNSET
    source_md_sha256: Union[Optional[str], _Unset] = UNSET
    chunk_min_chars: Union[Optional[int], _Unset] = UNSET
    chunk_max_chars: Union[Optional[int], _Unset] = UNSET
    provider: Union[Optional[str], _Unset] = UNSET
    glossary_strategy: Union[Optional[str], _Unset] = UNSET
    provider_glossary_id: Union[Optional[str], _Unset] = UNSET
    glossary_hash: Union[Optional[str], _Unset] = UNSET
    extractor_name: Union[Optional[str], _Unset] = UNSET
    fallback_used: Union[bool, _Unset] = UNSET
    detected_language: Union[Optional[str], _Unset] = UNSET
    warnings: Union[Sequence[str], _Unset] = UNSET
    # v2 overlay binding (doc 07, section 3.3 / 9.2); set together with overlay_sha256
    overlay_status: Union[str, _Unset] = UNSET
    overlay_sha256: Union[Optional[str], _Unset] = UNSET
    overlay_translate_headers: Union[bool, _Unset] = UNSET
    overlay_keep_figure_text: Union[bool, _Unset] = UNSET
    overlay_tool_version: Union[Optional[str], _Unset] = UNSET
    overlay_unit_count: Union[int, _Unset] = UNSET
    # CR-39: the job remembers the resolved absolute location of its (hash-verified) input
    input_path: Union[str, _Unset] = UNSET

    def values(self) -> Dict[str, Any]:
        """Column -> value for every field that is set."""
        result: Dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if value is UNSET:
                continue
            result[f.name] = tuple(value) if f.name == "warnings" else value
        return result


@dataclass(frozen=True)
class JobRecord:
    id: str
    input_path: str
    input_sha256: str
    source_md_sha256: Optional[str]
    status: str
    tool_version: str
    extractor_name: Optional[str]
    fallback_used: bool
    detected_language: Optional[str]
    chunk_min_chars: Optional[int]
    chunk_max_chars: Optional[int]
    provider: Optional[str]
    glossary_strategy: Optional[str]
    provider_glossary_id: Optional[str]
    glossary_hash: Optional[str]
    warnings: Tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    # v2 overlay binding
    overlay_status: str = "NONE"
    overlay_sha256: Optional[str] = None
    overlay_translate_headers: bool = False
    overlay_keep_figure_text: bool = False
    overlay_tool_version: Optional[str] = None
    overlay_unit_count: int = 0


@dataclass(frozen=True)
class RunRecord:
    id: str
    job_id: str
    command: str
    tool_version: str
    provider: Optional[str]
    provider_switch_allowed: bool
    started_at: datetime
    finished_at: Optional[datetime]
    outcome: Optional[str]
    exit_code: Optional[int]
    chunks_completed: int
    chunks_failed: int
    chars_sent: int
    provider_calls: int
    rate_limited_count: int
    mode: str = "reflow"  # v2: 'reflow' | 'overlay'

    @property
    def units_completed(self) -> int:
        """Alias: ``chunks_completed`` counts units of either pass (doc 07, DQ6)."""
        return self.chunks_completed

    @property
    def units_failed(self) -> int:
        return self.chunks_failed


@dataclass(frozen=True)
class RunOutcome:
    """Input for ``JobRepository.finish_run``."""

    outcome: str  # SUCCESS | PARTIAL | PAUSED | FAILED | INTERRUPTED
    exit_code: int
    chunks_completed: int
    chunks_failed: int
    chars_sent: int
    provider_calls: int
    rate_limited_count: int


@dataclass(frozen=True)
class LeaseHolder:
    pid: int
    host: str
    run_id: str


@dataclass(frozen=True)
class LeaseRecord:
    job_id: str
    holder_pid: int
    holder_host: str
    run_id: str
    acquired_at: datetime
    heartbeat_at: datetime


@dataclass(frozen=True)
class ChunkTotals:
    total_chunks: int
    chars_sent: int
    chars_billed: int
    completed_chars: int  # SUM(char_count) over COMPLETED rows
    translatable_count: int
    total_chars: int  # SUM(char_count) over all rows


UnitTotals = ChunkTotals  # the overlay repository returns the same aggregate (total_chunks = units)


# --------------------------------------------------------------------------- #
# Exception -> Err boundary
# --------------------------------------------------------------------------- #


class _Abort(Exception):
    """Raised inside a transaction to roll it back and return ``Err(error)``."""

    def __init__(self, error: AppError) -> None:
        super().__init__(error.message)
        self.error = error


def classify_exception(exc: BaseException, operation: str) -> AppError:
    """Map a database-layer exception to an ``AppError`` without leaking row contents."""
    orig: BaseException = exc
    if isinstance(exc, sa_exc.StatementError) and isinstance(exc.orig, BaseException):
        orig = exc.orig  # DBAPIError and StatementError both carry the original exception
    cause = f"{type(orig).__name__}: {orig}"
    text = str(orig).lower()

    if isinstance(orig, m.CorruptValueError):
        return AppError(
            code=ErrorCode.STATE_CORRUPT,
            message=f"{operation}: stored value is corrupt ({orig})",
            scope=ErrorScope.JOB_FATAL,
            cause=cause,
        )
    if isinstance(orig, ValueError) and not isinstance(orig, sqlite3.Error):
        return AppError(
            code=ErrorCode.INTERNAL,
            message=f"{operation}: invalid value ({orig})",
            scope=ErrorScope.JOB_FATAL,
            cause=cause,
        )
    if isinstance(orig, sqlite3.IntegrityError) or isinstance(exc, sa_exc.IntegrityError):
        return AppError(
            code=ErrorCode.INTERNAL,
            message=f"{operation}: integrity constraint violated ({orig})",
            scope=ErrorScope.JOB_FATAL,
            cause=cause,
        )
    if isinstance(orig, sqlite3.OperationalError) or isinstance(exc, sa_exc.OperationalError):
        if "locked" in text or "busy" in text:
            return AppError(
                code=ErrorCode.STATE_LOCKED,
                message=f"{operation}: state database is locked by another process",
                scope=ErrorScope.JOB_FATAL,
                cause=cause,
            )
        if "readonly" in text or "read-only" in text:
            return AppError(
                code=ErrorCode.INTERNAL,
                message=f"{operation}: write attempted on a read-only database",
                scope=ErrorScope.JOB_FATAL,
                cause=cause,
            )
    return AppError(
        code=ErrorCode.STATE_CORRUPT,
        message=f"{operation}: state database error ({type(orig).__name__})",
        scope=ErrorScope.JOB_FATAL,
        cause=cause,
    )


F = TypeVar("F", bound=Callable[..., Any])


def boundary(func: F) -> F:
    """Decorator: convert database exceptions raised by ``func`` into ``Err`` results."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except _Abort as abort:
            return Err(abort.error)
        except (sa_exc.SQLAlchemyError, sqlite3.Error, ValueError) as exc:
            return Err(classify_exception(exc, func.__name__))

    return cast(F, wrapper)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _rowcount(result: Any) -> int:
    return int(cast("CursorResult[Any]", result).rowcount)


def _batched(items: Sequence[int], size: int = IN_BATCH_SIZE) -> Iterator[Sequence[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def truncate_error(message: str, limit: int = LAST_ERROR_MAX_CHARS) -> str:
    """Truncate ``message`` to ``limit`` chars, ending with an ellipsis when cut."""
    if len(message) <= limit:
        return message
    return message[: limit - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


# ``jobs`` / ``runs`` are read column-wise so that a read-only v1 file (doc 07, D18: no
# overlay columns yet) can still be served: the v2 columns are selected only when
# ``Database.schema_version >= 2`` and the records fall back to the column defaults.
_JOB_V1_COLUMNS: Tuple[Any, ...] = (
    m.Job.id,
    m.Job.input_path,
    m.Job.input_sha256,
    m.Job.source_md_sha256,
    m.Job.status,
    m.Job.tool_version,
    m.Job.extractor_name,
    m.Job.fallback_used,
    m.Job.detected_language,
    m.Job.chunk_min_chars,
    m.Job.chunk_max_chars,
    m.Job.provider,
    m.Job.glossary_strategy,
    m.Job.provider_glossary_id,
    m.Job.glossary_hash,
    m.Job.warnings,
    m.Job.created_at,
    m.Job.updated_at,
)
_JOB_V2_COLUMNS: Tuple[Any, ...] = (
    m.Job.overlay_status,
    m.Job.overlay_sha256,
    m.Job.overlay_translate_headers,
    m.Job.overlay_keep_figure_text,
    m.Job.overlay_tool_version,
    m.Job.overlay_unit_count,
)
_RUN_V1_COLUMNS: Tuple[Any, ...] = (
    m.Run.id,
    m.Run.job_id,
    m.Run.command,
    m.Run.tool_version,
    m.Run.provider,
    m.Run.provider_switch_allowed,
    m.Run.started_at,
    m.Run.finished_at,
    m.Run.outcome,
    m.Run.exit_code,
    m.Run.chunks_completed,
    m.Run.chunks_failed,
    m.Run.chars_sent,
    m.Run.provider_calls,
    m.Run.rate_limited_count,
)
_RUN_V2_COLUMNS: Tuple[Any, ...] = (m.Run.mode,)


def _job_select(schema_version: int) -> Select[Any]:
    columns = _JOB_V1_COLUMNS + (_JOB_V2_COLUMNS if schema_version >= 2 else ())
    return select(*columns)


def _run_select(schema_version: int) -> Select[Any]:
    columns = _RUN_V1_COLUMNS + (_RUN_V2_COLUMNS if schema_version >= 2 else ())
    return select(*columns)


def _to_job(row: Any) -> JobRecord:
    """Build a ``JobRecord`` from a column-wise row (v1 rows lack the overlay columns)."""
    data = dict(row._mapping)
    return JobRecord(
        id=data["id"],
        input_path=data["input_path"],
        input_sha256=data["input_sha256"],
        source_md_sha256=data["source_md_sha256"],
        status=data["status"],
        tool_version=data["tool_version"],
        extractor_name=data["extractor_name"],
        fallback_used=bool(data["fallback_used"]),
        detected_language=data["detected_language"],
        chunk_min_chars=data["chunk_min_chars"],
        chunk_max_chars=data["chunk_max_chars"],
        provider=data["provider"],
        glossary_strategy=data["glossary_strategy"],
        provider_glossary_id=data["provider_glossary_id"],
        glossary_hash=data["glossary_hash"],
        warnings=tuple(data["warnings"]),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        overlay_status=data.get("overlay_status", "NONE"),
        overlay_sha256=data.get("overlay_sha256"),
        overlay_translate_headers=bool(data.get("overlay_translate_headers", False)),
        overlay_keep_figure_text=bool(data.get("overlay_keep_figure_text", False)),
        overlay_tool_version=data.get("overlay_tool_version"),
        overlay_unit_count=int(data.get("overlay_unit_count", 0)),
    )


def _to_run(row: Any) -> RunRecord:
    data = dict(row._mapping)
    return RunRecord(
        id=data["id"],
        job_id=data["job_id"],
        command=data["command"],
        tool_version=data["tool_version"],
        provider=data["provider"],
        provider_switch_allowed=bool(data["provider_switch_allowed"]),
        started_at=data["started_at"],
        finished_at=data["finished_at"],
        outcome=data["outcome"],
        exit_code=data["exit_code"],
        chunks_completed=data["chunks_completed"],
        chunks_failed=data["chunks_failed"],
        chars_sent=data["chars_sent"],
        provider_calls=data["provider_calls"],
        rate_limited_count=data["rate_limited_count"],
        mode=str(data.get("mode", "reflow")),
    )


def _to_lease(row: m.Lease) -> LeaseRecord:
    return LeaseRecord(
        job_id=row.job_id,
        holder_pid=row.holder_pid,
        holder_host=row.holder_host,
        run_id=row.run_id,
        acquired_at=row.acquired_at,
        heartbeat_at=row.heartbeat_at,
    )


def _to_chunk(row: m.Chunk) -> Chunk:
    return Chunk(
        chunk_id=row.id,
        order=row.id,
        kind=ChunkKind(row.kind),
        translatable=bool(row.translatable),
        source_text=row.source_text,
        content_hash=row.content_hash,
        char_count=row.char_count,
        parent_block_id=row.parent_block_id,
        sub_index=row.sub_index,
        sub_count=row.sub_count,
        heading_path=tuple(row.heading_path),
        status=ChunkStatus(row.status),
        retry_count=row.retry_count,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        translated_text=row.translated_text,
        review_flag=bool(row.review_flag),
        warnings=tuple(row.warnings),
    )


def claim_select(job_id: str, now: datetime, limit: int) -> Select[Tuple[int]]:
    """Step 1 of ``claim_pending`` (exposed so tests can EXPLAIN it)."""
    return (
        select(m.Chunk.id)
        .where(
            m.Chunk.job_id == job_id,
            m.Chunk.status == _PENDING,
            or_(m.Chunk.next_attempt_at.is_(None), m.Chunk.next_attempt_at <= now),
        )
        .order_by(m.Chunk.id)
        .limit(limit)
    )


def _validate_chunks(chunks: Sequence[Chunk]) -> Optional[AppError]:
    """Python pre-checks for ``replace_all``: dense ids, char_count and content_hash."""
    for index, chunk in enumerate(chunks, start=1):
        if chunk.chunk_id != index or chunk.order != index:
            return AppError(
                code=ErrorCode.CHUNK_INTEGRITY,
                message=f"chunk ids must be dense 1..n in order (position {index} has id "
                f"{chunk.chunk_id}, order {chunk.order})",
                scope=ErrorScope.JOB_FATAL,
            )
        if chunk.char_count != len(chunk.source_text):
            return AppError(
                code=ErrorCode.CHUNK_INTEGRITY,
                message=f"chunk {index}: char_count {chunk.char_count} != len(source_text) "
                f"{len(chunk.source_text)}",
                scope=ErrorScope.JOB_FATAL,
            )
        if chunk.content_hash != content_hash_of(chunk.source_text):
            return AppError(
                code=ErrorCode.CHUNK_INTEGRITY,
                message=f"chunk {index}: content_hash does not match source_text",
                scope=ErrorScope.JOB_FATAL,
            )
    return None


# --------------------------------------------------------------------------- #
# 5.1 JobRepository
# --------------------------------------------------------------------------- #


class JobRepository:
    """Jobs, runs and the schema version."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @boundary
    def schema_version(self) -> Result[int]:
        with self._db.read_session() as session:
            table = (
                session.connection()
                .exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
                )
                .first()
            )
            if table is None:
                raise _Abort(_corrupt("schema_meta table missing"))
            version = session.execute(
                select(m.SchemaMeta.schema_version).where(m.SchemaMeta.id == 1)
            ).scalar_one_or_none()
            if version is None:
                raise _Abort(_corrupt("schema_meta row missing"))
            return Ok(int(version))

    @boundary
    def get_or_create_job(self, spec: JobSpec) -> Result[JobRecord]:
        now = utcnow()
        job_query = _job_select(self._db.schema_version)
        try:
            with self._db.write_session() as session:
                rows = session.execute(job_query.limit(2)).all()
                if len(rows) == 1:
                    return Ok(_to_job(rows[0]))
                if len(rows) > 1:
                    raise _Abort(_corrupt("more than one job row in a single-job database"))
                job_id = str(uuid.uuid4())
                session.connection().execute(
                    insert(m.Job).values(
                        id=job_id,
                        singleton=1,
                        input_path=spec.input_path,
                        input_sha256=spec.input_sha256,
                        status="CREATED",
                        tool_version=spec.tool_version,
                        warnings=(),
                        created_at=now,
                        updated_at=now,
                    )
                )
                created = session.execute(job_query.where(m.Job.id == job_id)).one()
                return Ok(_to_job(created))
        except (sa_exc.IntegrityError, sqlite3.IntegrityError):
            # A concurrent creator won the UNIQUE(singleton) race: re-read once.
            with self._db.read_session() as session:
                existing = session.execute(job_query.limit(1)).one_or_none()
                if existing is None:
                    raise
                return Ok(_to_job(existing))

    @boundary
    def get_job(self, job_id: str) -> Result[Optional[JobRecord]]:
        with self._db.read_session() as session:
            row = session.execute(
                _job_select(self._db.schema_version).where(m.Job.id == job_id)
            ).one_or_none()
            return Ok(None if row is None else _to_job(row))

    @boundary
    def get_single_job(self) -> Result[Optional[JobRecord]]:
        """The one job of this single-job database (``None`` before ``get_or_create_job``).

        Two or more rows mean a corrupt state file (``STATE_CORRUPT``). The orchestrator
        uses this instead of building its own SQL (design doc 02, 1.3/1.4).
        """
        with self._db.read_session() as session:
            rows = session.execute(_job_select(self._db.schema_version).limit(2)).all()
            if len(rows) > 1:
                raise _Abort(_corrupt("more than one job row in a single-job database"))
            return Ok(_to_job(rows[0]) if rows else None)

    @boundary
    def update_job(self, job_id: str, patch: JobPatch) -> Result[None]:
        values = patch.values()
        if not values:
            return Ok(None)
        values["updated_at"] = utcnow()
        with self._db.write_session() as session:
            result = session.connection().execute(
                update(m.Job).where(m.Job.id == job_id).values(**values)
            )
            if _rowcount(result) == 0:
                raise _Abort(_not_found(f"job {job_id} not found"))
        return Ok(None)

    @boundary
    def start_run(
        self,
        job_id: str,
        run_id: str,
        command: str,
        tool_version: str,
        provider: Optional[str] = None,
        provider_switch_allowed: bool = False,
        mode: str = "reflow",
    ) -> Result[None]:
        """Insert the run row (D1: also records tool version, provider and the switch flag).

        ``mode`` (v2) is ``'reflow'`` or ``'overlay'``; the CHECK ``ck_runs_mode`` rejects
        anything else as ``Err(INTERNAL)``.
        """
        with self._db.write_session() as session:
            session.connection().execute(
                insert(m.Run).values(
                    id=run_id,
                    job_id=job_id,
                    command=command,
                    tool_version=tool_version,
                    provider=provider,
                    provider_switch_allowed=provider_switch_allowed,
                    started_at=utcnow(),
                    chunks_completed=0,
                    chunks_failed=0,
                    chars_sent=0,
                    provider_calls=0,
                    rate_limited_count=0,
                    mode=mode,
                )
            )
        return Ok(None)

    @boundary
    def finish_run(self, job_id: str, run_id: str, outcome: RunOutcome) -> Result[None]:
        with self._db.write_session() as session:
            result = session.connection().execute(
                update(m.Run)
                .where(m.Run.id == run_id, m.Run.job_id == job_id, m.Run.finished_at.is_(None))
                .values(
                    finished_at=utcnow(),
                    outcome=outcome.outcome,
                    exit_code=outcome.exit_code,
                    chunks_completed=outcome.chunks_completed,
                    chunks_failed=outcome.chunks_failed,
                    chars_sent=outcome.chars_sent,
                    provider_calls=outcome.provider_calls,
                    rate_limited_count=outcome.rate_limited_count,
                )
            )
            if _rowcount(result) == 0:
                raise _Abort(_not_found(f"run {run_id} missing or already finished"))
        return Ok(None)

    @boundary
    def get_last_run(self, job_id: str) -> Result[Optional[RunRecord]]:
        """A1: the most recently started run of the job."""
        with self._db.read_session() as session:
            row = session.execute(
                _run_select(self._db.schema_version)
                .where(m.Run.job_id == job_id)
                .order_by(m.Run.started_at.desc(), m.Run.id.desc())
                .limit(1)
            ).one_or_none()
            return Ok(None if row is None else _to_run(row))


# --------------------------------------------------------------------------- #
# 5.2 ChunkRepository
# --------------------------------------------------------------------------- #


class ChunkRepository:
    """Chunk rows and the chunk state machine.

    Bound per run (D5): ``run_id`` identifies the claiming run (``claim_pending`` still
    takes it explicitly for API symmetry) and ``glossary_hash`` is the effective
    glossary written by ``complete``.
    """

    def __init__(self, db: Database, run_id: str, glossary_hash: Optional[str]) -> None:
        self._db = db
        self.run_id = run_id
        self.glossary_hash = glossary_hash

    # -- chunk stage ------------------------------------------------------- #

    @boundary
    def replace_all(self, job_id: str, chunks: Iterable[Chunk]) -> Result[int]:
        items = list(chunks)
        problem = _validate_chunks(items)
        if problem is not None:
            return Err(problem)
        now = utcnow()
        rows: List[Dict[str, Any]] = [
            {
                "id": c.chunk_id,
                "job_id": job_id,
                "kind": c.kind.value,
                "translatable": c.translatable,
                "source_text": c.source_text,
                "content_hash": c.content_hash,
                "char_count": c.char_count,
                "parent_block_id": c.parent_block_id,
                "sub_index": c.sub_index,
                "sub_count": c.sub_count,
                "heading_path": tuple(c.heading_path),
                "status": "PENDING",
                "retry_count": 0,
                "review_flag": False,
                "warnings": tuple(c.warnings),
                "chars_sent": 0,
                "chars_billed": 0,
                "attempts": 0,
                "created_at": now,
                "updated_at": now,
            }
            for c in items
        ]
        with self._db.write_session() as session:
            completed = session.execute(
                select(func.count())
                .select_from(m.Chunk)
                .where(m.Chunk.job_id == job_id, m.Chunk.status == _COMPLETED)
            ).scalar_one()
            if completed:
                raise _Abort(
                    AppError(
                        code=ErrorCode.INTERNAL,
                        message=f"replace_all refused: {completed} COMPLETED chunks present",
                        scope=ErrorScope.JOB_FATAL,
                    )
                )
            session.connection().execute(delete(m.Chunk).where(m.Chunk.job_id == job_id))
            for start in range(0, len(rows), IN_BATCH_SIZE):
                session.connection().execute(insert(m.Chunk), rows[start : start + IN_BATCH_SIZE])
        return Ok(len(items))

    # -- transitions ------------------------------------------------------- #

    @boundary
    def requeue_processing(self, job_id: str) -> Result[int]:
        with self._db.write_session() as session:
            result = session.connection().execute(
                update(m.Chunk)
                .where(m.Chunk.job_id == job_id, m.Chunk.status == _PROCESSING)
                .values(status="PENDING", claimed_at=None, updated_at=utcnow())
            )
            return Ok(_rowcount(result))

    @boundary
    def claim_pending(
        self, job_id: str, limit: int, now: datetime, run_id: str
    ) -> Result[List[Chunk]]:
        if limit <= 0:
            return Ok([])
        with self._db.write_session() as session:
            ids = list(session.execute(claim_select(job_id, now, limit)).scalars().all())
            if not ids:
                return Ok([])
            updated = 0
            for batch in _batched(ids):
                result = session.connection().execute(
                    update(m.Chunk)
                    .where(
                        m.Chunk.job_id == job_id,
                        m.Chunk.id.in_(batch),
                        m.Chunk.status == _PENDING,
                    )
                    .values(
                        status="PROCESSING",
                        next_attempt_at=None,
                        last_run_id=run_id,
                        claimed_at=now,
                        updated_at=now,
                    )
                )
                updated += _rowcount(result)
            if updated != len(ids):
                raise _Abort(
                    AppError(
                        code=ErrorCode.INTERNAL,
                        message=f"claim tripwire: selected {len(ids)} PENDING ids but updated "
                        f"{updated}; the transaction setup is broken",
                        scope=ErrorScope.JOB_FATAL,
                    )
                )
            claimed: List[Chunk] = []
            for batch in _batched(ids):
                rows = session.execute(
                    select(m.Chunk).where(m.Chunk.id.in_(batch)).order_by(m.Chunk.id)
                ).scalars()
                claimed.extend(_to_chunk(row) for row in rows)
            return Ok(claimed)

    @boundary
    def complete(self, job_id: str, chunk_id: int, result: TranslationResult) -> Result[bool]:
        now = utcnow()
        with self._db.write_session() as session:
            outcome = session.connection().execute(
                update(m.Chunk)
                .where(
                    m.Chunk.job_id == job_id,
                    m.Chunk.id == chunk_id,
                    m.Chunk.status == _PROCESSING,
                )
                .values(
                    status="COMPLETED",
                    translated_text=result.translated_text,
                    provider=result.provider,
                    glossary_strategy=result.glossary_strategy.value,
                    glossary_hash=self.glossary_hash,
                    chars_sent=result.chars_sent,
                    chars_billed=result.chars_billed,
                    latency_ms=result.latency_ms,
                    attempts=result.attempts,
                    review_flag=result.review_flag,
                    warnings=tuple(result.warnings),
                    last_error=None,
                    last_error_code=None,
                    next_attempt_at=None,
                    claimed_at=None,
                    completed_at=now,
                    updated_at=now,
                )
            )
            return Ok(_rowcount(outcome) == 1)

    @boundary
    def reschedule(
        self, job_id: str, chunk_id: int, error: AppError, next_attempt_at: datetime
    ) -> Result[bool]:
        """PROCESSING -> PENDING with backoff (D3: ``False`` when the precondition failed)."""
        with self._db.write_session() as session:
            outcome = session.connection().execute(
                update(m.Chunk)
                .where(
                    m.Chunk.job_id == job_id,
                    m.Chunk.id == chunk_id,
                    m.Chunk.status == _PROCESSING,
                )
                .values(
                    status="PENDING",
                    retry_count=m.Chunk.retry_count + 1,
                    next_attempt_at=next_attempt_at,
                    last_error_code=error.code.value,
                    last_error=truncate_error(error.message),
                    claimed_at=None,
                    updated_at=utcnow(),
                )
            )
            return Ok(_rowcount(outcome) == 1)

    @boundary
    def fail(self, job_id: str, chunk_id: int, error: AppError) -> Result[bool]:
        """PROCESSING -> FAILED (D3: ``False`` when the precondition failed)."""
        with self._db.write_session() as session:
            outcome = session.connection().execute(
                update(m.Chunk)
                .where(
                    m.Chunk.job_id == job_id,
                    m.Chunk.id == chunk_id,
                    m.Chunk.status == _PROCESSING,
                )
                .values(
                    status="FAILED",
                    last_error_code=error.code.value,
                    last_error=truncate_error(error.message),
                    next_attempt_at=None,
                    claimed_at=None,
                    updated_at=utcnow(),
                )
            )
            return Ok(_rowcount(outcome) == 1)

    @boundary
    def release(self, job_id: str, chunk_ids: Sequence[int]) -> Result[int]:
        ids = list(chunk_ids)
        if not ids:
            return Ok(0)
        now = utcnow()
        released = 0
        with self._db.write_session() as session:
            for batch in _batched(ids):
                result = session.connection().execute(
                    update(m.Chunk)
                    .where(
                        m.Chunk.job_id == job_id,
                        m.Chunk.id.in_(batch),
                        m.Chunk.status == _PROCESSING,
                    )
                    .values(status="PENDING", claimed_at=None, updated_at=now)
                )
                released += _rowcount(result)
        return Ok(released)

    @boundary
    def reset_failed(self, job_id: str) -> Result[int]:
        with self._db.write_session() as session:
            result = session.connection().execute(
                update(m.Chunk)
                .where(m.Chunk.job_id == job_id, m.Chunk.status == _FAILED)
                .values(
                    status="PENDING",
                    retry_count=0,
                    next_attempt_at=None,
                    last_error=None,
                    last_error_code=None,
                    updated_at=utcnow(),
                )
            )
            return Ok(_rowcount(result))

    # -- reads ------------------------------------------------------------- #

    @boundary
    def counts(self, job_id: str, now: Optional[datetime] = None) -> Result[StatusCounts]:
        moment = now if now is not None else utcnow()
        with self._db.read_session() as session:
            per_status: Dict[str, int] = {
                str(status): int(count)
                for status, count in session.execute(
                    select(m.Chunk.status, func.count())
                    .where(m.Chunk.job_id == job_id)
                    .group_by(m.Chunk.status)
                ).all()
            }
            waiting = session.execute(
                select(func.count())
                .select_from(m.Chunk)
                .where(
                    m.Chunk.job_id == job_id,
                    m.Chunk.status == _PENDING,
                    m.Chunk.next_attempt_at.is_not(None),
                    m.Chunk.next_attempt_at > moment,
                )
            ).scalar_one()
        return Ok(
            StatusCounts(
                pending=per_status.get("PENDING", 0),
                processing=per_status.get("PROCESSING", 0),
                completed=per_status.get("COMPLETED", 0),
                failed=per_status.get("FAILED", 0),
                total=sum(per_status.values()),
                waiting_backoff=int(waiting),
            )
        )

    @boundary
    def earliest_next_attempt(self, job_id: str) -> Result[Optional[datetime]]:
        with self._db.read_session() as session:
            value = session.execute(
                select(func.min(m.Chunk.next_attempt_at)).where(
                    m.Chunk.job_id == job_id,
                    m.Chunk.status == _PENDING,
                    m.Chunk.next_attempt_at.is_not(None),
                )
            ).scalar_one_or_none()
            return Ok(value)

    def iter_ordered(self, job_id: str, batch_size: int = 200) -> Iterator[Chunk]:
        """Stream every chunk in id order with keyset pagination inside one read transaction.

        D4: a plain iterator; database errors propagate to the caller's ``Result``
        boundary (the assembler). At most ``batch_size`` rows are alive at a time.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        with self._db.read_session() as session:
            last_id = 0
            while True:
                rows = (
                    session.execute(
                        select(m.Chunk)
                        .where(m.Chunk.job_id == job_id, m.Chunk.id > last_id)
                        .order_by(m.Chunk.id)
                        .limit(batch_size)
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    yield _to_chunk(row)
                if len(rows) < batch_size:
                    return
                last_id = rows[-1].id

    @boundary
    def totals(self, job_id: str) -> Result[ChunkTotals]:
        with self._db.read_session() as session:
            row = session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(m.Chunk.chars_sent), 0),
                    func.coalesce(func.sum(m.Chunk.chars_billed), 0),
                    func.coalesce(
                        func.sum(case((m.Chunk.status == _COMPLETED, m.Chunk.char_count), else_=0)),
                        0,
                    ),
                    func.coalesce(func.sum(case((m.Chunk.translatable == _ONE, 1), else_=0)), 0),
                    func.coalesce(func.sum(m.Chunk.char_count), 0),
                ).where(m.Chunk.job_id == job_id)
            ).one()
        return Ok(
            ChunkTotals(
                total_chunks=int(row[0]),
                chars_sent=int(row[1]),
                chars_billed=int(row[2]),
                completed_chars=int(row[3]),
                translatable_count=int(row[4]),
                total_chars=int(row[5]),
            )
        )

    @boundary
    def failed_ids(self, job_id: str) -> Result[List[int]]:
        """A2: ids of FAILED chunks in id order."""
        with self._db.read_session() as session:
            ids = session.execute(
                select(m.Chunk.id)
                .where(m.Chunk.job_id == job_id, m.Chunk.status == _FAILED)
                .order_by(m.Chunk.id)
            ).scalars()
            return Ok([int(i) for i in ids])

    @boundary
    def review_ids(self, job_id: str) -> Result[List[int]]:
        """A2: ids of chunks flagged for review, in id order."""
        with self._db.read_session() as session:
            ids = session.execute(
                select(m.Chunk.id)
                .where(m.Chunk.job_id == job_id, m.Chunk.review_flag == _ONE)
                .order_by(m.Chunk.id)
            ).scalars()
            return Ok([int(i) for i in ids])

    @boundary
    def count_completed_with_other_glossary(self, job_id: str, glossary_hash: str) -> Result[int]:
        """A3: COMPLETED translatable chunks translated under a different (or no) glossary."""
        with self._db.read_session() as session:
            count = session.execute(
                select(func.count())
                .select_from(m.Chunk)
                .where(
                    m.Chunk.job_id == job_id,
                    m.Chunk.status == _COMPLETED,
                    m.Chunk.translatable == _ONE,
                    or_(m.Chunk.glossary_hash.is_(None), m.Chunk.glossary_hash != glossary_hash),
                )
            ).scalar_one()
            return Ok(int(count))

    @boundary
    def count_completed_by_run(self, run_id: str) -> Result[Tuple[int, int]]:
        """A4: ``(completed, failed)`` counts for a run (counter recovery after a crash)."""
        with self._db.read_session() as session:
            row = session.execute(
                select(
                    func.coalesce(func.sum(case((m.Chunk.status == _COMPLETED, 1), else_=0)), 0),
                    func.coalesce(func.sum(case((m.Chunk.status == _FAILED, 1), else_=0)), 0),
                ).where(m.Chunk.last_run_id == run_id)
            ).one()
            return Ok((int(row[0]), int(row[1])))


# --------------------------------------------------------------------------- #
# 5.4 LeaseRepository
# --------------------------------------------------------------------------- #


class LeaseRepository:
    """Single-process lock row (E-20)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @boundary
    def acquire(self, job_id: str, holder: LeaseHolder, stale_after_s: int) -> Result[bool]:
        """Take the lease when free, stale, or already held by the same pid/host.

        Must run under ``BEGIN IMMEDIATE`` (two deferred readers could both see "no row").
        """
        now = utcnow()
        cutoff = now - timedelta(seconds=stale_after_s)
        with self._db.write_session() as session:
            row = session.execute(
                select(
                    m.Lease.holder_pid, m.Lease.holder_host, m.Lease.run_id, m.Lease.heartbeat_at
                ).where(m.Lease.job_id == job_id)
            ).first()
            if row is None:
                session.connection().execute(
                    insert(m.Lease).values(
                        job_id=job_id,
                        holder_pid=holder.pid,
                        holder_host=holder.host,
                        run_id=holder.run_id,
                        acquired_at=now,
                        heartbeat_at=now,
                    )
                )
                return Ok(True)
            holder_pid, holder_host, _run_id, heartbeat_at = row
            same_holder = holder_pid == holder.pid and holder_host == holder.host
            if heartbeat_at <= cutoff or same_holder:
                session.connection().execute(
                    update(m.Lease)
                    .where(m.Lease.job_id == job_id)
                    .values(
                        holder_pid=holder.pid,
                        holder_host=holder.host,
                        run_id=holder.run_id,
                        acquired_at=now,
                        heartbeat_at=now,
                    )
                )
                return Ok(True)
            session.rollback()
            return Ok(False)

    @boundary
    def get(self, job_id: str) -> Result[Optional[LeaseRecord]]:
        """A5: the current lease row, ``None`` when free."""
        with self._db.read_session() as session:
            row = session.execute(
                select(m.Lease).where(m.Lease.job_id == job_id)
            ).scalar_one_or_none()
            return Ok(None if row is None else _to_lease(row))

    @boundary
    def heartbeat(self, job_id: str, holder: LeaseHolder) -> Result[None]:
        with self._db.write_session() as session:
            result = session.connection().execute(
                update(m.Lease)
                .where(
                    m.Lease.job_id == job_id,
                    m.Lease.holder_pid == holder.pid,
                    m.Lease.holder_host == holder.host,
                    m.Lease.run_id == holder.run_id,
                )
                .values(heartbeat_at=utcnow())
            )
            if _rowcount(result) == 0:
                raise _Abort(
                    AppError(
                        code=ErrorCode.STATE_LOCKED,
                        message="lease lost: it was broken or taken over by another process",
                        scope=ErrorScope.JOB_FATAL,
                    )
                )
        return Ok(None)

    @boundary
    def release(self, job_id: str, holder: LeaseHolder) -> Result[None]:
        with self._db.write_session() as session:
            result = session.connection().execute(
                delete(m.Lease).where(
                    m.Lease.job_id == job_id,
                    m.Lease.holder_pid == holder.pid,
                    m.Lease.holder_host == holder.host,
                    m.Lease.run_id == holder.run_id,
                )
            )
            if _rowcount(result) == 0:
                log.warning(
                    "lease release: no lease held by pid=%s host=%s run=%s "
                    "(already broken or taken)",
                    holder.pid,
                    holder.host,
                    holder.run_id,
                )
        return Ok(None)

    @boundary
    def force_break(self, job_id: str) -> Result[None]:
        with self._db.write_session() as session:
            session.connection().execute(delete(m.Lease).where(m.Lease.job_id == job_id))
        return Ok(None)


# --------------------------------------------------------------------------- #
# 9.1 OverlayBlockRepository (DB design doc 07): UnitRepository[OverlayBlock] + page helpers
# --------------------------------------------------------------------------- #


def _to_block(row: m.OverlayBlock) -> OverlayBlock:
    prefix: Optional[Tuple[str, bool, bool]] = None
    if row.prefix_text is not None:
        prefix = (row.prefix_text, bool(row.prefix_bold), bool(row.prefix_italic))
    return OverlayBlock(
        unit_id=row.id,
        block_id=format_block_id(row.page, row.index_on_page),
        page=row.page,
        index_on_page=row.index_on_page,
        kind=OverlayBlockKind(row.kind),
        bbox=(row.x0, row.y0, row.x1, row.y1),
        line_boxes=tuple(row.line_boxes),
        style=OverlayStyle(
            font_size=row.font_size,
            bold=bool(row.bold),
            italic=bool(row.italic),
            family=cast(Any, row.family),
            color=row.color,
            prefix_style=prefix,
        ),
        alignment=OverlayAlignment(row.alignment),
        source_text=row.source_text,
        content_hash=row.content_hash,
        char_count=row.char_count,
        translate=bool(row.translate),
        keep_reason=row.keep_reason,
        fragment=bool(row.fragment),
        over_image=bool(row.over_image),
        status=ChunkStatus(row.status),
        retry_count=row.retry_count,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        translated_text=row.translated_text,
        review_flag=bool(row.review_flag),
        warnings=tuple(row.warnings),
    )


def _to_page(row: m.OverlayPage) -> OverlayPage:
    return OverlayPage(
        page=row.page,
        width=row.width,
        height=row.height,
        rotation=row.rotation,
        has_text_layer=bool(row.has_text_layer),
        block_count=row.block_count,
        translatable_count=row.translatable_count,
        skip_reason=row.skip_reason,
        body_size=row.body_size,
    )


def overlay_claim_select(
    job_id: str, now: datetime, max_units: int
) -> Select[Tuple[int, int, int, bool]]:
    """Step 1 of ``claim_pending_batch`` (doc 07, section 6.3; exposed so tests can EXPLAIN it)."""
    return (
        select(
            m.OverlayBlock.id,
            m.OverlayBlock.page,
            m.OverlayBlock.char_count,
            m.OverlayBlock.translate,
        )
        .where(
            m.OverlayBlock.job_id == job_id,
            m.OverlayBlock.status == _PENDING,
            or_(m.OverlayBlock.next_attempt_at.is_(None), m.OverlayBlock.next_attempt_at <= now),
        )
        .order_by(m.OverlayBlock.id)
        .limit(max_units)
    )


def page_progress_select(job_id: str) -> Select[Tuple[int]]:
    """Completed-page count of ``page_progress`` (doc 07, section 6.4; exposed for EXPLAIN).

    The correlated per-page COMPLETED count runs on the covering index
    ``ix_overlay_blocks_job_page_status`` (V2).
    """
    completed_on_page = (
        select(func.count())
        .select_from(m.OverlayBlock)
        .where(
            m.OverlayBlock.job_id == job_id,
            m.OverlayBlock.page == m.OverlayPage.page,
            m.OverlayBlock.status == _COMPLETED,
        )
        .scalar_subquery()
    )
    return (
        select(func.count())
        .select_from(m.OverlayPage)
        .where(
            m.OverlayPage.job_id == job_id,
            m.OverlayPage.translatable_count > 0,
            m.OverlayPage.block_count == completed_on_page,
        )
    )


def select_batch(
    rows: Sequence[Tuple[int, int, int, bool]], min_chars: int, page_rule: bool
) -> List[int]:
    """The stop rule of doc 07 section 6.3 over the eligible rows ``(id, page, chars, translate)``.

    Takes rows in order; with ``page_rule`` the batch ends at the first page boundary
    reached with at least ``min_chars`` translatable characters. Kept units
    (``translate`` false) are taken like any other and count 0 chars. Without the page
    rule (``claim_pending``, "plain mode") every row is taken.
    """
    ids: List[int] = []
    chars = 0
    previous_page: Optional[int] = None
    for unit_id, page, char_count, translate in rows:
        if page_rule and previous_page is not None and page != previous_page and chars >= min_chars:
            break
        ids.append(int(unit_id))
        if translate:
            chars += int(char_count)
        previous_page = int(page)
    return ids


def _integrity(message: str) -> AppError:
    return AppError(code=ErrorCode.CHUNK_INTEGRITY, message=message, scope=ErrorScope.JOB_FATAL)


def _validate_blocks(
    units: Sequence[OverlayBlock], pages: Sequence[OverlayPage]
) -> Optional[AppError]:
    """Python pre-checks for ``OverlayBlockRepository.replace_all`` (doc 07, section 9.1)."""
    if units and not pages:
        return _integrity("overlay units given without pages (a block must sit on a page)")
    for position, page in enumerate(pages, start=1):
        if page.page != position:
            return _integrity(
                f"overlay pages must be dense 1..m in order (position {position} has page "
                f"{page.page})"
            )
    by_page: Dict[int, OverlayPage] = {p.page: p for p in pages}
    blocks_on_page: Dict[int, int] = {p.page: 0 for p in pages}
    translatable_on_page: Dict[int, int] = {p.page: 0 for p in pages}
    previous_page = 0
    previous_index = 0
    for index, unit in enumerate(units, start=1):
        if unit.unit_id != index:
            return _integrity(
                f"unit ids must be dense 1..n in order (position {index} has id {unit.unit_id})"
            )
        if unit.page not in by_page:
            return _integrity(f"unit {index}: page {unit.page} is not in the given pages")
        if unit.page < previous_page:
            return _integrity(f"unit {index}: page {unit.page} is out of order")
        expected_index = previous_index + 1 if unit.page == previous_page else 1
        if unit.index_on_page != expected_index:
            return _integrity(
                f"unit {index}: index_on_page {unit.index_on_page} on page {unit.page} "
                f"(expected {expected_index}, dense per page)"
            )
        if unit.block_id != format_block_id(unit.page, unit.index_on_page):
            return _integrity(
                f"unit {index}: block_id {unit.block_id!r} != "
                f"{format_block_id(unit.page, unit.index_on_page)!r}"
            )
        if unit.char_count != len(unit.source_text):
            return _integrity(
                f"unit {index}: char_count {unit.char_count} != len(source_text) "
                f"{len(unit.source_text)}"
            )
        if unit.content_hash != content_hash_of(unit.source_text):
            return _integrity(f"unit {index}: content_hash does not match source_text")
        if unit.translate != (unit.keep_reason is None):
            return _integrity(f"unit {index}: translate must equal (keep_reason is None)")
        if unit.kind is OverlayBlockKind.PAGE_NUMBER and unit.translate:
            return _integrity(f"unit {index}: page_number units are never translated")
        if by_page[unit.page].skip_reason is not None:
            return _integrity(f"unit {index}: page {unit.page} is skipped but carries units")
        blocks_on_page[unit.page] += 1
        if unit.translate:
            translatable_on_page[unit.page] += 1
        previous_page, previous_index = unit.page, unit.index_on_page
    for page in pages:
        if page.block_count != blocks_on_page[page.page]:
            return _integrity(
                f"page {page.page}: block_count {page.block_count} != {blocks_on_page[page.page]} "
                "units on the page"
            )
        if page.translatable_count != translatable_on_page[page.page]:
            return _integrity(
                f"page {page.page}: translatable_count {page.translatable_count} != "
                f"{translatable_on_page[page.page]} translatable units on the page"
            )
    return None


def _block_row(job_id: str, unit: OverlayBlock, now: datetime) -> Dict[str, Any]:
    prefix = unit.style.prefix_style
    return {
        "id": unit.unit_id,
        "job_id": job_id,
        "page": unit.page,
        "index_on_page": unit.index_on_page,
        "kind": unit.kind.value,
        "translate": unit.translate,
        "keep_reason": unit.keep_reason,
        "fragment": unit.fragment,
        "over_image": unit.over_image,
        "x0": unit.bbox[0],
        "y0": unit.bbox[1],
        "x1": unit.bbox[2],
        "y1": unit.bbox[3],
        "line_boxes": tuple(unit.line_boxes),
        "font_size": unit.style.font_size,
        "bold": unit.style.bold,
        "italic": unit.style.italic,
        "family": unit.style.family,
        "color": unit.style.color,
        "prefix_text": None if prefix is None else prefix[0],
        "prefix_bold": None if prefix is None else int(prefix[1]),
        "prefix_italic": None if prefix is None else int(prefix[2]),
        "alignment": unit.alignment.value,
        "source_text": unit.source_text,
        "content_hash": unit.content_hash,
        "char_count": unit.char_count,
        "status": "PENDING",
        "retry_count": 0,
        "review_flag": False,
        "warnings": tuple(unit.warnings),
        "chars_sent": 0,
        "chars_billed": 0,
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }


def _page_row(job_id: str, page: OverlayPage, now: datetime) -> Dict[str, Any]:
    return {
        "page": page.page,
        "job_id": job_id,
        "width": page.width,
        "height": page.height,
        "rotation": page.rotation,
        "has_text_layer": page.has_text_layer,
        "skip_reason": page.skip_reason,
        "block_count": page.block_count,
        "translatable_count": page.translatable_count,
        "body_size": page.body_size,
        "created_at": now,
    }


class OverlayBlockRepository:
    """Overlay unit rows, the (chunk-identical) unit state machine and the page helpers.

    Structurally a ``UnitRepository[OverlayBlock]`` (doc 06, section 3.6.2): same method
    names and signatures as :class:`ChunkRepository`, plus ``claim_pending_batch``,
    ``page_progress``, ``page_texts``, ``iter_page`` and ``pages``. Bound per run like
    ``ChunkRepository`` (D5).
    """

    def __init__(self, db: Database, run_id: str, glossary_hash: Optional[str]) -> None:
        self._db = db
        self.run_id = run_id
        self.glossary_hash = glossary_hash

    # -- extraction stage -------------------------------------------------- #

    @boundary
    def replace_all(
        self, job_id: str, units: Iterable[OverlayBlock], pages: Iterable[OverlayPage] = ()
    ) -> Result[int]:
        """Replace pages and blocks of the job in one transaction (doc 07, section 9.1).

        Refused (``INTERNAL``) when a COMPLETED unit exists; every Python pre-check fails
        with ``CHUNK_INTEGRITY`` before any SQL is issued.
        """
        items = list(units)
        page_items = list(pages)
        problem = _validate_blocks(items, page_items)
        if problem is not None:
            return Err(problem)
        now = utcnow()
        page_rows = [_page_row(job_id, p, now) for p in page_items]
        block_rows = [_block_row(job_id, u, now) for u in items]
        with self._db.write_session() as session:
            completed = session.execute(
                select(func.count())
                .select_from(m.OverlayBlock)
                .where(m.OverlayBlock.job_id == job_id, m.OverlayBlock.status == _COMPLETED)
            ).scalar_one()
            if completed:
                raise _Abort(
                    AppError(
                        code=ErrorCode.INTERNAL,
                        message=f"replace_all refused: {completed} COMPLETED overlay units present",
                        scope=ErrorScope.JOB_FATAL,
                    )
                )
            conn = session.connection()
            conn.execute(delete(m.OverlayBlock).where(m.OverlayBlock.job_id == job_id))
            conn.execute(delete(m.OverlayPage).where(m.OverlayPage.job_id == job_id))
            for start in range(0, len(page_rows), IN_BATCH_SIZE):
                conn.execute(insert(m.OverlayPage), page_rows[start : start + IN_BATCH_SIZE])
            for start in range(0, len(block_rows), IN_BATCH_SIZE):
                conn.execute(insert(m.OverlayBlock), block_rows[start : start + IN_BATCH_SIZE])
        return Ok(len(items))

    # -- transitions ------------------------------------------------------- #

    @boundary
    def requeue_processing(self, job_id: str) -> Result[int]:
        with self._db.write_session() as session:
            result = session.connection().execute(
                update(m.OverlayBlock)
                .where(m.OverlayBlock.job_id == job_id, m.OverlayBlock.status == _PROCESSING)
                .values(status="PENDING", claimed_at=None, updated_at=utcnow())
            )
            return Ok(_rowcount(result))

    @boundary
    def claim_pending(
        self, job_id: str, limit: int, now: datetime, run_id: str
    ) -> Result[List[OverlayBlock]]:
        """Protocol claim: the batch claim in plain mode (no page rule, ``min_chars = 0``)."""
        return self._claim(job_id, limit, 0, now, run_id, page_rule=False)

    @boundary
    def claim_pending_batch(
        self, job_id: str, max_units: int, min_chars: int, now: datetime, run_id: str
    ) -> Result[List[OverlayBlock]]:
        """PENDING -> PROCESSING for the next batch (doc 07, section 6.3).

        Eligible units in id order until a page boundary is reached with at least
        ``min_chars`` translatable characters, or ``max_units`` is hit. Atomic under
        ``BEGIN IMMEDIATE``; the ``status = 'PENDING'`` re-check plus ``rowcount`` is the
        tripwire against a double claim.
        """
        return self._claim(job_id, max_units, min_chars, now, run_id, page_rule=True)

    def _claim(
        self,
        job_id: str,
        max_units: int,
        min_chars: int,
        now: datetime,
        run_id: str,
        *,
        page_rule: bool,
    ) -> Result[List[OverlayBlock]]:
        if max_units <= 0:
            return Ok([])
        with self._db.write_session() as session:
            rows = [
                (int(r[0]), int(r[1]), int(r[2]), bool(r[3]))
                for r in session.execute(overlay_claim_select(job_id, now, max_units)).all()
            ]
            ids = select_batch(rows, min_chars, page_rule)
            if not ids:
                return Ok([])
            updated = 0
            for batch in _batched(ids):
                result = session.connection().execute(
                    update(m.OverlayBlock)
                    .where(
                        m.OverlayBlock.job_id == job_id,
                        m.OverlayBlock.id.in_(batch),
                        m.OverlayBlock.status == _PENDING,
                    )
                    .values(
                        status="PROCESSING",
                        next_attempt_at=None,
                        last_run_id=run_id,
                        claimed_at=now,
                        updated_at=now,
                    )
                )
                updated += _rowcount(result)
            if updated != len(ids):
                raise _Abort(
                    AppError(
                        code=ErrorCode.INTERNAL,
                        message=f"claim tripwire: selected {len(ids)} PENDING ids but updated "
                        f"{updated}; the transaction setup is broken",
                        scope=ErrorScope.JOB_FATAL,
                    )
                )
            claimed: List[OverlayBlock] = []
            for batch in _batched(ids):
                fetched = session.execute(
                    select(m.OverlayBlock)
                    .where(m.OverlayBlock.id.in_(batch))
                    .order_by(m.OverlayBlock.id)
                ).scalars()
                claimed.extend(_to_block(row) for row in fetched)
            return Ok(claimed)

    @boundary
    def complete(self, job_id: str, unit_id: int, result: TranslationResult) -> Result[bool]:
        """PROCESSING -> COMPLETED (``result.chunk_id`` is ignored; ``unit_id`` names the row)."""
        now = utcnow()
        with self._db.write_session() as session:
            outcome = session.connection().execute(
                update(m.OverlayBlock)
                .where(
                    m.OverlayBlock.job_id == job_id,
                    m.OverlayBlock.id == unit_id,
                    m.OverlayBlock.status == _PROCESSING,
                )
                .values(
                    status="COMPLETED",
                    translated_text=result.translated_text,
                    provider=result.provider,
                    glossary_strategy=result.glossary_strategy.value,
                    glossary_hash=self.glossary_hash,
                    chars_sent=result.chars_sent,
                    chars_billed=result.chars_billed,
                    latency_ms=result.latency_ms,
                    attempts=result.attempts,
                    review_flag=result.review_flag,
                    warnings=tuple(result.warnings),
                    last_error=None,
                    last_error_code=None,
                    next_attempt_at=None,
                    claimed_at=None,
                    completed_at=now,
                    updated_at=now,
                )
            )
            return Ok(_rowcount(outcome) == 1)

    @boundary
    def reschedule(
        self, job_id: str, unit_id: int, error: AppError, next_attempt_at: datetime
    ) -> Result[bool]:
        """PROCESSING -> PENDING with backoff (D3: ``False`` when the precondition failed)."""
        with self._db.write_session() as session:
            outcome = session.connection().execute(
                update(m.OverlayBlock)
                .where(
                    m.OverlayBlock.job_id == job_id,
                    m.OverlayBlock.id == unit_id,
                    m.OverlayBlock.status == _PROCESSING,
                )
                .values(
                    status="PENDING",
                    retry_count=m.OverlayBlock.retry_count + 1,
                    next_attempt_at=next_attempt_at,
                    last_error_code=error.code.value,
                    last_error=truncate_error(error.message),
                    claimed_at=None,
                    updated_at=utcnow(),
                )
            )
            return Ok(_rowcount(outcome) == 1)

    @boundary
    def fail(self, job_id: str, unit_id: int, error: AppError) -> Result[bool]:
        """PROCESSING -> FAILED (D3: ``False`` when the precondition failed)."""
        with self._db.write_session() as session:
            outcome = session.connection().execute(
                update(m.OverlayBlock)
                .where(
                    m.OverlayBlock.job_id == job_id,
                    m.OverlayBlock.id == unit_id,
                    m.OverlayBlock.status == _PROCESSING,
                )
                .values(
                    status="FAILED",
                    last_error_code=error.code.value,
                    last_error=truncate_error(error.message),
                    next_attempt_at=None,
                    claimed_at=None,
                    updated_at=utcnow(),
                )
            )
            return Ok(_rowcount(outcome) == 1)

    @boundary
    def release(self, job_id: str, unit_ids: Sequence[int]) -> Result[int]:
        ids = list(unit_ids)
        if not ids:
            return Ok(0)
        now = utcnow()
        released = 0
        with self._db.write_session() as session:
            for batch in _batched(ids):
                result = session.connection().execute(
                    update(m.OverlayBlock)
                    .where(
                        m.OverlayBlock.job_id == job_id,
                        m.OverlayBlock.id.in_(batch),
                        m.OverlayBlock.status == _PROCESSING,
                    )
                    .values(status="PENDING", claimed_at=None, updated_at=now)
                )
                released += _rowcount(result)
        return Ok(released)

    @boundary
    def reset_failed(self, job_id: str) -> Result[int]:
        with self._db.write_session() as session:
            result = session.connection().execute(
                update(m.OverlayBlock)
                .where(m.OverlayBlock.job_id == job_id, m.OverlayBlock.status == _FAILED)
                .values(
                    status="PENDING",
                    retry_count=0,
                    next_attempt_at=None,
                    last_error=None,
                    last_error_code=None,
                    updated_at=utcnow(),
                )
            )
            return Ok(_rowcount(result))

    # -- reads (unit state) ------------------------------------------------ #

    @boundary
    def counts(self, job_id: str, now: Optional[datetime] = None) -> Result[StatusCounts]:
        moment = now if now is not None else utcnow()
        with self._db.read_session() as session:
            per_status: Dict[str, int] = {
                str(status): int(count)
                for status, count in session.execute(
                    select(m.OverlayBlock.status, func.count())
                    .where(m.OverlayBlock.job_id == job_id)
                    .group_by(m.OverlayBlock.status)
                ).all()
            }
            waiting = session.execute(
                select(func.count())
                .select_from(m.OverlayBlock)
                .where(
                    m.OverlayBlock.job_id == job_id,
                    m.OverlayBlock.status == _PENDING,
                    m.OverlayBlock.next_attempt_at.is_not(None),
                    m.OverlayBlock.next_attempt_at > moment,
                )
            ).scalar_one()
        return Ok(
            StatusCounts(
                pending=per_status.get("PENDING", 0),
                processing=per_status.get("PROCESSING", 0),
                completed=per_status.get("COMPLETED", 0),
                failed=per_status.get("FAILED", 0),
                total=sum(per_status.values()),
                waiting_backoff=int(waiting),
            )
        )

    @boundary
    def earliest_next_attempt(self, job_id: str) -> Result[Optional[datetime]]:
        with self._db.read_session() as session:
            value = session.execute(
                select(func.min(m.OverlayBlock.next_attempt_at)).where(
                    m.OverlayBlock.job_id == job_id,
                    m.OverlayBlock.status == _PENDING,
                    m.OverlayBlock.next_attempt_at.is_not(None),
                )
            ).scalar_one_or_none()
            return Ok(value)

    def iter_ordered(self, job_id: str, batch_size: int = 200) -> Iterator[OverlayBlock]:
        """Stream every unit in id order with keyset pagination inside one read transaction (D4)."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        with self._db.read_session() as session:
            last_id = 0
            while True:
                rows = (
                    session.execute(
                        select(m.OverlayBlock)
                        .where(m.OverlayBlock.job_id == job_id, m.OverlayBlock.id > last_id)
                        .order_by(m.OverlayBlock.id)
                        .limit(batch_size)
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    yield _to_block(row)
                if len(rows) < batch_size:
                    return
                last_id = rows[-1].id

    @boundary
    def totals(self, job_id: str) -> Result[ChunkTotals]:
        with self._db.read_session() as session:
            row = session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(m.OverlayBlock.chars_sent), 0),
                    func.coalesce(func.sum(m.OverlayBlock.chars_billed), 0),
                    func.coalesce(
                        func.sum(
                            case(
                                (m.OverlayBlock.status == _COMPLETED, m.OverlayBlock.char_count),
                                else_=0,
                            )
                        ),
                        0,
                    ),
                    func.coalesce(
                        func.sum(case((m.OverlayBlock.translate == _ONE, 1), else_=0)), 0
                    ),
                    func.coalesce(func.sum(m.OverlayBlock.char_count), 0),
                ).where(m.OverlayBlock.job_id == job_id)
            ).one()
        return Ok(
            ChunkTotals(
                total_chunks=int(row[0]),
                chars_sent=int(row[1]),
                chars_billed=int(row[2]),
                completed_chars=int(row[3]),
                translatable_count=int(row[4]),
                total_chars=int(row[5]),
            )
        )

    @boundary
    def failed_ids(self, job_id: str) -> Result[List[int]]:
        with self._db.read_session() as session:
            ids = session.execute(
                select(m.OverlayBlock.id)
                .where(m.OverlayBlock.job_id == job_id, m.OverlayBlock.status == _FAILED)
                .order_by(m.OverlayBlock.id)
            ).scalars()
            return Ok([int(i) for i in ids])

    @boundary
    def review_ids(self, job_id: str) -> Result[List[int]]:
        with self._db.read_session() as session:
            ids = session.execute(
                select(m.OverlayBlock.id)
                .where(m.OverlayBlock.job_id == job_id, m.OverlayBlock.review_flag == _ONE)
                .order_by(m.OverlayBlock.id)
            ).scalars()
            return Ok([int(i) for i in ids])

    @boundary
    def count_completed_with_other_glossary(self, job_id: str, glossary_hash: str) -> Result[int]:
        with self._db.read_session() as session:
            count = session.execute(
                select(func.count())
                .select_from(m.OverlayBlock)
                .where(
                    m.OverlayBlock.job_id == job_id,
                    m.OverlayBlock.status == _COMPLETED,
                    m.OverlayBlock.translate == _ONE,
                    or_(
                        m.OverlayBlock.glossary_hash.is_(None),
                        m.OverlayBlock.glossary_hash != glossary_hash,
                    ),
                )
            ).scalar_one()
            return Ok(int(count))

    @boundary
    def count_completed_by_run(self, run_id: str) -> Result[Tuple[int, int]]:
        """``(completed, failed)`` counts for an overlay run (``runs.mode = 'overlay'``)."""
        with self._db.read_session() as session:
            row = session.execute(
                select(
                    func.coalesce(
                        func.sum(case((m.OverlayBlock.status == _COMPLETED, 1), else_=0)), 0
                    ),
                    func.coalesce(
                        func.sum(case((m.OverlayBlock.status == _FAILED, 1), else_=0)), 0
                    ),
                ).where(m.OverlayBlock.last_run_id == run_id)
            ).one()
            return Ok((int(row[0]), int(row[1])))

    # -- reads (pages, doc 07 section 6.4) --------------------------------- #

    @boundary
    def page_progress(self, job_id: str) -> Result[Tuple[int, int]]:
        """``(pages fully completed, pages with translatable units)``.

        A page counts as completed only when every unit on it (kept ones included) is
        COMPLETED; pages with ``translatable_count = 0`` are outside both numbers.
        """
        with self._db.read_session() as session:
            completed = session.execute(page_progress_select(job_id)).scalar_one()
            with_translatable = session.execute(
                select(func.count())
                .select_from(m.OverlayPage)
                .where(m.OverlayPage.job_id == job_id, m.OverlayPage.translatable_count > 0)
            ).scalar_one()
            return Ok((int(completed), int(with_translatable)))

    @boundary
    def page_texts(self, job_id: str, pages: Sequence[int]) -> Result[Dict[int, List[str]]]:
        """Source texts of the translatable units of ``pages`` in id order (context window).

        Pages without translatable units map to ``[]``.
        """
        wanted = [int(p) for p in pages]
        texts: Dict[int, List[str]] = {p: [] for p in wanted}
        if not wanted:
            return Ok(texts)
        with self._db.read_session() as session:
            for batch in _batched(wanted):
                rows = session.execute(
                    select(m.OverlayBlock.page, m.OverlayBlock.source_text)
                    .where(
                        m.OverlayBlock.job_id == job_id,
                        m.OverlayBlock.page.in_(batch),
                        m.OverlayBlock.translate == _ONE,
                    )
                    .order_by(m.OverlayBlock.id)
                ).all()
                for page, text in rows:
                    texts[int(page)].append(str(text))
        return Ok(texts)

    def iter_page(self, job_id: str, page: int) -> Iterator[OverlayBlock]:
        """Every unit of one page in id order, inside one read transaction (D4 semantics)."""
        with self._db.read_session() as session:
            rows = (
                session.execute(
                    select(m.OverlayBlock)
                    .where(m.OverlayBlock.job_id == job_id, m.OverlayBlock.page == page)
                    .order_by(m.OverlayBlock.id)
                )
                .scalars()
                .all()
            )
            for row in rows:
                yield _to_block(row)

    @boundary
    def pages(self, job_id: str) -> Result[List[OverlayPage]]:
        with self._db.read_session() as session:
            rows = (
                session.execute(
                    select(m.OverlayPage)
                    .where(m.OverlayPage.job_id == job_id)
                    .order_by(m.OverlayPage.page)
                )
                .scalars()
                .all()
            )
            return Ok([_to_page(row) for row in rows])


# --------------------------------------------------------------------------- #
# Error constructors
# --------------------------------------------------------------------------- #


def _corrupt(message: str) -> AppError:
    return AppError(code=ErrorCode.STATE_CORRUPT, message=message, scope=ErrorScope.JOB_FATAL)


def _not_found(message: str) -> AppError:
    return AppError(code=ErrorCode.STATE_NOT_FOUND, message=message, scope=ErrorScope.JOB_FATAL)
