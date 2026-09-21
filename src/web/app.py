"""FastAPI application for the local web UI (``PDF yükle -> çevir -> indir``).

The app is a thin shell around :mod:`book_translator.web.jobs`: it validates the
upload, hands the job to the manager and reports the manager's state. It never
talks to a provider, an extractor or an exporter itself - the pipeline is driven
through ``Orchestrator.run``, exactly as ``cli.run`` drives it.

Run it with ``python -m book_translator.web`` (binds ``127.0.0.1`` by default) or
``uvicorn book_translator.web.app:app --host 127.0.0.1``. There is no
authentication: the server is meant for loopback only.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from ..pipeline.orchestrator import MODE_OVERLAY, MODE_REFLOW
from .jobs import (
    INPUT_NAME,
    PHASE_ESTIMATING,
    PHASE_RUNNING,
    Job,
    JobManager,
    JobRunner,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

ENV_WORK_ROOT = "BOOK_TRANSLATOR_WEB_WORKDIR"
ENV_GLOSSARY_DIR = "BOOK_TRANSLATOR_WEB_GLOSSARIES"
ENV_MAX_MB = "BOOK_TRANSLATOR_WEB_MAX_MB"

DEFAULT_WORK_ROOT = Path("web-jobs")
DEFAULT_GLOSSARY_DIR = Path("glossaries")
DEFAULT_MAX_MB = 200.0

UPLOAD_CHUNK = 1024 * 1024
PDF_MAGIC = b"%PDF-"
MODES = (MODE_OVERLAY, MODE_REFLOW)

"""Value the form sends for "sözlük yok"."""

_UNSAFE_NAME = str.maketrans({c: "_" for c in '\\/:*?"<>|\r\n\t'})

# Form/File defaults as module-level singletons (the CLI declares its typer options the
# same way); calling them inline in a signature is what ruff's B008 objects to.
FILE_FIELD = File(...)
MODE_FIELD = Form(MODE_OVERLAY)
#: Repeated, one value per chosen glossary: a book usually needs more than one
#: vocabulary, and picking only one silently drops the rest of the terms.
GLOSSARY_FIELD = Form(None)
ESTIMATE_FIELD = Form(True)


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else default


def _env_max_bytes() -> int:
    raw = os.environ.get(ENV_MAX_MB, "").strip()
    try:
        megabytes = float(raw) if raw else DEFAULT_MAX_MB
    except ValueError:
        megabytes = DEFAULT_MAX_MB
    if megabytes <= 0:
        megabytes = DEFAULT_MAX_MB
    return int(megabytes * 1024 * 1024)


def safe_filename(raw: Optional[str]) -> str:
    """Display name for an upload: no directories, no separators, no control characters.

    The value is only ever shown in the UI - the file itself is always stored as
    :data:`~book_translator.web.jobs.INPUT_NAME` inside the job directory - but it is
    sanitised anyway so a crafted name cannot escape a log line or a path join.
    """
    name = Path((raw or "").replace("\\", "/")).name
    name = name.translate(_UNSAFE_NAME).strip().strip(".")
    name = "".join(ch for ch in name if ch.isprintable())
    if not name or name in {".", ".."}:
        return INPUT_NAME
    return name[:120]


def _json_error(status: int, message: str, hint: str = "") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "hint": hint}}, status_code=status)


def create_app(
    *,
    work_root: Optional[Path] = None,
    glossary_dir: Optional[Path] = None,
    translator_factory: Any = None,
    runner: Optional[JobRunner] = None,
    configure_logging: bool = True,
    max_upload_bytes: Optional[int] = None,
) -> FastAPI:
    """Build the application.

    Nothing is created on disk here: the work root appears with the first job, so
    importing the module has no side effects.
    """
    manager = JobManager(
        work_root if work_root is not None else _env_path(ENV_WORK_ROOT, DEFAULT_WORK_ROOT),
        (
            glossary_dir
            if glossary_dir is not None
            else _env_path(ENV_GLOSSARY_DIR, DEFAULT_GLOSSARY_DIR)
        ),
        translator_factory=translator_factory,
        runner=runner,
        configure_logging=configure_logging,
    )
    limit = max_upload_bytes if max_upload_bytes is not None else _env_max_bytes()

    app = FastAPI(
        title="book-translator",
        description="İngilizce PDF -> Türkçe çeviri, yerel arayüz.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.manager = manager
    app.state.max_upload_bytes = limit

    # -- pages ------------------------------------------------------------- #

    @app.get("/", response_class=HTMLResponse)
    def index() -> Response:
        try:
            return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))
        except OSError:
            return HTMLResponse("<h1>index.html bulunamadı</h1>", status_code=500)

    # -- api --------------------------------------------------------------- #

    @app.get("/api/config")
    def config() -> Dict[str, Any]:
        return {
            "glossaries": manager.glossary_names(),
            "modes": list(MODES),
            "max_upload_mb": round(limit / (1024 * 1024), 1),
            "work_root": str(manager.work_root),
        }

    @app.post("/api/jobs")
    async def create_job(
        file: UploadFile = FILE_FIELD,
        mode: str = MODE_FIELD,
        glossary: Optional[List[str]] = GLOSSARY_FIELD,
        estimate: bool = ESTIMATE_FIELD,
    ) -> Response:
        if mode not in MODES:
            return _json_error(400, f"Bilinmeyen mod: {mode!r}.", "overlay ya da reflow seçin.")
        name = safe_filename(file.filename)
        if not name.lower().endswith(".pdf"):
            return _json_error(
                400,
                "Yalnızca PDF dosyası yüklenebilir.",
                "Uzantısı .pdf olan bir dosya seçin.",
            )
        selected: List[str] = [value.strip() for value in (glossary or []) if value.strip()]
        for chosen in selected:
            if manager.glossary_path(chosen) is None:
                return _json_error(
                    400,
                    f"Sözlük bulunamadı: {safe_filename(chosen)}",
                    f"Sözlükler {manager.glossary_dir} altındaki .json dosyalarından seçilir.",
                )

        job = manager.create(filename=name, mode=mode, glossaries=selected)
        stored = await _store_upload(file, job, limit)
        if stored is not None:
            manager.discard(job)
            return stored
        manager.start(job, dry_run=estimate)
        return JSONResponse(manager.snapshot(job), status_code=201)

    @app.post("/api/jobs/{job_id}/start")
    def start_job(job_id: str) -> Response:
        job = manager.get(job_id)
        if job is None:
            return _json_error(404, "İş bulunamadı.")
        if job.phase in (PHASE_ESTIMATING, PHASE_RUNNING):
            return _json_error(409, "İş zaten çalışıyor.")
        manager.start(job, dry_run=False)
        return JSONResponse(manager.snapshot(job))

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> Response:
        job = manager.get(job_id)
        if job is None:
            return _json_error(404, "İş bulunamadı.")
        return JSONResponse(manager.snapshot(job))

    @app.get("/api/jobs/{job_id}/download/{key}")
    def download(job_id: str, key: str) -> Response:
        job = manager.get(job_id)
        if job is None:
            return _json_error(404, "İş bulunamadı.")
        path = manager.output_file(job, key)
        if path is None:
            return _json_error(404, "Bu iş için böyle bir çıktı yok.")
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    return app


async def _store_upload(file: UploadFile, job: Job, limit: int) -> Optional[Response]:
    """Stream the upload into the job directory; ``None`` when it was accepted.

    The size limit is enforced while reading (the client's ``Content-Length`` is not
    trusted) and the first bytes must be the PDF signature.
    """
    target = job.input_path
    written = 0
    head = b""
    try:
        with target.open("wb") as handle:
            while True:
                chunk = await file.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    handle.close()
                    target.unlink(missing_ok=True)
                    megabytes = round(limit / (1024 * 1024), 1)
                    return _json_error(
                        413,
                        f"Dosya çok büyük (sınır {megabytes} MB).",
                        f"Sınırı {ENV_MAX_MB} ortam değişkeni ile değiştirebilirsiniz.",
                    )
                if len(head) < len(PDF_MAGIC):
                    head += chunk[: len(PDF_MAGIC) - len(head)]
                handle.write(chunk)
    except OSError as exc:
        target.unlink(missing_ok=True)
        return _json_error(500, f"Dosya yazılamadı: {exc.strerror or 'bilinmeyen hata'}")
    finally:
        await file.close()
    if written == 0:
        target.unlink(missing_ok=True)
        return _json_error(400, "Boş dosya yüklendi.")
    if not head.startswith(PDF_MAGIC):
        target.unlink(missing_ok=True)
        return _json_error(
            400,
            "Dosya geçerli bir PDF değil.",
            "İçeriği PDF imzası ile başlamıyor.",
        )
    return None


app = create_app()
"""Module-level application for ``uvicorn book_translator.web.app:app``."""


__all__: List[str] = ["DEFAULT_HOST", "DEFAULT_PORT", "app", "create_app", "safe_filename"]
