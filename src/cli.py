"""Typer CLI (design doc 02, section 9).

Commands: ``run``, ``extract``, ``glossary``, ``translate``, ``export``,
``status``, ``providers``, ``extractors``, ``exporters``. The CLI composes
settings (flag > env > .env > default), wires logging, signals and the progress
UI, and maps ``Result`` values to exit codes (9.3). It never touches the
database or a concrete provider module: only the registries are imported for
the read-only listings (design 1.4).
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import logging
import os
import secrets
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, NoReturn, Optional, Sequence, TypeVar

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table

from . import __version__
from .config import (
    SOURCE_PAGE_SIZE,
    Settings,
    load_settings,
    margins_from_source,
    parse_pdf_margin,
)
from .domain.models import FigureTotals, JobSummary, OverlaySummary
from .domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, Result, err
from .exporters.base import ExportOptions, get_exporter, list_exporters
from .extractors.base import (
    FIGURE_DPI_MAX,
    FIGURE_DPI_MIN,
    FigureOptions,
    figure_options_from_settings,
    get_extractor,
    list_extractors,
)
from .logging_setup import ROOT_LOGGER_NAME, RedactionFilter, setup_logging
from .pipeline.orchestrator import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_SUCCESS,
    EXPORT_ERROR_PREFIX,
    LOGS_DIR_NAME,
    MODE_REFLOW,
    OVERLAY_FORMAT,
    ExportOutcome,
    ExtractOutcome,
    GlossaryOutcome,
    Orchestrator,
    OverlayOptions,
    ProgressEvent,
    StatusReport,
    describe_provider_units,
    exit_code_for,
    summary_to_jsonable,
)
from .translators.base import (
    GlossaryCleanup,
    TranslatorCapabilities,
    get_translator,
    list_translators,
)

__all__ = ["app", "main"]

T = TypeVar("T")

app = typer.Typer(
    name="book-translator",
    help="English PDF -> Turkish Markdown/EPUB/PDF translation (reflow or in-place overlay), "
    "resumable.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# --------------------------------------------------------------------------- #
# Choices
# --------------------------------------------------------------------------- #


class ProviderChoice(str, Enum):
    deepl = "deepl"
    gemini = "gemini"
    google = "google"
    local = "local"


class FallbackChoice(str, Enum):
    """``--fallback-provider``: the providers plus an explicit off switch."""

    deepl = "deepl"
    gemini = "gemini"
    google = "google"
    local = "local"
    none = "none"


class ExtractorChoice(str, Enum):
    pymupdf = "pymupdf"
    marker = "marker"


class FormatChoice(str, Enum):
    md = "md"
    epub = "epub"
    pdf = "pdf"
    pdf_overlay = "pdf-overlay"


class ModeChoice(str, Enum):
    reflow = "reflow"
    overlay = "overlay"


class PdfEngineChoice(str, Enum):
    native = "native"
    weasyprint = "weasyprint"


class LogLevelChoice(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


DEFAULT_FORMATS: Sequence[str] = ("md", "epub")
"""Reflow default (v1); the orchestrator adds ``pdf-overlay`` whenever an overlay pass exists
(design doc 06, Addendum A) - see ``_formats``."""


# --------------------------------------------------------------------------- #
# Shared option definitions (9.2)
# --------------------------------------------------------------------------- #

OUTPUT_OPTION = typer.Option(
    ..., "--output", "-o", help="Output directory for state and artifacts.", show_default=True
)
STATUS_OUTPUT_OPTION = typer.Option(
    Path("."), "--output", "-o", help="Output directory to inspect.", show_default=True
)
INPUT_OPTION = typer.Option(
    ..., "--input", "-i", help="Input PDF (English).", exists=False, show_default=True
)
EXPORT_INPUT_OPTION = typer.Option(
    None,
    "--input",
    "-i",
    help="Source PDF when it no longer sits at the path recorded in the state (moved or "
    "renamed file). It must be the very file the state was created from: the SHA-256 is "
    "compared before anything is written. A different file is refused with exit 5 for "
    "pdf-overlay; md/epub/pdf are written anyway (exit 2) with the figure labels left in "
    "English and their legend lists kept.",
    exists=False,
    show_default="the path recorded in the state",
)
LOG_LEVEL_OPTION = typer.Option(
    None,
    "--log-level",
    envvar="BOOK_TRANSLATOR_LOG_LEVEL",
    help="Console/file log level.",
    show_default="INFO",
    case_sensitive=False,
)
LOG_FILE_OPTION = typer.Option(
    None,
    "--log-file",
    help="JSONL log file.",
    show_default="<output>/logs/run-<run_id>.jsonl",
)
NO_PROGRESS_OPTION = typer.Option(
    False, "--no-progress", help="Disable the progress bar (auto-off when not a TTY)."
)
JSON_OPTION = typer.Option(False, "--json", help="Machine-readable summary on stdout.")
QUIET_OPTION = typer.Option(
    False, "--quiet", "-q", help="Only warnings and errors on the console."
)

EXTRACTOR_OPTION = typer.Option(
    None,
    "--extractor",
    envvar="BOOK_TRANSLATOR_EXTRACTOR",
    help="PDF extractor.",
    show_default="pymupdf",
    case_sensitive=False,
)
ALLOW_NON_ENGLISH_OPTION = typer.Option(
    False, "--allow-non-english", help="Continue when the source does not look English."
)
MIN_CHARS_PER_PAGE_OPTION = typer.Option(
    50, "--min-chars-per-page", min=0, help="Median chars/page below this -> no text layer."
)
FRESH_OPTION = typer.Option(
    False, "--fresh", help="Archive the previous state to state-archive/ and start over."
)

# v1.1 F1 figure options (design doc 06, §7.1)
FIGURE_DPI_OPTION = typer.Option(
    None,
    "--figure-dpi",
    envvar="BOOK_TRANSLATOR_FIGURE_DPI",
    min=FIGURE_DPI_MIN,
    max=FIGURE_DPI_MAX,
    help="Render DPI for figure PNGs (72-600).",
    show_default="200",
)
NO_FIGURES_OPTION = typer.Option(
    False,
    "--no-figures",
    help="v1 behaviour: bare image tokens, no images/ directory, no figures.json.",
)
FIGURE_LEGEND_OPTION = typer.Option(
    None,
    "--figure-legend/--no-figure-legend",
    help="Export: also render the label legend list after each figure caption in "
    "md/epub/pdf. The labels are translated inside the figure image either way; a figure "
    "whose labels could not be painted keeps its legend list.",
    show_default="--no-figure-legend",
)
FIGURE_LEGEND_LIMIT_OPTION = typer.Option(
    None,
    "--figure-legend-limit",
    envvar="BOOK_TRANSLATOR_FIGURE_LEGEND_LIMIT",
    min=0,
    help="Maximum labels per legend; the rest is noted as '... and K more'.",
    show_default="40",
)
FIGURE_EXCLUDE_PAGES_OPTION = typer.Option(
    None,
    "--figure-exclude-pages",
    envvar="BOOK_TRANSLATOR_FIGURE_EXCLUDE_PAGES",
    help='Pages skipped by figure detection, e.g. "2,5-7".',
    show_default="none",
)
FIGURE_MAX_MB_OPTION = typer.Option(
    None,
    "--figure-max-mb",
    envvar="BOOK_TRANSLATOR_FIGURE_MAX_MB",
    min=0.001,
    help="Per-figure PNG size limit in MB; the DPI backs off stepwise (floor 96).",
    show_default="8",
)

MIN_COUNT_OPTION = typer.Option(3, "--min-count", min=1, help="Minimum term frequency.")
FORCE_OPTION = typer.Option(
    False, "--force", help="Re-discover and merge into an existing glossary.json."
)
GLOSSARY_OPTION = typer.Option(
    None, "--glossary", help="User glossary JSON; its entries win over glossary.json."
)
CLEANUP_REMOTE_OPTION = typer.Option(
    False,
    "--cleanup-remote",
    help="Delete the provider-side glossaries this tool created earlier (DeepL caps how "
    "many an account may hold). Keeps the current one and never touches a glossary you "
    "created yourself. Deletion cannot be undone; the deleted names are printed.",
)

PROVIDER_OPTION = typer.Option(
    None,
    "--provider",
    envvar="BOOK_TRANSLATOR_PROVIDER",
    help="Translation provider.",
    show_default="deepl",
    case_sensitive=False,
)
CONCURRENCY_OPTION = typer.Option(
    None,
    "--concurrency",
    envvar="BOOK_TRANSLATOR_CONCURRENCY",
    min=1,
    max=64,
    help="Maximum concurrent provider requests.",
    show_default="4",
)
MAX_RETRIES_OPTION = typer.Option(
    None,
    "--max-retries",
    envvar="BOOK_TRANSLATOR_MAX_RETRIES",
    min=0,
    max=50,
    help="Retries per chunk for retryable errors.",
    show_default="5",
)
CHUNK_MIN_OPTION = typer.Option(
    None,
    "--chunk-min",
    envvar="BOOK_TRANSLATOR_CHUNK_MIN",
    min=100,
    help="Minimum chunk size (chars).",
    show_default="1000",
)
CHUNK_MAX_OPTION = typer.Option(
    None,
    "--chunk-max",
    envvar="BOOK_TRANSLATOR_CHUNK_MAX",
    min=200,
    help="Maximum chunk size (chars).",
    show_default="1500",
)
RETRY_FAILED_OPTION = typer.Option(
    False, "--retry-failed", help="Reset FAILED chunks to PENDING before translating."
)
ALLOW_PROVIDER_SWITCH_OPTION = typer.Option(
    False, "--allow-provider-switch", help="Resume with a different provider than recorded."
)
FALLBACK_PROVIDER_OPTION = typer.Option(
    None,
    "--fallback-provider",
    envvar="BOOK_TRANSLATOR_FALLBACK_PROVIDER",
    help=(
        "Switch to this provider (for the rest of the run) if --provider runs out of "
        "quota mid-translation, so a book does not stop half-translated. Left unset it "
        "picks any other provider whose key is configured; pass 'none' to switch that "
        "off. Unlike --allow-provider-switch, which permits a manual change between "
        "runs, this is automatic and only fires on an exhausted quota."
    ),
    show_default="automatic",
    case_sensitive=False,
)
WEB_HOST_OPTION = typer.Option("127.0.0.1", "--host", help="Address to listen on.")
WEB_PORT_OPTION = typer.Option(8765, "--port", help="Port to listen on.")
WEB_WORK_ROOT_OPTION = typer.Option(
    None, "--work-root", help="Root of the per-job directories (default: ./web-jobs)."
)
WEB_GLOSSARIES_OPTION = typer.Option(
    None, "--glossaries", help="Glossary directory (default: ./glossaries)."
)

REQUIRE_GLOSSARY_OPTION = typer.Option(
    False, "--require-glossary", help="Refuse to start with 0 approved glossary terms."
)
STRICT_GLOSSARY_OPTION = typer.Option(
    False, "--strict-glossary", help="Flag chunks with glossary misses for review."
)
GRACE_SECONDS_OPTION = typer.Option(
    None,
    "--grace-seconds",
    envvar="BOOK_TRANSLATOR_GRACE_S",
    min=0,
    help="Seconds to let in-flight requests finish after Ctrl+C.",
    show_default="30",
)
FORCE_UNLOCK_OPTION = typer.Option(
    False, "--force-unlock", help="Break a lease left by a dead process."
)
DRY_RUN_OPTION = typer.Option(
    False, "--dry-run", help="Chunk and report counts/characters without sending anything."
)
LIMIT_OPTION = typer.Option(
    None, "--limit", min=1, help="Translate at most N units this run (quality preview)."
)

# v1.1 F2 overlay options (design doc 06, 7.1; Addendum A)
MODE_OPTION = typer.Option(
    None,
    "--mode",
    envvar="BOOK_TRANSLATOR_MODE",
    help="reflow: Markdown/EPUB/PDF re-typeset from the text; overlay: translation written "
    "back into the source PDF layout (pdf-overlay).",
    show_default="reflow",
    case_sensitive=False,
)
OVERLAY_TRANSLATE_HEADERS_OPTION = typer.Option(
    False,
    "--overlay-translate-headers",
    help="Overlay: also translate running headers/footers (page numbers never).",
)
OVERLAY_KEEP_FIGURE_TEXT_OPTION = typer.Option(
    False,
    "--overlay-keep-figure-text",
    help="Overlay: leave the text inside figures untouched.",
)
OVERLAY_MIN_SCALE_OPTION = typer.Option(
    None,
    "--overlay-min-scale",
    envvar="BOOK_TRANSLATOR_OVERLAY_MIN_SCALE",
    min=0.05,
    max=1.0,
    help="Overlay: blocks shrunk below this factor are listed for review.",
    show_default="0.65",
)
OVERLAY_FLOOR_SCALE_OPTION = typer.Option(
    None,
    "--overlay-floor-scale",
    envvar="BOOK_TRANSLATOR_OVERLAY_FLOOR_SCALE",
    min=0.05,
    max=1.0,
    help="Overlay: hard shrink floor (must be <= --overlay-min-scale).",
    show_default="0.5",
)
OVERLAY_UNIFORM_SCALE_OPTION = typer.Option(
    True,
    "--overlay-uniform-scale/--no-overlay-uniform-scale",
    help="Overlay: body paragraphs of one page share one font scale (even type size); "
    "off: every block shrinks on its own.",
)

FORMAT_OPTION = typer.Option(
    None,
    "--format",
    "-f",
    help="Output format; repeatable. pdf-overlay = the source PDF with the translation "
    "written into its layout (default PDF output); pdf = re-typeset reflow PDF.",
    show_default="md,epub (+ pdf-overlay when an overlay pass exists; overlay mode: pdf-overlay)",
    case_sensitive=False,
)
ALLOW_PARTIAL_OPTION = typer.Option(
    False, "--allow-partial", help="Export with FAILED chunks marked as untranslated."
)
TITLE_OPTION = typer.Option(None, "--title", help="Book title for EPUB/PDF metadata.")
AUTHOR_OPTION = typer.Option(None, "--author", help="Author for EPUB/PDF metadata.")
CSS_OPTION = typer.Option(None, "--css", help="Custom stylesheet for EPUB/PDF.")
PDF_FONT_OPTION = typer.Option(
    None,
    "--pdf-font",
    help="TTF/OTF font file for the PDF export (validated for Turkish glyphs first).",
)
PDF_ENGINE_OPTION = typer.Option(
    None,
    "--pdf-engine",
    envvar="BOOK_TRANSLATOR_PDF_ENGINE",
    help="PDF renderer: native (pymupdf, always available) or weasyprint "
    r"(\[pdf] extra).",
    show_default="native",
    case_sensitive=False,
)
PDF_PAGE_SIZE_OPTION = typer.Option(
    None,
    "--pdf-page-size",
    envvar="BOOK_TRANSLATOR_PDF_PAGE_SIZE",
    help="Reflow PDF page: 'source' (same size as the input PDF) or a paper name "
    "(A4, Letter, ...; append -l for landscape).",
    show_default=SOURCE_PAGE_SIZE,
)
PDF_MARGIN_OPTION = typer.Option(
    None,
    "--pdf-margin",
    envvar="BOOK_TRANSLATOR_PDF_MARGIN",
    help="Reflow PDF margins: 'source' (measured from the input PDF) or 1/2/4 CSS-style "
    "values in mm or pt (top right bottom left).",
    show_default=SOURCE_PAGE_SIZE,
)
CHAPTER_LEVEL_OPTION = typer.Option(
    1, "--chapter-level", min=1, max=2, help="Heading level that starts an EPUB chapter."
)


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #


@dataclass
class _Common:
    output: Path
    log_level: Optional[str]
    log_file: Optional[Path]
    no_progress: bool
    json_output: bool
    quiet: bool
    run_id: str
    console: Console
    secrets: List[str] = field(default_factory=list)  # values that must never be printed
    job_id: Optional[str] = None  # last job bound by the orchestrator (for the failure log)


def _configure_stdio() -> None:
    """UTF-8 console on Windows (design 8.5); a no-op elsewhere or under test runners."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _new_run_id() -> str:
    return secrets.token_hex(6)


def _common(
    output: Path,
    log_level: Optional[LogLevelChoice],
    log_file: Optional[Path],
    no_progress: bool,
    json_output: bool,
    quiet: bool,
) -> _Common:
    _configure_stdio()
    console = Console(stderr=True, force_terminal=None, legacy_windows=False)
    raw_key = os.environ.get("DEEPL_API_KEY", "").strip()
    return _Common(
        output=Path(output),
        log_level=log_level.value if log_level is not None else None,
        log_file=log_file,
        no_progress=no_progress,
        json_output=json_output,
        quiet=quiet,
        run_id=_new_run_id(),
        console=console,
        secrets=[raw_key] if raw_key else [],
    )


def _fail(common: _Common, error: AppError) -> NoReturn:
    """Log, print (human or JSON) and exit with the mapped code.

    The message is redacted once more here (the logging filter does not cover the
    console or ``--json`` output), written to the JSONL log as ``command_failed`` when
    logging is configured, and printed with rich markup escaped so bracketed text such
    as ``[local]`` survives.
    """
    code = exit_code_for(error)
    message = RedactionFilter(common.secrets).redact_text(error.message)
    if logging.getLogger(ROOT_LOGGER_NAME).handlers:
        logging.getLogger(f"{ROOT_LOGGER_NAME}.cli").error(
            "command failed [%s]: %s",
            error.code.value,
            message,
            extra={"event": "command_failed", "file_only": True, "job_id": common.job_id},
        )
    if common.json_output:
        payload = {
            "ok": False,
            "run_id": common.run_id,
            "exit_code": code,
            "error": {"code": error.code.value, "scope": error.scope.value,
                      "message": message, "context": dict(error.context)},
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        text = escape(f"[{error.code.value}] {message}")
        common.console.print(f"[red]error[/red] {text}", markup=True)
    raise typer.Exit(code)


def _settings(common: _Common, **overrides: object) -> Settings:
    loaded = load_settings(log_level=common.log_level, **overrides)
    if isinstance(loaded, Err):
        _fail(common, loaded.error)
    settings = loaded.value
    if settings.deepl_api_key is not None:
        secret = settings.deepl_api_key.get_secret_value()
        if secret not in common.secrets:
            common.secrets.append(secret)
    return settings


def _setup_logging(common: _Common, settings: Optional[Settings], *, with_file: bool) -> None:
    level = common.log_level or (settings.log_level if settings is not None else "INFO")
    log_file = common.log_file
    if with_file and log_file is None:
        log_file = common.output / LOGS_DIR_NAME / f"run-{common.run_id}.jsonl"
    secrets_to_hide: List[str] = []
    if settings is not None and settings.deepl_api_key is not None:
        secrets_to_hide.append(settings.deepl_api_key.get_secret_value())
    try:
        setup_logging(level, log_file, common.run_id, common.console, common.quiet,
                      secrets=secrets_to_hide)
    except OSError as exc:
        setup_logging(level, None, common.run_id, common.console, common.quiet,
                      secrets=secrets_to_hide)
        common.console.print(
            f"[yellow]warning[/yellow] log file not writable: {escape(str(exc))}"
        )


def _orchestrator(common: _Common, settings: Settings) -> Orchestrator:
    return Orchestrator(
        settings, common.output, tool_version=__version__, run_id=common.run_id
    )


def _remember_job(common: _Common, orchestrator: Orchestrator) -> None:
    """Keep the job id after the stage unbound its logging context (CR-37)."""
    common.job_id = orchestrator.last_job_id


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


class _ProgressUI:
    """Rich bar on a TTY, throttled plain lines otherwise (9.6)."""

    def __init__(self, common: _Common) -> None:
        self._console = common.console
        self._tty = (
            not common.no_progress and not common.quiet and bool(sys.stderr.isatty())
        )
        self._progress: Optional[Progress] = None
        self._task_id: Optional[int] = None
        self._last_line = 0.0
        self._last_completed = -1

    def start(self) -> None:
        if not self._tty:
            return
        self._progress = Progress(
            TextColumn("translating"),
            BarColumn(),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            TextColumn("failed={task.fields[failed]}"),
            TextColumn("chars={task.fields[chars]}"),
            TextColumn("permits={task.fields[permits]}"),
            console=self._console,
            transient=False,
        )
        self._progress.start()
        self._task_id = int(
            self._progress.add_task("translating", total=None, failed=0, chars=0, permits=0)
        )

    def __call__(self, event: ProgressEvent) -> None:
        if self._progress is not None and self._task_id is not None:
            self._progress.update(
                self._task_id,  # type: ignore[arg-type]
                completed=event.completed,
                total=event.total,
                failed=event.failed,
                chars=event.chars_sent,
                permits=event.permits,
            )
            return
        now = time.monotonic()
        due = now - self._last_line >= 1.0 or event.completed % 10 == 0
        if not due or event.completed == self._last_completed:
            return
        self._last_line = now
        self._last_completed = event.completed
        typer.echo(
            f"progress completed={event.completed}/{event.total} failed={event.failed} "
            f"elapsed={_format_elapsed(event.elapsed_s)} chars_sent={event.chars_sent}",
            err=True,
        )

    def stop(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None


def _run_async(
    orchestrator: Orchestrator, make: Callable[[], Coroutine[Any, Any, Result[T]]]
) -> Result[T]:
    """Run a coroutine with Ctrl+C wiring: first signal cancels gracefully, second aborts."""

    async def main() -> Result[T]:
        loop = asyncio.get_running_loop()
        signals_seen = 0

        def handler(signum: int, frame: object) -> None:
            nonlocal signals_seen
            signals_seen += 1
            if signals_seen == 1:
                loop.call_soon_threadsafe(orchestrator.cancel.set)
            else:
                loop.call_soon_threadsafe(orchestrator.abort.set)

        previous: Dict[int, Any] = {}
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                previous[int(sig)] = signal.signal(sig, handler)
            except (ValueError, OSError):  # not the main thread / unsupported
                continue
        try:
            return await make()
        finally:
            for sig_value, old in previous.items():
                try:
                    signal.signal(sig_value, old)
                except (ValueError, OSError):
                    pass

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return err(
            ErrorCode.INTERRUPTED, "interrupted by the user; state intact", ErrorScope.USER
        )


def _run_sync(call: Callable[[], Result[T]]) -> Result[T]:
    """Run a synchronous stage; Ctrl+C becomes ``INTERRUPTED`` (exit 130), not click's abort."""
    try:
        return call()
    except KeyboardInterrupt:
        return err(
            ErrorCode.INTERRUPTED, "interrupted by the user; state intact", ErrorScope.USER
        )


def _echo_json(payload: object) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _warning_line(warning: str) -> str:
    """CR-91: a format that failed is printed as ``error:`` even though the export went on
    and the outputs that reached the disk are listed above it."""
    label = "error" if warning.startswith(EXPORT_ERROR_PREFIX) else "warning"
    return f"{label}: {warning}"


def _cleanup_lines(cleanup: GlossaryCleanup) -> List[str]:
    """Report a destructive step precisely: what went, what stayed, and why."""
    if not cleanup.supported:
        return [
            f"--cleanup-remote: provider {cleanup.provider!r} keeps no glossaries on its "
            "side; nothing to clean up"
        ]
    out: List[str] = []
    if cleanup.deleted:
        out.append(
            f"--cleanup-remote: deleted {len(cleanup.deleted)} {cleanup.provider} "
            f"{_glossaries(len(cleanup.deleted))} created by this tool:"
        )
        out.extend(f"  - {item.name} ({item.entries} entries)" for item in cleanup.deleted)
    else:
        out.append(
            f"--cleanup-remote: deleted nothing; this tool has no other glossary on "
            f"{cleanup.provider}"
        )
    kept: List[str] = []
    if cleanup.kept_current is not None:
        kept.append(f"{cleanup.kept_current} (the current one)")
    if cleanup.kept_foreign:
        kept.append(f"{cleanup.kept_foreign} {_glossaries(cleanup.kept_foreign)} not made by "
                    "this tool")
    if kept:
        out.append("--cleanup-remote: kept " + ", ".join(kept))
    out.extend(f"warning: could not delete {failure}" for failure in cleanup.failed)
    return out


def _glossaries(count: int) -> str:
    return "glossary" if count == 1 else "glossaries"


def _provider_units_lines(
    provider_units: Dict[str, int], primary: Optional[str] = None
) -> List[str]:
    """One "units by provider" line, but only when it says something new.

    A job translated entirely by the provider that is already printed above needs no
    breakdown; a job that fell back to a second provider very much does.
    """
    if not provider_units:
        return []
    if len(provider_units) == 1 and (primary is None or primary in provider_units):
        return []
    return [f"units by provider: {describe_provider_units(provider_units)}"]


def _print_summary(common: _Common, summary: JobSummary) -> None:
    if common.json_output:
        payload = summary_to_jsonable(summary)
        payload["ok"] = summary.exit_code == EXIT_SUCCESS
        _echo_json(payload)
        return
    c = summary.counts
    unit = "chunks" if summary.mode == MODE_REFLOW else "units"
    lines = [
        f"job {summary.job_id} run {summary.run_id} ({summary.command}, mode {summary.mode})",
        f"input: {summary.input_path}",
        f"provider: {summary.provider or '-'} glossary: "
        f"{summary.glossary_strategy.value if summary.glossary_strategy else '-'} "
        f"({summary.glossary_entries_applied} terms)",
        f"{unit}: {c.completed}/{c.total} completed, {c.failed} failed, {c.pending} pending"
        f" ({c.waiting_backoff} waiting for backoff)",
        f"chars sent: {summary.chars_sent_this_run} this run, {summary.chars_sent_total} total",
        f"duration: {_format_elapsed(summary.duration_s)}",
    ]
    lines[3:3] = _provider_units_lines(summary.provider_units, summary.provider)
    if summary.figures is not None:
        lines.append(_figure_line(summary.figures))
    if summary.overlay is not None:
        lines.append(_overlay_line(summary.overlay))
    if summary.failed_chunk_ids:
        lines.append(f"failed {unit[:-1]} ids: {summary.failed_chunk_ids}")
    if summary.review_chunk_ids:
        lines.append(f"review {unit[:-1]} ids: {summary.review_chunk_ids}")
    for name, path in summary.outputs.items():
        lines.append(f"output {name}: {path}")
    for warning in summary.warnings:
        lines.append(_warning_line(warning))
    lines.append(f"exit code: {summary.exit_code}")
    for line in lines:
        typer.echo(line)


def _finish_summary(common: _Common, result: Result[JobSummary]) -> None:
    if isinstance(result, Err):
        _fail(common, result.error)
    _print_summary(common, result.value)
    raise typer.Exit(result.value.exit_code)


def _finish_stage(common: _Common, result: Result[Any], lines: Callable[[Any], List[str]],
                  exit_code: int = EXIT_SUCCESS) -> None:
    if isinstance(result, Err):
        _fail(common, result.error)
    if common.json_output:
        payload = summary_to_jsonable(result.value)
        if isinstance(payload, dict):
            payload["ok"] = exit_code == EXIT_SUCCESS
            payload["run_id"] = common.run_id
            payload["exit_code"] = exit_code
        _echo_json(payload)
    else:
        for line in lines(result.value):
            typer.echo(line)
    raise typer.Exit(exit_code)


def _export_options(
    common: _Common,
    settings: Settings,
    title: Optional[str],
    author: Optional[str],
    css: Optional[Path],
    pdf_font: Optional[Path],
    chapter_level: int,
) -> ExportOptions:
    """Export options from the flags plus the F3 settings (``pdf_engine``, page size, margins).

    ``--pdf-page-size source`` / ``--pdf-margin source`` (the defaults, Addendum A) leave the
    page design to the export stage, which reads ``source_profile.json``.
    """
    options = ExportOptions(
        title=title,
        author=author,
        chapter_split_level=chapter_level,
        css_path=css,
        font_path=pdf_font,
        pdf_engine=settings.pdf_engine,
        page_size=settings.pdf_page_size,
    )
    if margins_from_source(settings.pdf_margin):
        return options
    margins = parse_pdf_margin(settings.pdf_margin)
    if isinstance(margins, Err):
        _fail(common, margins.error)
    return dataclasses.replace(options, margins_mm=margins.value)


def _overlay_options(translate_headers: bool, keep_figure_text: bool) -> OverlayOptions:
    return OverlayOptions(translate_headers=translate_headers, keep_figure_text=keep_figure_text)


def _overlay_overrides(
    overlay_min_scale: Optional[float], overlay_floor_scale: Optional[float]
) -> Dict[str, object]:
    return {"overlay_min_scale": overlay_min_scale, "overlay_floor_scale": overlay_floor_scale}


def _overlay_line(overlay: OverlaySummary) -> str:
    """``overlay: 412/412 units, 37/37 pages, ...`` (design doc 06, 5.6)."""
    c = overlay.counts
    parts = [
        f"{c.completed}/{c.total} units",
        f"{overlay.pages_completed}/{overlay.pages_total} pages",
        f"status {overlay.status}",
    ]
    if overlay.pages_skipped:
        parts.append(f"{overlay.pages_skipped} pages without text layer")
    if overlay.kept_units:
        parts.append(f"{overlay.kept_units} kept as-is")
    if overlay.review_unit_ids:
        parts.append(f"review {len(overlay.review_unit_ids)}")
    if overlay.review_path:  # an overlay export ran: the real placement counts (CR-51)
        parts.append(
            f"placed {overlay.placed}, shrunk below threshold "
            f"{overlay.shrunk_below_threshold}, could not fit {overlay.could_not_fit}, "
            f"kept original {overlay.kept_original}"
        )
    if overlay.review_path:
        parts.append(f"review list {overlay.review_path}")
    return f"overlay: {', '.join(parts)}"


def _placement_line(overlay: OverlaySummary, review_entries: int) -> str:
    """``overlay placement: placed 34, ...`` after an overlay export (CR-51)."""
    parts = [
        f"placed {overlay.placed}",
        f"shrunk below threshold {overlay.shrunk_below_threshold}",
        f"could not fit {overlay.could_not_fit}",
        f"kept original {overlay.kept_original}",
        f"review entries {review_entries}",
    ]
    if overlay.review_path:
        parts.append(f"review list {overlay.review_path}")
    return f"overlay placement: {', '.join(parts)}"


def _pdf_overrides(
    pdf_engine: Optional[PdfEngineChoice],
    pdf_page_size: Optional[str],
    pdf_margin: Optional[str],
) -> Dict[str, object]:
    """CLI F3 flags as ``Settings`` overrides (``None`` -> env/.env/default)."""
    return {
        "pdf_engine": pdf_engine.value if pdf_engine else None,
        "pdf_page_size": pdf_page_size,
        "pdf_margin": pdf_margin,
    }


def _formats(values: Optional[List[FormatChoice]]) -> Optional[List[str]]:
    """``None`` when no ``--format`` was given: the orchestrator applies the default rule
    (md,epub for a reflow pass, pdf-overlay for an overlay pass, both when both exist)."""
    if not values:
        return None
    return [v.value for v in values]


def _figure_overrides(
    no_figures: bool,
    figure_dpi: Optional[int],
    figure_legend: Optional[bool],
    figure_legend_limit: Optional[int],
    figure_exclude_pages: Optional[str],
    figure_max_mb: Optional[float],
) -> Dict[str, object]:
    """CLI figure flags as ``Settings`` overrides (``None`` -> env/.env/default)."""
    return {
        "figures": False if no_figures else None,
        "figure_dpi": figure_dpi,
        "figure_legend": figure_legend,
        "figure_legend_limit": figure_legend_limit,
        "figure_exclude_pages": figure_exclude_pages,
        "figure_max_mb": figure_max_mb,
    }


def _figure_options(common: _Common, settings: Settings) -> FigureOptions:
    resolved = figure_options_from_settings(settings)
    if isinstance(resolved, Err):
        _fail(common, resolved.error)
    return resolved.value


def _figure_line(totals: FigureTotals) -> str:
    """``figures: 3 (vector 2, raster 1, 412 KB, largest p002-f01.png)`` (design doc 06, §5.6)."""
    parts = [f"vector {totals.vector}", f"raster {totals.raster}"]
    if totals.mixed:
        parts.append(f"mixed {totals.mixed}")
    parts.append(f"{totals.total_bytes // 1024} KB")
    if totals.largest_file:
        parts.append(f"largest {totals.largest_file}")
    if totals.dpi_reduced:
        parts.append(f"dpi reduced for {totals.dpi_reduced}")
    if totals.pages_skipped:
        parts.append(f"{len(totals.pages_skipped)} pages skipped")
    return f"figures: {totals.count} ({', '.join(parts)})"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@app.callback()
def _root() -> None:
    """book-translator: English PDF -> Turkish Markdown/EPUB."""


@app.command()
def run(
    input: Path = INPUT_OPTION,
    output: Path = OUTPUT_OPTION,
    extractor: Optional[ExtractorChoice] = EXTRACTOR_OPTION,
    allow_non_english: bool = ALLOW_NON_ENGLISH_OPTION,
    min_chars_per_page: int = MIN_CHARS_PER_PAGE_OPTION,
    fresh: bool = FRESH_OPTION,
    figure_dpi: Optional[int] = FIGURE_DPI_OPTION,
    no_figures: bool = NO_FIGURES_OPTION,
    figure_legend: Optional[bool] = FIGURE_LEGEND_OPTION,
    figure_legend_limit: Optional[int] = FIGURE_LEGEND_LIMIT_OPTION,
    figure_exclude_pages: Optional[str] = FIGURE_EXCLUDE_PAGES_OPTION,
    figure_max_mb: Optional[float] = FIGURE_MAX_MB_OPTION,
    min_count: int = MIN_COUNT_OPTION,
    provider: Optional[ProviderChoice] = PROVIDER_OPTION,
    fallback_provider: Optional[FallbackChoice] = FALLBACK_PROVIDER_OPTION,
    glossary: Optional[Path] = GLOSSARY_OPTION,
    concurrency: Optional[int] = CONCURRENCY_OPTION,
    max_retries: Optional[int] = MAX_RETRIES_OPTION,
    chunk_min: Optional[int] = CHUNK_MIN_OPTION,
    chunk_max: Optional[int] = CHUNK_MAX_OPTION,
    retry_failed: bool = RETRY_FAILED_OPTION,
    allow_provider_switch: bool = ALLOW_PROVIDER_SWITCH_OPTION,
    require_glossary: bool = REQUIRE_GLOSSARY_OPTION,
    strict_glossary: bool = STRICT_GLOSSARY_OPTION,
    grace_seconds: Optional[int] = GRACE_SECONDS_OPTION,
    force_unlock: bool = FORCE_UNLOCK_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
    limit: Optional[int] = LIMIT_OPTION,
    mode: Optional[ModeChoice] = MODE_OPTION,
    overlay_translate_headers: bool = OVERLAY_TRANSLATE_HEADERS_OPTION,
    overlay_keep_figure_text: bool = OVERLAY_KEEP_FIGURE_TEXT_OPTION,
    overlay_min_scale: Optional[float] = OVERLAY_MIN_SCALE_OPTION,
    overlay_floor_scale: Optional[float] = OVERLAY_FLOOR_SCALE_OPTION,
    overlay_uniform_scale: bool = OVERLAY_UNIFORM_SCALE_OPTION,
    format: Optional[List[FormatChoice]] = FORMAT_OPTION,
    allow_partial: bool = ALLOW_PARTIAL_OPTION,
    title: Optional[str] = TITLE_OPTION,
    author: Optional[str] = AUTHOR_OPTION,
    css: Optional[Path] = CSS_OPTION,
    pdf_font: Optional[Path] = PDF_FONT_OPTION,
    pdf_engine: Optional[PdfEngineChoice] = PDF_ENGINE_OPTION,
    pdf_page_size: Optional[str] = PDF_PAGE_SIZE_OPTION,
    pdf_margin: Optional[str] = PDF_MARGIN_OPTION,
    chapter_level: int = CHAPTER_LEVEL_OPTION,
    log_level: Optional[LogLevelChoice] = LOG_LEVEL_OPTION,
    log_file: Optional[Path] = LOG_FILE_OPTION,
    no_progress: bool = NO_PROGRESS_OPTION,
    json_output: bool = JSON_OPTION,
    quiet: bool = QUIET_OPTION,
) -> None:
    """Full pipeline: extract -> glossary -> translate -> export (reflow or overlay mode)."""
    common = _common(output, log_level, log_file, no_progress, json_output, quiet)
    settings = _settings(
        common,
        provider=provider.value if provider else None,
        fallback_provider=fallback_provider.value if fallback_provider else None,
        extractor=extractor.value if extractor else None,
        concurrency=concurrency,
        max_retries=max_retries,
        chunk_min=chunk_min,
        chunk_max=chunk_max,
        grace_s=grace_seconds,
        **_figure_overrides(
            no_figures, figure_dpi, figure_legend, figure_legend_limit,
            figure_exclude_pages, figure_max_mb,
        ),
        **_pdf_overrides(pdf_engine, pdf_page_size, pdf_margin),
        **_overlay_overrides(overlay_min_scale, overlay_floor_scale),
    )
    run_mode = mode.value if mode else MODE_REFLOW
    valid = settings.validate_for_provider()
    if isinstance(valid, Err):
        _fail(common, valid.error)
    figure_options = _figure_options(common, settings)
    export_options = _export_options(
        common, settings, title, author, css, pdf_font, chapter_level
    )
    _setup_logging(common, settings, with_file=True)
    orchestrator = _orchestrator(common, settings)
    ui = _ProgressUI(common)
    orchestrator._progress = ui  # noqa: SLF001 - CLI owns the progress UI
    ui.start()
    try:
        result = _run_async(
            orchestrator,
            lambda: orchestrator.run(
                input,
                extractor_name=settings.extractor,
                allow_non_english=allow_non_english,
                fresh=fresh,
                min_chars_per_page=min_chars_per_page,
                min_count=min_count,
                provider=settings.provider,
                fallback_provider=settings.fallback_provider,
                user_glossary=glossary,
                retry_failed=retry_failed,
                allow_provider_switch=allow_provider_switch,
                require_glossary=require_glossary,
                strict_glossary=strict_glossary,
                dry_run=dry_run,
                limit=limit,
                force_unlock=force_unlock,
                formats=_formats(format),
                allow_partial=allow_partial,
                export_options=export_options,
                figure_options=figure_options,
                mode=run_mode,
                overlay_options=_overlay_options(
                    overlay_translate_headers, overlay_keep_figure_text
                ),
                overlay_min_scale=settings.overlay_min_scale,
                overlay_floor_scale=settings.overlay_floor_scale,
                figure_legend=settings.figure_legend,
                margins_from_source=margins_from_source(settings.pdf_margin),
                overlay_uniform_scale=overlay_uniform_scale,
            ),
        )
    finally:
        ui.stop()
        _remember_job(common, orchestrator)
    _finish_summary(common, result)


@app.command()
def extract(
    input: Path = INPUT_OPTION,
    output: Path = OUTPUT_OPTION,
    extractor: Optional[ExtractorChoice] = EXTRACTOR_OPTION,
    allow_non_english: bool = ALLOW_NON_ENGLISH_OPTION,
    min_chars_per_page: int = MIN_CHARS_PER_PAGE_OPTION,
    fresh: bool = FRESH_OPTION,
    figure_dpi: Optional[int] = FIGURE_DPI_OPTION,
    no_figures: bool = NO_FIGURES_OPTION,
    figure_legend_limit: Optional[int] = FIGURE_LEGEND_LIMIT_OPTION,
    figure_exclude_pages: Optional[str] = FIGURE_EXCLUDE_PAGES_OPTION,
    figure_max_mb: Optional[float] = FIGURE_MAX_MB_OPTION,
    force_unlock: bool = FORCE_UNLOCK_OPTION,
    log_level: Optional[LogLevelChoice] = LOG_LEVEL_OPTION,
    log_file: Optional[Path] = LOG_FILE_OPTION,
    no_progress: bool = NO_PROGRESS_OPTION,
    json_output: bool = JSON_OPTION,
    quiet: bool = QUIET_OPTION,
) -> None:
    """Extract the PDF into source_book.md and images/ (no provider contact).

    The legend block is always extracted (it carries the figure labels to the provider);
    whether the list is *rendered* is an export decision (`export --figure-legend`).
    """
    common = _common(output, log_level, log_file, no_progress, json_output, quiet)
    settings = _settings(
        common,
        extractor=extractor.value if extractor else None,
        **_figure_overrides(
            no_figures, figure_dpi, None, figure_legend_limit,
            figure_exclude_pages, figure_max_mb,
        ),
    )
    figure_options = _figure_options(common, settings)
    _setup_logging(common, settings, with_file=True)
    orchestrator = _orchestrator(common, settings)
    result = _run_sync(
        lambda: orchestrator.extract(
            input,
            extractor_name=settings.extractor,
            allow_non_english=allow_non_english,
            fresh=fresh,
            min_chars_per_page=min_chars_per_page,
            force_unlock=force_unlock,
            figure_options=figure_options,
        )
    )
    _remember_job(common, orchestrator)

    def lines(outcome: ExtractOutcome) -> List[str]:
        out = [
            f"extracted {outcome.pages} pages ({outcome.skipped_pages} skipped) with "
            f"{outcome.extractor}{' (fallback)' if outcome.fallback_used else ''}",
            f"language: {outcome.detected_language or 'unknown'} "
            f"({outcome.language_confidence:.2f}), images: {outcome.image_count}",
            f"written: {outcome.source_path}",
        ]
        if outcome.figures is not None:
            out.append(_figure_line(outcome.figures))
        out.extend(f"warning: {w}" for w in outcome.warnings)
        return out

    _finish_stage(common, result, lines)


@app.command()
def glossary(
    output: Path = OUTPUT_OPTION,
    min_count: int = MIN_COUNT_OPTION,
    force: bool = FORCE_OPTION,
    glossary: Optional[Path] = GLOSSARY_OPTION,
    cleanup_remote: bool = CLEANUP_REMOTE_OPTION,
    log_level: Optional[LogLevelChoice] = LOG_LEVEL_OPTION,
    log_file: Optional[Path] = LOG_FILE_OPTION,
    no_progress: bool = NO_PROGRESS_OPTION,
    json_output: bool = JSON_OPTION,
    quiet: bool = QUIET_OPTION,
) -> None:
    """Discover terms from source_book.md into glossary.json."""
    common = _common(output, log_level, log_file, no_progress, json_output, quiet)
    settings = _settings(common)
    _setup_logging(common, settings, with_file=True)
    orchestrator = _orchestrator(common, settings)
    result = _run_sync(
        lambda: orchestrator.glossary(min_count=min_count, force=force, user_glossary=glossary)
    )
    if cleanup_remote and isinstance(result, Ok):
        cleaned = _run_async(
            orchestrator,
            lambda: orchestrator.cleanup_remote_glossaries(
                provider=settings.provider, user_glossary=glossary
            ),
        )
        if isinstance(cleaned, Err):
            _remember_job(common, orchestrator)
            _fail(common, cleaned.error)
        result = Ok(dataclasses.replace(result.value, cleanup=cleaned.value))
    _remember_job(common, orchestrator)

    def lines(outcome: GlossaryOutcome) -> List[str]:
        out = [
            f"glossary {'written' if outcome.written else 'kept (use --force to re-discover)'}: "
            f"{outcome.path}",
            f"entries: {outcome.entries} (approved {outcome.approved}, proposed "
            f"{outcome.proposed}, ambiguous {outcome.ambiguous}, rejected {outcome.rejected})",
        ]
        if outcome.cleanup is not None:
            out.extend(_cleanup_lines(outcome.cleanup))
        return out

    _finish_stage(common, result, lines)


@app.command()
def translate(
    input: Path = INPUT_OPTION,
    output: Path = OUTPUT_OPTION,
    provider: Optional[ProviderChoice] = PROVIDER_OPTION,
    fallback_provider: Optional[FallbackChoice] = FALLBACK_PROVIDER_OPTION,
    glossary: Optional[Path] = GLOSSARY_OPTION,
    concurrency: Optional[int] = CONCURRENCY_OPTION,
    max_retries: Optional[int] = MAX_RETRIES_OPTION,
    chunk_min: Optional[int] = CHUNK_MIN_OPTION,
    chunk_max: Optional[int] = CHUNK_MAX_OPTION,
    retry_failed: bool = RETRY_FAILED_OPTION,
    allow_provider_switch: bool = ALLOW_PROVIDER_SWITCH_OPTION,
    require_glossary: bool = REQUIRE_GLOSSARY_OPTION,
    strict_glossary: bool = STRICT_GLOSSARY_OPTION,
    grace_seconds: Optional[int] = GRACE_SECONDS_OPTION,
    force_unlock: bool = FORCE_UNLOCK_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
    limit: Optional[int] = LIMIT_OPTION,
    mode: Optional[ModeChoice] = MODE_OPTION,
    overlay_translate_headers: bool = OVERLAY_TRANSLATE_HEADERS_OPTION,
    overlay_keep_figure_text: bool = OVERLAY_KEEP_FIGURE_TEXT_OPTION,
    log_level: Optional[LogLevelChoice] = LOG_LEVEL_OPTION,
    log_file: Optional[Path] = LOG_FILE_OPTION,
    no_progress: bool = NO_PROGRESS_OPTION,
    json_output: bool = JSON_OPTION,
    quiet: bool = QUIET_OPTION,
) -> None:
    """Chunk (if needed) and translate; resumable. --mode overlay translates the page blocks."""
    common = _common(output, log_level, log_file, no_progress, json_output, quiet)
    settings = _settings(
        common,
        provider=provider.value if provider else None,
        fallback_provider=fallback_provider.value if fallback_provider else None,
        concurrency=concurrency,
        max_retries=max_retries,
        chunk_min=chunk_min,
        chunk_max=chunk_max,
        grace_s=grace_seconds,
    )
    valid = settings.validate_for_provider()
    if isinstance(valid, Err):
        _fail(common, valid.error)
    _setup_logging(common, settings, with_file=True)
    orchestrator = _orchestrator(common, settings)
    ui = _ProgressUI(common)
    orchestrator._progress = ui  # noqa: SLF001 - CLI owns the progress UI
    ui.start()
    try:
        result = _run_async(
            orchestrator,
            lambda: orchestrator.translate(
                input,
                provider=settings.provider,
                fallback_provider=settings.fallback_provider,
                user_glossary=glossary,
                retry_failed=retry_failed,
                allow_provider_switch=allow_provider_switch,
                require_glossary=require_glossary,
                strict_glossary=strict_glossary,
                dry_run=dry_run,
                limit=limit,
                force_unlock=force_unlock,
                mode=mode.value if mode else MODE_REFLOW,
                overlay_options=_overlay_options(
                    overlay_translate_headers, overlay_keep_figure_text
                ),
            ),
        )
    finally:
        ui.stop()
        _remember_job(common, orchestrator)
    _finish_summary(common, result)


@app.command()
def export(
    output: Path = OUTPUT_OPTION,
    input: Optional[Path] = EXPORT_INPUT_OPTION,
    format: Optional[List[FormatChoice]] = FORMAT_OPTION,
    allow_partial: bool = ALLOW_PARTIAL_OPTION,
    title: Optional[str] = TITLE_OPTION,
    author: Optional[str] = AUTHOR_OPTION,
    css: Optional[Path] = CSS_OPTION,
    pdf_font: Optional[Path] = PDF_FONT_OPTION,
    pdf_engine: Optional[PdfEngineChoice] = PDF_ENGINE_OPTION,
    pdf_page_size: Optional[str] = PDF_PAGE_SIZE_OPTION,
    pdf_margin: Optional[str] = PDF_MARGIN_OPTION,
    chapter_level: int = CHAPTER_LEVEL_OPTION,
    overlay_min_scale: Optional[float] = OVERLAY_MIN_SCALE_OPTION,
    overlay_floor_scale: Optional[float] = OVERLAY_FLOOR_SCALE_OPTION,
    overlay_uniform_scale: bool = OVERLAY_UNIFORM_SCALE_OPTION,
    figure_legend: Optional[bool] = FIGURE_LEGEND_OPTION,
    force_unlock: bool = FORCE_UNLOCK_OPTION,
    log_level: Optional[LogLevelChoice] = LOG_LEVEL_OPTION,
    log_file: Optional[Path] = LOG_FILE_OPTION,
    no_progress: bool = NO_PROGRESS_OPTION,
    json_output: bool = JSON_OPTION,
    quiet: bool = QUIET_OPTION,
) -> None:
    """Write translated_book.<md|epub|pdf> (reflow pass) and/or translated_book.overlay.pdf."""
    common = _common(output, log_level, log_file, no_progress, json_output, quiet)
    settings = _settings(
        common,
        figure_legend=figure_legend,
        **_pdf_overrides(pdf_engine, pdf_page_size, pdf_margin),
        **_overlay_overrides(overlay_min_scale, overlay_floor_scale),
    )
    scales = settings.validate_overlay_scales()
    if isinstance(scales, Err):
        _fail(common, scales.error)
    export_options = _export_options(
        common, settings, title, author, css, pdf_font, chapter_level
    )
    _setup_logging(common, settings, with_file=True)
    orchestrator = _orchestrator(common, settings)
    result = _run_sync(
        lambda: orchestrator.export(
            formats=_formats(format),
            allow_partial=allow_partial,
            options=export_options,
            force_unlock=force_unlock,
            overlay_min_scale=settings.overlay_min_scale,
            overlay_floor_scale=settings.overlay_floor_scale,
            figure_legend=settings.figure_legend,
            margins_from_source=margins_from_source(settings.pdf_margin),
            input_pdf=input,
            overlay_uniform_scale=overlay_uniform_scale,
        )
    )
    _remember_job(common, orchestrator)

    def lines(outcome: ExportOutcome) -> List[str]:
        out = [f"output {name}: {path}" for name, path in outcome.outputs.items()]
        if outcome.overlay is not None:
            out.append(_placement_line(outcome.overlay, outcome.overlay_review_entries))
        if outcome.failed_chunk_ids:
            out.append(f"untranslated chunks marked: {outcome.failed_chunk_ids}")
        if outcome.review_chunk_ids:
            out.append(f"review chunk ids: {outcome.review_chunk_ids}")
        out.extend(_warning_line(w) for w in outcome.warnings)
        out.append(f"exit code: {outcome.exit_code}")
        return out

    code = result.value.exit_code if isinstance(result, Ok) else EXIT_FAILURE
    _finish_stage(common, result, lines, exit_code=code)


@app.command()
def status(
    output: Path = STATUS_OUTPUT_OPTION,
    json_output: bool = JSON_OPTION,
    log_level: Optional[LogLevelChoice] = LOG_LEVEL_OPTION,
    quiet: bool = QUIET_OPTION,
) -> None:
    """Show job/chunk state without touching the provider (read-only)."""
    common = _common(output, log_level, None, True, json_output, quiet)
    settings = _settings(common)
    _setup_logging(common, settings, with_file=False)
    orchestrator = _orchestrator(common, settings)
    result = orchestrator.status()

    def lines(report: StatusReport) -> List[str]:
        if not report.exists:
            return [f"no translation state at {report.state_path}"]
        if report.job_id is None:
            return [f"state at {report.state_path} has no job", *report.warnings]
        counts = report.counts
        out = [
            f"job {report.job_id} status {report.job_status} (schema v{report.schema_version})",
            f"input: {report.input_path}",
            f"input sha256: {report.input_sha256}",
            f"provider: {report.provider or '-'} glossary strategy: "
            f"{report.glossary_strategy or '-'}",
            *_provider_units_lines(report.provider_units),
            f"extractor: {report.extractor or '-'}"
            f"{' (fallback)' if report.fallback_used else ''}",
        ]
        if counts is not None:
            out.append(
                f"chunks: {counts.completed} completed, {counts.pending} pending "
                f"({counts.waiting_backoff} waiting for backoff), {counts.processing} "
                f"processing, {counts.failed} failed, {counts.total} total"
            )
        if report.earliest_next_attempt is not None:
            out.append(f"next attempt at: {report.earliest_next_attempt.isoformat()}")
        if report.failed_chunk_ids:
            out.append(f"failed chunk ids: {report.failed_chunk_ids}")
        if report.review_chunk_ids:
            out.append(f"review chunk ids: {report.review_chunk_ids}")
        out.append(
            f"chars sent: {report.chars_sent_total} (billed {report.chars_billed_total})"
        )
        if report.figures is not None:
            out.append(_figure_line(report.figures))
        if report.overlay is not None:
            ov = report.overlay
            oc = ov.get("counts") or {}
            line = (
                f"overlay: {ov.get('status')}, {oc.get('completed', 0)}/{oc.get('total', 0)} "
                f"units, {ov.get('pages_completed', 0)}/{ov.get('pages_total', 0)} pages"
            )
            skipped = ov.get("pages_skipped") or 0
            if skipped:
                line += f", {skipped} pages without text layer"
            review_counts = ov.get("review_counts") or {}
            if review_counts:
                listed = ", ".join(f"{k} {v}" for k, v in sorted(review_counts.items()))
                line += f", review: {listed}"
            if ov.get("output_path"):
                line += f", output {ov.get('output_path')}"
            out.append(line)
        if report.lease:
            lease = report.lease
            hb = lease.get("heartbeat_at")
            hb_text = hb.isoformat() if isinstance(hb, datetime) else str(hb)
            out.append(
                f"lease: pid {lease.get('pid')} on {lease.get('host')} run {lease.get('run_id')}"
                f" heartbeat {hb_text}{' (stale)' if lease.get('stale') else ''}"
            )
        if report.last_run:
            run_info = report.last_run
            out.append(
                f"last run {run_info.get('run_id')} ({run_info.get('command')}): "
                f"{run_info.get('outcome') or 'unfinished'} exit {run_info.get('exit_code')}"
            )
        out.extend(f"warning: {w}" for w in report.warnings)
        return out

    _finish_stage(common, result, lines)


def _print_listing(common: _Common, title: str, rows: List[Dict[str, Any]]) -> None:
    if common.json_output:
        _echo_json(rows)
        return
    table = Table(title=escape(title))
    for column in rows[0].keys() if rows else ("name",):
        table.add_column(escape(str(column)))
    for row in rows:
        table.add_row(*(escape(str(v)) for v in row.values()))
    Console(force_terminal=None, legacy_windows=False).print(table)


def _glossary_support(caps: TranslatorCapabilities) -> str:
    """How the provider applies the glossary, which is not a two-way choice.

    A native glossary is uploaded, a prompt glossary is written into every request,
    and only the third case leaves the terms to the post-check alone.
    """
    if caps.supports_glossary:
        return "native"
    if caps.glossary_in_prompt:
        return "in prompt"
    return "post-check only"


@app.command()
def providers(json_output: bool = JSON_OPTION) -> None:
    """List translation providers and whether they can be used right now."""
    common = _common(Path("."), None, None, True, json_output, False)
    rows: List[Dict[str, Any]] = []
    for name in list_translators():
        loaded = load_settings(provider=name)
        if isinstance(loaded, Err):
            rows.append({"name": name, "available": False, "detail": loaded.error.message})
            continue
        made = get_translator(name, loaded.value)
        if isinstance(made, Ok):
            caps = made.value.capabilities
            detail = (
                f"glossary={_glossary_support(caps)}, "
                f"max_chars={caps.max_chars_per_request}"
            )
            rows.append({"name": name, "available": True, "detail": detail})
        else:
            rows.append({"name": name, "available": False, "detail": made.error.message})
    _print_listing(common, "providers", rows)
    raise typer.Exit(EXIT_SUCCESS)


@app.command()
def extractors(json_output: bool = JSON_OPTION) -> None:
    """List PDF extractors and their availability."""
    common = _common(Path("."), None, None, True, json_output, False)
    rows: List[Dict[str, Any]] = []
    for name in list_extractors():
        got = get_extractor(name)
        if isinstance(got, Ok):
            rows.append(
                {
                    "name": name,
                    "available": True,
                    "detail": f"ocr={'yes' if got.value.supports_ocr else 'no'}, "
                    f"footnotes={'yes' if got.value.supports_footnotes else 'no'}",
                }
            )
        else:
            rows.append({"name": name, "available": False, "detail": got.error.message})
    _print_listing(common, "extractors", rows)
    raise typer.Exit(EXIT_SUCCESS)


@app.command()
def exporters(json_output: bool = JSON_OPTION) -> None:
    """List export formats and their availability."""
    common = _common(Path("."), None, None, True, json_output, False)
    rows: List[Dict[str, Any]] = []
    for name in list_exporters():
        got = get_exporter(name)
        if isinstance(got, Ok):
            exporter = got.value
            engines = exporter.engines()
            detail = (
                f"suffix={exporter.file_suffix}, optional={'yes' if exporter.optional else 'no'}"
            )
            row: Dict[str, Any] = {
                "name": name,
                "available": exporter.is_available(),
                "detail": detail,
            }
            if engines:
                listed = ", ".join(
                    f"{engine} ({'available' if ok else 'unavailable'})"
                    for engine, ok in engines.items()
                )
                row["detail"] = f"{detail}, engines: {listed}"
                if common.json_output:  # the table shows engines inside ``detail``
                    row["engines"] = engines
            rows.append(row)
        else:
            rows.append({"name": name, "available": False, "detail": got.error.message})
    if not any(row["name"] == OVERLAY_FORMAT for row in rows):
        available = importlib.util.find_spec(f"{__package__}.exporters.pdf_overlay_exporter")
        rows.append(
            {
                "name": OVERLAY_FORMAT,
                "available": available is not None,
                "detail": "suffix=.overlay.pdf, optional=no, needs `translate --mode overlay`",
            }
        )
    _print_listing(common, "exporters", rows)
    raise typer.Exit(EXIT_SUCCESS)


def main() -> None:
    """Console-script entry point (``book-translator``)."""
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover - last resort
        raise SystemExit(EXIT_INTERRUPTED) from None


if __name__ == "__main__":  # pragma: no cover
    main()


@app.command()
def web(
    host: str = WEB_HOST_OPTION,
    port: int = WEB_PORT_OPTION,
    work_root: Optional[Path] = WEB_WORK_ROOT_OPTION,
    glossaries: Optional[Path] = WEB_GLOSSARIES_OPTION,
) -> None:
    """Start the local web UI: upload a PDF, translate it, download the result.

    The same server as ``python -m book_translator.web``. This spelling exists because
    the module form only resolves once the virtual environment is active, which is easy
    to miss - the console script always runs the interpreter it was installed into.
    """
    argv: List[str] = ["--host", host, "--port", str(port)]
    if work_root is not None:
        argv += ["--work-root", str(work_root)]
    if glossaries is not None:
        argv += ["--glossaries", str(glossaries)]
    try:
        from .web.__main__ import main as web_main
    except ImportError as exc:  # the [web] extra is not installed
        typer.echo(
            'the web UI needs its optional dependencies: pip install -e ".[web]"'
            f" ({exc.__class__.__name__}: {exc})",
            err=True,
        )
        raise typer.Exit(code=EXIT_FAILURE) from exc
    raise typer.Exit(code=web_main(argv))
