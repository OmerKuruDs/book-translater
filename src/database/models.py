"""SQLAlchemy schema for ``translation_state.db`` (DB design docs 03 and 07).

Schema version 2. Contains no business logic: only the declarative models, the
type decorators, the enum value lists used by the CHECK constraints and the
index definitions. ``Base.metadata.create_all`` is the DDL source for new files
and for the tables added by the 1 -> 2 migration, so every constraint here must
reproduce the design documents exactly (named ``ck_<table>_<what>`` CHECKs,
``Boolean(create_constraint=True)``, partial indexes via ``sqlite_where``).

The runtime-state column group shared by ``chunks`` and ``overlay_blocks`` (doc 07,
B8) is defined once in :class:`RuntimeStateMixin` plus
:func:`runtime_state_constraints` / :func:`runtime_state_indexes`, so the two
tables cannot drift.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional, Sequence, Tuple

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.types import Boolean, TypeDecorator

CURRENT_SCHEMA_VERSION = 3

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
TIMESTAMP_LENGTH = 27

JOB_STATUSES: Tuple[str, ...] = (
    "CREATED",
    "EXTRACTED",
    "CHUNKED",
    "TRANSLATING",
    "PAUSED",
    "TRANSLATED",
    "EXPORTED",
)
CHUNK_STATUSES: Tuple[str, ...] = ("PENDING", "PROCESSING", "COMPLETED", "FAILED")
CHUNK_KINDS: Tuple[str, ...] = (
    "TEXT",
    "HEADING",
    "LIST",
    "TABLE",
    "CODE",
    "IMAGE",
    "HR",
    "FOOTNOTE",
)
NON_TRANSLATABLE_KINDS: Tuple[str, ...] = ("CODE", "IMAGE", "HR")
RUN_COMMANDS: Tuple[str, ...] = ("run", "extract", "glossary", "translate", "export")
RUN_OUTCOMES: Tuple[str, ...] = ("SUCCESS", "PARTIAL", "PAUSED", "FAILED", "INTERRUPTED")
GLOSSARY_STRATEGIES: Tuple[str, ...] = ("native", "prompt", "post_replace", "none")

# v2 (doc 07, section 9.4)
OVERLAY_JOB_STATUSES: Tuple[str, ...] = (
    "NONE",
    "OVERLAY_EXTRACTED",
    "OVERLAY_TRANSLATING",
    "OVERLAY_PAUSED",
    "OVERLAY_TRANSLATED",
    "OVERLAY_EXPORTED",
)
RUN_MODES: Tuple[str, ...] = ("reflow", "overlay")
OVERLAY_BLOCK_KINDS: Tuple[str, ...] = (
    "body",
    "heading",
    "caption",
    "header",
    "footer",
    "page_number",
    "figure_label",
    "table_cell",
    "footnote",
)
OVERLAY_KEEP_REASONS: Tuple[str, ...] = (
    "page_number",
    "header_footer",
    "figure_text",
    "rotated",
    "no_letters",
    "math",
    "no_bbox",
    "widget_overlap",
)
OVERLAY_FAMILIES: Tuple[str, ...] = ("serif", "sans", "mono")
OVERLAY_ALIGNMENTS: Tuple[str, ...] = ("left", "center", "right", "justify")
OVERLAY_PAGE_SKIP_REASONS: Tuple[str, ...] = ("no_text_layer",)

#: The runtime-state column group shared by ``chunks`` and ``overlay_blocks`` (doc 07, B8).
RUNTIME_STATE_COLUMNS: Tuple[str, ...] = (
    "status",
    "retry_count",
    "next_attempt_at",
    "last_error_code",
    "last_error",
    "translated_text",
    "review_flag",
    "warnings",
    "provider",
    "glossary_strategy",
    "glossary_hash",
    "chars_sent",
    "chars_billed",
    "latency_ms",
    "attempts",
    "last_run_id",
    "claimed_at",
    "completed_at",
    "created_at",
    "updated_at",
)


# --------------------------------------------------------------------------- #
# Timestamp helpers and type decorators (doc 03 section 2.6, doc 07 section 3.6)
# --------------------------------------------------------------------------- #


def format_timestamp(value: datetime) -> str:
    """Render an aware datetime as the fixed 27-char UTC form ``YYYY-MM-DDTHH:MM:SS.ffffffZ``.

    Raises ``ValueError`` for naive datetimes (programming error; surfaces as ``INTERNAL``).
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("timestamp must be timezone-aware (naive datetime given)")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def parse_timestamp(value: str) -> datetime:
    """Inverse of :func:`format_timestamp`; returns an aware UTC datetime."""
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


class CorruptValueError(ValueError):
    """A stored value cannot be decoded; the repository boundary maps it to ``STATE_CORRUPT``."""


class UtcTimestamp(TypeDecorator[datetime]):
    """TEXT column holding a 27-char UTC timestamp; binds/returns aware ``datetime`` objects."""

    impl = String(TIMESTAMP_LENGTH)
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect: Dialect) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise ValueError(f"UtcTimestamp expects datetime, got {type(value).__name__}")
        return format_timestamp(value)

    def process_result_value(self, value: Optional[str], dialect: Dialect) -> Optional[datetime]:
        if value is None:
            return None
        try:
            return parse_timestamp(value)
        except (TypeError, ValueError) as exc:
            raise CorruptValueError(f"stored timestamp is not in {TIMESTAMP_FORMAT} form") from exc


class JsonList(TypeDecorator[Tuple[str, ...]]):
    """TEXT column holding a JSON array of strings.

    Binds any list/tuple of ``str``; returns a tuple.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Optional[Sequence[str]], dialect: Dialect) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError(f"JsonList expects a list/tuple of str, got {type(value).__name__}")
        items = list(value)
        if not all(isinstance(item, str) for item in items):
            raise ValueError("JsonList expects every item to be a str")
        return json.dumps(items, ensure_ascii=False, separators=(",", ":"))

    def process_result_value(
        self, value: Optional[str], dialect: Dialect
    ) -> Optional[Tuple[str, ...]]:
        if value is None:
            return None
        try:
            decoded: Any = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise CorruptValueError("stored JSON list cannot be decoded") from exc
        if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
            raise CorruptValueError("stored JSON value is not a list of strings")
        return tuple(decoded)


Box = Tuple[float, float, float, float]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class JsonBoxes(TypeDecorator[Tuple[Box, ...]]):
    """TEXT column holding a JSON array of ``[x0, y0, x1, y1]`` arrays (doc 07, section 3.6).

    Binds a list/tuple of 4-tuples of numbers (``ValueError`` otherwise -> ``INTERNAL``);
    returns a tuple of 4-tuples of ``float`` (``CorruptValueError`` otherwise ->
    ``STATE_CORRUPT``). Rounding is the extractor's job, not the decorator's.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Optional[Any], dialect: Dialect) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError(f"JsonBoxes expects a list/tuple of boxes, got {type(value).__name__}")
        boxes: list[list[float]] = []
        for box in value:
            if isinstance(box, (str, bytes)) or not isinstance(box, (list, tuple)) or len(box) != 4:
                raise ValueError("JsonBoxes expects every box to be a 4-item list/tuple")
            if not all(_is_number(n) for n in box):
                raise ValueError("JsonBoxes expects every box coordinate to be a number")
            boxes.append([float(n) for n in box])
        return json.dumps(boxes, separators=(",", ":"))

    def process_result_value(
        self, value: Optional[str], dialect: Dialect
    ) -> Optional[Tuple[Box, ...]]:
        if value is None:
            return None
        try:
            decoded: Any = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise CorruptValueError("stored JSON boxes cannot be decoded") from exc
        if not isinstance(decoded, list):
            raise CorruptValueError("stored JSON boxes value is not a list")
        boxes: list[Box] = []
        for box in decoded:
            if not isinstance(box, list) or len(box) != 4 or not all(_is_number(n) for n in box):
                raise CorruptValueError("stored JSON box is not a list of 4 numbers")
            boxes.append((float(box[0]), float(box[1]), float(box[2]), float(box[3])))
        return tuple(boxes)


# --------------------------------------------------------------------------- #
# Constraint helpers
# --------------------------------------------------------------------------- #


def _ts_check(table: str, column: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} IS NULL OR length({column}) = {TIMESTAMP_LENGTH}",
        name=f"ck_{table}_{column}_len",
    )


def _enum_check(table: str, column: str, values: Sequence[str], nullable: bool) -> CheckConstraint:
    joined = ", ".join(f"'{v}'" for v in values)
    expr = f"{column} IN ({joined})"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"ck_{table}_{column}")


def _nonneg_check(table: str, column: str, nullable: bool = False) -> CheckConstraint:
    expr = f"{column} >= 0"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"ck_{table}_{column}")


def _bool(table: str, column: str) -> Boolean:
    return Boolean(create_constraint=True, name=f"ck_{table}_{column}")


class Base(DeclarativeBase):
    """Declarative base for the state database."""


# --------------------------------------------------------------------------- #
# Runtime-state column group (doc 07, B8): shared by chunks and overlay_blocks
# --------------------------------------------------------------------------- #


class RuntimeStateMixin:
    """The mutable runtime-state columns of a translation unit, identical for both unit tables.

    ``declared_attr`` is used for every column so that the copies are created when the
    concrete class is scanned (after its own static columns, keeping the static /
    runtime order of the design tables) and so that the boolean CHECK can carry the
    table name.
    """

    __tablename__: str

    @declared_attr
    def status(cls) -> Mapped[str]:
        return mapped_column(String(10), nullable=False, server_default=text("'PENDING'"))

    @declared_attr
    def retry_count(cls) -> Mapped[int]:
        return mapped_column(Integer, nullable=False, server_default=text("0"))

    @declared_attr
    def next_attempt_at(cls) -> Mapped[Optional[datetime]]:
        return mapped_column(UtcTimestamp)

    @declared_attr
    def last_error_code(cls) -> Mapped[Optional[str]]:
        return mapped_column(String(40))

    @declared_attr
    def last_error(cls) -> Mapped[Optional[str]]:
        return mapped_column(String(2000))

    @declared_attr
    def translated_text(cls) -> Mapped[Optional[str]]:
        return mapped_column(Text)

    @declared_attr
    def review_flag(cls) -> Mapped[bool]:
        return mapped_column(
            _bool(cls.__tablename__, "review_flag"), nullable=False, server_default=text("0")
        )

    @declared_attr
    def warnings(cls) -> Mapped[Tuple[str, ...]]:
        return mapped_column(JsonList, nullable=False, server_default=text("'[]'"))

    @declared_attr
    def provider(cls) -> Mapped[Optional[str]]:
        return mapped_column(String(40))

    @declared_attr
    def glossary_strategy(cls) -> Mapped[Optional[str]]:
        return mapped_column(String(12))

    @declared_attr
    def glossary_hash(cls) -> Mapped[Optional[str]]:
        return mapped_column(String(64))

    @declared_attr
    def chars_sent(cls) -> Mapped[int]:
        return mapped_column(Integer, nullable=False, server_default=text("0"))

    @declared_attr
    def chars_billed(cls) -> Mapped[int]:
        return mapped_column(Integer, nullable=False, server_default=text("0"))

    @declared_attr
    def latency_ms(cls) -> Mapped[Optional[int]]:
        return mapped_column(Integer)

    @declared_attr
    def attempts(cls) -> Mapped[int]:
        return mapped_column(Integer, nullable=False, server_default=text("0"))

    @declared_attr
    def last_run_id(cls) -> Mapped[Optional[str]]:
        return mapped_column(String(12))  # soft reference, no FK

    @declared_attr
    def claimed_at(cls) -> Mapped[Optional[datetime]]:
        return mapped_column(UtcTimestamp)

    @declared_attr
    def completed_at(cls) -> Mapped[Optional[datetime]]:
        return mapped_column(UtcTimestamp)

    @declared_attr
    def created_at(cls) -> Mapped[datetime]:
        return mapped_column(UtcTimestamp, nullable=False)

    @declared_attr
    def updated_at(cls) -> Mapped[datetime]:
        return mapped_column(UtcTimestamp, nullable=False)


def runtime_state_constraints(table: str) -> Tuple[CheckConstraint, ...]:
    """The CHECKs of the runtime-state group, named ``ck_<table>_<what>``."""
    return (
        _enum_check(table, "status", CHUNK_STATUSES, nullable=False),
        _nonneg_check(table, "retry_count"),
        CheckConstraint(
            "status = 'PENDING' OR next_attempt_at IS NULL",
            name=f"ck_{table}_next_attempt_pending_only",
        ),
        CheckConstraint(
            "last_error IS NULL OR length(last_error) <= 2000", name=f"ck_{table}_last_error_len"
        ),
        CheckConstraint(
            "(status = 'COMPLETED') = (translated_text IS NOT NULL)",
            name=f"ck_{table}_translated_text_completed",
        ),
        _enum_check(table, "glossary_strategy", GLOSSARY_STRATEGIES, nullable=True),
        _nonneg_check(table, "chars_sent"),
        _nonneg_check(table, "chars_billed"),
        _nonneg_check(table, "latency_ms", nullable=True),
        _nonneg_check(table, "attempts"),
        _ts_check(table, "next_attempt_at"),
        _ts_check(table, "claimed_at"),
        _ts_check(table, "completed_at"),
        _ts_check(table, "created_at"),
        _ts_check(table, "updated_at"),
    )


def runtime_state_indexes(table: str) -> Tuple[Index, ...]:
    """The three hot indexes of the runtime-state group (doc 03 I2-I4, doc 07 O2-O4)."""
    return (
        # counts per status, FAILED/PROCESSING id lists, requeue/reset, claim, FK support.
        Index(f"ix_{table}_job_status", "job_id", "status"),
        # earliest_next_attempt and the "waiting for backoff" count (partial: PENDING rows only).
        Index(
            f"ix_{table}_pending_backoff",
            "job_id",
            "next_attempt_at",
            sqlite_where=text("status = 'PENDING'"),
        ),
        # review id list (partial: flagged rows only).
        Index(f"ix_{table}_review", "job_id", sqlite_where=text("review_flag = 1")),
    )


# --------------------------------------------------------------------------- #
# 2.1 schema_meta
# --------------------------------------------------------------------------- #


class SchemaMeta(Base):
    __tablename__ = "schema_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_tool_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    upgraded_by_tool_version: Mapped[Optional[str]] = mapped_column(String(40))
    upgraded_at: Mapped[Optional[datetime]] = mapped_column(UtcTimestamp)

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_schema_meta_id"),
        CheckConstraint("schema_version >= 1", name="ck_schema_meta_schema_version"),
        _ts_check("schema_meta", "created_at"),
        _ts_check("schema_meta", "upgraded_at"),
    )


# --------------------------------------------------------------------------- #
# 2.2 jobs (+ v2 overlay binding columns, doc 07 section 3.3)
# --------------------------------------------------------------------------- #


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    singleton: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    input_path: Mapped[str] = mapped_column(Text, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_md_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default=text("'CREATED'")
    )
    tool_version: Mapped[str] = mapped_column(String(40), nullable=False)
    extractor_name: Mapped[Optional[str]] = mapped_column(String(20))
    fallback_used: Mapped[bool] = mapped_column(
        _bool("jobs", "fallback_used"), nullable=False, server_default=text("0")
    )
    detected_language: Mapped[Optional[str]] = mapped_column(String(8))
    chunk_min_chars: Mapped[Optional[int]] = mapped_column(Integer)
    chunk_max_chars: Mapped[Optional[int]] = mapped_column(Integer)
    provider: Mapped[Optional[str]] = mapped_column(String(40))
    glossary_strategy: Mapped[Optional[str]] = mapped_column(String(12))
    provider_glossary_id: Mapped[Optional[str]] = mapped_column(String(120))
    glossary_hash: Mapped[Optional[str]] = mapped_column(String(64))
    warnings: Mapped[Tuple[str, ...]] = mapped_column(
        JsonList, nullable=False, server_default=text("'[]'")
    )
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    # v2: overlay pass binding (appended last so that create_all and ADD COLUMN agree on order)
    overlay_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'NONE'")
    )
    overlay_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    overlay_translate_headers: Mapped[bool] = mapped_column(
        _bool("jobs", "overlay_translate_headers"), nullable=False, server_default=text("0")
    )
    overlay_keep_figure_text: Mapped[bool] = mapped_column(
        _bool("jobs", "overlay_keep_figure_text"), nullable=False, server_default=text("0")
    )
    overlay_tool_version: Mapped[Optional[str]] = mapped_column(String(40))
    overlay_unit_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    __table_args__ = (
        UniqueConstraint("singleton", name="uq_jobs_singleton"),
        CheckConstraint("length(id) = 36", name="ck_jobs_id_len"),
        CheckConstraint("singleton = 1", name="ck_jobs_singleton"),
        CheckConstraint("length(input_sha256) = 64", name="ck_jobs_input_sha256_len"),
        CheckConstraint(
            "source_md_sha256 IS NULL OR length(source_md_sha256) = 64",
            name="ck_jobs_source_md_sha256_len",
        ),
        _enum_check("jobs", "status", JOB_STATUSES, nullable=False),
        CheckConstraint(
            "chunk_min_chars IS NULL OR chunk_min_chars > 0", name="ck_jobs_chunk_min_chars"
        ),
        CheckConstraint(
            "chunk_max_chars IS NULL OR chunk_max_chars >= chunk_min_chars",
            name="ck_jobs_chunk_max_chars",
        ),
        _enum_check("jobs", "glossary_strategy", GLOSSARY_STRATEGIES, nullable=True),
        _ts_check("jobs", "created_at"),
        _ts_check("jobs", "updated_at"),
        # v2
        _enum_check("jobs", "overlay_status", OVERLAY_JOB_STATUSES, nullable=False),
        CheckConstraint(
            "overlay_sha256 IS NULL OR length(overlay_sha256) = 64",
            name="ck_jobs_overlay_sha256_len",
        ),
        CheckConstraint(
            "(overlay_status = 'NONE') = (overlay_sha256 IS NULL)", name="ck_jobs_overlay_binding"
        ),
        _nonneg_check("jobs", "overlay_unit_count"),
    )


# --------------------------------------------------------------------------- #
# 2.3 chunks
# --------------------------------------------------------------------------- #


class Chunk(RuntimeStateMixin, Base):
    __tablename__ = "chunks"

    # INTEGER PRIMARY KEY = rowid alias = chunk_id = order. Explicit values, no AUTOINCREMENT.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    translatable: Mapped[bool] = mapped_column(_bool("chunks", "translatable"), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_block_id: Mapped[Optional[int]] = mapped_column(Integer)
    sub_index: Mapped[Optional[int]] = mapped_column(Integer)
    sub_count: Mapped[Optional[int]] = mapped_column(Integer)
    heading_path: Mapped[Tuple[str, ...]] = mapped_column(
        JsonList, nullable=False, server_default=text("'[]'")
    )
    # runtime-state group: RuntimeStateMixin

    __table_args__ = (
        CheckConstraint("id >= 1", name="ck_chunks_id"),
        _enum_check("chunks", "kind", CHUNK_KINDS, nullable=False),
        CheckConstraint(
            "kind NOT IN ('CODE', 'IMAGE', 'HR') OR translatable = 0",
            name="ck_chunks_nontranslatable_kinds",
        ),
        CheckConstraint("length(content_hash) = 64", name="ck_chunks_content_hash_len"),
        _nonneg_check("chunks", "char_count"),
        CheckConstraint(
            "((parent_block_id IS NULL) = (sub_index IS NULL))"
            " AND ((sub_index IS NULL) = (sub_count IS NULL))"
            " AND (sub_index IS NULL OR (sub_index >= 0 AND sub_count >= 1"
            " AND sub_index < sub_count))",
            name="ck_chunks_subsplit",
        ),
        *runtime_state_constraints("chunks"),
        *runtime_state_indexes("chunks"),  # I2, I3, I4
    )


# --------------------------------------------------------------------------- #
# 2.4 runs (+ v2 mode column, doc 07 section 3.4)
# --------------------------------------------------------------------------- #


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(12), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    command: Mapped[str] = mapped_column(String(10), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[Optional[str]] = mapped_column(String(40))
    provider_switch_allowed: Mapped[bool] = mapped_column(
        _bool("runs", "provider_switch_allowed"), nullable=False, server_default=text("0")
    )
    started_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UtcTimestamp)
    outcome: Mapped[Optional[str]] = mapped_column(String(12))
    exit_code: Mapped[Optional[int]] = mapped_column(Integer)
    chunks_completed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chunks_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chars_sent: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    rate_limited_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # v2: appended last (ADD COLUMN order)
    mode: Mapped[str] = mapped_column(String(7), nullable=False, server_default=text("'reflow'"))

    __table_args__ = (
        _enum_check("runs", "command", RUN_COMMANDS, nullable=False),
        _enum_check("runs", "outcome", RUN_OUTCOMES, nullable=True),
        CheckConstraint(
            "(finished_at IS NULL) = (outcome IS NULL)", name="ck_runs_finished_outcome"
        ),
        _nonneg_check("runs", "chunks_completed"),
        _nonneg_check("runs", "chunks_failed"),
        _nonneg_check("runs", "chars_sent"),
        _nonneg_check("runs", "provider_calls"),
        _nonneg_check("runs", "rate_limited_count"),
        _ts_check("runs", "started_at"),
        _ts_check("runs", "finished_at"),
        _enum_check("runs", "mode", RUN_MODES, nullable=False),
    )


# --------------------------------------------------------------------------- #
# 2.5 lease
# --------------------------------------------------------------------------- #


class Lease(Base):
    __tablename__ = "lease"

    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    holder_pid: Mapped[int] = mapped_column(Integer, nullable=False)
    holder_host: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[str] = mapped_column(String(12), nullable=False)  # not an FK (acquired first)
    acquired_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        _ts_check("lease", "acquired_at"),
        _ts_check("lease", "heartbeat_at"),
    )


# --------------------------------------------------------------------------- #
# 3.1 overlay_pages (v2)
# --------------------------------------------------------------------------- #


class OverlayPage(Base):
    __tablename__ = "overlay_pages"

    # INTEGER PRIMARY KEY = rowid alias = the 1-based PDF page number (single-job database).
    page: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    width: Mapped[float] = mapped_column(Float, nullable=False)
    height: Mapped[float] = mapped_column(Float, nullable=False)
    rotation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    has_text_layer: Mapped[bool] = mapped_column(
        _bool("overlay_pages", "has_text_layer"), nullable=False
    )
    skip_reason: Mapped[Optional[str]] = mapped_column(String(20))
    block_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    translatable_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    body_size: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(UtcTimestamp, nullable=False)

    __table_args__ = (
        CheckConstraint("page >= 1", name="ck_overlay_pages_page"),
        CheckConstraint("width > 0", name="ck_overlay_pages_width"),
        CheckConstraint("height > 0", name="ck_overlay_pages_height"),
        CheckConstraint("rotation IN (0, 90, 180, 270)", name="ck_overlay_pages_rotation"),
        _enum_check("overlay_pages", "skip_reason", OVERLAY_PAGE_SKIP_REASONS, nullable=True),
        _nonneg_check("overlay_pages", "block_count"),
        CheckConstraint(
            "translatable_count >= 0 AND translatable_count <= block_count",
            name="ck_overlay_pages_translatable_count",
        ),
        _nonneg_check("overlay_pages", "body_size"),
        CheckConstraint(
            "has_text_layer = 1 OR skip_reason IS NOT NULL", name="ck_overlay_pages_skip"
        ),
        CheckConstraint(
            "skip_reason IS NULL OR block_count = 0", name="ck_overlay_pages_skip_empty"
        ),
        _ts_check("overlay_pages", "created_at"),
    )


# --------------------------------------------------------------------------- #
# 3.2 overlay_blocks (v2)
# --------------------------------------------------------------------------- #


class OverlayBlock(RuntimeStateMixin, Base):
    __tablename__ = "overlay_blocks"

    # INTEGER PRIMARY KEY = rowid alias = unit_id = order. Explicit values, no AUTOINCREMENT.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    page: Mapped[int] = mapped_column(
        Integer, ForeignKey("overlay_pages.page", ondelete="CASCADE"), nullable=False
    )
    index_on_page: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    translate: Mapped[bool] = mapped_column(_bool("overlay_blocks", "translate"), nullable=False)
    keep_reason: Mapped[Optional[str]] = mapped_column(String(16))
    fragment: Mapped[bool] = mapped_column(
        _bool("overlay_blocks", "fragment"), nullable=False, server_default=text("0")
    )
    over_image: Mapped[bool] = mapped_column(
        _bool("overlay_blocks", "over_image"), nullable=False, server_default=text("0")
    )
    x0: Mapped[float] = mapped_column(Float, nullable=False)
    y0: Mapped[float] = mapped_column(Float, nullable=False)
    x1: Mapped[float] = mapped_column(Float, nullable=False)
    y1: Mapped[float] = mapped_column(Float, nullable=False)
    line_boxes: Mapped[Tuple[Box, ...]] = mapped_column(
        JsonBoxes, nullable=False, server_default=text("'[]'")
    )
    font_size: Mapped[float] = mapped_column(Float, nullable=False)
    bold: Mapped[bool] = mapped_column(
        _bool("overlay_blocks", "bold"), nullable=False, server_default=text("0")
    )
    italic: Mapped[bool] = mapped_column(
        _bool("overlay_blocks", "italic"), nullable=False, server_default=text("0")
    )
    family: Mapped[str] = mapped_column(String(5), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    prefix_text: Mapped[Optional[str]] = mapped_column(String(8))
    prefix_bold: Mapped[Optional[int]] = mapped_column(Integer)
    prefix_italic: Mapped[Optional[int]] = mapped_column(Integer)
    alignment: Mapped[str] = mapped_column(String(7), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # runtime-state group: RuntimeStateMixin

    __table_args__ = (
        CheckConstraint("id >= 1", name="ck_overlay_blocks_id"),
        CheckConstraint("page >= 1", name="ck_overlay_blocks_page"),
        CheckConstraint("index_on_page >= 1", name="ck_overlay_blocks_index_on_page"),
        _enum_check("overlay_blocks", "kind", OVERLAY_BLOCK_KINDS, nullable=False),
        CheckConstraint(
            "kind <> 'page_number' OR translate = 0", name="ck_overlay_blocks_page_number_kept"
        ),
        _enum_check("overlay_blocks", "keep_reason", OVERLAY_KEEP_REASONS, nullable=True),
        CheckConstraint(
            "(translate = 1) = (keep_reason IS NULL)", name="ck_overlay_blocks_keep_reason"
        ),
        CheckConstraint("x1 >= x0 AND y1 >= y0", name="ck_overlay_blocks_bbox"),
        CheckConstraint("font_size > 0", name="ck_overlay_blocks_font_size"),
        _enum_check("overlay_blocks", "family", OVERLAY_FAMILIES, nullable=False),
        CheckConstraint(
            "length(color) = 7 AND substr(color, 1, 1) = '#'", name="ck_overlay_blocks_color"
        ),
        CheckConstraint(
            "((prefix_text IS NULL) = (prefix_bold IS NULL))"
            " AND ((prefix_bold IS NULL) = (prefix_italic IS NULL))"
            " AND (prefix_bold IS NULL OR (prefix_bold IN (0, 1) AND prefix_italic IN (0, 1)))",
            name="ck_overlay_blocks_prefix_style",
        ),
        _enum_check("overlay_blocks", "alignment", OVERLAY_ALIGNMENTS, nullable=False),
        CheckConstraint("length(content_hash) = 64", name="ck_overlay_blocks_content_hash_len"),
        _nonneg_check("overlay_blocks", "char_count"),
        *runtime_state_constraints("overlay_blocks"),
        *runtime_state_indexes("overlay_blocks"),  # O2, O3, O4
        # O5: page access (iter_page / page_texts) and the covering per-page COMPLETED count
        #     of page_progress. Three columns by design (doc 07, B18).
        Index("ix_overlay_blocks_job_page_status", "job_id", "page", "status"),
    )


TABLE_NAMES: Tuple[str, ...] = (
    "schema_meta",
    "jobs",
    "chunks",
    "runs",
    "lease",
    "overlay_pages",
    "overlay_blocks",
)
INDEX_NAMES: Tuple[str, ...] = (
    "ix_chunks_job_status",
    "ix_chunks_pending_backoff",
    "ix_chunks_review",
    "ix_overlay_blocks_job_status",
    "ix_overlay_blocks_pending_backoff",
    "ix_overlay_blocks_review",
    "ix_overlay_blocks_job_page_status",
)
