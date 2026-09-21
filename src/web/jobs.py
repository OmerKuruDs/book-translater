"""Background job registry for the local web UI.

One job is one output directory under the work root: the uploaded PDF is written
there as ``input.pdf`` and :meth:`Orchestrator.run` is driven over it exactly the way
``cli.run`` drives it (same settings loading, same ``ExportOptions``/``FigureOptions``,
same ``Result`` handling, same exit-code mapping). Nothing here re-implements a
pipeline stage; :class:`_TrackingOrchestrator` only *records* which stage the run
entered, so the browser can poll it.

A job runs in its own thread with its own event loop (the CLI's ``asyncio.run`` +
signal wiring cannot be reused: signal handlers only install on the main thread).
Job state lives in memory - this is a single-user local tool - but it is kept after
the run finishes, so the estimate, the character counter and the download links stay
valid for the lifetime of the process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from rich.console import Console

from .. import __version__
from ..config import Settings, load_settings, margins_from_source, parse_pdf_margin
from ..domain.models import JobSummary
from ..domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, Result
from ..exporters.base import ExportOptions
from ..extractors.base import FigureOptions, figure_options_from_settings
from ..logging_setup import RedactionFilter, setup_logging
from ..pipeline.orchestrator import (
    EXIT_FAILURE,
    EXIT_SUCCESS,
    LOGS_DIR_NAME,
    MODE_OVERLAY,
    MODE_REFLOW,
    ExportOutcome,
    ExtractOutcome,
    GlossaryOutcome,
    Orchestrator,
    ProgressEvent,
    TranslatorFactory,
    exit_code_for,
)

log = logging.getLogger(f"{__name__}")

INPUT_NAME = "input.pdf"
"""The uploaded PDF is always stored under this fixed name: the browser-supplied file
name never reaches the file system (path traversal)."""

STAGE_EXTRACT = "extract"
STAGE_GLOSSARY = "glossary"
STAGE_TRANSLATE = "translate"
STAGE_EXPORT = "export"

STAGE_ORDER: Sequence[str] = (STAGE_EXTRACT, STAGE_GLOSSARY, STAGE_TRANSLATE, STAGE_EXPORT)
STAGE_LABELS: Dict[str, str] = {
    STAGE_EXTRACT: "metin çıkarma",
    STAGE_GLOSSARY: "sözlük",
    STAGE_TRANSLATE: "çeviri",
    STAGE_EXPORT: "çıktı yazma",
}

PHASE_NEW = "new"
PHASE_ESTIMATING = "estimating"
PHASE_AWAITING_CONFIRM = "awaiting_confirm"
PHASE_RUNNING = "running"
PHASE_DONE = "done"
PHASE_ERROR = "error"

PHASE_LABELS: Dict[str, str] = {
    PHASE_NEW: "hazır",
    PHASE_ESTIMATING: "tahmin çıkarılıyor",
    PHASE_AWAITING_CONFIRM: "onay bekliyor",
    PHASE_RUNNING: "çalışıyor",
    PHASE_DONE: "bitti",
    PHASE_ERROR: "hata",
}

OUTPUT_LABELS: Dict[str, str] = {
    "markdown": "Markdown (.md)",
    "epub": "EPUB (.epub)",
    "pdf": "PDF (yeniden dizilmiş)",
    "pdf-overlay": "PDF (kaynak düzeni korunmuş)",
}

#: Turkish explanation added next to ``AppError.message`` for the failures a user of the
#: web UI actually runs into. The message itself is produced (and redacted) by the tool.
ERROR_HINTS: Dict[ErrorCode, str] = {
    ErrorCode.NO_TEXT_LAYER: (
        "PDF taranmış görünüyor: sayfalarda seçilebilir metin katmanı yok. "
        "Önce OCR uygulanmış bir kopya kullanın."
    ),
    ErrorCode.OVERLAY_PDF_RESTRICTED: (
        "PDF şifreli ya da değiştirme izni kapalı. Kaynak düzenini koruyan overlay modu "
        "PDF'i değiştirmek zorunda; izinsiz bir kopyada çalışamaz. Kısıtsız bir kopya "
        "kullanın ya da reflow modunu seçin."
    ),
    ErrorCode.PROVIDER_QUOTA: (
        "Sağlayıcı kotası doldu; çeviri durduruldu. Durum korundu: kota yenilendiğinde "
        "aynı işi yeniden başlatmak kalınan yerden devam eder, ödenen karakterler "
        "ikinci kez gönderilmez."
    ),
    ErrorCode.PROVIDER_GLOSSARY_LIMIT: (
        "Sağlayıcı hesabında tutulabilecek sözlük sayısı dolu; karakter kotasıyla ilgisi yok, "
        "beklemek çözmez. Var olan bir sözlüğü silin: `book-translator glossary "
        "--cleanup-remote` bu aracın oluşturduğu eski sözlükleri temizler."
    ),
    ErrorCode.PROVIDER_AUTH: (
        "Sağlayıcı kimlik doğrulamayı reddetti. .env dosyasındaki API anahtarını kontrol edin."
    ),
    ErrorCode.PROVIDER_CONFIG: (
        "Yapılandırma eksik ya da hatalı. Sunucuyu başlattığınız dizindeki .env dosyasını "
        "kontrol edin."
    ),
    ErrorCode.INPUT_NOT_PDF: "Yüklenen dosya geçerli bir PDF değil.",
    ErrorCode.INPUT_UNREADABLE: "PDF okunamadı; dosya bozuk ya da parola korumalı olabilir.",
    ErrorCode.INPUT_NOT_FOUND: "Yüklenen PDF bulunamadı; işi yeniden başlatın.",
    ErrorCode.NON_ENGLISH_SOURCE: (
        "Kaynak metin İngilizce görünmüyor. Bu araç İngilizce -> Türkçe çeviri yapar."
    ),
    ErrorCode.GLOSSARY_INVALID: "Seçilen sözlük dosyası geçersiz.",
    ErrorCode.STATE_LOCKED: (
        "Bu işin çıktı dizini başka bir işlem tarafından kilitli. Diğer işlemin bitmesini "
        "bekleyin."
    ),
    ErrorCode.STATE_HASH_MISMATCH: (
        "Kayıtlı durum başka bir PDF ile oluşturulmuş. Yeni bir iş başlatın."
    ),
    ErrorCode.EXPORT_REFUSED_PARTIAL: (
        "Bazı birimler çevrilemediği için çıktı eksik kalacaktı ve yazılmadı."
    ),
    ErrorCode.EXPORTER_UNAVAILABLE: (
        "İstenen çıktı biçimi için gereken paket kurulu değil."
    ),
    ErrorCode.INTERRUPTED: "İş durduruldu; kayıtlı durum bozulmadı.",
    ErrorCode.INTERNAL: "Beklenmeyen bir hata oluştu; ayrıntı için sunucu günlüğüne bakın.",
}

#: ``_dry_run`` reports its estimate as a warning line; there is no structured field for
#: it on ``JobSummary`` yet (see the report accompanying this branch).
DRY_RUN_WARNING = re.compile(
    r"^dry_run:\s*(?P<units>\d+)\s+\S+(?:\s+\S+)?\s+pending,\s*(?P<chars>\d+)\s+payload characters"
)


# --------------------------------------------------------------------------- #
# Stage tracking
# --------------------------------------------------------------------------- #


class _TrackingOrchestrator(Orchestrator):
    """``Orchestrator`` that announces the stage it is entering.

    ``run`` calls ``self.extract`` / ``self.glossary`` / ``self.translate`` /
    ``self.export``; overriding them is enough to follow the pipeline without changing
    a single line of it. Every override delegates straight to ``super()``.
    """

    def __init__(self, *args: Any, on_stage: Callable[[str], None], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_stage = on_stage

    def extract(self, *args: Any, **kwargs: Any) -> Result[ExtractOutcome]:
        self._on_stage(STAGE_EXTRACT)
        return super().extract(*args, **kwargs)

    def glossary(self, *args: Any, **kwargs: Any) -> Result[GlossaryOutcome]:
        self._on_stage(STAGE_GLOSSARY)
        return super().glossary(*args, **kwargs)

    async def translate(self, *args: Any, **kwargs: Any) -> Result[JobSummary]:
        self._on_stage(STAGE_TRANSLATE)
        return await super().translate(*args, **kwargs)

    def export(self, *args: Any, **kwargs: Any) -> Result[ExportOutcome]:
        self._on_stage(STAGE_EXPORT)
        return super().export(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Job state
# --------------------------------------------------------------------------- #


@dataclass
class JobProgress:
    """Live translate-stage counters (the only stage that reports unit progress)."""

    stage: Optional[str] = None
    completed: int = 0
    failed: int = 0
    total: int = 0
    chars_sent: int = 0
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class JobEstimate:
    """What a ``--dry-run`` said the real run would send."""

    units: int
    chars: int


@dataclass(frozen=True)
class JobOutput:
    key: str
    name: str
    label: str
    size_bytes: int


@dataclass(frozen=True)
class JobResult:
    exit_code: int
    chars_sent_this_run: int
    chars_sent_total: int
    outputs: List[JobOutput]
    warnings: List[str]


@dataclass(frozen=True)
class JobFailure:
    code: str
    message: str
    exit_code: int
    hint: str


@dataclass
class Job:
    """One upload and the run(s) driven over it. Mutated only under the manager's lock."""

    id: str
    directory: Path
    filename: str
    mode: str
    glossary: Optional[str]
    created_at: float = field(default_factory=time.time)
    phase: str = PHASE_NEW
    progress: JobProgress = field(default_factory=JobProgress)
    estimate: Optional[JobEstimate] = None
    result: Optional[JobResult] = None
    failure: Optional[JobFailure] = None
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def input_path(self) -> Path:
        return self.directory / INPUT_NAME


JobRunner = Callable[[Callable[[], None]], None]
"""How a job body is executed. The default starts a daemon thread; tests can pass an
inline runner to make a request deterministic."""


def thread_runner(body: Callable[[], None]) -> None:
    threading.Thread(target=body, daemon=True).start()


def inline_runner(body: Callable[[], None]) -> None:
    """Run the job to completion before returning (deterministic tests).

    Still on its own thread: the job opens an event loop of its own and the caller may
    already be running inside one.
    """
    thread = threading.Thread(target=body)
    thread.start()
    thread.join()


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #


class JobManager:
    """Creates job directories, runs the pipeline over them and keeps the state."""

    def __init__(
        self,
        work_root: Path,
        glossary_dir: Path,
        *,
        translator_factory: Optional[TranslatorFactory] = None,
        runner: Optional[JobRunner] = None,
        configure_logging: bool = True,
    ) -> None:
        self.work_root = Path(work_root)
        self.glossary_dir = Path(glossary_dir)
        self._translator_factory = translator_factory
        self._runner: JobRunner = runner or thread_runner
        self._configure_logging = configure_logging
        self._logging_ready = False
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}

    # -- glossaries -------------------------------------------------------- #

    def glossary_names(self) -> List[str]:
        """``*.json`` files directly under the glossary directory, sorted."""
        try:
            return sorted(p.name for p in self.glossary_dir.glob("*.json") if p.is_file())
        except OSError:
            return []

    def glossary_path(self, name: str) -> Optional[Path]:
        """Resolve a *listed* glossary name; anything else (including a path) is ``None``."""
        if name not in self.glossary_names():
            return None
        return self.glossary_dir / name

    # -- job lifecycle ----------------------------------------------------- #

    def create(self, *, filename: str, mode: str, glossary: Optional[str]) -> Job:
        job_id = secrets.token_hex(8)
        directory = self.work_root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        job = Job(
            id=job_id,
            directory=directory,
            filename=filename,
            mode=mode,
            glossary=glossary,
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def discard(self, job: Job) -> None:
        """Forget a job whose upload was refused and remove its (empty) directory."""
        with self._lock:
            self._jobs.pop(job.id, None)
        shutil.rmtree(job.directory, ignore_errors=True)

    def start(self, job: Job, *, dry_run: bool) -> None:
        """Queue the pipeline for ``job``; returns as soon as the body is scheduled."""
        with self._lock:
            job.phase = PHASE_ESTIMATING if dry_run else PHASE_RUNNING
            job.progress = JobProgress()
            job.failure = None
            if not dry_run:
                job.result = None
            job.done.clear()
        self._runner(lambda: self._execute(job, dry_run=dry_run))

    def snapshot(self, job: Job) -> Dict[str, Any]:
        """JSON-ready view of ``job`` taken under the lock."""
        with self._lock:
            progress = dataclasses.replace(job.progress)
            estimate = job.estimate
            result = job.result
            failure = job.failure
            payload: Dict[str, Any] = {
                "id": job.id,
                "filename": job.filename,
                "mode": job.mode,
                "glossary": job.glossary,
                "phase": job.phase,
                "phase_label": PHASE_LABELS.get(job.phase, job.phase),
                "stage": progress.stage,
                "stage_label": (
                    STAGE_LABELS.get(progress.stage, progress.stage)
                    if progress.stage
                    else None
                ),
                "stages": list(STAGE_ORDER),
                "completed": progress.completed,
                "failed": progress.failed,
                "total": progress.total,
                "chars_sent": progress.chars_sent,
                "elapsed_s": round(progress.elapsed_s, 1),
            }
        payload["estimate"] = (
            None if estimate is None else {"units": estimate.units, "chars": estimate.chars}
        )
        payload["result"] = (
            None
            if result is None
            else {
                "exit_code": result.exit_code,
                "chars_sent_this_run": result.chars_sent_this_run,
                "chars_sent_total": result.chars_sent_total,
                "warnings": list(result.warnings),
                "outputs": [
                    {
                        "key": o.key,
                        "name": o.name,
                        "label": o.label,
                        "size_bytes": o.size_bytes,
                    }
                    for o in result.outputs
                ],
            }
        )
        payload["error"] = (
            None
            if failure is None
            else {
                "code": failure.code,
                "message": failure.message,
                "exit_code": failure.exit_code,
                "hint": failure.hint,
            }
        )
        return payload

    def output_file(self, job: Job, key: str) -> Optional[Path]:
        """Path of a produced output, or ``None`` when ``key`` is not one of them.

        Only keys the export stage reported are served, and the resolved path must stay
        inside the job directory: the browser can never name a file of its own.
        """
        with self._lock:
            result = job.result
        if result is None:
            return None
        for output in result.outputs:
            if output.key != key:
                continue
            candidate = (job.directory / output.name).resolve()
            root = job.directory.resolve()
            if candidate == root or root not in candidate.parents:
                return None
            return candidate if candidate.is_file() else None
        return None

    # -- the run ----------------------------------------------------------- #

    def _set(self, job: Job, **changes: Any) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(job, name, value)

    def _on_stage(self, job: Job, stage: str) -> None:
        with self._lock:
            job.progress.stage = stage

    def _on_progress(self, job: Job, event: ProgressEvent) -> None:
        with self._lock:
            job.progress.completed = event.completed
            job.progress.failed = event.failed
            job.progress.total = event.total
            job.progress.chars_sent = event.chars_sent
            job.progress.elapsed_s = event.elapsed_s

    def _fail(self, job: Job, error: AppError, redaction: RedactionFilter) -> None:
        failure = JobFailure(
            code=error.code.value,
            message=redaction.redact_text(error.message),
            exit_code=exit_code_for(error),
            hint=ERROR_HINTS.get(error.code, ""),
        )
        with self._lock:
            job.failure = failure
            job.phase = PHASE_ERROR
            job.progress.stage = None

    def _ensure_logging(self, settings: Settings) -> None:
        """Configure the tool's logger tree once per manager (secrets redacted)."""
        if not self._configure_logging or self._logging_ready:
            return
        self._logging_ready = True
        key = (
            settings.deepl_api_key.get_secret_value()
            if settings.deepl_api_key is not None
            else None
        )
        log_dir = self.work_root / LOGS_DIR_NAME
        console = Console(stderr=True, force_terminal=None, legacy_windows=False)
        for target in (log_dir / "web.jsonl", None):
            try:
                if target is not None:
                    log_dir.mkdir(parents=True, exist_ok=True)
                setup_logging(
                    settings.log_level,
                    target,
                    "web",
                    console,
                    False,
                    secrets=[key] if key else [],
                )
                return
            except OSError:
                continue

    def _execute(self, job: Job, *, dry_run: bool) -> None:
        redaction = RedactionFilter()
        try:
            loaded = load_settings()
            if isinstance(loaded, Err):
                self._fail(job, loaded.error, redaction)
                return
            settings = loaded.value
            if settings.deepl_api_key is not None:
                redaction = RedactionFilter([settings.deepl_api_key.get_secret_value()])
            self._ensure_logging(settings)
            options = self._options(settings)
            if isinstance(options, Err):
                self._fail(job, options.error, redaction)
                return
            export_options, figure_options = options.value
            orchestrator = _TrackingOrchestrator(
                settings,
                job.directory,
                tool_version=__version__,
                run_id=secrets.token_hex(6),
                translator_factory=self._translator_factory,
                progress=lambda event: self._on_progress(job, event),
                on_stage=lambda stage: self._on_stage(job, stage),
            )
            glossary_path = (
                self.glossary_path(job.glossary) if job.glossary is not None else None
            )
            if job.glossary is not None and glossary_path is None:
                self._fail(
                    job,
                    AppError(
                        ErrorCode.GLOSSARY_INVALID,
                        f"unknown glossary {job.glossary!r}",
                        ErrorScope.USER,
                    ),
                    redaction,
                )
                return
            result = asyncio.run(
                orchestrator.run(
                    job.input_path,
                    extractor_name=settings.extractor,
                    provider=settings.provider,
                    user_glossary=glossary_path,
                    dry_run=dry_run,
                    export_options=export_options,
                    figure_options=figure_options,
                    mode=job.mode,
                    overlay_min_scale=settings.overlay_min_scale,
                    overlay_floor_scale=settings.overlay_floor_scale,
                    figure_legend=settings.figure_legend,
                    margins_from_source=margins_from_source(settings.pdf_margin),
                )
            )
            if isinstance(result, Err):
                self._fail(job, result.error, redaction)
                return
            self._record(job, result.value, dry_run=dry_run, redaction=redaction)
        except Exception as exc:  # noqa: BLE001 - thread boundary: never lose the job
            log.exception("web job crashed", extra={"event": "stage_crashed"})
            self._fail(
                job,
                AppError(
                    ErrorCode.INTERNAL,
                    f"the job crashed: {type(exc).__name__}",
                    ErrorScope.JOB_FATAL,
                ),
                redaction,
            )
        finally:
            job.done.set()

    def _options(self, settings: Settings) -> Result[Tuple[ExportOptions, FigureOptions]]:
        """``(ExportOptions, FigureOptions)`` built the way ``cli._export_options`` builds them."""
        figures = figure_options_from_settings(settings)
        if isinstance(figures, Err):
            return figures
        export = ExportOptions(
            pdf_engine=settings.pdf_engine,
            page_size=settings.pdf_page_size,
        )
        if not margins_from_source(settings.pdf_margin):
            margins = parse_pdf_margin(settings.pdf_margin)
            if isinstance(margins, Err):
                return margins
            export = dataclasses.replace(export, margins_mm=margins.value)
        return Ok((export, figures.value))

    def _record(
        self,
        job: Job,
        summary: JobSummary,
        *,
        dry_run: bool,
        redaction: RedactionFilter,
    ) -> None:
        warnings = [redaction.redact_text(w) for w in summary.warnings]
        if dry_run:
            estimate = _parse_estimate(summary.warnings)
            with self._lock:
                job.estimate = estimate
                job.phase = PHASE_AWAITING_CONFIRM
                job.progress.stage = None
            return
        outputs: List[JobOutput] = []
        for key, raw in summary.outputs.items():
            path = Path(raw)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            outputs.append(
                JobOutput(
                    key=key,
                    name=path.name,
                    label=OUTPUT_LABELS.get(key, key),
                    size_bytes=size,
                )
            )
        result = JobResult(
            exit_code=summary.exit_code,
            chars_sent_this_run=summary.chars_sent_this_run,
            chars_sent_total=summary.chars_sent_total,
            outputs=sorted(outputs, key=lambda o: o.key),
            warnings=warnings,
        )
        with self._lock:
            job.result = result
            job.phase = PHASE_DONE if summary.exit_code == EXIT_SUCCESS else PHASE_ERROR
            job.progress.stage = None
            if summary.exit_code != EXIT_SUCCESS and job.failure is None:
                job.failure = JobFailure(
                    code=f"exit_{summary.exit_code}",
                    message=_exit_message(summary.exit_code),
                    exit_code=summary.exit_code,
                    hint="Ayrıntılar uyarı listesinde.",
                )


def _parse_estimate(warnings: Sequence[str]) -> Optional[JobEstimate]:
    for warning in warnings:
        match = DRY_RUN_WARNING.match(warning)
        if match is not None:
            return JobEstimate(units=int(match["units"]), chars=int(match["chars"]))
    return None


def _exit_message(exit_code: int) -> str:
    return {
        2: "Çıktı eksik: bazı birimler çevrilemedi, yazılabilen dosyalar aşağıda.",
        3: (
            "İş duraklatıldı (kota, kimlik doğrulama ya da sağlayıcının hız sınırı). "
            "Durum korundu; aynı çıktı dizinini yeniden çalıştırınca kaldığı yerden devam eder."
        ),
        4: "Çıktı dizini başka bir işlem tarafından kilitli.",
        5: "Kayıtlı durum ile bu çalıştırma uyuşmuyor.",
        130: "İş yarıda kesildi; kayıtlı durum bozulmadı.",
    }.get(exit_code, "İş başarısız oldu.")


__all__ = [
    "EXIT_FAILURE",
    "EXIT_SUCCESS",
    "INPUT_NAME",
    "MODE_OVERLAY",
    "MODE_REFLOW",
    "Job",
    "JobEstimate",
    "JobFailure",
    "JobManager",
    "JobOutput",
    "JobProgress",
    "JobResult",
    "JobRunner",
    "PHASE_AWAITING_CONFIRM",
    "PHASE_DONE",
    "PHASE_ERROR",
    "PHASE_ESTIMATING",
    "PHASE_NEW",
    "PHASE_RUNNING",
    "STAGE_ORDER",
    "inline_runner",
    "thread_runner",
]
