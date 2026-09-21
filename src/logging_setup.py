"""Logging configuration (design doc 02, section 9.5).

* ``run_id`` / ``job_id`` / ``chunk_id`` / ``attempt`` are injected into every
  record through ``contextvars`` (:class:`ContextFilter`), so worker tasks that
  run concurrently each carry their own chunk context.
* The file handler writes JSON lines with a fixed field set; structured events
  are passed as ``extra={"event": "provider_call", "latency_ms": ..., ...}``.
* :class:`RedactionFilter` replaces configured secrets (exact match) and the
  DeepL Free key shape (``<uuid>:fx``) with ``***`` in message and args. Bare
  UUIDs (job ids, archive paths) and sha256 strings are left alone; Pro keys are
  caught by the exact-secret rule because the CLI registers the configured key.
* Console output goes through a rich handler on the console owned by the CLI.

Logger names are ``book_translator.<package>``; :data:`ROOT_LOGGER_NAME` is
the common parent configured here.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from rich.console import Console
from rich.logging import RichHandler

__all__ = [
    "ConsoleFilter",
    "ContextFilter",
    "JsonlFormatter",
    "ROOT_LOGGER_NAME",
    "RedactionFilter",
    "bind_job",
    "bind_run",
    "chunk_context",
    "setup_logging",
]

ROOT_LOGGER_NAME = "book_translator"

JSONL_FIELDS: Tuple[str, ...] = (
    "ts",
    "level",
    "run_id",
    "job_id",
    "event",
    "chunk_id",
    "attempt",
    "latency_ms",
    "chars",
    "http_status",
    "message",
)

DEEPL_KEY_PATTERN = re.compile(
    r"(?<![0-9A-Za-z-])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:fx"
    r"(?![0-9A-Za-z])"
)
REDACTED = "***"

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("bt_run_id", default="")
_job_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("bt_job_id", default=None)
_chunk_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "bt_chunk_id", default=None
)
_attempt: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "bt_attempt", default=None
)


# --------------------------------------------------------------------------- #
# Context binding
# --------------------------------------------------------------------------- #


def bind_run(run_id: str) -> None:
    """Set the run id for the current context (normally once per process)."""
    _run_id.set(run_id)


def bind_job(job_id: Optional[str]) -> None:
    """Set (or clear) the job id for the current context."""
    _job_id.set(job_id)


@contextmanager
def chunk_context(chunk_id: int, attempt: int) -> Iterator[None]:
    """Attach ``chunk_id``/``attempt`` to every record logged inside the block."""
    token_chunk = _chunk_id.set(chunk_id)
    token_attempt = _attempt.set(attempt)
    try:
        yield
    finally:
        _chunk_id.reset(token_chunk)
        _attempt.reset(token_attempt)


class ConsoleFilter(logging.Filter):
    """Drop records marked ``extra={"file_only": True}`` (the CLI already printed them)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "file_only", False)


class ContextFilter(logging.Filter):
    """Inject the context variables; explicit ``extra`` values win."""

    def filter(self, record: logging.LogRecord) -> bool:
        values = record.__dict__
        if values.get("run_id") is None:
            record.run_id = _run_id.get()
        if values.get("job_id") is None:
            record.job_id = _job_id.get()
        if values.get("chunk_id") is None:
            record.chunk_id = _chunk_id.get()
        if values.get("attempt") is None:
            record.attempt = _attempt.get()
        return True


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


class RedactionFilter(logging.Filter):
    """Replace configured secrets and DeepL-key-shaped strings in message and args."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets: List[str] = sorted(
            {s for s in secrets if s and s.strip()}, key=len, reverse=True
        )

    def redact_text(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return DEEPL_KEY_PATTERN.sub(REDACTED, text)

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact_text(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(self._redact_value(a) for a in args)
        elif isinstance(args, dict):
            record.args = {k: self._redact_value(v) for k, v in args.items()}
        for name in ("event", "cause"):
            value = getattr(record, name, None)
            if isinstance(value, str):
                setattr(record, name, self.redact_text(value))
        return True


# --------------------------------------------------------------------------- #
# JSONL formatter
# --------------------------------------------------------------------------- #


def _json_default(value: object) -> str:
    return str(value)


class JsonlFormatter(logging.Formatter):
    """One JSON object per line with the fixed field set of design 9.5."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "run_id": getattr(record, "run_id", "") or "",
            "job_id": getattr(record, "job_id", None),
            "event": getattr(record, "event", None),
            "chunk_id": getattr(record, "chunk_id", None),
            "attempt": getattr(record, "attempt", None),
            "latency_ms": getattr(record, "latency_ms", None),
            "chars": getattr(record, "chars", None),
            "http_status": getattr(record, "http_status", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        payload["logger"] = record.name
        return json.dumps(payload, ensure_ascii=False, default=_json_default)


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


def _level_value(level: str) -> int:
    value = logging.getLevelName(level.upper())
    return value if isinstance(value, int) else logging.INFO


def setup_logging(
    level: str,
    log_file: Optional[Path],
    run_id: str,
    console: Console,
    quiet: bool,
    *,
    secrets: Sequence[str] = (),
) -> None:
    """Configure the ``book_translator`` logger tree.

    Idempotent: existing handlers of the root logger are replaced, so tests and
    repeated CLI invocations in one process do not accumulate handlers.
    ``quiet`` raises the console threshold to WARNING (warnings and errors are
    always shown); the file handler keeps ``level``.
    """
    bind_run(run_id)
    root = logging.getLogger(ROOT_LOGGER_NAME)
    numeric = _level_value(level)
    root.setLevel(min(numeric, logging.INFO) if quiet else numeric)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass

    context_filter = ContextFilter()
    redaction = RedactionFilter(secrets)

    console_handler = RichHandler(
        console=console,
        show_path=False,
        show_time=False,
        rich_tracebacks=False,
        markup=False,
    )
    console_handler.setLevel(max(numeric, logging.WARNING) if quiet else numeric)
    console_handler.addFilter(ConsoleFilter())
    console_handler.addFilter(context_filter)
    console_handler.addFilter(redaction)
    root.addHandler(console_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(numeric)
        file_handler.setFormatter(JsonlFormatter())
        file_handler.addFilter(context_filter)
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)
