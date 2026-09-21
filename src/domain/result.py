"""``Result`` type and the error taxonomy shared by every layer.

Dependency-free (standard library only). A function that can fail for an
expected reason returns ``Result[T]`` and never raises across a layer boundary
(design doc 02, section 2.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generic, Literal, Mapping, Optional, TypeGuard, TypeVar, Union

T = TypeVar("T")
U = TypeVar("U")


class ErrorScope(str, Enum):
    """How the caller must react to an error."""

    CHUNK_RETRYABLE = "chunk_retryable"  # back off and retry this chunk
    CHUNK_FATAL = "chunk_fatal"  # mark this chunk FAILED, continue the job
    JOB_FATAL = "job_fatal"  # pause/stop the whole job, keep state
    USER = "user"  # invalid input/config; nothing started


class ErrorCode(str, Enum):
    """Stable machine-readable error codes (persisted in ``chunks.last_error_code``)."""

    INPUT_NOT_FOUND = "input_not_found"
    INPUT_NOT_PDF = "input_not_pdf"
    INPUT_UNREADABLE = "input_unreadable"
    NO_TEXT_LAYER = "no_text_layer"
    NON_ENGLISH_SOURCE = "non_english_source"
    EXTRACTOR_UNAVAILABLE = "extractor_unavailable"
    EXTRACTION_FAILED = "extraction_failed"
    GLOSSARY_INVALID = "glossary_invalid"
    GLOSSARY_BIND_FAILED = "glossary_bind_failed"
    PROVIDER_CONFIG = "provider_config"
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_QUOTA = "provider_quota"  # characters are used up; waiting/raising helps
    # The account holds as many provider-side glossaries as it may. Deliberately *not*
    # PROVIDER_QUOTA: that code arms the automatic fallback (translators/fallback.py),
    # and paying a second provider because a glossary slot is missing is the wrong move -
    # the fix is to delete one glossary.
    PROVIDER_GLOSSARY_LIMIT = "provider_glossary_limit"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_BAD_REQUEST = "provider_bad_request"
    PROVIDER_EMPTY_RESPONSE = "provider_empty_response"
    STATE_LOCKED = "state_locked"
    STATE_HASH_MISMATCH = "state_hash_mismatch"
    STATE_PROVIDER_MISMATCH = "state_provider_mismatch"
    STATE_SCHEMA_INCOMPATIBLE = "state_schema_incompatible"
    STATE_CORRUPT = "state_corrupt"
    STATE_NOT_FOUND = "state_not_found"
    CHUNK_INTEGRITY = "chunk_integrity"  # lossless verification failed
    EXPORT_REFUSED_PARTIAL = "export_refused_partial"
    EXPORTER_UNAVAILABLE = "exporter_unavailable"
    EXPORT_FAILED = "export_failed"
    INTERRUPTED = "interrupted"
    INTERNAL = "internal"
    SQLITE_UNSUPPORTED = "sqlite_unsupported"  # SQLite library below the 3.8.0 floor (DB design Q5)
    # v1.1 (design doc 06, §3.7)
    FIGURE_SINK_FAILED = "figure_sink_failed"  # images/ not writable during extraction
    OVERLAY_STATE_MISSING = "overlay_state_missing"  # export needs a pass that does not exist
    OVERLAY_PDF_RESTRICTED = "overlay_pdf_restricted"  # encrypted / no-modify PDF in overlay mode
    FONT_UNSUPPORTED = "font_unsupported"  # --pdf-font fails Turkish coverage / format validation


@dataclass(frozen=True)
class AppError:
    """A classified, printable error. ``message`` never contains secrets or book text."""

    code: ErrorCode
    message: str
    scope: ErrorScope
    retry_after_s: Optional[float] = None
    context: Mapping[str, Any] = field(default_factory=dict)  # chunk_id, attempt, http_status ...
    cause: Optional[str] = None  # repr of the original exception (redacted), for logs only

    def __str__(self) -> str:
        return f"[{self.code.value}/{self.scope.value}] {self.message}"


@dataclass(frozen=True)
class Ok(Generic[T]):
    """Successful outcome carrying ``value``."""

    value: T

    def is_ok(self) -> Literal[True]:
        return True

    def is_err(self) -> Literal[False]:
        return False


@dataclass(frozen=True)
class Err:
    """Failed outcome carrying an ``AppError``."""

    error: AppError

    def is_ok(self) -> Literal[False]:
        return False

    def is_err(self) -> Literal[True]:
        return True


Result = Union[Ok[T], Err]


class UnwrapError(RuntimeError):
    """Raised by :func:`unwrap` when called on an ``Err``."""

    def __init__(self, error: AppError) -> None:
        super().__init__(str(error))
        self.error = error


def is_ok(result: Result[T]) -> TypeGuard[Ok[T]]:
    """Type guard narrowing ``result`` to ``Ok[T]``."""
    return isinstance(result, Ok)


def is_err(result: Result[T]) -> TypeGuard[Err]:
    """Type guard narrowing ``result`` to ``Err``."""
    return isinstance(result, Err)


def unwrap(result: Result[T]) -> T:
    """Return the value of an ``Ok`` or raise :class:`UnwrapError`.

    Intended for tests and for call sites that have already checked ``is_ok``.
    """
    if isinstance(result, Ok):
        return result.value
    raise UnwrapError(result.error)


def map_ok(result: Result[T], fn: Callable[[T], U]) -> Result[U]:
    """Apply ``fn`` to the value of an ``Ok``; pass an ``Err`` through unchanged."""
    if isinstance(result, Ok):
        return Ok(fn(result.value))
    return result


def err(
    code: ErrorCode,
    message: str,
    scope: ErrorScope,
    *,
    retry_after_s: Optional[float] = None,
    context: Optional[Mapping[str, Any]] = None,
    cause: Optional[str] = None,
) -> Err:
    """Convenience constructor for ``Err(AppError(...))``."""
    return Err(
        AppError(
            code=code,
            message=message,
            scope=scope,
            retry_after_s=retry_after_s,
            context=dict(context) if context else {},
            cause=cause,
        )
    )
