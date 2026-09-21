"""Stage coordinator and async job runner (design doc 02, section 5).

Stages: ``extract`` -> ``glossary`` -> (chunk) -> ``translate`` -> ``export``;
``run`` chains them and combines exit codes with the severity rule of 9.3.
Every public method returns ``Result``; no exception crosses this boundary
except ``asyncio.CancelledError`` (5.7).

Collaborators (translator/extractor/exporter registries, chunk repository,
clock, sleep, progress callback, cancel/abort events) are injectable so the
runner can be tested with fakes and a virtual clock. All repository calls made
from the event loop go through ``asyncio.to_thread`` under one ``asyncio.Lock``
(SQLite is single-writer; the heartbeat task shares the lock).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import json
import logging
import os
import random
import re
import shutil
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
)

from ..config import Settings
from ..database.repository import (
    UNSET,
    ChunkRepository,
    JobPatch,
    JobRecord,
    JobRepository,
    JobSpec,
    LeaseHolder,
    LeaseRepository,
    OverlayBlockRepository,
    RunOutcome,
)
from ..database.session import (
    ARCHIVE_DIR_NAME,
    DB_FILE_NAME,
    Database,
    archive_database,
    archive_stamp,
    close_database,
    open_database,
    utcnow,
)
from ..domain.models import (
    AssembledDocument,
    ChunkStatus,
    EffectiveGlossary,
    ExtractedDocument,
    FigureRecord,
    FigureTotals,
    GlossaryEntry,
    GlossaryStatus,
    GlossaryStrategy,
    JobSummary,
    OverlaySummary,
    StatusCounts,
    TranslationResult,
)
from ..domain.overlay import BBox, OverlayBlock, OverlayBlockKind
from ..domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, Result, err
from ..exporters.base import (
    BaseExporter,
    ExportOptions,
    atomic_write_bytes,
    get_exporter,
    image_lines,
)
from ..exporters.fonts import resolve_font
from ..extractors.base import (
    BaseExtractor,
    ExtractOptions,
    FigureOptions,
    FigureSink,
    figure_options_from_settings,
    get_extractor,
    validate_pdf_path,
)
from ..extractors.figure_inventory import (
    FIGURES_FILE_NAME,
    FiguresFile,
    figures_file_from_document,
    inventory_signature,
    load_figures_file,
    save_figures_file,
)
from ..extractors.langdetect import check_english
from ..extractors.overlay_extractor import (
    OverlayOptions,
    check_modifiable,
    extract_overlay,
    overlay_signature,
)
from ..extractors.source_profile import (
    SOURCE_PROFILE_FILE_NAME,
    load_source_profile,
    save_source_profile,
)
from ..glossary.manager import (
    build_or_update,
    effective_glossary,
    glossary_path,
    load_user_glossary,
    merge_with_precedence,
)
from ..glossary.schema import load_glossary_file
from ..logging_setup import bind_job, chunk_context
from ..translators.base import (
    BaseTranslator,
    GlossaryBinding,
    ProviderResponse,
    TranslationRequest,
    get_translator,
)
from ..translators.fallback import FallbackResponse, FallbackTranslator
from ..translators.protect import ProtectMode
from .assembler import assemble
from .backoff import AdaptiveLimiter, BackoffPolicy
from .chunker import chunk as chunk_markdown
from .figure_labels import (
    build_replacements,
    legend_ids,
    legend_pairs,
    rewrite_image_sources,
    strip_legends,
    translated_figure_name,
)
from .units import (
    EMPTY_FAIL_AT,
    EMPTY_PREFIX,
    ChunkUnitAdapter,
    FinishContext,
    OverlayUnitAdapter,
    UnitAdapter,
    UnitRepository,
    verbatim_result,
)

__all__ = [
    "DEFAULT_OVERLAY_FLOOR_SCALE",
    "DEFAULT_OVERLAY_MIN_SCALE",
    "IMAGES_DIR_NAME",
    "MODE_OVERLAY",
    "MODE_REFLOW",
    "OVERLAY_FORMAT",
    "OVERLAY_OUTPUT_NAME",
    "OVERLAY_REVIEW_NAME",
    "OverlayOptions",
    "REFLOW_FORMATS",
    "SOURCE_PAGE_SIZE",
    "EXIT_FAILURE",
    "EXIT_INTERRUPTED",
    "EXIT_LOCKED",
    "EXIT_MISMATCH",
    "EXIT_PARTIAL",
    "EXIT_PAUSED",
    "EXIT_SUCCESS",
    "EXPORT_ERROR_PREFIX",
    "ExportOutcome",
    "ExtractOutcome",
    "GlossaryOutcome",
    "Orchestrator",
    "ProgressEvent",
    "StatusReport",
    "active_provider_of",
    "default_formats",
    "describe_provider_units",
    "exit_code_for",
    "normalise_formats",
    "outcome_name",
    "summary_to_jsonable",
    "worst_exit",
]

log = logging.getLogger("book_translator.pipeline.orchestrator")

# --------------------------------------------------------------------------- #
# Exit codes (design 9.3)
# --------------------------------------------------------------------------- #

#: CR-91: prefix of the warning that carries a format which *failed* (as opposed to an
#: optional exporter that was skipped, ``export_failed:``/``exporter_unavailable:``). The
#: outputs written before it are reported as usual; the CLI prints this one as an error.
EXPORT_ERROR_PREFIX = "export_error:"

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_PARTIAL = 2
EXIT_PAUSED = 3
EXIT_LOCKED = 4
EXIT_MISMATCH = 5
EXIT_INTERRUPTED = 130

_SEVERITY_ORDER: Tuple[int, ...] = (
    EXIT_FAILURE,
    EXIT_PAUSED,
    EXIT_LOCKED,
    EXIT_MISMATCH,
    EXIT_INTERRUPTED,
    EXIT_PARTIAL,
    EXIT_SUCCESS,
)

_MISMATCH_CODES = frozenset(
    {
        ErrorCode.STATE_HASH_MISMATCH,
        ErrorCode.STATE_PROVIDER_MISMATCH,
        ErrorCode.STATE_SCHEMA_INCOMPATIBLE,
    }
)
_PARTIAL_CODES = frozenset({ErrorCode.EXPORT_REFUSED_PARTIAL, ErrorCode.EXPORTER_UNAVAILABLE})
_PAUSE_CODES = frozenset(
    {ErrorCode.PROVIDER_QUOTA, ErrorCode.PROVIDER_AUTH, ErrorCode.GLOSSARY_BIND_FAILED}
)


def exit_code_for(error: AppError) -> int:
    """Map an ``AppError`` to the process exit code of design 9.3."""
    if error.code is ErrorCode.STATE_LOCKED:
        return EXIT_LOCKED
    if error.code in _MISMATCH_CODES:
        return EXIT_MISMATCH
    if error.code is ErrorCode.INTERRUPTED:
        return EXIT_INTERRUPTED
    if error.code in _PARTIAL_CODES:
        return EXIT_PARTIAL
    if error.code in _PAUSE_CODES:
        return EXIT_PAUSED
    return EXIT_FAILURE


def worst_exit(codes: Iterable[int]) -> int:
    """Highest-severity code with the order ``1 > 3 > 4 > 5 > 130 > 2 > 0``."""
    present = set(codes)
    unknown = {c for c in present if c not in _SEVERITY_ORDER}
    if unknown:
        return EXIT_FAILURE
    for code in _SEVERITY_ORDER:
        if code in present:
            return code
    return EXIT_SUCCESS


def outcome_name(exit_code: int) -> str:
    """Run outcome label (``runs.outcome``) for an exit code."""
    return {
        EXIT_SUCCESS: "SUCCESS",
        EXIT_PARTIAL: "PARTIAL",
        EXIT_PAUSED: "PAUSED",
        EXIT_INTERRUPTED: "INTERRUPTED",
    }.get(exit_code, "FAILED")


# --------------------------------------------------------------------------- #
# Public value types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProgressEvent:
    completed: int
    failed: int
    total: int
    elapsed_s: float
    chars_sent: int
    permits: int


@dataclass(frozen=True)
class ExtractOutcome:
    job_id: str
    source_path: Path
    extractor: str
    fallback_used: bool
    pages: int
    skipped_pages: int
    detected_language: Optional[str]
    language_confidence: float
    image_count: int
    warnings: List[str]
    figures: Optional[FigureTotals] = None  # v1.1: None with --no-figures / marker


@dataclass(frozen=True)
class GlossaryOutcome:
    path: Path
    entries: int
    approved: int
    proposed: int
    ambiguous: int
    rejected: int
    written: bool  # False when an existing file was kept untouched


@dataclass(frozen=True)
class ExportOutcome:
    job_id: str
    outputs: Dict[str, str]
    failed_chunk_ids: List[int]
    review_chunk_ids: List[int]
    warnings: List[str]
    exit_code: int
    # CR-51: the real placement result of an overlay export (``None`` without one)
    overlay: Optional[OverlaySummary] = None
    overlay_review_entries: int = 0


@dataclass(frozen=True)
class StatusReport:
    state_path: str
    exists: bool
    job_id: Optional[str] = None
    input_path: Optional[str] = None
    input_sha256: Optional[str] = None
    job_status: Optional[str] = None
    provider: Optional[str] = None
    glossary_strategy: Optional[str] = None
    glossary_hash: Optional[str] = None
    extractor: Optional[str] = None
    fallback_used: bool = False
    schema_version: Optional[int] = None
    counts: Optional[StatusCounts] = None
    earliest_next_attempt: Optional[datetime] = None
    failed_chunk_ids: List[int] = field(default_factory=list)
    review_chunk_ids: List[int] = field(default_factory=list)
    chars_sent_total: int = 0
    chars_billed_total: int = 0
    lease: Optional[Dict[str, Any]] = None
    last_run: Optional[Dict[str, Any]] = None
    warnings: List[str] = field(default_factory=list)
    figures: Optional[FigureTotals] = None  # from figures.json when present (v1.1)
    overlay: Optional[Dict[str, Any]] = None  # overlay pass section (v1.1 F2, 5.6)
    mode: Optional[str] = None  # mode of the last run
    provider_units: Dict[str, int] = field(default_factory=dict)  # COMPLETED per provider


# --------------------------------------------------------------------------- #
# Internal state
# --------------------------------------------------------------------------- #

SOURCE_MD_NAME = "source_book.md"
SUMMARY_NAME = "summary.json"
LOGS_DIR_NAME = "logs"
IMAGES_DIR_NAME = "images"
OUTPUT_ARTIFACT_GLOBS: Tuple[str, ...] = ("translated_book.*",)
OVERLAY_REVIEW_NAME = "overlay_review.json"
OVERLAY_OUTPUT_NAME = "translated_book.overlay.pdf"
OUTPUT_ARTIFACT_NAMES: Tuple[str, ...] = (
    SOURCE_MD_NAME,
    SUMMARY_NAME,
    FIGURES_FILE_NAME,
    OVERLAY_REVIEW_NAME,
    SOURCE_PROFILE_FILE_NAME,
)
OVERLAY_PASS_ARTIFACTS: Tuple[str, ...] = (OVERLAY_OUTPUT_NAME, OVERLAY_REVIEW_NAME)
"""Outputs of an overlay pass: stale after any ``--fresh`` (the pass lives in the archived
database), so ``translate --fresh`` archives them even though it keeps the stage artifacts
(CR-46)."""
IMAGES_STAGING_PREFIX = "images.tmp-"
"""``images.tmp-<run_id>/``: the figure sink renders here; the PNGs are promoted into
``images/`` only after every refusal check passed (CR-55)."""
TOOL_FIGURE_NAME = re.compile(r"^p\d{3}-f\d{2}\.png$")
TOOL_TRANSLATED_FIGURE_NAME = re.compile(r"^(p\d{3}-f\d{2})\.tr\.png$")
"""The tool's own file names inside ``images/``. Stale-image cleanup only ever deletes
files matching them; anything else belongs to the user and is reported as
``figure_orphan:<file>`` (CR-56 / CR-62)."""
OUTPUT_ARTIFACT_DIRS: Tuple[str, ...] = (IMAGES_DIR_NAME,)
"""Derived artifacts moved into the archive by ``--fresh`` (C13: ``figures.json`` and the
``images/`` directory included); ``glossary.json`` is user input and is only copied (see
``_keep_glossary``)."""
OVERLAY_FORMAT = "pdf-overlay"
REFLOW_FORMATS: Tuple[str, ...] = ("md", "epub")
MODE_REFLOW = "reflow"
MODE_OVERLAY = "overlay"
SOURCE_PAGE_SIZE = "source"
DEFAULT_OVERLAY_MIN_SCALE = 0.65
DEFAULT_OVERLAY_FLOOR_SCALE = 0.5
FORMAT_TO_EXPORTER: Dict[str, str] = {
    "md": "markdown",
    "markdown": "markdown",
    "epub": "epub",
    "pdf": "pdf",
    OVERLAY_FORMAT: OVERLAY_FORMAT,
}


def normalise_formats(formats: Sequence[str]) -> List[str]:
    """Exporter names for ``--format`` values (order kept, duplicates dropped)."""
    names: List[str] = []
    for fmt in formats:
        name = FORMAT_TO_EXPORTER.get(fmt.lower().lstrip("."), fmt.lower())
        if name not in names:
            names.append(name)
    return names


def default_formats(has_reflow: bool, has_overlay: bool) -> List[str]:
    """The ``--format`` default rule of Addendum A: ``md,epub`` whenever a reflow pass
    exists, ``pdf-overlay`` whenever an overlay pass exists (``pdf-overlay`` is the default
    PDF output; the reflow ``pdf`` is only produced on request). With neither pass the
    reflow default applies so the v1 refusal message is reported."""
    names: List[str] = []
    if has_reflow or not has_overlay:
        names.extend(REFLOW_FORMATS)
    if has_overlay:
        names.append(OVERLAY_FORMAT)
    return names

_EMPTY_PREFIX = EMPTY_PREFIX
_EMPTY_FAIL_AT = EMPTY_FAIL_AT
_GRACE_LEASE_MARGIN_S = 10  # the grace drain must end this long before the lease goes stale

T = TypeVar("T")

TranslatorFactory = Callable[[str, Settings], Result[BaseTranslator]]
ExtractorFactory = Callable[[str], Result[BaseExtractor]]
ExporterFactory = Callable[[str], Result[BaseExporter]]
ChunkRepositoryFactory = Callable[[Database, str, Optional[str]], ChunkRepository]
OverlayRepositoryFactory = Callable[[Database, str, Optional[str]], OverlayBlockRepository]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]
ProgressCallback = Callable[[ProgressEvent], None]


class _LeaseKeepAlive:
    """Thread that refreshes the lease while a stage is bound (CR-49, closes CR-18).

    The long synchronous stages (``extract``, the overlay pre-flight, a 300-page overlay
    export) run outside the async runner, whose own heartbeat task only lives inside
    ``_run_job``. Without a beat a book of a few hundred pages outlives ``lease_stale_s``
    and a second process could take the state over. The thread uses its own pooled
    connection (file databases) and only ever *refreshes* the row of this holder; detecting
    a lost lease stays the job of ``_heartbeat_loop`` - a failed beat is logged, never fatal.
    """

    def __init__(self, db: Database, job_id: str, holder: LeaseHolder, interval_s: float) -> None:
        self._leases = LeaseRepository(db)
        self._job_id = job_id
        self._holder = holder
        self._interval = max(0.01, float(interval_s))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="bt-lease-keepalive", daemon=True
        )
        self.beats = 0

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        warned = False
        while not self._stop.wait(self._interval):
            try:
                beat = self._leases.heartbeat(self._job_id, self._holder)
            except Exception as exc:  # noqa: BLE001 - a keep-alive must never crash the stage
                beat = _internal("lease keep-alive", exc)
            if isinstance(beat, Err):
                if not warned:
                    log.warning(
                        "lease keep-alive failed: %s", beat.error.message,
                        extra={"event": "lease_keepalive_failed"},
                    )
                    warned = True
                continue
            self.beats += 1

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10.0)


@dataclass
class _Bound:
    db: Database
    job: JobRecord
    holder: LeaseHolder
    lease_held: bool
    keepalive: Optional[_LeaseKeepAlive] = None


_RESIDUAL_REASONS = frozenset({"residual_glyphs", "collateral_redaction"})


@dataclass(frozen=True)
class _OverlayExport:
    """What ``_export_overlay`` hands back (CR-51: the placement counts are not dropped)."""

    exit_code: int
    pdf_path: Path
    review_path: Path
    warnings: List[str]
    placed: Tuple[int, int, int, int]  # placed, shrunk_below_threshold, could_not_fit, kept
    review_entries: int


@dataclass(frozen=True)
class _SourceCheck:
    """What ``_verify_source`` hands back (CR-39, CR-92).

    ``path`` is the hash-verified source PDF, or ``None`` when the file cannot be used but is
    not required for the formats that were asked for; ``exit_code`` is what that costs
    (``EXIT_PARTIAL`` when an output is degraded by it, ``EXIT_SUCCESS`` otherwise).
    """

    path: Optional[Path]
    exit_code: int = EXIT_SUCCESS


@dataclass
class _RunStats:
    completed: int = 0
    failed: int = 0
    chars_sent: int = 0
    provider_calls: int = 0
    rate_limited: int = 0
    claimed: int = 0


@dataclass
class _TranslateContext:
    bound: _Bound
    translator: BaseTranslator
    binding: Optional[GlossaryBinding]
    glossary: EffectiveGlossary
    repo: UnitRepository[Any]  # ChunkRepository | OverlayBlockRepository (adapter.repo)
    adapter: UnitAdapter[Any]  # ChunkUnitAdapter | OverlayUnitAdapter
    jobs: JobRepository
    limiter: AdaptiveLimiter
    policy: BackoffPolicy
    lock: asyncio.Lock
    strict_glossary: bool
    base_counts: StatusCounts
    started: float
    mode: ProtectMode
    pause_patch: JobPatch = field(default_factory=lambda: JobPatch(status="PAUSED"))
    stats: _RunStats = field(default_factory=_RunStats)
    empty_counts: Dict[int, int] = field(default_factory=dict)
    provider_units: Dict[str, int] = field(default_factory=dict)  # this run, per provider
    pause_error: Optional[AppError] = None
    lease_lost: Optional[AppError] = None
    db_error: Optional[AppError] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def sha256_file(path: Path) -> Result[str]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"cannot read {path}: {exc.__class__.__name__}",
            ErrorScope.USER,
            cause=repr(exc),
        )
    return Ok(digest.hexdigest())


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def absolute_path(path: Path) -> Path:
    """Resolved absolute form of ``path`` (``os.path.abspath`` when resolving fails)."""
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return Path(os.path.abspath(str(path)))


def _json_default(value: object) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def summary_to_jsonable(summary: object) -> Any:
    """``dataclasses.asdict`` followed by a JSON round trip (datetimes -> ISO strings)."""
    payload: Any = summary
    if dataclasses.is_dataclass(summary) and not isinstance(summary, type):
        payload = dataclasses.asdict(summary)
    return json.loads(json.dumps(payload, default=_json_default, ensure_ascii=False))


def _internal(stage: str, exc: BaseException) -> Err:
    return err(
        ErrorCode.INTERNAL,
        f"internal error during {stage}: {type(exc).__name__}",
        ErrorScope.JOB_FATAL,
        cause=repr(exc)[:500],
    )


def _interrupted() -> Err:
    return err(ErrorCode.INTERRUPTED, "interrupted by the user; state intact", ErrorScope.USER)


def active_provider_of(translator: BaseTranslator) -> str:
    """The provider a request would reach now: ``FallbackTranslator`` may have switched."""
    if isinstance(translator, FallbackTranslator):
        return translator.active_provider
    return translator.name


def describe_provider_units(counts: Mapping[str, int]) -> str:
    """``{"deepl": 61, "google": 12}`` -> ``"deepl 61, google 12"`` (empty -> "")."""
    return ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))


def _with_warnings(
    result: Result[TranslationResult], extra: Tuple[str, ...]
) -> Result[TranslationResult]:
    """Append ``extra`` to a unit result's warnings (provider fallback notices)."""
    if not isinstance(result, Ok) or not extra:
        return result
    present = result.value.warnings
    missing = tuple(w for w in extra if w not in present)
    if not missing:
        return result
    return Ok(dataclasses.replace(result.value, warnings=present + missing))


def _zero_outcome(exit_code: int) -> RunOutcome:
    return RunOutcome(outcome_name(exit_code), exit_code, 0, 0, 0, 0, 0)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class Orchestrator:
    """Stage coordinator bound to one output directory and one run id."""

    def __init__(
        self,
        settings: Settings,
        output_dir: Path,
        *,
        tool_version: str,
        run_id: str,
        translator_factory: Optional[TranslatorFactory] = None,
        extractor_factory: Optional[ExtractorFactory] = None,
        exporter_factory: Optional[ExporterFactory] = None,
        chunk_repository_factory: Optional[ChunkRepositoryFactory] = None,
        overlay_repository_factory: Optional[OverlayRepositoryFactory] = None,
        clock: Optional[Clock] = None,
        sleep: Optional[Sleep] = None,
        progress: Optional[ProgressCallback] = None,
        cancel: Optional[asyncio.Event] = None,
        abort: Optional[asyncio.Event] = None,
        heartbeat_interval_s: float = 10.0,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.settings = settings
        self.output_dir = Path(output_dir)
        self.tool_version = tool_version
        self.run_id = run_id
        # Last job id bound by ``_bind``; survives ``_unbind`` so the CLI can
        # correlate its final ``command_failed`` record with the job (CR-37).
        self.last_job_id: Optional[str] = None
        # Resolved at call time so tests can monkeypatch the module-level registries.
        self._translator_factory: TranslatorFactory = translator_factory or get_translator
        self._extractor_factory: ExtractorFactory = extractor_factory or get_extractor
        self._exporter_factory: ExporterFactory = exporter_factory or get_exporter
        self._chunk_repo_factory: ChunkRepositoryFactory = (
            chunk_repository_factory or ChunkRepository
        )
        self._overlay_repo_factory: OverlayRepositoryFactory = (
            overlay_repository_factory or OverlayBlockRepository
        )
        self._clock: Clock = clock or utcnow
        self._sleep: Sleep = sleep or asyncio.sleep
        self._progress = progress
        self.cancel = cancel if cancel is not None else asyncio.Event()
        self.abort = abort if abort is not None else asyncio.Event()
        self._heartbeat_interval = heartbeat_interval_s
        self._rng = rng
        self._bound: Optional[_Bound] = None
        self._composite = False
        self._run_started = False
        self._run_finished = False
        self._deferred_outcome: Optional[RunOutcome] = None
        # CR-42: mode of the composite run; the first stage inserts the one ``runs`` row.
        self._run_mode: Optional[str] = None
        self.limiter_events: List[Tuple[str, int]] = []
        self.warnings: List[str] = []

    # -- paths ------------------------------------------------------------- #

    @property
    def db_path(self) -> Path:
        return self.output_dir / DB_FILE_NAME

    @property
    def source_path(self) -> Path:
        return self.output_dir / SOURCE_MD_NAME

    @property
    def summary_path(self) -> Path:
        return self.output_dir / SUMMARY_NAME

    @property
    def archive_dir(self) -> Path:
        return self.output_dir / ARCHIVE_DIR_NAME

    @property
    def figures_path(self) -> Path:
        return self.output_dir / FIGURES_FILE_NAME

    @property
    def images_dir(self) -> Path:
        return self.output_dir / IMAGES_DIR_NAME

    @property
    def source_profile_path(self) -> Path:
        return self.output_dir / SOURCE_PROFILE_FILE_NAME

    # ------------------------------------------------------------------ #
    # Binding: open DB, job, lease, hash check (5.1 steps 2-4)
    # ------------------------------------------------------------------ #

    def _existing_job(self, db: Database) -> Result[JobRecord]:
        found = JobRepository(db).get_single_job()
        if isinstance(found, Err):
            return found
        if found.value is None:
            return err(
                ErrorCode.STATE_NOT_FOUND,
                "the state database has no job; run `extract` (or `run`) first",
                ErrorScope.USER,
            )
        return Ok(found.value)

    def _holder(self) -> LeaseHolder:
        return LeaseHolder(pid=os.getpid(), host=socket.gethostname(), run_id=self.run_id)

    def _acquire_lease(self, db: Database, job_id: str, force_unlock: bool) -> Result[LeaseHolder]:
        leases = LeaseRepository(db)
        holder = self._holder()
        if force_unlock:
            broken = leases.force_break(job_id)
            if isinstance(broken, Err):
                return broken
            log.warning("lease forcibly broken (--force-unlock)", extra={"event": "lease_broken"})
        acquired = leases.acquire(job_id, holder, self.settings.lease_stale_s)
        if isinstance(acquired, Err):
            return acquired
        if not acquired.value:
            record = leases.get(job_id)
            detail = ""
            if isinstance(record, Ok) and record.value is not None:
                rec = record.value
                detail = (
                    f" (pid {rec.holder_pid} on {rec.holder_host}, run {rec.run_id}, "
                    f"last heartbeat {rec.heartbeat_at.isoformat()})"
                )
            return err(
                ErrorCode.STATE_LOCKED,
                f"another process holds the state lock{detail}; wait for it to finish or "
                "pass --force-unlock if you know it is dead",
                ErrorScope.USER,
            )
        log.info(
            "lease acquired by pid %s on %s", holder.pid, holder.host,
            extra={"event": "lease_acquired"},
        )
        return Ok(holder)

    def _archive_state(
        self,
        *,
        force_unlock: bool,
        keep_artifacts: bool,
        extra_names: Sequence[str] = (),
    ) -> Result[Path]:
        """``--fresh``: copy the DB (online backup) and move stage artifacts into the archive."""
        db_path = self.db_path
        opened = open_database(db_path, tool_version=self.tool_version)
        if isinstance(opened, Err):
            return opened
        db = opened.value
        try:
            found = JobRepository(db).get_single_job()
            if isinstance(found, Err):
                return found
            if found.value is not None:
                lease = self._acquire_lease(db, found.value.id, force_unlock)
                if isinstance(lease, Err):
                    return lease
                LeaseRepository(db).release(found.value.id, lease.value)
        finally:
            close_database(db)

        dest = self._archive_dest()
        archived = archive_database(db_path, dest)
        if isinstance(archived, Err):
            return archived
        for extra in (db_path, db_path.with_name(db_path.name + "-wal"),
                      db_path.with_name(db_path.name + "-shm")):
            try:
                if extra.exists():
                    extra.unlink()
            except OSError as exc:
                return err(
                    ErrorCode.INTERNAL,
                    f"previous state was archived to {dest} but {extra.name} could not be "
                    f"removed: {exc.__class__.__name__}",
                    ErrorScope.USER,
                    cause=repr(exc),
                )
        if not keep_artifacts:
            names = list(OUTPUT_ARTIFACT_NAMES)
            for pattern in OUTPUT_ARTIFACT_GLOBS:
                names.extend(p.name for p in self.output_dir.glob(pattern))
            for name in names:
                src = self.output_dir / name
                if src.is_file():
                    try:
                        shutil.move(str(src), str(dest / name))
                    except OSError as exc:
                        self.warnings.append(f"archive:{name}:{exc.__class__.__name__}")
            for name in OUTPUT_ARTIFACT_DIRS:  # C13: images/ moved as a whole
                src = self.output_dir / name
                if src.is_dir():
                    try:
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dest / name))
                    except OSError as exc:
                        self.warnings.append(f"archive:{name}:{exc.__class__.__name__}")
            self._keep_glossary(dest)
        else:
            # CR-46: outputs of the archived pass must not survive next to the new state.
            for name in extra_names:
                src = self.output_dir / name
                if src.is_file():
                    try:
                        dest.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(dest / name))
                    except OSError as exc:
                        self.warnings.append(f"archive:{name}:{exc.__class__.__name__}")
        message = f"previous state archived to {dest}"
        log.warning(message, extra={"event": "state_archived"})
        self.warnings.append(message)
        return Ok(dest)

    def _archive_dest(self) -> Path:
        """An unused ``state-archive/<stamp>`` directory for the current clock value."""
        dest = self.archive_dir / archive_stamp(self._clock())
        suffix = 1
        while dest.exists():
            dest = self.archive_dir / f"{archive_stamp(self._clock())}-{suffix}"
            suffix += 1
        return dest

    def _keep_glossary(self, dest: Path) -> None:
        """``--fresh`` keeps ``glossary.json`` in place (user input, A-6) and archives a copy."""
        path = glossary_path(self.output_dir)
        if not path.is_file():
            return
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(dest / path.name))
        except OSError as exc:
            self.warnings.append(f"archive:{path.name}:{exc.__class__.__name__}")
        approved = 0
        loaded = load_glossary_file(path)
        if isinstance(loaded, Ok):
            approved = sum(
                1 for entry in loaded.value.to_entries() if entry.status is GlossaryStatus.APPROVED
            )
        if approved > 0:
            message = (
                f"{path.name} kept in place with {approved} approved entries (copy archived to "
                f"{dest}); edit or delete it if the new job needs a different glossary"
            )
            log.warning(message, extra={"event": "glossary_kept"})
            self.warnings.append(message)
        else:
            log.info(
                "%s kept in place (copy archived to %s)", path.name, dest,
                extra={"event": "glossary_kept"},
            )

    def _bind(
        self,
        input_pdf: Optional[Path],
        *,
        fresh: bool,
        force_unlock: bool,
        keep_artifacts: bool = False,
        archive_names: Sequence[str] = (),
    ) -> Result[_Bound]:
        if self._bound is not None:
            bind_job(self._bound.job.id)
            return Ok(self._bound)
        input_sha: Optional[str] = None
        resolved_input: Optional[str] = None
        if input_pdf is not None:
            hashed = sha256_file(input_pdf)
            if isinstance(hashed, Err):
                return hashed
            input_sha = hashed.value
            # CR-39: the job stores the resolved absolute path, never the path as typed,
            # so a later `export` finds the file from any working directory.
            resolved_input = str(absolute_path(input_pdf))
        if fresh and self.db_path.exists():
            archived = self._archive_state(
                force_unlock=force_unlock,
                keep_artifacts=keep_artifacts,
                extra_names=archive_names,
            )
            if isinstance(archived, Err):
                return archived
        opened = open_database(self.db_path, tool_version=self.tool_version)
        if isinstance(opened, Err):
            return opened
        db = opened.value
        if resolved_input is not None and input_sha is not None:
            job_result: Result[JobRecord] = JobRepository(db).get_or_create_job(
                JobSpec(resolved_input, input_sha, self.tool_version)
            )
        else:
            job_result = self._existing_job(db)
        if isinstance(job_result, Err):
            close_database(db)
            return job_result
        job = job_result.value
        lease = self._acquire_lease(db, job.id, force_unlock)
        if isinstance(lease, Err):
            close_database(db)
            return lease
        holder = lease.value
        if input_sha is not None and job.input_sha256 != input_sha:
            LeaseRepository(db).release(job.id, holder)
            close_database(db)
            return err(
                ErrorCode.STATE_HASH_MISMATCH,
                f"the input file changed since this state was created (state hash "
                f"{job.input_sha256[:12]}…, input hash {input_sha[:12]}…, state input "
                f"{job.input_path}); pass --fresh to archive the old state and start over",
                ErrorScope.USER,
                context={"expected": job.input_sha256, "actual": input_sha},
            )
        bind_job(job.id)
        self.last_job_id = job.id
        if resolved_input is not None and job.input_path != resolved_input:
            job = self._remember_input_path(db, job, resolved_input)
        bound = _Bound(db=db, job=job, holder=holder, lease_held=True)
        bound.keepalive = _LeaseKeepAlive(db, job.id, holder, self._heartbeat_interval)
        bound.keepalive.start()
        if self._composite:
            self._bound = bound
        return Ok(bound)

    def _remember_input_path(self, db: Database, job: JobRecord, resolved: str) -> JobRecord:
        """Record where the hash-verified input lives now (moved file, or a state created
        before CR-39 that stored the path as typed). Never fatal."""
        updated = JobRepository(db).update_job(job.id, JobPatch(input_path=resolved))
        if isinstance(updated, Err):
            log.warning("could not update the stored input path: %s", updated.error.message)
            return job
        log.info(
            "stored input path updated to the verified location",
            extra={"event": "input_path_updated"},
        )
        return dataclasses.replace(job, input_path=resolved)

    def _unbind(self, bound: _Bound) -> None:
        if bound.keepalive is not None:
            bound.keepalive.stop()
            bound.keepalive = None
        if bound.lease_held:
            released = LeaseRepository(bound.db).release(bound.job.id, bound.holder)
            if isinstance(released, Err):
                log.warning("lease release failed: %s", released.error.message)
            bound.lease_held = False
        close_database(bound.db)
        bind_job(None)
        if self._bound is bound:
            self._bound = None

    # -- run records --------------------------------------------------------- #

    def _start_run(
        self,
        bound: _Bound,
        command: str,
        *,
        provider: Optional[str],
        provider_switch_allowed: bool = False,
        mode: Optional[str] = None,
    ) -> Result[None]:
        """Insert the ``runs`` row once per run id. ``mode`` defaults to the mode of the
        composite run (CR-42: `run --mode overlay` starts with the extract stage, which
        must not record the row as ``reflow``)."""
        if self._run_started:
            return Ok(None)
        mode = mode or self._run_mode or MODE_REFLOW
        started = JobRepository(bound.db).start_run(
            bound.job.id,
            self.run_id,
            "run" if self._composite else command,
            self.tool_version,
            provider,
            provider_switch_allowed,
            mode=mode,
        )
        if isinstance(started, Ok):
            self._run_started = True
        return started

    def _finish_run(self, bound: _Bound, outcome: RunOutcome) -> None:
        if not self._run_started or self._run_finished:
            return
        if self._composite:
            self._deferred_outcome = outcome
            return
        finished = JobRepository(bound.db).finish_run(bound.job.id, self.run_id, outcome)
        if isinstance(finished, Err):
            log.warning("finish_run failed: %s", finished.error.message)
        self._run_finished = True

    def _end_stage(self, bound: _Bound, result: Result[Any], exit_code: int) -> None:
        """Close a standalone stage: record the run outcome (if unrecorded) and unbind."""
        if self._composite:
            if isinstance(result, Err):
                self._deferred_outcome = self._deferred_outcome or _zero_outcome(exit_code)
            return
        if self._run_started and not self._run_finished:
            self._finish_run(bound, _zero_outcome(exit_code))
        self._unbind(bound)

    # ------------------------------------------------------------------ #
    # Stage: extract (6.1-6.3)
    # ------------------------------------------------------------------ #

    def extract(
        self,
        input_pdf: Path,
        *,
        extractor_name: Optional[str] = None,
        allow_non_english: bool = False,
        fresh: bool = False,
        min_chars_per_page: int = 50,
        force_unlock: bool = False,
        figure_options: Optional[FigureOptions] = None,
    ) -> Result[ExtractOutcome]:
        try:
            return self._extract(
                Path(input_pdf),
                extractor_name=extractor_name or self.settings.extractor,
                allow_non_english=allow_non_english,
                fresh=fresh,
                min_chars_per_page=min_chars_per_page,
                force_unlock=force_unlock,
                figure_options=figure_options,
            )
        except Exception as exc:  # noqa: BLE001 - boundary
            log.exception("extract crashed", extra={"event": "stage_crashed"})
            return _internal("extract", exc)

    def _extract(
        self,
        input_pdf: Path,
        *,
        extractor_name: str,
        allow_non_english: bool,
        fresh: bool,
        min_chars_per_page: int,
        force_unlock: bool,
        figure_options: Optional[FigureOptions],
    ) -> Result[ExtractOutcome]:
        valid = validate_pdf_path(input_pdf)
        if isinstance(valid, Err):
            return valid
        if figure_options is None:
            resolved = figure_options_from_settings(self.settings)
            if isinstance(resolved, Err):
                return resolved
            figure_options = resolved.value
        bound: Optional[_Bound] = None
        # With existing state (or --fresh) the binding checks run before the expensive
        # extraction; with no state yet the DB is only created after a successful extraction
        # (NO_TEXT_LAYER must not leave a database behind).
        if self._bound is not None or self.db_path.exists() or fresh:
            b = self._bind(input_pdf, fresh=fresh, force_unlock=force_unlock)
            if isinstance(b, Err):
                return b
            bound = b.value
            started = self._start_run(bound, "extract", provider=self.settings.provider)
            if isinstance(started, Err):
                self._end_stage(bound, started, exit_code_for(started.error))
                return started
        result: Result[ExtractOutcome] = err(ErrorCode.INTERNAL, "extract", ErrorScope.JOB_FATAL)
        # CR-55: the sink renders into a staging directory; ``images/`` is only touched
        # after the language and preserve-previous-source checks passed.
        staging = self.output_dir / f"{IMAGES_STAGING_PREFIX}{self.run_id}"
        try:
            old_inventory = self._load_inventory()
            extracted = self._run_extractor(
                input_pdf,
                extractor_name,
                min_chars_per_page,
                figure_options,
                self._figure_sink(staging),
            )
            if isinstance(extracted, Err):
                result = extracted
                return result
            document, used_name, fallback, warnings = extracted.value
            language = check_english(document.markdown, allow_non_english)
            if isinstance(language, Err):
                result = language
                return result
            detected, confidence = language.value
            if detected != "en":
                warnings.append(
                    f"non_english_source:{detected or 'unknown'}:{confidence:.2f}"
                    " (accepted by --allow-non-english)"
                )
            new_bytes = document.markdown.encode("utf-8")
            new_inventory: Optional[FiguresFile] = None
            if figure_options.enabled and document.extractor_name == "pymupdf":
                new_inventory = figures_file_from_document(
                    document, figure_options, extractor=used_name, generated_at=self._clock()
                )
            preserved = self._preserve_previous_source(
                bound, new_bytes, old_inventory, new_inventory
            )
            if isinstance(preserved, Err):
                result = preserved
                return result
            promoted = self._promote_staged_images(staging)
            if isinstance(promoted, Err):
                result = promoted
                return result
            written = atomic_write_bytes(self.source_path, new_bytes)
            if isinstance(written, Err):
                result = written
                return result
            figure_totals: Optional[FigureTotals] = None
            if new_inventory is not None:
                saved = save_figures_file(self.figures_path, new_inventory)
                if isinstance(saved, Err):
                    result = saved
                    return result
                warnings.extend(self._remove_stale_images(new_inventory))
                figure_totals = new_inventory.totals.to_totals()
                log.info(
                    "figures: %d (vector %d, raster %d, mixed %d, %d bytes)",
                    figure_totals.count,
                    figure_totals.vector,
                    figure_totals.raster,
                    figure_totals.mixed,
                    figure_totals.total_bytes,
                    extra={"event": "figures_written", "count": figure_totals.count},
                )
            if document.source_profile is not None:
                profiled = save_source_profile(
                    self.source_profile_path, document.source_profile, extractor=used_name
                )
                if isinstance(profiled, Err):
                    warnings.append(f"source_profile:{profiled.error.message}")
                else:
                    log.info(
                        "source profile: %gx%g pt, body %g pt, margins %s",
                        document.source_profile.page_width_pt,
                        document.source_profile.page_height_pt,
                        document.source_profile.body_font_size_pt,
                        document.source_profile.margins_pt,
                        extra={"event": "source_profile_written"},
                    )
            if bound is None:
                b = self._bind(input_pdf, fresh=False, force_unlock=force_unlock)
                if isinstance(b, Err):
                    result = b
                    return result
                bound = b.value
                started = self._start_run(bound, "extract", provider=self.settings.provider)
                if isinstance(started, Err):
                    result = started
                    return result
            job = bound.job
            patch = JobPatch(
                status="EXTRACTED" if job.status == "CREATED" else UNSET,
                extractor_name=used_name,
                fallback_used=fallback,
                detected_language=detected,
                warnings=tuple(document.warnings) + tuple(warnings),
            )
            updated = JobRepository(bound.db).update_job(job.id, patch)
            if isinstance(updated, Err):
                result = updated
                return result
            log.info(
                "extracted %d pages with %s into %s",
                len(document.pages),
                used_name,
                self.source_path.name,
                extra={"event": "extracted", "chars": len(document.markdown)},
            )
            result = Ok(
                ExtractOutcome(
                    job_id=job.id,
                    source_path=self.source_path,
                    extractor=used_name,
                    fallback_used=fallback,
                    pages=len(document.pages),
                    skipped_pages=sum(1 for p in document.pages if p.skipped),
                    detected_language=detected,
                    language_confidence=confidence,
                    image_count=document.image_count,
                    warnings=list(document.warnings) + warnings + list(self.warnings),
                    figures=figure_totals,
                )
            )
            return result
        except KeyboardInterrupt:
            result = _interrupted()
            raise
        finally:
            # every failure path (and a crash) leaves no staging directory behind
            shutil.rmtree(staging, ignore_errors=True)
            if bound is not None:
                code = EXIT_SUCCESS if isinstance(result, Ok) else exit_code_for(result.error)
                self._end_stage(bound, result, code)

    def _preserve_previous_source(
        self,
        bound: Optional[_Bound],
        new_bytes: bytes,
        old_inventory: Optional[FiguresFile] = None,
        new_inventory: Optional[FiguresFile] = None,
    ) -> Result[None]:
        """Never overwrite a differing ``source_book.md`` silently (E-12 spirit, R4/O1).

        Identical content: nothing to do. Different content while translated chunks exist:
        refuse with ``STATE_HASH_MISMATCH`` (``--fresh`` archives the state first and never
        reaches this point with an existing file). Otherwise the old file is moved to
        ``state-archive/<stamp>/`` and a warning is recorded. v1.1: when the previous
        ``figures.json`` differs from the new inventory it is archived into the same stamp
        directory together with the PNGs the new inventory no longer references (§5.1).
        """
        path = self.source_path
        source_differs = False
        if path.exists():
            try:
                old_bytes = path.read_bytes()
            except OSError as exc:
                return err(
                    ErrorCode.INTERNAL,
                    f"cannot read the existing {SOURCE_MD_NAME}: {exc.__class__.__name__}",
                    ErrorScope.USER,
                    cause=repr(exc),
                )
            source_differs = old_bytes != new_bytes
        inventory_differs = old_inventory is not None and (
            new_inventory is None
            or inventory_signature(old_inventory) != inventory_signature(new_inventory)
        )
        if not source_differs and not inventory_differs:
            return Ok(None)
        if source_differs and bound is not None:
            counted = ChunkRepository(bound.db, self.run_id, None).counts(
                bound.job.id, self._clock()
            )
            if isinstance(counted, Err):
                return counted
            if counted.value.completed > 0:
                return err(
                    ErrorCode.STATE_HASH_MISMATCH,
                    f"{SOURCE_MD_NAME} differs from the new extraction and "
                    f"{counted.value.completed} chunks are already translated; keep the file "
                    "(run `translate`) or start over with --fresh",
                    ErrorScope.USER,
                    context={"completed": counted.value.completed},
                )
        dest = self._archive_dest()
        try:
            dest.mkdir(parents=True, exist_ok=True)
            if source_differs:
                shutil.move(str(path), str(dest / SOURCE_MD_NAME))
            if inventory_differs and old_inventory is not None:
                self._archive_figures(dest, old_inventory, new_inventory)
        except OSError as exc:
            return err(
                ErrorCode.INTERNAL,
                f"cannot archive the previous {SOURCE_MD_NAME} to {dest}: "
                f"{exc.__class__.__name__}",
                ErrorScope.USER,
                cause=repr(exc),
            )
        what = SOURCE_MD_NAME if source_differs else FIGURES_FILE_NAME
        message = f"previous {what} differed from the new extraction; archived to {dest}"
        log.warning(message, extra={"event": "source_archived"})
        self.warnings.append(message)
        return Ok(None)

    def _archive_figures(
        self, dest: Path, old_inventory: FiguresFile, new_inventory: Optional[FiguresFile]
    ) -> None:
        """Move the old ``figures.json`` and the PNGs the new inventory does not reference."""
        if self.figures_path.is_file():
            shutil.move(str(self.figures_path), str(dest / FIGURES_FILE_NAME))
        keep = set(new_inventory.file_names()) if new_inventory is not None else set()
        for name in old_inventory.file_names():
            src = self.images_dir / name
            if name in keep or not src.is_file():
                continue
            (dest / IMAGES_DIR_NAME).mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest / IMAGES_DIR_NAME / name))

    def _load_inventory(self) -> Optional[FiguresFile]:
        """The current ``figures.json`` when present and valid (else ``None`` + warning)."""
        if not self.figures_path.is_file():
            return None
        loaded = load_figures_file(self.figures_path)
        if isinstance(loaded, Err):
            log.warning(
                "ignoring unreadable %s: %s", FIGURES_FILE_NAME, loaded.error.message,
                extra={"event": "figures_unreadable"},
            )
            self.warnings.append(f"figures_unreadable:{loaded.error.message}")
            return None
        return loaded.value

    def _figure_totals(self) -> Optional[FigureTotals]:
        inventory = self._load_inventory()
        return inventory.totals.to_totals() if inventory is not None else None

    def _figure_sink(self, staging: Path) -> FigureSink:
        """Sink writing ``<staging>/<file name>`` atomically (§4.2; E-57 semantics).

        CR-55: nothing under ``images/`` changes until :meth:`_promote_staged_images`
        runs, i.e. after every check that can still refuse the extraction.
        """

        def sink(record: FigureRecord, png: bytes) -> Result[Path]:
            if self.images_dir.exists() and not self.images_dir.is_dir():
                return err(
                    ErrorCode.FIGURE_SINK_FAILED,
                    f"cannot write figure {record.file}: {IMAGES_DIR_NAME} is not a directory",
                    ErrorScope.JOB_FATAL,
                    context={"path": str(self.images_dir)},
                )
            target = staging / record.file.rsplit("/", 1)[-1]
            written = atomic_write_bytes(target, png)
            if isinstance(written, Err):
                return err(
                    ErrorCode.FIGURE_SINK_FAILED,
                    f"cannot write figure {record.file}: {written.error.message}",
                    ErrorScope.JOB_FATAL,
                    context={"path": str(target)},
                    cause=written.error.cause,
                )
            return Ok(target)

        return sink

    def _promote_staged_images(self, staging: Path) -> Result[int]:
        """Move the staged PNGs into ``images/`` (``os.replace`` per file, CR-55)."""
        if not staging.is_dir():
            return Ok(0)
        moved = 0
        current = ""
        try:
            for path in sorted(staging.iterdir()):
                if not path.is_file() or path.name.endswith(".tmp"):
                    continue
                current = path.name
                self.images_dir.mkdir(parents=True, exist_ok=True)
                os.replace(path, self.images_dir / path.name)
                moved += 1
        except OSError as exc:
            return err(
                ErrorCode.FIGURE_SINK_FAILED,
                f"cannot write figure {IMAGES_DIR_NAME}/{current}: {exc.__class__.__name__}",
                ErrorScope.JOB_FATAL,
                context={"path": str(self.images_dir)},
                cause=repr(exc),
            )
        return Ok(moved)

    def _remove_stale_images(self, inventory: FiguresFile) -> List[str]:
        """Delete the tool's own PNGs that the new inventory no longer references (E-35).

        CR-56: only ``pNNN-fKK.png`` / ``pNNN-fKK.tr.png`` names are ever deleted; every
        other file in ``images/`` belongs to the user, stays where it is and is reported as
        ``figure_orphan:<file>`` (CR-62). Returns those warnings.
        """
        warnings: List[str] = []
        if not self.images_dir.is_dir():
            return warnings
        keep = set(inventory.file_names())
        for path in sorted(self.images_dir.iterdir()):
            if not path.is_file() or path.name in keep:
                continue
            translated = TOOL_TRANSLATED_FIGURE_NAME.match(path.name)
            if translated is not None and f"{translated.group(1)}.png" in keep:
                continue  # derived at export from a figure that still exists
            if translated is None and TOOL_FIGURE_NAME.match(path.name) is None:
                warnings.append(f"figure_orphan:{path.name}")
                log.warning(
                    "%s/%s is not referenced by the figure inventory; left untouched",
                    IMAGES_DIR_NAME, path.name,
                    extra={"event": "figure_orphan"},
                )
                continue
            try:
                path.unlink()
            except OSError as exc:
                warnings.append(f"figure_stale:{path.name}:{exc.__class__.__name__}")
                continue
            log.info(
                "removed stale figure %s", path.name, extra={"event": "figure_stale_removed"}
            )
        return warnings

    def _figure_orphans(self, markdown: str) -> List[str]:
        """``figure_orphan:<file>`` for files in ``images/`` that neither the exported
        document nor ``figures.json`` references (design doc 06 5.4, E-33; CR-62)."""
        if not self.images_dir.is_dir():
            return []
        referenced = {
            line.src.rsplit("/", 1)[-1] for line in image_lines(markdown) if line.src
        }
        inventory = self._load_inventory()
        if inventory is not None:
            referenced.update(inventory.file_names())
        orphans: List[str] = []
        for path in sorted(self.images_dir.iterdir()):
            if not path.is_file() or path.name in referenced:
                continue
            translated = TOOL_TRANSLATED_FIGURE_NAME.match(path.name)
            if translated is not None and f"{translated.group(1)}.png" in referenced:
                continue
            orphans.append(f"figure_orphan:{path.name}")
            log.warning(
                "%s/%s is referenced by neither the document nor the inventory",
                IMAGES_DIR_NAME, path.name,
                extra={"event": "figure_orphan"},
            )
        return orphans

    def _run_extractor(
        self,
        input_pdf: Path,
        name: str,
        min_chars_per_page: int,
        figure_options: Optional[FigureOptions] = None,
        figure_sink: Optional[FigureSink] = None,
    ) -> Result[Tuple[ExtractedDocument, str, bool, List[str]]]:
        """Resolve and run the extractor with the marker -> pymupdf fallback (6.2)."""
        options = ExtractOptions(
            min_chars_per_page=min_chars_per_page,
            figures=figure_options if figure_options is not None else FigureOptions(),
        )
        warnings: List[str] = []
        fallback = False
        got = self._extractor_factory(name)
        if isinstance(got, Err):
            if name == "marker" and got.error.code is ErrorCode.EXTRACTOR_UNAVAILABLE:
                log.warning(
                    "marker extractor unavailable (%s); falling back to pymupdf",
                    got.error.message,
                    extra={"event": "extractor_fallback"},
                )
                warnings.append("extractor_fallback:marker_unavailable")
                fallback = True
                got = self._extractor_factory("pymupdf")
                if isinstance(got, Err):
                    return got
            else:
                return got
        extractor = got.value
        document = extractor.extract(input_pdf, options, figure_sink=figure_sink)
        if (
            isinstance(document, Err)
            and not fallback
            and name == "marker"
            and document.error.code is ErrorCode.EXTRACTION_FAILED
        ):
            log.warning(
                "marker extraction failed (%s); falling back to pymupdf",
                document.error.message,
                extra={"event": "extractor_fallback"},
            )
            warnings.append("extractor_fallback:marker_failed")
            fallback = True
            got = self._extractor_factory("pymupdf")
            if isinstance(got, Err):
                return got
            extractor = got.value
            document = extractor.extract(input_pdf, options, figure_sink=figure_sink)
        if isinstance(document, Err):
            return document
        doc = document.value
        return Ok((doc, extractor.name, fallback or doc.fallback_used, warnings))

    # ------------------------------------------------------------------ #
    # Stage: glossary (7.1-7.3)
    # ------------------------------------------------------------------ #

    def glossary(
        self,
        *,
        min_count: int = 3,
        force: bool = False,
        user_glossary: Optional[Path] = None,
    ) -> Result[GlossaryOutcome]:
        try:
            if not self.source_path.exists():
                return err(
                    ErrorCode.STATE_NOT_FOUND,
                    f"{SOURCE_MD_NAME} not found in {self.output_dir}; run `extract` first",
                    ErrorScope.USER,
                )
            markdown = self.source_path.read_text(encoding="utf-8")
            path = glossary_path(self.output_dir)
            existed = path.exists()
            built = build_or_update(self.output_dir, markdown, force=force, min_count=min_count)
            if isinstance(built, Err):
                return built
            entries: List[GlossaryEntry] = built.value.to_entries()
            if user_glossary is not None:
                user = load_user_glossary(Path(user_glossary))
                if isinstance(user, Err):
                    return user
                entries = merge_with_precedence(entries, user.value)
            by_status = {status: 0 for status in GlossaryStatus}
            for entry in entries:
                by_status[entry.status] += 1
            written = force or not existed
            log.info(
                "glossary %s: %d entries (%d approved)",
                "written" if written else "kept",
                len(entries),
                by_status[GlossaryStatus.APPROVED],
                extra={"event": "glossary_built"},
            )
            return Ok(
                GlossaryOutcome(
                    path=path,
                    entries=len(entries),
                    approved=by_status[GlossaryStatus.APPROVED],
                    proposed=by_status[GlossaryStatus.PROPOSED],
                    ambiguous=by_status[GlossaryStatus.AMBIGUOUS],
                    rejected=by_status[GlossaryStatus.REJECTED],
                    written=written,
                )
            )
        except Exception as exc:  # noqa: BLE001 - boundary
            log.exception("glossary stage crashed", extra={"event": "stage_crashed"})
            return _internal("glossary", exc)

    def _load_effective_glossary(
        self, user_glossary: Optional[Path]
    ) -> Result[EffectiveGlossary]:
        entries: List[GlossaryEntry] = []
        path = glossary_path(self.output_dir)
        if path.exists():
            loaded = load_glossary_file(path)
            if isinstance(loaded, Err):
                return loaded
            entries = loaded.value.to_entries()
        if user_glossary is not None:
            user = load_user_glossary(Path(user_glossary))
            if isinstance(user, Err):
                return user
            entries = merge_with_precedence(entries, user.value)
        return Ok(effective_glossary(entries))

    # ------------------------------------------------------------------ #
    # Stage: translate (5.1-5.8)
    # ------------------------------------------------------------------ #

    async def translate(
        self,
        input_pdf: Path,
        *,
        provider: Optional[str] = None,
        fallback_provider: Optional[str] = None,
        user_glossary: Optional[Path] = None,
        retry_failed: bool = False,
        allow_provider_switch: bool = False,
        require_glossary: bool = False,
        strict_glossary: bool = False,
        dry_run: bool = False,
        limit: Optional[int] = None,
        force_unlock: bool = False,
        fresh: bool = False,
        mode: str = MODE_REFLOW,
        overlay_options: Optional[OverlayOptions] = None,
    ) -> Result[JobSummary]:
        try:
            valid = self.settings.validate_for_provider()
            if isinstance(valid, Err):
                return valid
            if mode not in (MODE_REFLOW, MODE_OVERLAY):
                return err(
                    ErrorCode.PROVIDER_CONFIG,
                    f"unknown mode {mode!r}; use reflow or overlay",
                    ErrorScope.USER,
                )
            if mode == MODE_REFLOW and not self.source_path.exists():
                return err(
                    ErrorCode.STATE_NOT_FOUND,
                    f"{SOURCE_MD_NAME} not found in {self.output_dir}; run `extract` first",
                    ErrorScope.USER,
                )
            if mode == MODE_OVERLAY:
                valid_pdf = validate_pdf_path(Path(input_pdf))
                if isinstance(valid_pdf, Err):
                    return valid_pdf
            b = self._bind(
                Path(input_pdf),
                fresh=fresh,
                force_unlock=force_unlock,
                keep_artifacts=True,
                # CR-46: the archived database held the overlay pass these files belong to
                archive_names=OVERLAY_PASS_ARTIFACTS,
            )
            if isinstance(b, Err):
                return b
            bound = b.value
            result: Result[JobSummary] = err(
                ErrorCode.INTERNAL, "translate", ErrorScope.JOB_FATAL
            )
            try:
                result = await self._translate_bound(
                    bound,
                    provider=provider or self.settings.provider,
                    fallback_provider=(
                        fallback_provider or self.settings.effective_fallback_provider()
                    ),
                    user_glossary=user_glossary,
                    retry_failed=retry_failed,
                    allow_provider_switch=allow_provider_switch,
                    require_glossary=require_glossary,
                    strict_glossary=strict_glossary,
                    dry_run=dry_run,
                    limit=limit,
                    mode=mode,
                    overlay_options=overlay_options or OverlayOptions(),
                    input_pdf=Path(input_pdf),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - boundary
                log.exception("translate crashed", extra={"event": "stage_crashed"})
                result = _internal("translate", exc)
            finally:
                code = result.value.exit_code if isinstance(result, Ok) else exit_code_for(
                    result.error
                )
                self._end_stage(bound, result, code)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - boundary
            log.exception("translate crashed", extra={"event": "stage_crashed"})
            return _internal("translate", exc)

    async def _translate_bound(
        self,
        bound: _Bound,
        *,
        provider: str,
        fallback_provider: Optional[str],
        user_glossary: Optional[Path],
        retry_failed: bool,
        allow_provider_switch: bool,
        require_glossary: bool,
        strict_glossary: bool,
        dry_run: bool,
        limit: Optional[int],
        mode: str,
        overlay_options: OverlayOptions,
        input_pdf: Path,
    ) -> Result[JobSummary]:
        started_at = self._clock()
        started_mono = time.monotonic()
        warnings: List[str] = list(self.warnings)
        db = bound.db
        job = bound.job
        jobs = JobRepository(db)
        overlay = mode == MODE_OVERLAY

        # 4b. E-49: an overlay pass needs a modifiable PDF (before any provider contact).
        if overlay:
            modifiable = check_modifiable(input_pdf)
            if isinstance(modifiable, Err):
                return modifiable

        # 5. provider binding
        made = self._translator_factory(provider, self.settings)
        if isinstance(made, Err):
            return made
        translator = made.value
        try:
            # ``provider_name`` stays the *primary*: it is the job's identity (the
            # --allow-provider-switch check below and jobs.provider). Enabling the
            # fallback must not look like a provider switch.
            provider_name = translator.name
            if fallback_provider is not None and fallback_provider == provider_name:
                warnings.append(f"fallback_provider_ignored:{fallback_provider}")
                log.warning(
                    "--fallback-provider %s is the primary provider; no fallback armed",
                    fallback_provider,
                    extra={"event": "provider_fallback_ignored"},
                )
            elif fallback_provider is not None:
                wrapped = self._wrap_with_fallback(translator, fallback_provider)
                if isinstance(wrapped, Err):
                    return wrapped
                translator = wrapped.value
                warnings.append(f"provider_fallback_armed:{provider_name}->{fallback_provider}")
                log.info(
                    "automatic fallback armed: %s -> %s on an exhausted quota",
                    provider_name,
                    fallback_provider,
                    extra={"event": "provider_fallback_armed"},
                )
            if job.provider and job.provider != provider_name:
                if not allow_provider_switch:
                    return err(
                        ErrorCode.STATE_PROVIDER_MISMATCH,
                        f"this state was created with provider {job.provider!r}, now "
                        f"{provider_name!r} is selected; mixing providers may reduce "
                        "consistency - pass --allow-provider-switch to continue",
                        ErrorScope.USER,
                        context={"job_provider": job.provider, "selected": provider_name},
                    )
                warnings.append(f"provider_switch:{job.provider}->{provider_name}")
                log.warning(
                    "provider switched from %s to %s (--allow-provider-switch)",
                    job.provider,
                    provider_name,
                    extra={"event": "provider_switch"},
                )
            started = self._start_run(
                bound,
                "translate",
                provider=provider_name,
                provider_switch_allowed=allow_provider_switch,
                mode=mode,
            )
            if isinstance(started, Err):
                return started

            # 6. glossary
            loaded = self._load_effective_glossary(user_glossary)
            if isinstance(loaded, Err):
                return loaded
            glossary = loaded.value
            if not glossary.entries:
                if require_glossary:
                    return err(
                        ErrorCode.GLOSSARY_INVALID,
                        "the effective glossary has 0 approved terms and --require-glossary "
                        "is set; approve entries in glossary.json or pass --glossary",
                        ErrorScope.USER,
                    )
                warnings.append("glossary:0 approved terms")
                log.warning("glossary has 0 approved terms", extra={"event": "glossary_empty"})
            binding: Optional[GlossaryBinding] = None
            strategy = GlossaryStrategy.NONE
            if not dry_run:
                prepared = await translator.prepare(glossary, self.run_id)
                if isinstance(prepared, Err):
                    return prepared
                binding = prepared.value
                strategy = binding.strategy
                log.info(
                    "glossary bound: %d entries, strategy %s",
                    len(glossary.entries),
                    strategy.value,
                    extra={"event": "glossary_bound"},
                )
            glossary_hash: Optional[str] = glossary.glossary_hash if glossary.entries else None
            protect_mode: ProtectMode = (
                "xml" if translator.capabilities.supports_tag_protection else "sentinel"
            )
            capabilities = translator.capabilities

            # 7. units: chunking (reflow) or overlay unit extraction / binding check
            adapter: UnitAdapter[Any]
            if not overlay:
                chunks = self._chunk_repo_factory(db, self.run_id, glossary_hash)
                self._warn_glossary_changed(chunks, job.id, glossary_hash, warnings)
                chunked = self._ensure_chunks(bound, jobs, chunks, translator, warnings)
                if isinstance(chunked, Err):
                    return chunked
                adapter = ChunkUnitAdapter(chunks, protect_mode, capabilities.supports_context)
            else:
                prepared_units = await self._prepare_overlay_units(
                    bound, jobs, input_pdf, overlay_options, glossary_hash, warnings
                )
                if isinstance(prepared_units, Err):
                    return prepared_units
                overlay_repo = prepared_units.value
                self._warn_glossary_changed(overlay_repo, job.id, glossary_hash, warnings)
                adapter = OverlayUnitAdapter(
                    overlay_repo,
                    protect_mode,
                    supports_context=capabilities.supports_context,
                    max_texts_per_request=capabilities.max_texts_per_request,
                    min_chars=self.settings.overlay_batch_min_chars,
                    context_chars=self.settings.overlay_context_chars,
                )
            repo = adapter.repo
            patched = jobs.update_job(
                job.id,
                JobPatch(
                    provider=provider_name,
                    glossary_strategy=strategy.value,
                    provider_glossary_id=binding.provider_glossary_id if binding else None,
                    glossary_hash=glossary_hash,
                ),
            )
            if isinstance(patched, Err):
                return patched

            # 7b. --retry-failed, re-queue orphans
            if retry_failed:
                reset = repo.reset_failed(job.id)
                if isinstance(reset, Err):
                    return reset
                log.info("reset %d FAILED units to PENDING", reset.value,
                         extra={"event": "failed_reset"})
            requeued = repo.requeue_processing(job.id)
            if isinstance(requeued, Err):
                return requeued
            if requeued.value:
                log.warning(
                    "re-queued %d units left in PROCESSING by a previous run",
                    requeued.value,
                    extra={"event": "requeued"},
                )
            counted = repo.counts(job.id, self._clock())
            if isinstance(counted, Err):
                return counted
            counts = counted.value

            if dry_run:
                return self._dry_run(
                    bound, adapter, jobs, counts, glossary, provider_name, strategy,
                    started_at, started_mono, warnings, mode,
                )

            # 8. status TRANSLATING + run
            running_patch = (
                JobPatch(overlay_status="OVERLAY_TRANSLATING")
                if overlay
                else JobPatch(status="TRANSLATING")
            )
            patched = jobs.update_job(job.id, running_patch)
            if isinstance(patched, Err):
                return patched
            limiter = AdaptiveLimiter(
                self.settings.concurrency, on_change=self._on_limiter_change
            )
            policy = BackoffPolicy(
                base_s=self.settings.backoff_base_s,
                cap_s=self.settings.backoff_cap_s,
                max_retries=self.settings.max_retries,
                rng=self._rng,
            )
            ctx = _TranslateContext(
                bound=bound,
                translator=translator,
                binding=binding,
                glossary=glossary,
                repo=repo,
                adapter=adapter,
                jobs=jobs,
                limiter=limiter,
                policy=policy,
                lock=asyncio.Lock(),
                strict_glossary=strict_glossary,
                base_counts=counts,
                started=started_mono,
                mode=protect_mode,
                pause_patch=(
                    JobPatch(overlay_status="OVERLAY_PAUSED")
                    if overlay
                    else JobPatch(status="PAUSED")
                ),
            )
            log.info(
                "translating %d pending %s units (%d completed, %d failed) with %s x%d",
                counts.pending,
                mode,
                counts.completed,
                counts.failed,
                provider_name,
                self.settings.concurrency,
                extra={"event": "translate_start", "mode": mode},
            )
            await self._run_job(ctx, limit)

            final = repo.counts(job.id, self._clock())
            if isinstance(final, Err):
                return final
            fc = final.value
            if ctx.lease_lost is not None:
                exit_code = EXIT_LOCKED
                warnings.append(f"lease_lost:{ctx.lease_lost.message}")
            elif ctx.db_error is not None:
                exit_code = EXIT_FAILURE
                warnings.append(f"state_error:{ctx.db_error.message}")
            elif ctx.pause_error is not None:
                exit_code = EXIT_PAUSED
                warnings.append(f"paused:{ctx.pause_error.message}")
            elif self.cancel.is_set():
                exit_code = EXIT_INTERRUPTED
                warnings.append("interrupted by the user; state intact, re-run to resume")
            elif fc.processing > 0:
                # A release/complete write must have failed: never report success (NFR-02).
                exit_code = EXIT_FAILURE
                warnings.append(
                    f"state_error:{fc.processing} units are still PROCESSING after the run"
                )
            elif fc.failed > 0:
                exit_code = EXIT_PARTIAL
            else:
                exit_code = EXIT_SUCCESS
                if fc.pending == 0:
                    done_patch = (
                        JobPatch(overlay_status="OVERLAY_TRANSLATED")
                        if overlay
                        else JobPatch(status="TRANSLATED")
                    )
                    jobs.update_job(job.id, done_patch)
            outcome = RunOutcome(
                outcome=outcome_name(exit_code),
                exit_code=exit_code,
                chunks_completed=ctx.stats.completed,
                chunks_failed=ctx.stats.failed,
                chars_sent=ctx.stats.chars_sent,
                provider_calls=ctx.stats.provider_calls,
                rate_limited_count=ctx.stats.rate_limited,
            )
            summary = self._build_summary(
                bound,
                adapter,
                jobs,
                counts=fc,
                exit_code=exit_code,
                chars_sent_this_run=ctx.stats.chars_sent,
                glossary=glossary,
                provider=provider_name,
                strategy=strategy,
                started_at=started_at,
                started_mono=started_mono,
                warnings=warnings,
                mode=mode,
            )
            self._finish_run(bound, outcome)
            self._write_summary(summary)
            log.info(
                "run finished: %s (exit %d) completed=%d failed=%d chars_sent=%d by %s",
                outcome.outcome,
                exit_code,
                ctx.stats.completed,
                ctx.stats.failed,
                ctx.stats.chars_sent,
                describe_provider_units(ctx.provider_units) or provider_name,
                extra={"event": "run_finished", "chars": ctx.stats.chars_sent},
            )
            return Ok(summary)
        finally:
            with suppress(Exception):
                await translator.aclose()

    def _wrap_with_fallback(
        self, primary: BaseTranslator, secondary_name: str
    ) -> Result[BaseTranslator]:
        """Compose ``FallbackTranslator(primary, secondary)`` (5.3, user decision Q-fb).

        The secondary is built through the same factory as the primary, so it goes
        through its own fail-fast ``create`` (key present, packages installed) *before*
        the run starts: discovering at the moment the quota dies that the fallback cannot
        be constructed either would be the worst possible time.
        """
        made = self._translator_factory(secondary_name, self.settings)
        if isinstance(made, Err):
            error = made.error
            return err(
                error.code,
                f"--fallback-provider {secondary_name}: {error.message}",
                ErrorScope.USER,
                context=dict(error.context),
                cause=error.cause,
            )
        return Ok(FallbackTranslator(primary, made.value))

    def _warn_glossary_changed(
        self,
        repo: UnitRepository[Any],
        job_id: str,
        glossary_hash: Optional[str],
        warnings: List[str],
    ) -> None:
        if glossary_hash is None:
            return
        other = repo.count_completed_with_other_glossary(job_id, glossary_hash)
        if isinstance(other, Ok) and other.value > 0:
            message = (
                f"consistency risk: {other.value} units were translated with a "
                "previous glossary (re-run with --fresh for full consistency)"
            )
            warnings.append(message)
            log.warning(message, extra={"event": "glossary_changed"})

    def _ensure_chunks(
        self,
        bound: _Bound,
        jobs: JobRepository,
        chunks: ChunkRepository,
        translator: BaseTranslator,
        warnings: List[str],
    ) -> Result[None]:
        """v1 step 7: chunk ``source_book.md`` when the job has no chunks yet (O1)."""
        job = bound.job
        markdown = self.source_path.read_text(encoding="utf-8")
        md_sha = sha256_text(markdown)
        counted = chunks.counts(job.id, self._clock())
        if isinstance(counted, Err):
            return counted
        counts = counted.value
        need_chunk = job.source_md_sha256 is None or counts.total == 0
        if not need_chunk and md_sha != job.source_md_sha256:
            if counts.completed == 0:
                need_chunk = True
                warnings.append("source_book.md changed; re-chunked (no completed chunks)")
            else:
                return err(
                    ErrorCode.STATE_HASH_MISMATCH,
                    f"{SOURCE_MD_NAME} changed after {counts.completed} chunks were "
                    "translated; restore the file or start over with --fresh",
                    ErrorScope.USER,
                )
        if not need_chunk:
            return Ok(None)
        chunked = chunk_markdown(
            markdown,
            min_chars=self.settings.chunk_min,
            max_chars=self.settings.chunk_max,
            provider_limit=translator.capabilities.max_chars_per_request,
        )
        if isinstance(chunked, Err):
            return chunked
        warnings.extend(f"chunker:{w}" for w in chunked.value.warnings)
        patched = jobs.update_job(
            job.id,
            JobPatch(
                source_md_sha256=md_sha,
                chunk_min_chars=self.settings.chunk_min,
                chunk_max_chars=self.settings.chunk_max,
                status="CHUNKED",
            ),
        )
        if isinstance(patched, Err):
            return patched
        replaced = chunks.replace_all(job.id, chunked.value.chunks)
        if isinstance(replaced, Err):
            return replaced
        log.info(
            "chunked %s into %d chunks", SOURCE_MD_NAME, replaced.value,
            extra={"event": "chunked", "chars": len(markdown)},
        )
        return Ok(None)

    def _figure_regions(self) -> Optional[Dict[int, List[BBox]]]:
        """Figure rects per page from ``figures.json`` (labels become FIGURE_LABEL units).

        ``None`` without an inventory: the overlay extractor then detects the regions itself
        with the same detector, so a fresh ``translate --mode overlay`` behaves like one
        that follows ``extract``.
        """
        inventory = self._load_inventory()
        if inventory is None:
            return None
        regions: Dict[int, List[BBox]] = {}
        for record in inventory.records():
            regions.setdefault(record.page, []).append(record.region)
        return regions

    async def _prepare_overlay_units(
        self,
        bound: _Bound,
        jobs: JobRepository,
        input_pdf: Path,
        options: OverlayOptions,
        glossary_hash: Optional[str],
        warnings: List[str],
    ) -> Result[OverlayBlockRepository]:
        """§5.5: extract the unit set once, else verify the pass binding (``overlay_sha256``).

        The extraction runs in a worker thread (CR-49): the event loop stays responsive to
        Ctrl+C and the lease keep-alive of the binding keeps beating while a long book is
        read. CR-45: rows without a binding (a crash between ``replace_all`` and the job
        patch) are replaced instead of locking the pass at exit 5.
        """
        db = bound.db
        job = bound.job
        if db.schema_version < 2:
            return err(
                ErrorCode.STATE_SCHEMA_INCOMPATIBLE,
                f"the state database has schema v{db.schema_version}; the overlay pass needs "
                "v2 (re-open the state for writing to migrate)",
                ErrorScope.USER,
            )
        repo = self._overlay_repo_factory(db, self.run_id, glossary_hash)
        counted = repo.counts(job.id, self._clock())
        if isinstance(counted, Err):
            return counted
        unbound = job.overlay_status == "NONE" or job.overlay_sha256 is None
        if counted.value.total > 0 and unbound and counted.value.completed == 0:
            # doc 07 3.3: "NONE with rows present -> re-extract and replace". Nothing was
            # translated yet (replace_all itself refuses when COMPLETED units exist).
            warnings.append("overlay:unbound_units_replaced")
            log.warning(
                "overlay units without a pass binding found (interrupted extraction); "
                "re-extracting",
                extra={"event": "overlay_unbound_units_replaced", "units": counted.value.total},
            )
        if counted.value.total == 0 or (unbound and counted.value.completed == 0):
            figure_options = figure_options_from_settings(self.settings)
            if isinstance(figure_options, Err):
                return figure_options
            regions = self._figure_regions() if figure_options.value.enabled else {}
            extracted = await asyncio.to_thread(
                functools.partial(
                    extract_overlay, input_pdf, options, regions,
                    figure_options=figure_options.value,
                )
            )
            if isinstance(extracted, Err):
                return extracted
            extraction = extracted.value
            replaced = repo.replace_all(job.id, extraction.blocks, pages=extraction.pages)
            if isinstance(replaced, Err):
                return replaced
            patched = jobs.update_job(
                job.id,
                JobPatch(
                    overlay_status="OVERLAY_EXTRACTED",
                    overlay_sha256=extraction.overlay_sha256,
                    overlay_translate_headers=options.translate_headers,
                    overlay_keep_figure_text=options.keep_figure_text,
                    overlay_tool_version=self.tool_version,
                    overlay_unit_count=len(extraction.blocks),
                ),
            )
            if isinstance(patched, Err):
                return patched
            warnings.extend(f"overlay:{w}" for w in extraction.warnings)
            skipped = [p.page for p in extraction.pages if p.skip_reason]
            if skipped:
                warnings.append(f"overlay:pages_without_text_layer:{len(skipped)}")
            # ``overlay_extracted`` itself is logged once, by the extractor (CR-48 d).
            log.info(
                "overlay units stored: %d units on %d pages (%d translatable, %d skipped)",
                len(extraction.blocks),
                len(extraction.pages),
                sum(1 for b in extraction.blocks if b.translate),
                len(skipped),
                extra={"event": "overlay_units_stored", "units": len(extraction.blocks)},
            )
            return Ok(repo)
        if (
            job.overlay_translate_headers != options.translate_headers
            or job.overlay_keep_figure_text != options.keep_figure_text
        ):
            return err(
                ErrorCode.STATE_HASH_MISMATCH,
                "the overlay pass was started with --overlay-translate-headers="
                f"{'on' if job.overlay_translate_headers else 'off'} and "
                f"--overlay-keep-figure-text={'on' if job.overlay_keep_figure_text else 'off'}; "
                "keep those flags or start over with --fresh",
                ErrorScope.USER,
            )
        try:
            signature = overlay_signature(list(repo.iter_ordered(job.id)), options)
        except Exception as exc:  # noqa: BLE001 - iterator boundary (D4)
            return err(
                ErrorCode.STATE_CORRUPT,
                "cannot read the overlay units from the state database",
                ErrorScope.JOB_FATAL,
                cause=repr(exc)[:300],
            )
        if signature != job.overlay_sha256:
            return err(
                ErrorCode.STATE_HASH_MISMATCH,
                "the overlay unit set no longer matches the one this pass was started with "
                f"(state {str(job.overlay_sha256)[:12]}…, now {signature[:12]}…); start over "
                "with --fresh",
                ErrorScope.USER,
                context={"expected": job.overlay_sha256, "actual": signature},
            )
        return Ok(repo)

    def _dry_run(
        self,
        bound: _Bound,
        adapter: UnitAdapter[Any],
        jobs: JobRepository,
        counts: StatusCounts,
        glossary: EffectiveGlossary,
        provider_name: str,
        strategy: GlossaryStrategy,
        started_at: datetime,
        started_mono: float,
        warnings: List[str],
        mode: str = MODE_REFLOW,
    ) -> Result[JobSummary]:
        """Report what a run would send without contacting the provider."""
        pending = 0
        chars = 0
        try:
            for unit in adapter.repo.iter_ordered(bound.job.id):
                status = getattr(unit, "status", ChunkStatus.PENDING)
                if status is not ChunkStatus.PENDING or not adapter.is_translatable(unit):
                    continue
                pending += 1
                chars += adapter.dry_run_chars(unit)
        except Exception as exc:  # noqa: BLE001 - iterator boundary (D4)
            return err(
                ErrorCode.STATE_CORRUPT,
                "cannot read units for the dry run",
                ErrorScope.JOB_FATAL,
                cause=repr(exc)[:300],
            )
        noun = "chunks" if mode == MODE_REFLOW else "overlay units"
        message = (
            f"dry_run: {pending} {noun} pending, {chars} payload characters would be sent "
            f"to {provider_name} ({counts.completed} already completed)"
        )
        warnings.append(message)
        log.info(message, extra={"event": "dry_run", "chars": chars})
        summary = self._build_summary(
            bound,
            adapter,
            jobs,
            counts=counts,
            exit_code=EXIT_SUCCESS,
            chars_sent_this_run=0,
            glossary=glossary,
            provider=provider_name,
            strategy=strategy,
            started_at=started_at,
            started_mono=started_mono,
            warnings=warnings,
            mode=mode,
        )
        self._finish_run(bound, _zero_outcome(EXIT_SUCCESS))
        self._write_summary(summary)
        return Ok(summary)

    def _overlay_summary(
        self,
        job: JobRecord,
        repo: OverlayBlockRepository,
        counts: StatusCounts,
        *,
        review_path: Optional[Path] = None,
        placed: Tuple[int, int, int, int] = (0, 0, 0, 0),
    ) -> OverlaySummary:
        progress = repo.page_progress(job.id)
        pages = repo.pages(job.id)
        totals = repo.totals(job.id)
        failed = repo.failed_ids(job.id)
        review = repo.review_ids(job.id)
        translatable = totals.value.translatable_count if isinstance(totals, Ok) else 0
        return OverlaySummary(
            status=job.overlay_status,
            counts=counts,
            pages_completed=progress.value[0] if isinstance(progress, Ok) else 0,
            pages_total=progress.value[1] if isinstance(progress, Ok) else 0,
            pages_skipped=(
                sum(1 for p in pages.value if p.skip_reason) if isinstance(pages, Ok) else 0
            ),
            translatable_units=translatable,
            kept_units=max(0, counts.total - translatable),
            overlay_sha256=job.overlay_sha256,
            failed_unit_ids=failed.value if isinstance(failed, Ok) else [],
            review_unit_ids=review.value if isinstance(review, Ok) else [],
            placed=placed[0],
            shrunk_below_threshold=placed[1],
            could_not_fit=placed[2],
            kept_original=placed[3],
            review_path=str(review_path) if review_path is not None else None,
        )

    def _build_summary(
        self,
        bound: _Bound,
        adapter: UnitAdapter[Any],
        jobs: JobRepository,
        *,
        counts: StatusCounts,
        exit_code: int,
        chars_sent_this_run: int,
        glossary: EffectiveGlossary,
        provider: Optional[str],
        strategy: Optional[GlossaryStrategy],
        started_at: datetime,
        started_mono: float,
        warnings: List[str],
        outputs: Optional[Dict[str, str]] = None,
        mode: str = MODE_REFLOW,
    ) -> JobSummary:
        job = bound.job
        refreshed = jobs.get_job(job.id)
        if isinstance(refreshed, Ok) and refreshed.value is not None:
            job = refreshed.value
        repo = adapter.repo
        totals = repo.totals(job.id)
        failed_ids = repo.failed_ids(job.id)
        review_ids = repo.review_ids(job.id)
        # Job-wide, read back from the ``provider`` column, so a resumed job keeps the
        # units an earlier run's fallback translated.
        providers = repo.provider_counts(job.id)
        finished_at = self._clock()
        overlay: Optional[OverlaySummary] = None
        if mode == MODE_OVERLAY and isinstance(repo, OverlayBlockRepository):
            overlay = self._overlay_summary(job, repo, counts)
        return JobSummary(
            job_id=job.id,
            run_id=self.run_id,
            command="run" if self._composite else "translate",
            input_path=job.input_path,
            input_sha256=job.input_sha256,
            source_md_sha256=job.source_md_sha256,
            provider=provider,
            glossary_strategy=strategy,
            glossary_entries_applied=len(glossary.entries),
            extractor=job.extractor_name,
            fallback_used=job.fallback_used,
            counts=counts,
            failed_chunk_ids=failed_ids.value if isinstance(failed_ids, Ok) else [],
            review_chunk_ids=review_ids.value if isinstance(review_ids, Ok) else [],
            chars_sent_this_run=chars_sent_this_run,
            chars_sent_total=totals.value.chars_sent if isinstance(totals, Ok) else 0,
            started_at=started_at,
            finished_at=finished_at,
            duration_s=round(time.monotonic() - started_mono, 3),
            outputs=dict(outputs or {}),
            exit_code=exit_code,
            warnings=list(warnings),
            figures=self._figure_totals(),
            mode=mode,
            overlay=overlay,
            provider_units=providers.value if isinstance(providers, Ok) else {},
        )

    def _write_summary(self, summary: JobSummary) -> Optional[Path]:
        payload = json.dumps(
            summary_to_jsonable(summary), indent=2, ensure_ascii=False
        ) + "\n"
        written = atomic_write_bytes(self.summary_path, payload.encode("utf-8"))
        if isinstance(written, Err):
            log.warning("could not write %s: %s", SUMMARY_NAME, written.error.message)
            return None
        return self.summary_path

    # -- 5.2 async runner (generalised over UnitAdapter, design doc 06 3.6.2) ------ #

    def _on_limiter_change(self, event: str, permits: int) -> None:
        self.limiter_events.append((event, permits))
        log.info("limiter %s: permits now %d", event, permits, extra={"event": "limiter_changed"})

    async def _db(self, lock: asyncio.Lock, fn: Callable[..., T], *args: Any) -> T:
        async with lock:
            return await asyncio.to_thread(fn, *args)

    async def _wait_for_cancel(self, awaitable_task: "asyncio.Task[Any]") -> None:
        """Await ``awaitable_task`` unless ``cancel`` fires first."""
        cancel_task = asyncio.create_task(self.cancel.wait())
        try:
            await asyncio.wait({awaitable_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (awaitable_task, cancel_task):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task

    async def _wait_until(self, when: datetime) -> None:
        delay = (when - self._clock()).total_seconds()
        if delay <= 0:
            return
        log.info(
            "all pending units are waiting for backoff; sleeping %.1fs", delay,
            extra={"event": "backoff_wait"},
        )
        await self._wait_for_cancel(asyncio.create_task(self._sleep_coro(delay)))

    async def _sleep_coro(self, delay: float) -> None:
        await self._sleep(delay)

    async def _wait_any(self, in_flight: Dict["asyncio.Task[None]", List[int]]) -> None:
        cancel_task = asyncio.create_task(self.cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {*in_flight, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cancel_task
        self._reap(in_flight, done)

    def _reap(
        self,
        in_flight: Dict["asyncio.Task[None]", List[int]],
        done: Iterable["asyncio.Task[Any]"],
    ) -> None:
        for task in done:
            unit_ids = in_flight.pop(task, None)
            if unit_ids is None or task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                log.error(
                    "worker for unit %d raised %s", unit_ids[0], type(exc).__name__,
                    extra={"event": "worker_crashed", "chunk_id": unit_ids[0]},
                )

    async def _run_job(self, ctx: _TranslateContext, limit: Optional[int]) -> None:
        job_id = ctx.bound.job.id
        heartbeat = asyncio.create_task(self._heartbeat_loop(ctx))
        in_flight: Dict[asyncio.Task[None], List[int]] = {}
        try:
            while True:
                if (
                    self.cancel.is_set()
                    or ctx.pause_error is not None
                    or ctx.lease_lost is not None
                    or ctx.db_error is not None
                ):
                    break
                free = ctx.limiter.permits - len(in_flight)
                unit_cap = None if limit is None else limit - ctx.stats.claimed
                claimed: List[Any] = []
                if free > 0 and (unit_cap is None or unit_cap > 0):
                    got = await self._db(
                        ctx.lock, ctx.adapter.claim, job_id, free, unit_cap, self._clock(),
                        self.run_id,
                    )
                    if isinstance(got, Err):
                        ctx.db_error = got.error
                        log.error("claim failed: %s", got.error.message,
                                  extra={"event": "claim_failed"})
                        break
                    claimed = got.value
                    ctx.stats.claimed += len(claimed)
                    for batch in ctx.adapter.group(claimed):
                        task = asyncio.create_task(self._worker(ctx, batch))
                        in_flight[task] = [ctx.adapter.unit_id(u) for u in batch]
                if claimed:
                    continue
                if not in_flight:
                    if limit is not None and ctx.stats.claimed >= limit:
                        break
                    earliest = await self._db(ctx.lock, ctx.repo.earliest_next_attempt, job_id)
                    if isinstance(earliest, Err):
                        ctx.db_error = earliest.error
                        break
                    if earliest.value is None:
                        break
                    await self._wait_until(earliest.value)
                    continue
                await self._wait_any(in_flight)
        finally:
            try:
                # The heartbeat keeps the lease alive while in-flight units drain (5.7).
                await self._drain(ctx, in_flight)
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await heartbeat

    async def _drain(
        self, ctx: _TranslateContext, in_flight: Dict["asyncio.Task[None]", List[int]]
    ) -> None:
        """5.7: let in-flight work finish within the grace period, then cancel and release."""
        if in_flight:
            immediate = (
                self.abort.is_set() or ctx.lease_lost is not None or ctx.db_error is not None
            )
            grace = 0.0 if immediate else self._effective_grace()
            if grace > 0:
                log.warning(
                    "finishing %d in-flight requests (grace %.0fs); press Ctrl+C again to abort",
                    len(in_flight),
                    grace,
                    extra={"event": "grace_wait"},
                )
                deadline = time.monotonic() + grace
                while in_flight and not self.abort.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    abort_task = asyncio.create_task(self.abort.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {*in_flight, abort_task},
                            timeout=remaining,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        if not abort_task.done():
                            abort_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await abort_task
                    self._reap(in_flight, done)
        if not in_flight:
            return
        unit_ids = sorted(uid for ids in in_flight.values() for uid in ids)
        for task in in_flight:
            task.cancel()
        await asyncio.gather(*in_flight, return_exceptions=True)
        released = await self._db(ctx.lock, ctx.repo.release, ctx.bound.job.id, unit_ids)
        if isinstance(released, Err):
            self._state_failure(ctx, released.error, "release_failed")
        else:
            log.warning(
                "cancelled %d in-flight units; %d released to PENDING",
                len(unit_ids),
                released.value,
                extra={"event": "chunks_released"},
            )

    def _effective_grace(self) -> float:
        """``grace_s`` capped below ``lease_stale_s`` so a drain can never outlive the lease."""
        grace = float(self.settings.grace_s)
        cap = float(max(0, self.settings.lease_stale_s - _GRACE_LEASE_MARGIN_S))
        if grace > cap:
            log.warning(
                "grace period %.0fs capped to %.0fs (lease_stale_s - %d)",
                grace,
                cap,
                _GRACE_LEASE_MARGIN_S,
                extra={"event": "grace_capped"},
            )
            return cap
        return grace

    def _state_failure(self, ctx: _TranslateContext, error: AppError, event: str) -> None:
        """A repository write failed: record the error once and stop the run (2.1, NFR-02)."""
        log.error("%s: %s", event, error.message, extra={"event": event})
        if ctx.db_error is None:
            ctx.db_error = error
        self.cancel.set()

    async def _heartbeat_loop(self, ctx: _TranslateContext) -> None:
        leases = LeaseRepository(ctx.bound.db)
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            beat = await self._db(ctx.lock, leases.heartbeat, ctx.bound.job.id, ctx.bound.holder)
            if isinstance(beat, Err):
                ctx.lease_lost = beat.error
                log.error("lease lost: %s", beat.error.message, extra={"event": "lease_lost"})
                self.cancel.set()
                return
            log.debug("heartbeat", extra={"event": "heartbeat"})

    # -- worker (one batch = one limiter permit = one provider call) -------------- #

    async def _worker(self, ctx: _TranslateContext, units: Sequence[Any]) -> None:
        adapter = ctx.adapter
        first_id = adapter.unit_id(units[0])
        attempt = max(adapter.retry_count(u) for u in units) + 1
        for unit in units:
            last_error = adapter.last_error(unit)
            unit_id = adapter.unit_id(unit)
            if (
                last_error
                and last_error.startswith(_EMPTY_PREFIX)
                and unit_id not in ctx.empty_counts
            ):
                ctx.empty_counts[unit_id] = 1  # a previous run already saw one empty response
        with chunk_context(first_id, attempt):
            try:
                todo: List[Any] = []
                for unit in units:
                    if adapter.is_translatable(unit):
                        todo.append(unit)
                        continue
                    with chunk_context(adapter.unit_id(unit), adapter.retry_count(unit) + 1):
                        await self._complete(
                            ctx,
                            adapter.unit_id(unit),
                            verbatim_result(
                                adapter.unit_id(unit), adapter.source_text(unit),
                                active_provider_of(ctx.translator),
                            ),
                        )
                if not todo:
                    return
                try:
                    outcome = await self._translate_batch(ctx, todo, attempt)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - programming error -> CHUNK_FATAL
                    log.exception("unit worker crashed", extra={"event": "chunk_crashed"})
                    outcome = err(
                        ErrorCode.INTERNAL,
                        f"internal error while translating unit {first_id}: "
                        f"{type(exc).__name__}",
                        ErrorScope.CHUNK_FATAL,
                        context={"chunk_id": first_id, "attempt": attempt},
                        cause=repr(exc)[:500],
                    )
                if isinstance(outcome, Err):
                    await self._handle_batch_error(ctx, todo, outcome.error)
                    return
                # CR-44: the provider was paid for this batch. Every unit is completed
                # before a cancellation is honoured, so `_drain` never releases (and a
                # later run never re-sends) the rest of an already-translated batch.
                await self._shielded(self._finish_batch(ctx, todo, outcome.value))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - never let a worker crash the runner
                log.exception("unit bookkeeping crashed", extra={"event": "chunk_crashed"})

    async def _shielded(self, work: Awaitable[None]) -> None:
        """Await ``work`` to its end even when this task is cancelled meanwhile; the
        cancellation is re-raised afterwards (the lease heartbeat keeps running in
        ``_run_job`` until ``_drain`` has gathered the workers)."""
        task = asyncio.ensure_future(work)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        task.result()  # surfaces an exception of the bookkeeping itself
        if cancelled:
            raise asyncio.CancelledError

    async def _finish_batch(
        self,
        ctx: _TranslateContext,
        units: Sequence[Any],
        results: Sequence[Result[TranslationResult]],
    ) -> None:
        """Store the per-unit results of one successful provider round trip.

        The limiter sees one success per *request* (CR-43), and every event is logged
        under the id of its own unit (CR-48 a)."""
        adapter = ctx.adapter
        if any(isinstance(r, Ok) and r.value.chars_sent > 0 for r in results):
            ctx.limiter.on_success()
        for unit, result in zip(units, results, strict=True):
            with chunk_context(adapter.unit_id(unit), adapter.retry_count(unit) + 1):
                if isinstance(result, Ok):
                    await self._complete(ctx, adapter.unit_id(unit), result.value)
                else:
                    await self._handle_error(ctx, unit, result.error)

    async def _handle_batch_error(
        self, ctx: _TranslateContext, units: Sequence[Any], error: AppError
    ) -> None:
        """A batch-level provider error is handled once per batch (CR-43).

        One limiter reaction, one ``rate_limited_count`` increment and one backoff delay
        (from the highest retry count of the batch); every unit is rescheduled with the
        same ``next_attempt_at`` so the next claim picks the batch up again as one request
        instead of N single-unit requests.
        """
        adapter = ctx.adapter
        if error.code is ErrorCode.PROVIDER_RATE_LIMITED:
            ctx.limiter.on_rate_limited()
            ctx.stats.rate_limited += 1
        when: Optional[datetime] = None
        delay = 0.0
        if error.scope is ErrorScope.CHUNK_RETRYABLE:
            delay = ctx.policy.next_delay(
                max(adapter.retry_count(u) for u in units), error.retry_after_s
            )
            when = self._clock() + timedelta(seconds=delay)
        for unit in units:
            with chunk_context(adapter.unit_id(unit), adapter.retry_count(unit) + 1):
                await self._handle_error(
                    ctx, unit, error, signal=False, when=when, delay=delay
                )

    async def _translate_batch(
        self, ctx: _TranslateContext, units: Sequence[Any], attempt: int
    ) -> Result[List[Result[TranslationResult]]]:
        """One provider round trip for ``units``; ``Err`` applies to every unit of the batch."""
        adapter = ctx.adapter
        built = adapter.build_request(units, ctx.bound.job.id)
        if isinstance(built, Err):
            return built
        batch = built.value
        provider_name = active_provider_of(ctx.translator)
        strategy = ctx.binding.strategy if ctx.binding is not None else GlossaryStrategy.NONE
        finish_ctx = FinishContext(
            provider=provider_name,
            strategy=strategy,
            glossary=ctx.glossary,
            strict_glossary=ctx.strict_glossary,
            latency_ms=0,
            attempt=attempt,
            empty_occurrences=dict(ctx.empty_counts),
        )
        if not batch.texts:
            empty = ProviderResponse(texts=[], chars_billed=0, latency_ms=0)
            return Ok(adapter.finish(batch, empty, finish_ctx))
        request = TranslationRequest(
            job_id=ctx.bound.job.id,
            run_id=self.run_id,
            chunk_id=batch.unit_ids[0],
            attempt=attempt,
            glossary=ctx.binding,
            context=batch.context if ctx.translator.capabilities.supports_context else None,
        )
        started = time.perf_counter()
        async with ctx.limiter:
            response = await ctx.translator.translate(list(batch.texts), request)
        latency_ms = int((time.perf_counter() - started) * 1000)
        ctx.stats.provider_calls += 1
        if isinstance(response, Err):
            error = response.error
            log.warning(
                "provider call failed: %s",
                error.message,
                extra={
                    "event": "provider_call",
                    "latency_ms": latency_ms,
                    "chars": batch.chars,
                    "http_status": error.context.get("http_status"),
                },
            )
            return response
        log.info(
            "provider call ok (%d texts, %d chars, %d ms)",
            len(batch.texts),
            batch.chars,
            latency_ms,
            extra={"event": "provider_call", "latency_ms": latency_ms, "chars": batch.chars},
        )
        if adapter.name == MODE_OVERLAY:
            log.info(
                "overlay_batch: %d units, %d chars, context %d chars",
                len(batch.unit_ids),
                batch.chars,
                len(batch.context or ""),
                extra={
                    "event": "overlay_batch",
                    "units": len(batch.unit_ids),
                    "chars": batch.chars,
                },
            )
        answer = response.value
        extra_warnings: Tuple[str, ...] = ()
        if isinstance(answer, FallbackResponse):
            # The wrapper may have switched mid-run: the unit belongs to whoever answered.
            provider_name = answer.provider or provider_name
            extra_warnings = answer.warnings
        finish_ctx = dataclasses.replace(
            finish_ctx, latency_ms=latency_ms, provider=provider_name
        )
        results = adapter.finish(batch, answer, finish_ctx)
        if extra_warnings:
            results = [_with_warnings(r, extra_warnings) for r in results]
        for result in results:
            if isinstance(result, Ok) and result.value.review_flag and any(
                w.startswith(("empty_response", "truncated_response", "provider_empty"))
                for w in result.value.warnings
            ):
                log.warning(
                    "unit kept with review flag after repeated empty/altered response",
                    extra={"event": "chunk_review"},
                )
        return Ok(results)

    async def _complete(
        self, ctx: _TranslateContext, unit_id: int, result: TranslationResult
    ) -> None:
        done = await self._db(ctx.lock, ctx.repo.complete, ctx.bound.job.id, unit_id, result)
        if isinstance(done, Err):
            self._state_failure(ctx, done.error, "complete_failed")
            return
        if not done.value:
            log.warning(
                "unit %d was no longer PROCESSING at completion (cancellation race); "
                "result discarded",
                unit_id,
                extra={"event": "complete_precondition_failed"},
            )
            return
        ctx.stats.completed += 1
        ctx.stats.chars_sent += result.chars_sent
        ctx.provider_units[result.provider] = ctx.provider_units.get(result.provider, 0) + 1
        log.info(
            "chunk completed",
            extra={
                "event": "chunk_completed",
                "chars": result.chars_sent,
                "latency_ms": result.latency_ms,
            },
        )
        self._emit_progress(ctx)

    async def _fail(
        self, ctx: _TranslateContext, unit_id: int, error: AppError, reason: Optional[str]
    ) -> None:
        message = f"{error.message} ({reason})" if reason else error.message
        stored = dataclasses.replace(error, message=message)
        failed = await self._db(ctx.lock, ctx.repo.fail, ctx.bound.job.id, unit_id, stored)
        if isinstance(failed, Err):
            self._state_failure(ctx, failed.error, "fail_failed")
            return
        if failed.value:
            ctx.stats.failed += 1
        log.error(
            "chunk failed: %s", message,
            extra={"event": "chunk_failed", "http_status": error.context.get("http_status")},
        )
        self._emit_progress(ctx)

    async def _handle_error(
        self,
        ctx: _TranslateContext,
        unit: Any,
        error: AppError,
        *,
        signal: bool = True,
        when: Optional[datetime] = None,
        delay: float = 0.0,
    ) -> None:
        """Classify ``error`` for one unit. ``signal=False`` / ``when`` come from
        :meth:`_handle_batch_error`, which already told the limiter and computed the one
        backoff of the batch."""
        adapter = ctx.adapter
        unit_id = adapter.unit_id(unit)
        retry_count = adapter.retry_count(unit)
        if signal and error.code is ErrorCode.PROVIDER_RATE_LIMITED:
            ctx.limiter.on_rate_limited()
            ctx.stats.rate_limited += 1
        if error.scope is ErrorScope.CHUNK_RETRYABLE:
            stored = error
            if error.code is ErrorCode.PROVIDER_EMPTY_RESPONSE:
                count = ctx.empty_counts.get(unit_id, 0) + 1
                ctx.empty_counts[unit_id] = count
                if not error.message.startswith(_EMPTY_PREFIX):
                    stored = dataclasses.replace(error, message=f"{_EMPTY_PREFIX}: {error.message}")
                if count >= _EMPTY_FAIL_AT:
                    await self._fail(ctx, unit_id, stored, f"empty response repeated {count} times")
                    return
            if ctx.policy.can_retry(retry_count):
                if when is None:
                    delay = ctx.policy.next_delay(retry_count, error.retry_after_s)
                    when = self._clock() + timedelta(seconds=delay)
                rescheduled = await self._db(
                    ctx.lock, ctx.repo.reschedule, ctx.bound.job.id, unit_id, stored, when
                )
                if isinstance(rescheduled, Err):
                    self._state_failure(ctx, rescheduled.error, "reschedule_failed")
                elif not rescheduled.value:
                    log.warning("unit %d was no longer PROCESSING at reschedule", unit_id,
                                extra={"event": "reschedule_precondition_failed"})
                else:
                    log.warning(
                        "chunk rescheduled in %.1fs (retry %d/%d): %s",
                        delay,
                        retry_count + 1,
                        ctx.policy.max_retries,
                        error.message,
                        extra={
                            "event": "chunk_rescheduled",
                            "http_status": error.context.get("http_status"),
                        },
                    )
                return
            await self._fail(
                ctx, unit_id, stored, f"retries exhausted after {retry_count + 1} attempts"
            )
            return
        if error.scope is ErrorScope.CHUNK_FATAL:
            await self._fail(ctx, unit_id, error, None)
            return
        await self._pause(ctx, unit_id, error)

    async def _pause(self, ctx: _TranslateContext, unit_id: int, error: AppError) -> None:
        released = await self._db(ctx.lock, ctx.repo.release, ctx.bound.job.id, [unit_id])
        if isinstance(released, Err):
            self._state_failure(ctx, released.error, "release_failed")
        if ctx.pause_error is None:
            ctx.pause_error = error
            paused = await self._db(
                ctx.lock, ctx.jobs.update_job, ctx.bound.job.id, ctx.pause_patch
            )
            if isinstance(paused, Err):
                log.error("could not mark the job PAUSED: %s", paused.error.message)
            log.error(
                "job paused: %s", error.message,
                extra={"event": "job_paused", "http_status": error.context.get("http_status")},
            )
        self.cancel.set()

    def _emit_progress(self, ctx: _TranslateContext) -> None:
        if self._progress is None:
            return
        event = ProgressEvent(
            completed=ctx.base_counts.completed + ctx.stats.completed,
            failed=ctx.base_counts.failed + ctx.stats.failed,
            total=ctx.base_counts.total,
            elapsed_s=time.monotonic() - ctx.started,
            chars_sent=ctx.stats.chars_sent,
            permits=ctx.limiter.permits,
        )
        try:
            self._progress(event)
        except Exception:  # noqa: BLE001 - UI must not break the runner
            log.exception("progress callback failed")

    # ------------------------------------------------------------------ #
    # Stage: export (8)
    # ------------------------------------------------------------------ #

    def export(
        self,
        *,
        formats: Optional[Sequence[str]] = None,
        allow_partial: bool = False,
        options: Optional[ExportOptions] = None,
        force_unlock: bool = False,
        render_placeholders: bool = True,
        overlay_min_scale: float = DEFAULT_OVERLAY_MIN_SCALE,
        overlay_floor_scale: float = DEFAULT_OVERLAY_FLOOR_SCALE,
        figure_legend: bool = False,
        margins_from_source: bool = True,
        input_pdf: Optional[Path] = None,
        overlay_uniform_scale: bool = True,
    ) -> Result[ExportOutcome]:
        """Write the requested formats (``None`` -> the default rule of :func:`default_formats`).

        ``figure_legend`` renders the legend *list* in the reflow outputs (Addendum A; the
        translated figure PNG is produced regardless); ``margins_from_source`` tells the
        reflow PDF to take its margins from ``source_profile.json`` (``--pdf-margin source``).
        ``input_pdf`` (``export --input``) names the source PDF when it no longer sits at the
        path recorded in the state; it must still hash to ``jobs.input_sha256`` (CR-39).
        """
        try:
            if self._bound is None and not self.db_path.exists():
                return err(
                    ErrorCode.STATE_NOT_FOUND,
                    f"no translation state in {self.output_dir}; run `translate` first",
                    ErrorScope.USER,
                )
            if not 0.0 < overlay_floor_scale <= overlay_min_scale <= 1.0:
                return err(
                    ErrorCode.PROVIDER_CONFIG,
                    "--overlay-floor-scale must be > 0 and <= --overlay-min-scale (<= 1)",
                    ErrorScope.USER,
                )
            b = self._bind(None, fresh=False, force_unlock=force_unlock)
            if isinstance(b, Err):
                return b
            bound = b.value
            result: Result[ExportOutcome] = err(
                ErrorCode.INTERNAL, "export", ErrorScope.JOB_FATAL
            )
            try:
                started = self._start_run(
                    bound,
                    "export",
                    provider=bound.job.provider,
                    mode=None if self._run_started else self._export_run_mode(bound, formats),
                )
                if isinstance(started, Err):
                    result = started
                else:
                    result = self._export_bound(
                        bound,
                        formats=formats,
                        allow_partial=allow_partial,
                        options=options or ExportOptions(),
                        render_placeholders=render_placeholders,
                        overlay_min_scale=overlay_min_scale,
                        overlay_floor_scale=overlay_floor_scale,
                        figure_legend=figure_legend,
                        margins_from_source=margins_from_source,
                        input_pdf=Path(input_pdf) if input_pdf is not None else None,
                        overlay_uniform_scale=overlay_uniform_scale,
                    )
                    if isinstance(result, Ok) and not self._composite:
                        self._merge_export_into_summary(result.value)
            except KeyboardInterrupt:
                result = _interrupted()
                raise
            except Exception as exc:  # noqa: BLE001 - boundary
                log.exception("export crashed", extra={"event": "stage_crashed"})
                result = _internal("export", exc)
            finally:
                code = result.value.exit_code if isinstance(result, Ok) else exit_code_for(
                    result.error
                )
                self._end_stage(bound, result, code)
            return result
        except Exception as exc:  # noqa: BLE001 - boundary
            log.exception("export crashed", extra={"event": "stage_crashed"})
            return _internal("export", exc)

    def _overlay_repo_for(
        self, bound: _Bound, glossary_hash: Optional[str]
    ) -> Optional[OverlayBlockRepository]:
        """The overlay repository when the schema has the overlay tables (B15), else ``None``."""
        if bound.db.schema_version < 2:
            return None
        return self._overlay_repo_factory(bound.db, self.run_id, glossary_hash)

    def _export_run_mode(self, bound: _Bound, formats: Optional[Sequence[str]]) -> str:
        """``runs.mode`` of a standalone export: ``overlay`` when only ``pdf-overlay`` is
        written, else ``reflow`` (CR-42, doc 07 B14)."""
        if formats is not None:
            names = normalise_formats(formats)
        else:
            chunk_total = 0
            overlay_total = 0
            counted = self._chunk_repo_factory(bound.db, self.run_id, None).counts(
                bound.job.id, self._clock()
            )
            if isinstance(counted, Ok):
                chunk_total = counted.value.total
            overlay_repo = self._overlay_repo_for(bound, None)
            if overlay_repo is not None:
                overlay_counted = overlay_repo.counts(bound.job.id, self._clock())
                if isinstance(overlay_counted, Ok):
                    overlay_total = overlay_counted.value.total
            names = default_formats(chunk_total > 0, overlay_total > 0)
        if names and all(name == OVERLAY_FORMAT for name in names):
            return MODE_OVERLAY
        return MODE_REFLOW

    def _verify_source(
        self,
        bound: _Bound,
        job: JobRecord,
        input_pdf: Optional[Path],
        *,
        required: bool,
        figure_step: bool = False,
        warnings: List[str],
    ) -> Result[_SourceCheck]:
        """CR-39: bind the export to the file the state was created from.

        ``translate`` checks ``input_sha256`` before any provider contact, but the stored
        rectangles are *used* here: the overlay PDF is the source file with holes punched at
        those rects, and a translated figure is a re-render of a source page. So the file is
        hashed again before anything is written.

        ``required`` means an output cannot be produced at all without the source (the overlay
        PDF *is* the source). Then a file that is missing or no longer the same one is a hard
        refusal. Otherwise the source is only the raw material of the figure-label step, and a
        file that cannot be used costs the labels, not the document (CR-92):

        * file missing, required or an explicit ``--input`` -> ``INPUT_NOT_FOUND`` (1)
        * file missing, only the figure step needs it -> warning, exit 0; the figures keep
          their English labels and the legend lists stay in the document (CR-57)
        * hash differs, required -> ``STATE_HASH_MISMATCH`` (5), nothing is written
        * hash differs, not required -> warning ``figure_translate_failed:input_changed``,
          the source is not touched, the legend lists stay, exit 2 (CR-92)
        """
        explicit = input_pdf is not None
        path = absolute_path(input_pdf) if input_pdf is not None else Path(job.input_path)
        if not path.is_file():
            if required or explicit:
                origin = "given with --input" if explicit else "recorded in the state"
                return err(
                    ErrorCode.INPUT_NOT_FOUND,
                    f"the source PDF {path} ({origin}) was not found; pass `export --input "
                    "FILE` to point at its current location (it must be the very file this "
                    "state was created from)",
                    ErrorScope.USER,
                    context={"path": str(path)},
                )
            warnings.append(
                f"figure_translate_failed:input_missing:{path.name} (pass `export --input "
                "FILE` to translate the figure labels; the legend lists are kept)"
            )
            log.warning(
                "source PDF not found; figures keep their English labels",
                extra={"event": "figure_translate_failed"},
            )
            return Ok(_SourceCheck(None))
        hashed = sha256_file(path)
        if isinstance(hashed, Err):
            return hashed
        if hashed.value != job.input_sha256:
            if required:
                return err(
                    ErrorCode.STATE_HASH_MISMATCH,
                    f"{path} is not the file this state was created from (state hash "
                    f"{job.input_sha256[:12]}…, file hash {hashed.value[:12]}…); nothing was "
                    "written. Point `export --input` at the original PDF or start over with "
                    "`translate --fresh`",
                    ErrorScope.USER,
                    context={"expected": job.input_sha256, "actual": hashed.value},
                )
            # CR-92: the requested formats do not need the source; only the figure labels do.
            # The changed file is never read (its pages no longer match the stored geometry),
            # the documents are written, and the paid label translations stay in the legend.
            detail = (
                f"{path.name} changed since `translate` (state hash "
                f"{job.input_sha256[:12]}…, file hash {hashed.value[:12]}…); it was not read"
            )
            if figure_step:
                warnings.append(
                    f"figure_translate_failed:input_changed:{detail}; the figures keep their "
                    "English labels and the legend lists are kept. Point `export --input` at "
                    "the original PDF to translate them"
                )
            else:
                warnings.append(f"input_changed:{detail}")
            log.warning(
                "source PDF changed since translate; it was not read",
                extra={"event": "figure_translate_failed" if figure_step else "input_changed"},
            )
            return Ok(_SourceCheck(None, EXIT_PARTIAL))
        resolved = absolute_path(path)
        if str(resolved) != job.input_path:
            self._remember_input_path(bound.db, job, str(resolved))
        return Ok(_SourceCheck(resolved))

    def _paintable_figures(self) -> List[FigureRecord]:
        """Inventory records that carry label geometry (the figure step can paint them)."""
        inventory = self._load_inventory()
        if inventory is None:
            return []
        return [record for record in inventory.records() if record.label_boxes]

    def _check_page_design(self, options: ExportOptions) -> Result[None]:
        """CR-63: a bad ``--pdf-page-size`` / ``--pdf-margin`` is a USER error (exit 1)
        reported before anything is written, not an "optional exporter failed" (exit 2)."""
        try:
            from ..exporters.pdf_native import resolve_page_design
        except ImportError:
            return Ok(None)
        design = resolve_page_design(options)
        if isinstance(design, Err) and design.error.scope is ErrorScope.USER:
            return design
        return Ok(None)

    def _export_bound(
        self,
        bound: _Bound,
        *,
        formats: Optional[Sequence[str]],
        allow_partial: bool,
        options: ExportOptions,
        render_placeholders: bool,
        overlay_min_scale: float = DEFAULT_OVERLAY_MIN_SCALE,
        overlay_floor_scale: float = DEFAULT_OVERLAY_FLOOR_SCALE,
        figure_legend: bool = False,
        margins_from_source: bool = True,
        input_pdf: Optional[Path] = None,
        overlay_uniform_scale: bool = True,
    ) -> Result[ExportOutcome]:
        jobs = JobRepository(bound.db)
        job = bound.job
        refreshed = jobs.get_job(job.id)
        if isinstance(refreshed, Ok) and refreshed.value is not None:
            job = refreshed.value
        chunks = self._chunk_repo_factory(bound.db, self.run_id, job.glossary_hash)
        counted = chunks.counts(job.id, self._clock())
        if isinstance(counted, Err):
            return counted
        chunk_total = counted.value.total
        overlay_repo = self._overlay_repo_for(bound, job.glossary_hash)
        overlay_counts: Optional[StatusCounts] = None
        if overlay_repo is not None:
            overlay_counted = overlay_repo.counts(job.id, self._clock())
            if isinstance(overlay_counted, Err):
                return overlay_counted
            overlay_counts = overlay_counted.value
        overlay_total = overlay_counts.total if overlay_counts is not None else 0

        names = normalise_formats(
            formats if formats is not None else default_formats(chunk_total > 0, overlay_total > 0)
        )
        reflow_names = [n for n in names if n != OVERLAY_FORMAT]
        wants_overlay = OVERLAY_FORMAT in names

        # FR-49: every requested group is checked before anything is written.
        if reflow_names and chunk_total == 0:
            if overlay_total > 0:
                return err(
                    ErrorCode.OVERLAY_STATE_MISSING,
                    "md/epub/pdf need a reflow translation pass but this directory only holds "
                    "an overlay pass; run `translate --mode reflow` first or export with "
                    "--format pdf-overlay",
                    ErrorScope.USER,
                )
            return err(
                ErrorCode.EXPORT_REFUSED_PARTIAL,
                "no chunks in the state database; run `translate` first",
                ErrorScope.USER,
            )
        if wants_overlay and (overlay_repo is None or overlay_counts is None or overlay_total == 0):
            return err(
                ErrorCode.OVERLAY_STATE_MISSING,
                "pdf-overlay needs an overlay translation pass; run "
                f"`translate --mode overlay --input {job.input_path}` first",
                ErrorScope.USER,
            )
        overlay_partial = False
        if wants_overlay and overlay_counts is not None:
            overlay_partial = overlay_counts.completed < overlay_counts.total
            if overlay_partial and not allow_partial:
                return err(
                    ErrorCode.EXPORT_REFUSED_PARTIAL,
                    f"{overlay_counts.total - overlay_counts.completed} overlay units are not "
                    "COMPLETED; finish `translate --mode overlay` or pass --allow-partial",
                    ErrorScope.USER,
                )

        outputs: Dict[str, str] = {}
        warnings: List[str] = []
        # CR-64 / FR-53: --pdf-font is validated before *any* artefact of either group.
        if options.font_path is not None and ("pdf" in reflow_names or wants_overlay):
            font_checked = resolve_font(options.font_path)
            if isinstance(font_checked, Err):
                return font_checked
            log.info(
                "pdf font %s validated for Turkish coverage", options.font_path.name,
                extra={"event": "pdf_font_validated"},
            )
        # CR-63: the page design of the reflow PDF is a USER input; refuse it up front.
        if "pdf" in reflow_names:
            options, profile_warnings = self._apply_source_profile(options, margins_from_source)
            warnings.extend(profile_warnings)
            design_checked = self._check_page_design(options)
            if isinstance(design_checked, Err):
                return design_checked
        # CR-39: the source file is hashed before it is used (and before anything is written).
        source_pdf: Optional[Path] = None
        exit_code = EXIT_SUCCESS
        figure_step = bool(reflow_names) and bool(self._paintable_figures())
        if wants_overlay or figure_step or input_pdf is not None:
            verified = self._verify_source(
                bound,
                job,
                input_pdf,
                required=wants_overlay,  # CR-92: only the overlay group cannot go on without it
                figure_step=figure_step,
                warnings=warnings,
            )
            if isinstance(verified, Err):
                return verified
            source_pdf = verified.value.path
            exit_code = worst_exit((exit_code, verified.value.exit_code))

        failed_ids: List[int] = []
        review_ids: List[int] = []
        if reflow_names:
            reflow = self._export_reflow(
                job,
                chunks,
                reflow_names,
                allow_partial=allow_partial,
                options=options,
                render_placeholders=render_placeholders,
                figure_legend=figure_legend,
                source_pdf=source_pdf,
                uniform_scale=overlay_uniform_scale,
            )
            if isinstance(reflow, Err):
                return reflow
            reflow_outcome = reflow.value
            outputs.update(reflow_outcome.outputs)
            warnings.extend(reflow_outcome.warnings)
            failed_ids = list(reflow_outcome.failed_chunk_ids)
            review_ids = list(reflow_outcome.review_chunk_ids)
            exit_code = worst_exit((exit_code, reflow_outcome.exit_code))
            if reflow_outcome.exit_code == EXIT_SUCCESS:
                marked = jobs.update_job(job.id, JobPatch(status="EXPORTED"))
                if isinstance(marked, Err):
                    warnings.append(f"job_status:{marked.error.message}")
        overlay_summary: Optional[OverlaySummary] = None
        review_entries = 0
        if wants_overlay and overlay_repo is not None and overlay_counts is not None:
            if source_pdf is None:  # unreachable: required=True either returns a path or Err
                return err(ErrorCode.INPUT_NOT_FOUND, "source PDF missing", ErrorScope.USER)
            rendered = self._export_overlay(
                job,
                overlay_repo,
                options,
                source_pdf,
                allow_partial=overlay_partial,
                min_scale=overlay_min_scale,
                floor_scale=overlay_floor_scale,
                uniform_scale=overlay_uniform_scale,
            )
            if isinstance(rendered, Err):
                return rendered
            overlay_export = rendered.value
            outputs[OVERLAY_FORMAT] = str(overlay_export.pdf_path)
            warnings.extend(overlay_export.warnings)
            exit_code = worst_exit((exit_code, overlay_export.exit_code))
            if overlay_export.exit_code == EXIT_SUCCESS:
                marked = jobs.update_job(job.id, JobPatch(overlay_status="OVERLAY_EXPORTED"))
                if isinstance(marked, Err):
                    warnings.append(f"job_status:{marked.error.message}")
                else:
                    job = dataclasses.replace(job, overlay_status="OVERLAY_EXPORTED")
            # CR-51: the placement result travels into summary.json / the console line.
            overlay_summary = self._overlay_summary(
                job,
                overlay_repo,
                overlay_counts,
                review_path=overlay_export.review_path,
                placed=overlay_export.placed,
            )
            review_entries = overlay_export.review_entries
        return Ok(
            ExportOutcome(
                job_id=job.id,
                outputs=outputs,
                failed_chunk_ids=failed_ids,
                review_chunk_ids=review_ids,
                warnings=warnings,
                exit_code=exit_code,
                overlay=overlay_summary,
                overlay_review_entries=review_entries,
            )
        )

    def _merge_export_into_summary(self, outcome: ExportOutcome) -> None:
        """CR-51: a standalone ``export`` refreshes ``summary.json`` (outputs + the overlay
        placement counts / ``review_path``); the file of the last ``translate`` is kept
        otherwise. Never fatal."""
        if not self.summary_path.is_file():
            return
        try:
            payload = json.loads(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict) or payload.get("job_id") != outcome.job_id:
            return
        merged = dict(payload.get("outputs") or {})
        merged.update(outcome.outputs)
        payload["outputs"] = merged
        if outcome.overlay is not None:
            payload["overlay"] = summary_to_jsonable(outcome.overlay)
        data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        written = atomic_write_bytes(self.summary_path, data.encode("utf-8"))
        if isinstance(written, Err):
            log.warning("could not update %s: %s", SUMMARY_NAME, written.error.message)

    def _export_reflow(
        self,
        job: JobRecord,
        chunks: ChunkRepository,
        exporter_names: Sequence[str],
        *,
        allow_partial: bool,
        options: ExportOptions,
        render_placeholders: bool,
        figure_legend: bool,
        source_pdf: Optional[Path],
        uniform_scale: bool = True,
    ) -> Result[ExportOutcome]:
        """The v1 reflow export (md/epub/pdf) plus Addendum A: source-derived page design
        (already applied by the caller), figure labels translated into ``*.tr.png`` files,
        legend list only on request - or when a figure could not be painted (CR-57)."""
        source_markdown: Optional[str] = None
        if self.source_path.exists():
            source_markdown = self.source_path.read_text(encoding="utf-8")
        metadata: Dict[str, str] = {
            "job_id": job.id,
            "source_file": Path(job.input_path).name,
            "input_path": job.input_path,
            "provider": job.provider or "",
        }
        if options.title:
            metadata["title"] = options.title
        if options.author:
            metadata["author"] = options.author
        try:
            assembled = assemble(
                chunks.iter_ordered(job.id),
                allow_partial=allow_partial,
                metadata=metadata,
                source_markdown=source_markdown,
            )
        except Exception as exc:  # noqa: BLE001 - iterator boundary (D4)
            return err(
                ErrorCode.STATE_CORRUPT,
                "cannot read chunks from the state database",
                ErrorScope.JOB_FATAL,
                cause=repr(exc)[:300],
            )
        if isinstance(assembled, Err):
            return assembled
        document = assembled.value
        warnings: List[str] = list(document.warnings)
        # Addendum A: the legend pairs drive the translated figure PNGs ...
        sources, painted = self._translate_figures(
            document, options, warnings, source_pdf, uniform_scale
        )
        markdown = rewrite_image_sources(document.markdown, sources)
        # ... and the legend list itself is rendered only on request - except for figures
        # whose labels did not make it into a PNG: their (paid) translations stay visible.
        if not figure_legend:
            markdown = strip_legends(markdown, painted)
            for image_id in legend_ids(markdown):
                warnings.append(f"figure_labels_not_painted:{image_id}")
                log.warning(
                    "figure %d: labels not painted into the image; legend list kept",
                    image_id,
                    extra={"event": "figure_labels_not_painted"},
                )
        document = dataclasses.replace(document, markdown=markdown)
        outputs: Dict[str, str] = {}
        exit_code = EXIT_PARTIAL if document.failed_chunk_ids else EXIT_SUCCESS
        # CR-91: a format that fails does not delete the formats already on disk. The first
        # hard error is remembered, the loop finishes, and the outcome carries both the files
        # that were written and the failure (exit code of the error, `export_error:` warning).
        failure: Optional[AppError] = None
        for name in exporter_names:
            got = self._exporter_factory(name)
            if isinstance(got, Err):
                return got
            exporter = got.value
            if not exporter.is_available():
                message = f"exporter_unavailable:{name}"
                if exporter.optional:
                    warnings.append(message)
                    exit_code = worst_exit((exit_code, EXIT_PARTIAL))
                    log.warning(
                        "%s exporter is not available; skipped", name,
                        extra={"event": "exporter_unavailable"},
                    )
                    continue
                failure = failure or AppError(
                    code=ErrorCode.EXPORTER_UNAVAILABLE,
                    message=f"exporter {name!r} is not available",
                    scope=ErrorScope.USER,
                    context={"exporter": name},
                )
                warnings.append(f"{EXPORT_ERROR_PREFIX}{name}:{message}")
                log.error(
                    "%s exporter is not available", name,
                    extra={"event": "export_error"},
                )
                continue
            if name == "markdown" and hasattr(exporter, "render_placeholders"):
                exporter.render_placeholders = render_placeholders
            if name == "pdf":
                log.info(
                    "pdf engine: %s (page %s)", options.pdf_engine, options.page_size,
                    extra={"event": "pdf_engine"},
                )
            artifact = exporter.export(document, self.output_dir, options)
            if isinstance(artifact, Err):
                # CR-63: only a missing engine / a render failure of an *optional* exporter
                # is downgraded to a warning (exit 2); a USER error stays exit 1.
                downgrade = exporter.optional and (
                    artifact.error.code is ErrorCode.EXPORTER_UNAVAILABLE
                    or artifact.error.scope is not ErrorScope.USER
                )
                if downgrade:
                    warnings.append(f"export_failed:{name}:{artifact.error.message}")
                    exit_code = worst_exit((exit_code, EXIT_PARTIAL))
                    continue
                # CR-91: keep going; the formats already written stay in the report.
                failure = failure or artifact.error
                warnings.append(f"{EXPORT_ERROR_PREFIX}{name}:{artifact.error.message}")
                log.error(
                    "%s export failed: %s", name, artifact.error.message,
                    extra={"event": "export_error"},
                )
                continue
            outputs[name] = str(artifact.value.path)
            warnings.extend(artifact.value.warnings)
            log.info(
                "wrote %s (%d bytes)", artifact.value.path.name, artifact.value.bytes_written,
                extra={"event": "exported"},
            )
        warnings.extend(self._figure_orphans(document.markdown))  # CR-62
        if failure is not None:
            if not outputs:
                # Nothing reached the disk: the error itself is the whole answer (CR-63).
                return Err(failure)
            exit_code = worst_exit((exit_code, exit_code_for(failure)))
        return Ok(
            ExportOutcome(
                job_id=job.id,
                outputs=outputs,
                failed_chunk_ids=list(document.failed_chunk_ids),
                review_chunk_ids=list(document.review_chunk_ids),
                warnings=warnings,
                exit_code=exit_code,
            )
        )

    def _apply_source_profile(
        self, options: ExportOptions, margins_from_source: bool
    ) -> Tuple[ExportOptions, List[str]]:
        """Fill ``page_rect_pt`` / ``body_font_pt`` / ``margins_pt`` from ``source_profile.json``
        unless the user chose a page size / margins explicitly (Addendum A)."""
        warnings: List[str] = []
        if not self.source_profile_path.is_file():
            if options.page_size.strip().lower() == SOURCE_PAGE_SIZE:
                warnings.append(
                    f"pdf_page_design:{SOURCE_PROFILE_FILE_NAME} missing; the engine falls back "
                    "to its default page (re-run `extract` to record the source properties)"
                )
            return options, warnings
        loaded = load_source_profile(self.source_profile_path)
        if isinstance(loaded, Err):
            warnings.append(f"pdf_page_design:{loaded.error.message}")
            return options, warnings
        profile = loaded.value
        field_names = {f.name for f in dataclasses.fields(options)}
        updates: Dict[str, Any] = {}
        if "page_rect_pt" in field_names and options.page_size.strip().lower() == SOURCE_PAGE_SIZE:
            if getattr(options, "page_rect_pt", None) is None:
                updates["page_rect_pt"] = (profile.page_width_pt, profile.page_height_pt)
        if "body_font_pt" in field_names and getattr(options, "body_font_pt", None) is None:
            if profile.body_font_size_pt > 0:
                updates["body_font_pt"] = profile.body_font_size_pt
        if "margins_pt" in field_names and margins_from_source:
            if getattr(options, "margins_pt", None) is None:
                updates["margins_pt"] = profile.margins_pt
        if "body_family" in field_names and not profile.serif:
            if getattr(options, "body_family", None) in (None, "serif"):
                updates["body_family"] = "sans"  # CR-66: follow the source's typeface class
        if updates:
            options = dataclasses.replace(options, **updates)
            log.info(
                "pdf page design from the source: %s",
                ", ".join(f"{k}={v}" for k, v in updates.items()),
                extra={"event": "pdf_page_design"},
            )
        return options, warnings

    def _translate_figures(
        self,
        document: AssembledDocument,
        options: ExportOptions,
        warnings: List[str],
        source_pdf: Optional[Path],
        uniform_scale: bool = True,
    ) -> Tuple[Dict[int, str], List[int]]:
        """Paint the translated labels into ``images/pNNN-fKK.tr.png`` (Addendum A; CR-57).

        The English original, ``source_book.md`` and ``figures.json`` are never touched:
        the translated PNG is written next to the original and only the *translated*
        document is re-pointed at it. Returns ``(image id -> new src, image ids whose legend
        may be stripped)``; a legend is strippable only when every paired label of the
        figure was painted without truncation, so a paid label translation is never lost.
        Never fatal: every failure keeps the English PNG, keeps the legend and warns.
        ``source_pdf`` is the hash-verified source (``None``: missing, already warned).
        """
        sources: Dict[int, str] = {}
        painted: List[int] = []
        inventory = self._load_inventory()
        if inventory is None:
            return sources, painted
        pairs = legend_pairs(document.markdown)
        records = [
            r for r in inventory.records() if r.label_boxes and pairs.get(r.image_id)
        ]
        if not records or source_pdf is None:
            return sources, painted
        try:
            from ..exporters import figure_overlay
        except ImportError as exc:
            warnings.append(
                f"figure_translate_unavailable:{type(exc).__name__}; figures keep their "
                "English labels"
            )
            log.warning(
                "figure_overlay module unavailable; translated figure PNGs skipped",
                extra={"event": "figure_translate_unavailable"},
            )
            return sources, painted
        font = resolve_font(options.font_path)
        if isinstance(font, Err):
            warnings.append(f"figure_translate_failed:font:{font.error.message}")
            return sources, painted
        # CR-87: one batch call, so the source PDF is read and opened once for the whole
        # document instead of once per figure. Each job still renders on its own page copy
        # (CR-88), and a failing figure only costs its own outcome.
        jobs: List[Any] = []
        staged: List[Tuple[Any, bool, str]] = []
        for record in records:
            mapping = pairs[record.image_id]
            box_texts = {box.text for box in record.label_boxes}
            covered = set(mapping) <= box_texts  # every paired label has a box to paint
            replacements = build_replacements(record, mapping)
            if not replacements:
                if covered and all(mapping[text] == text for text in mapping):
                    painted.append(record.image_id)  # nothing differs: the PNG is right
                continue
            jobs.append(
                figure_overlay.FigureJob(record.page, record.region, replacements, record.dpi)
            )
            staged.append((record, covered, record.file.rsplit("/", 1)[-1]))
        if not jobs:
            return sources, painted
        batch = figure_overlay.render_translated_figures(
            source_pdf, jobs, font.value, uniform_scale=uniform_scale
        )
        if isinstance(batch, Err):
            warnings.append(f"figure_translate_failed:{source_pdf.name}:{batch.error.message}")
            log.warning(
                "figures kept in English: %s", batch.error.message,
                extra={"event": "figure_translate_failed"},
            )
            return sources, painted
        for (record, covered, file_name), job_spec, rendered in zip(
            staged, jobs, batch.value, strict=True
        ):
            replacements = job_spec.replacements
            if isinstance(rendered, Err):
                warnings.append(f"figure_translate_failed:{file_name}:{rendered.error.message}")
                log.warning(
                    "figure %s kept in English: %s", file_name, rendered.error.message,
                    extra={"event": "figure_translate_failed"},
                )
                continue
            png, stats = rendered.value
            translated_file = translated_figure_name(record.file)
            target = self.output_dir / Path(*translated_file.split("/"))
            written = atomic_write_bytes(target, png)
            if isinstance(written, Err):
                warnings.append(f"figure_translate_failed:{file_name}:{written.error.message}")
                continue
            sources[record.image_id] = translated_file
            truncated = sum(1 for flag in stats.truncated if flag)
            if truncated:
                warnings.append(f"figure_label_truncated:{record.image_id}:{truncated}")
                log.warning(
                    "figure %s: %d translated labels were truncated to fit",
                    file_name, truncated,
                    extra={"event": "figure_label_truncated"},
                )
            if covered and not truncated:
                painted.append(record.image_id)
            log.info(
                "figure %s translated into %s (%d labels)",
                file_name, target.name, len(replacements),
                extra={"event": "figure_translated"},
            )
        return sources, painted

    def _export_overlay(
        self,
        job: JobRecord,
        repo: OverlayBlockRepository,
        options: ExportOptions,
        source_pdf: Path,
        *,
        allow_partial: bool,
        min_scale: float,
        floor_scale: float,
        uniform_scale: bool = True,
    ) -> Result[_OverlayExport]:
        """``translated_book.overlay.pdf`` + ``overlay_review.json`` through developer B's
        ``OverlayRenderer`` (Q21 exit rules). ``source_pdf`` is the hash-verified source."""
        try:
            from ..exporters.pdf_overlay_exporter import OverlayRenderer, OverlayRenderOptions
        except ImportError as exc:
            return err(
                ErrorCode.EXPORTER_UNAVAILABLE,
                "the pdf-overlay exporter is not available in this build",
                ErrorScope.USER,
                context={"exporter": OVERLAY_FORMAT},
                cause=repr(exc)[:200],
            )
        font = resolve_font(options.font_path)
        if isinstance(font, Err):
            return font
        pages = repo.pages(job.id)
        if isinstance(pages, Err):
            return pages
        outline_map: Dict[Tuple[int, str], str] = {}
        try:
            for block in repo.iter_ordered(job.id):
                if (
                    block.kind is OverlayBlockKind.HEADING
                    and block.translate
                    and block.translated_text
                ):
                    outline_map[(block.page, " ".join(block.source_text.split()))] = (
                        block.translated_text
                    )
        except Exception as exc:  # noqa: BLE001 - iterator boundary (D4)
            return err(
                ErrorCode.STATE_CORRUPT,
                "cannot read the overlay units from the state database",
                ErrorScope.JOB_FATAL,
                cause=repr(exc)[:300],
            )
        # ``uniform_scale`` (shared per-page scale) is passed by keyword and only when the
        # renderer knows it, so both exporter revisions work.
        known = {f.name for f in dataclasses.fields(OverlayRenderOptions)}
        extra: Dict[str, Any] = {"uniform_scale": uniform_scale} if "uniform_scale" in known else {}
        render_options = OverlayRenderOptions(
            font=font.value,
            title=options.title,
            author=options.author,
            translate_headers=job.overlay_translate_headers,
            allow_partial=allow_partial,
            review_threshold=min_scale,
            floor_scale=floor_scale,
            **extra,
        )
        job_id = job.id

        def blocks_by_page(page: int) -> Iterator[OverlayBlock]:
            return repo.iter_page(job_id, page)

        def progress(done: int, total: int) -> None:
            if done == total or done % 10 == 0:
                log.info(
                    "overlay export: page %d/%d", done, total,
                    extra={"event": "overlay_page_progress"},
                )

        artifact = OverlayRenderer().render(
            source_pdf,
            pages.value,
            blocks_by_page,
            outline_map,
            self.output_dir,
            render_options,
            progress,
        )
        if isinstance(artifact, Err):
            return artifact
        result = artifact.value
        warnings = list(result.warnings)
        code = EXIT_SUCCESS
        if result.could_not_fit > 0 or result.pages_skipped or allow_partial:
            code = EXIT_PARTIAL
        self._log_placement(result)
        self._stamp_review_file(result.review_path, job)
        log.info(
            "overlay written: %s (%d pages, placed %d, shrunk %d, could_not_fit %d, kept %d)",
            result.pdf_path.name,
            result.pages,
            result.placed,
            result.shrunk_below_threshold,
            result.could_not_fit,
            result.kept_original,
            extra={"event": "exported", "format": OVERLAY_FORMAT},
        )
        return Ok(
            _OverlayExport(
                exit_code=code,
                pdf_path=result.pdf_path,
                review_path=result.review_path,
                warnings=warnings,
                placed=(
                    result.placed,
                    result.shrunk_below_threshold,
                    result.could_not_fit,
                    result.kept_original,
                ),
                review_entries=len(result.review),
            )
        )

    def _log_placement(self, result: Any) -> None:
        """CR-48 b: the placement outcome reaches the JSONL log - counts at INFO, one WARNING
        per review entry with ``block_id`` / ``reason`` / ``scale``; never the text."""
        log.info(
            "overlay blocks placed: %d (shrunk below threshold %d, could not fit %d, kept "
            "original %d, pages skipped %d)",
            result.placed,
            result.shrunk_below_threshold,
            result.could_not_fit,
            result.kept_original,
            len(result.pages_skipped),
            extra={"event": "overlay_block_placed"},
        )
        for entry in result.review:
            reason = getattr(entry.reason, "value", str(entry.reason))
            scale = "-" if entry.scale is None else f"{entry.scale:.2f}"
            is_residual = reason in _RESIDUAL_REASONS
            log.warning(
                "overlay review: page %d block %s reason %s scale %s",
                entry.page, entry.block_id, reason, scale,
                extra={
                    "event": "overlay_residual" if is_residual else "overlay_block_review",
                    "chunk_id": entry.unit_id,
                },
            )

    def _stamp_review_file(self, review_path: Path, job: JobRecord) -> None:
        """CR-46: record which pass ``overlay_review.json`` belongs to (``job_id`` +
        ``overlay_sha256``) so ``status`` never attributes a stale file to a new pass."""
        try:
            payload = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        payload["binding"] = {"job_id": job.id, "overlay_sha256": job.overlay_sha256}
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        written = atomic_write_bytes(review_path, data)
        if isinstance(written, Err):
            log.warning("could not stamp %s: %s", review_path.name, written.error.message)

    # ------------------------------------------------------------------ #
    # Stage: status (read-only)
    # ------------------------------------------------------------------ #

    def status(self) -> Result[StatusReport]:
        try:
            return self._status()
        except Exception as exc:  # noqa: BLE001 - boundary
            log.exception("status crashed", extra={"event": "stage_crashed"})
            return _internal("status", exc)

    def _is_stale_output(self, path: Path, job: JobRecord, payload: Optional[Any]) -> bool:
        """CR-46: does ``path`` belong to an *earlier* pass than the one in the database?

        ``overlay_review.json`` written by this version carries ``binding.job_id`` /
        ``binding.overlay_sha256``; a file without it (older version, the PDF itself) is
        judged by its mtime against the creation time of the job row.
        """
        binding = payload.get("binding") if isinstance(payload, dict) else None
        if isinstance(binding, dict) and binding.get("job_id"):
            return (
                binding.get("job_id") != job.id
                or binding.get("overlay_sha256") != job.overlay_sha256
            )
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            return True
        created = job.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return modified < created

    def _review_counts(self, job: JobRecord) -> Dict[str, int]:
        """Review counts of ``overlay_review.json`` when present and written for *this*
        pass (design doc 06, 5.6 / O5; CR-46)."""
        path = self.output_dir / OVERLAY_REVIEW_NAME
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if self._is_stale_output(path, job, payload):
            return {}
        counts: Dict[str, int] = {}
        raw_counts = payload.get("counts") if isinstance(payload, dict) else None
        if isinstance(raw_counts, dict):
            for key, value in raw_counts.items():
                if isinstance(value, int):
                    counts[str(key)] = value
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if isinstance(entries, list):
            counts.setdefault("entries", len(entries))
            # The by-reason tally only *fills in* reasons the file's own counts block does
            # not already carry. Several reason names are also count keys
            # (``shrunk_below_threshold``, ``could_not_fit``, ``kept_original``); adding the
            # tally on top of them reported every one of those twice.
            tally: Dict[str, int] = {}
            for entry in entries:
                reason = entry.get("reason") if isinstance(entry, dict) else None
                if isinstance(reason, str):
                    tally[reason] = tally.get(reason, 0) + 1
            for reason, total in tally.items():
                counts.setdefault(reason, total)
        return counts

    def _overlay_status(
        self, db: Database, job: JobRecord, now: datetime
    ) -> Optional[Dict[str, Any]]:
        """The ``overlay`` section of ``status`` (``None`` on a v1 file or without a pass)."""
        if db.schema_version < 2:
            return None
        repo = self._overlay_repo_factory(db, self.run_id, job.glossary_hash)
        counted = repo.counts(job.id, now)
        if isinstance(counted, Err):
            return None
        counts = counted.value
        if counts.total == 0 and job.overlay_status == "NONE":
            return None
        summary = self._overlay_summary(job, repo, counts)
        earliest = repo.earliest_next_attempt(job.id)
        output = self.output_dir / OVERLAY_OUTPUT_NAME
        review = self.output_dir / OVERLAY_REVIEW_NAME
        review_payload: Optional[Any] = None
        if review.is_file():
            try:
                review_payload = json.loads(review.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                review_payload = None
        # CR-46: outputs of an archived pass are never attributed to the current one.
        output_current = output.is_file() and not self._is_stale_output(
            output, job, review_payload
        )
        stale = [
            path.name
            for path, payload in ((output, review_payload), (review, review_payload))
            if path.is_file() and self._is_stale_output(path, job, payload)
        ]
        return {
            "status": summary.status,
            "counts": dataclasses.asdict(counts),
            "pages_completed": summary.pages_completed,
            "pages_total": summary.pages_total,
            "pages_skipped": summary.pages_skipped,
            "translatable_units": summary.translatable_units,
            "kept_units": summary.kept_units,
            "overlay_sha256": summary.overlay_sha256,
            "translate_headers": job.overlay_translate_headers,
            "keep_figure_text": job.overlay_keep_figure_text,
            "failed_unit_ids": summary.failed_unit_ids,
            "review_unit_ids": summary.review_unit_ids,
            "earliest_next_attempt": earliest.value if isinstance(earliest, Ok) else None,
            "review_counts": self._review_counts(job),
            "output_path": str(output) if output_current else None,
            "stale_outputs": stale,
        }

    def _status(self) -> Result[StatusReport]:
        db_path = self.db_path
        if not db_path.exists():
            return Ok(StatusReport(state_path=str(db_path), exists=False))
        opened = open_database(db_path, read_only=True, tool_version=self.tool_version)
        if isinstance(opened, Err):
            return opened
        db = opened.value
        try:
            jobs = JobRepository(db)
            found = jobs.get_single_job()
            if isinstance(found, Err):
                return found
            if found.value is None:
                return Ok(
                    StatusReport(
                        state_path=str(db_path),
                        exists=True,
                        schema_version=db.schema_version,
                        warnings=["state database has no job row"],
                    )
                )
            job = found.value
            chunks = ChunkRepository(db, self.run_id, job.glossary_hash)
            now = self._clock()
            counts = chunks.counts(job.id, now)
            if isinstance(counts, Err):
                return counts
            earliest = chunks.earliest_next_attempt(job.id)
            failed_ids = chunks.failed_ids(job.id)
            review_ids = chunks.review_ids(job.id)
            totals = chunks.totals(job.id)
            lease = LeaseRepository(db).get(job.id)
            last_run = jobs.get_last_run(job.id)
            warnings: List[str] = []
            lease_info: Optional[Dict[str, Any]] = None
            if isinstance(lease, Ok) and lease.value is not None:
                rec = lease.value
                stale = (now - rec.heartbeat_at).total_seconds() > self.settings.lease_stale_s
                lease_info = {
                    "pid": rec.holder_pid,
                    "host": rec.holder_host,
                    "run_id": rec.run_id,
                    "acquired_at": rec.acquired_at,
                    "heartbeat_at": rec.heartbeat_at,
                    "stale": stale,
                }
            run_info: Optional[Dict[str, Any]] = None
            mode: Optional[str] = None
            if isinstance(last_run, Ok) and last_run.value is not None:
                run = last_run.value
                mode = run.mode
                run_info = {
                    "run_id": run.id,
                    "command": run.command,
                    "mode": run.mode,
                    "provider": run.provider,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "outcome": run.outcome,
                    "exit_code": run.exit_code,
                    "chunks_completed": run.chunks_completed,
                    "chunks_failed": run.chunks_failed,
                    "chars_sent": run.chars_sent,
                    "provider_calls": run.provider_calls,
                    "rate_limited_count": run.rate_limited_count,
                }
                if run.outcome is None:
                    # CR-42: an unfinished overlay run has its counters in overlay_blocks.
                    if run.mode == MODE_OVERLAY and db.schema_version >= 2:
                        recovered = self._overlay_repo_factory(
                            db, self.run_id, job.glossary_hash
                        ).count_completed_by_run(run.id)
                    else:
                        recovered = chunks.count_completed_by_run(run.id)
                    if isinstance(recovered, Ok):
                        run_info["chunks_completed"], run_info["chunks_failed"] = recovered.value
                    warnings.append(f"last run {run.id} did not finish cleanly")
            # Units per provider: from the pass the last run worked on (same rule as
            # CR-42 above), so a fallback run shows "deepl 61, google 12".
            unit_repo: UnitRepository[Any] = chunks
            if mode == MODE_OVERLAY and db.schema_version >= 2:
                unit_repo = self._overlay_repo_factory(db, self.run_id, job.glossary_hash)
            counted_providers = unit_repo.provider_counts(job.id)
            provider_units = counted_providers.value if isinstance(counted_providers, Ok) else {}
            return Ok(
                StatusReport(
                    state_path=str(db_path),
                    exists=True,
                    job_id=job.id,
                    input_path=job.input_path,
                    input_sha256=job.input_sha256,
                    job_status=job.status,
                    provider=job.provider,
                    glossary_strategy=job.glossary_strategy,
                    glossary_hash=job.glossary_hash,
                    extractor=job.extractor_name,
                    fallback_used=job.fallback_used,
                    schema_version=db.schema_version,
                    counts=counts.value,
                    earliest_next_attempt=earliest.value if isinstance(earliest, Ok) else None,
                    failed_chunk_ids=failed_ids.value if isinstance(failed_ids, Ok) else [],
                    review_chunk_ids=review_ids.value if isinstance(review_ids, Ok) else [],
                    chars_sent_total=totals.value.chars_sent if isinstance(totals, Ok) else 0,
                    chars_billed_total=totals.value.chars_billed if isinstance(totals, Ok) else 0,
                    lease=lease_info,
                    last_run=run_info,
                    warnings=warnings + list(job.warnings),
                    figures=self._figure_totals(),
                    overlay=self._overlay_status(db, job, now),
                    mode=mode,
                    provider_units=provider_units,
                )
            )
        finally:
            close_database(db)

    # ------------------------------------------------------------------ #
    # Composite: run (9.3 severity rule)
    # ------------------------------------------------------------------ #

    def _has_reflow_pass(self) -> bool:
        """Chunks exist in the state database (read-only probe; ``False`` when unsure)."""
        if not self.db_path.exists():
            return False
        opened = open_database(self.db_path, read_only=True, tool_version=self.tool_version)
        if isinstance(opened, Err):
            return False
        db = opened.value
        try:
            found = JobRepository(db).get_single_job()
            if isinstance(found, Err) or found.value is None:
                return False
            counted = ChunkRepository(db, self.run_id, None).counts(found.value.id, self._clock())
            return isinstance(counted, Ok) and counted.value.total > 0
        finally:
            close_database(db)

    async def run(
        self,
        input_pdf: Path,
        *,
        extractor_name: Optional[str] = None,
        allow_non_english: bool = False,
        fresh: bool = False,
        min_chars_per_page: int = 50,
        min_count: int = 3,
        provider: Optional[str] = None,
        fallback_provider: Optional[str] = None,
        user_glossary: Optional[Path] = None,
        retry_failed: bool = False,
        allow_provider_switch: bool = False,
        require_glossary: bool = False,
        strict_glossary: bool = False,
        dry_run: bool = False,
        limit: Optional[int] = None,
        force_unlock: bool = False,
        formats: Optional[Sequence[str]] = None,
        allow_partial: bool = False,
        export_options: Optional[ExportOptions] = None,
        figure_options: Optional[FigureOptions] = None,
        mode: str = MODE_REFLOW,
        overlay_options: Optional[OverlayOptions] = None,
        overlay_min_scale: float = DEFAULT_OVERLAY_MIN_SCALE,
        overlay_floor_scale: float = DEFAULT_OVERLAY_FLOOR_SCALE,
        figure_legend: bool = False,
        margins_from_source: bool = True,
        overlay_uniform_scale: bool = True,
    ) -> Result[JobSummary]:
        self._composite = True
        self._run_mode = mode if mode in (MODE_REFLOW, MODE_OVERLAY) else None
        final_code = EXIT_FAILURE
        try:
            valid = self.settings.validate_for_provider()
            if isinstance(valid, Err):
                final_code = exit_code_for(valid.error)
                return valid
            if mode == MODE_OVERLAY:
                # E-51: reflow formats need a reflow pass; refused before any work is done.
                requested = normalise_formats(formats) if formats is not None else []
                if any(f != OVERLAY_FORMAT for f in requested) and not (
                    not fresh and self._has_reflow_pass()
                ):
                    return err(
                        ErrorCode.OVERLAY_STATE_MISSING,
                        "reflow formats (md/epub/pdf) need `--mode reflow`; `run --mode overlay` "
                        "produces pdf-overlay only (unless a reflow pass already exists here)",
                        ErrorScope.USER,
                    )
            extracted = self.extract(
                input_pdf,
                extractor_name=extractor_name,
                allow_non_english=allow_non_english,
                fresh=fresh,
                min_chars_per_page=min_chars_per_page,
                force_unlock=force_unlock,
                figure_options=figure_options,
            )
            if isinstance(extracted, Err):
                final_code = exit_code_for(extracted.error)
                return extracted
            built = self.glossary(min_count=min_count, user_glossary=user_glossary)
            if isinstance(built, Err):
                final_code = exit_code_for(built.error)
                return built
            translated = await self.translate(
                input_pdf,
                provider=provider,
                fallback_provider=fallback_provider,
                user_glossary=user_glossary,
                retry_failed=retry_failed,
                allow_provider_switch=allow_provider_switch,
                require_glossary=require_glossary,
                strict_glossary=strict_glossary,
                dry_run=dry_run,
                limit=limit,
                force_unlock=force_unlock,
                mode=mode,
                overlay_options=overlay_options,
            )
            if isinstance(translated, Err):
                final_code = exit_code_for(translated.error)
                return translated
            summary = translated.value
            final_code = summary.exit_code
            if dry_run or summary.exit_code not in (EXIT_SUCCESS, EXIT_PARTIAL):
                return Ok(summary)
            run_formats: Optional[Sequence[str]] = formats
            if run_formats is None and mode == MODE_OVERLAY:
                run_formats = [OVERLAY_FORMAT]
            exported = self.export(
                formats=run_formats,
                allow_partial=allow_partial,
                options=export_options,
                overlay_min_scale=overlay_min_scale,
                overlay_floor_scale=overlay_floor_scale,
                figure_legend=figure_legend,
                margins_from_source=margins_from_source,
                input_pdf=Path(input_pdf),  # CR-39: the export uses the verified input
                overlay_uniform_scale=overlay_uniform_scale,
            )
            warnings = list(summary.warnings)
            outputs: Dict[str, str] = {}
            overlay_section = summary.overlay
            if isinstance(exported, Err):
                final_code = worst_exit((summary.exit_code, exit_code_for(exported.error)))
                warnings.append(f"export:{exported.error.message}")
            else:
                final_code = worst_exit((summary.exit_code, exported.value.exit_code))
                outputs = exported.value.outputs
                warnings.extend(exported.value.warnings)
                if exported.value.overlay is not None:  # CR-51: real placement counts
                    overlay_section = exported.value.overlay
            finished_at = self._clock()
            summary = dataclasses.replace(
                summary,
                exit_code=final_code,
                outputs=outputs,
                warnings=warnings,
                overlay=overlay_section,
                finished_at=finished_at,
                duration_s=round(
                    summary.duration_s + (finished_at - summary.finished_at).total_seconds(), 3
                ),
            )
            self._write_summary(summary)
            return Ok(summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - boundary
            log.exception("run crashed", extra={"event": "stage_crashed"})
            return _internal("run", exc)
        finally:
            self._composite = False
            self._run_mode = None
            bound = self._bound
            if bound is not None:
                if self._run_started and not self._run_finished:
                    base = self._deferred_outcome or _zero_outcome(final_code)
                    self._finish_run(
                        bound,
                        dataclasses.replace(
                            base, outcome=outcome_name(final_code), exit_code=final_code
                        ),
                    )
                self._unbind(bound)
