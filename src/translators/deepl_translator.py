"""DeepL provider (design doc 02, sections 2.3, 5.3, 7.4).

The SDK client is built through an injectable factory so tests can pass a fake
object that raises the real ``deepl.exceptions`` classes. No retry happens in
this module: the SDK's network retries are switched off and every failure is
classified into an ``Err``.

v1.1 (design doc 06, C5): ``supports_context`` is ``True``; a request that carries
``context`` (the figure caption for a legend chunk, page text for overlay
batches) is forwarded as the SDK's ``context`` keyword, which is per request,
not per text - exactly the batching shape the pipeline uses.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, ClassVar, Dict, List, Optional, Sequence

import deepl
from deepl import exceptions as deepl_exc

from ..config import Settings
from ..domain.models import EffectiveGlossary, GlossaryStrategy
from ..domain.result import AppError, ErrorCode, ErrorScope, Ok, Result, err
from .base import (
    BaseTranslator,
    GlossaryBinding,
    ProviderResponse,
    TranslationRequest,
    TranslatorCapabilities,
)
from .protect import strip_control_chars

__all__ = ["DeepLTranslator", "classify_deepl_exception", "glossary_name_for"]

ClientFactory = Callable[[str, Optional[str]], Any]

_GLOSSARY_NAME_PREFIX = "book-translator:"
_BAD_REQUEST_STATUSES = frozenset({400, 404, 413, 414, 415, 422})
_AUTH_STATUSES = frozenset({401, 403})


def glossary_name_for(glossary_hash: str) -> str:
    return f"{_GLOSSARY_NAME_PREFIX}{glossary_hash[:16]}"


def _default_client_factory(auth_key: str, server_url: Optional[str]) -> Any:
    """Real SDK client with network retries disabled (design 2.3: ``max_retries=0``).

    ``deepl.Translator`` (SDK >= 1.18) has no ``max_retries`` argument; the retry
    count is the module-level ``deepl.http_client.max_network_retries``.
    """
    deepl.http_client.max_network_retries = 0
    return deepl.Translator(auth_key, server_url=server_url)


def _redact(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def classify_deepl_exception(exc: BaseException) -> AppError:
    """Map an SDK/transport exception to the error table of design doc 02, 5.3."""
    status: Optional[int] = getattr(exc, "http_status_code", None)
    cause = _redact(exc)
    if isinstance(exc, deepl_exc.TooManyRequestsException):
        retry_after = getattr(exc, "retry_after", None)
        return AppError(
            ErrorCode.PROVIDER_RATE_LIMITED,
            "DeepL rate limit reached (HTTP 429)",
            ErrorScope.CHUNK_RETRYABLE,
            retry_after_s=float(retry_after) if retry_after is not None else None,
            context={"http_status": 429},
            cause=cause,
        )
    if isinstance(exc, deepl_exc.QuotaExceededException):
        return AppError(
            ErrorCode.PROVIDER_QUOTA,
            "DeepL character quota exhausted for this billing period (HTTP 456); "
            "the job is paused - raise the quota or wait for the next period, then resume",
            ErrorScope.JOB_FATAL,
            context={"http_status": status or 456},
            cause=cause,
        )
    if isinstance(exc, deepl_exc.AuthorizationException):
        return AppError(
            ErrorCode.PROVIDER_AUTH,
            "DeepL rejected the API key (HTTP 401/403); check DEEPL_API_KEY / DEEPL_SERVER_URL",
            ErrorScope.JOB_FATAL,
            context={"http_status": status or 403},
            cause=cause,
        )
    if isinstance(exc, deepl_exc.ConnectionException):
        return AppError(
            ErrorCode.PROVIDER_TRANSIENT,
            "connection to DeepL failed",
            ErrorScope.CHUNK_RETRYABLE,
            context={"should_retry": bool(getattr(exc, "should_retry", False))},
            cause=cause,
        )
    if isinstance(exc, deepl_exc.GlossaryNotFoundException):
        return AppError(
            ErrorCode.PROVIDER_BAD_REQUEST,
            "DeepL glossary id not found",
            ErrorScope.CHUNK_FATAL,
            context={"http_status": status or 404},
            cause=cause,
        )
    if isinstance(exc, deepl_exc.DeepLException):
        if status is not None and status in _BAD_REQUEST_STATUSES:
            return AppError(
                ErrorCode.PROVIDER_BAD_REQUEST,
                f"DeepL rejected the request (HTTP {status})",
                ErrorScope.CHUNK_FATAL,
                context={"http_status": status},
                cause=cause,
            )
        if status is not None and status in _AUTH_STATUSES:
            return AppError(
                ErrorCode.PROVIDER_AUTH,
                f"DeepL authorization failed (HTTP {status})",
                ErrorScope.JOB_FATAL,
                context={"http_status": status},
                cause=cause,
            )
        if status == 429:
            return AppError(
                ErrorCode.PROVIDER_RATE_LIMITED,
                "DeepL rate limit reached (HTTP 429)",
                ErrorScope.CHUNK_RETRYABLE,
                context={"http_status": status},
                cause=cause,
            )
        if status == 456:
            return AppError(
                ErrorCode.PROVIDER_QUOTA,
                "DeepL character quota exhausted (HTTP 456)",
                ErrorScope.JOB_FATAL,
                context={"http_status": status},
                cause=cause,
            )
        return AppError(
            ErrorCode.PROVIDER_TRANSIENT,
            f"DeepL request failed (HTTP {status})" if status else "DeepL request failed",
            ErrorScope.CHUNK_RETRYABLE,
            context={"http_status": status} if status else {},
            cause=cause,
        )
    if isinstance(exc, (TimeoutError, ConnectionError, OSError, asyncio.TimeoutError)):
        return AppError(
            ErrorCode.PROVIDER_TRANSIENT,
            "network error while calling DeepL",
            ErrorScope.CHUNK_RETRYABLE,
            cause=cause,
        )
    return AppError(
        ErrorCode.INTERNAL,
        "unexpected error while calling DeepL",
        ErrorScope.CHUNK_FATAL,
        cause=cause,
    )


def _glossary_entries(glossary: EffectiveGlossary) -> Dict[str, str]:
    """De-duplicated, trimmed entries; identity entries and empties are dropped (7.4)."""
    entries: Dict[str, str] = {}
    for entry in glossary.entries:
        source = entry.source.strip()
        target = entry.target.strip()
        if not source or not target or source == target or source in entries:
            continue
        entries[source] = target
    return entries


class DeepLTranslator(BaseTranslator):
    name: ClassVar[str] = "deepl"
    capabilities: ClassVar[TranslatorCapabilities] = TranslatorCapabilities(
        supports_glossary=True,
        supports_batch=True,
        max_chars_per_request=100_000,
        max_texts_per_request=50,
        supports_context=True,
        supports_tag_protection=True,
    )
    client_factory: ClassVar[ClientFactory] = staticmethod(_default_client_factory)

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def create(
        cls, settings: Settings, *, client_factory: Optional[ClientFactory] = None
    ) -> Result[BaseTranslator]:
        if settings.deepl_api_key is None:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                "DEEPL_API_KEY is not set (environment variable or .env)",
                ErrorScope.USER,
            )
        factory = client_factory if client_factory is not None else cls.client_factory
        try:
            client = factory(settings.deepl_api_key.get_secret_value(), settings.deepl_server_url)
        except Exception as exc:  # noqa: BLE001 - fail-fast, classified below
            return err(
                ErrorCode.PROVIDER_CONFIG,
                "could not construct the DeepL client",
                ErrorScope.USER,
                cause=_redact(exc),
            )
        return Ok(cls(client))

    # -- 7.4 glossary binding ------------------------------------------------ #

    async def prepare(self, glossary: EffectiveGlossary, run_id: str) -> Result[GlossaryBinding]:
        entries = _glossary_entries(glossary)
        if not entries:
            return Ok(GlossaryBinding(GlossaryStrategy.NONE, None, glossary.glossary_hash))
        name = glossary_name_for(glossary.glossary_hash)
        try:
            existing = await asyncio.to_thread(self._client.list_glossaries)
            for info in existing:
                if getattr(info, "name", None) == name and bool(getattr(info, "ready", False)):
                    return Ok(
                        GlossaryBinding(
                            GlossaryStrategy.NATIVE, str(info.glossary_id), glossary.glossary_hash
                        )
                    )
            created = await asyncio.to_thread(
                self._client.create_glossary, name, "EN", "TR", entries
            )
        except Exception as exc:  # noqa: BLE001 - every failure stops the job (5.3)
            classified = classify_deepl_exception(exc)
            return err(
                ErrorCode.GLOSSARY_BIND_FAILED,
                f"could not bind the DeepL glossary {name!r}: {classified.message}",
                ErrorScope.JOB_FATAL,
                context={"entries": len(entries), **dict(classified.context)},
                cause=classified.cause,
            )
        return Ok(
            GlossaryBinding(
                GlossaryStrategy.NATIVE, str(created.glossary_id), glossary.glossary_hash
            )
        )

    # -- translation ---------------------------------------------------------- #

    def _batches(self, texts: Sequence[str]) -> List[List[str]]:
        caps = self.capabilities
        batches: List[List[str]] = []
        current: List[str] = []
        current_chars = 0
        for text in texts:
            too_many = len(current) >= caps.max_texts_per_request
            too_long = current and current_chars + len(text) > caps.max_chars_per_request
            if current and (too_many or too_long):
                batches.append(current)
                current, current_chars = [], 0
            current.append(text)
            current_chars += len(text)
        if current:
            batches.append(current)
        return batches

    async def translate(
        self, texts: Sequence[str], request: TranslationRequest
    ) -> Result[ProviderResponse]:
        started = time.perf_counter()
        if not texts:
            return Ok(ProviderResponse(texts=[], chars_billed=0, latency_ms=0))
        glossary_id = request.glossary.provider_glossary_id if request.glossary else None
        extra: Dict[str, Any] = {}
        if request.context:
            # the context window never goes through ``protect``; ``tag_handling="xml"``
            # judges it the same way, so one control character there fails the batch too
            extra["context"] = strip_control_chars(request.context)
        out: List[str] = []
        billed = 0
        detected: Optional[str] = None
        for batch in self._batches(texts):
            try:
                results = await asyncio.to_thread(
                    self._client.translate_text,
                    batch,
                    source_lang=request.source_lang,
                    target_lang=request.target_lang,
                    glossary=glossary_id,
                    tag_handling="xml",
                    ignore_tags=["x"],
                    preserve_formatting=True,
                    **extra,
                )
            except Exception as exc:  # noqa: BLE001 - classified, never re-raised
                error = classify_deepl_exception(exc)
                return err(
                    error.code,
                    error.message,
                    error.scope,
                    retry_after_s=error.retry_after_s,
                    context={"chunk_id": request.chunk_id, "attempt": request.attempt,
                             **dict(error.context)},
                    cause=error.cause,
                )
            items = list(results) if isinstance(results, list) else [results]
            if len(items) != len(batch):
                return err(
                    ErrorCode.PROVIDER_EMPTY_RESPONSE,
                    f"DeepL returned {len(items)} texts for {len(batch)} inputs",
                    ErrorScope.CHUNK_RETRYABLE,
                    context={"chunk_id": request.chunk_id, "attempt": request.attempt},
                )
            for source, item in zip(batch, items, strict=True):
                translated = getattr(item, "text", None)
                if not isinstance(translated, str) or (source.strip() and not translated.strip()):
                    return err(
                        ErrorCode.PROVIDER_EMPTY_RESPONSE,
                        "DeepL returned an empty translation",
                        ErrorScope.CHUNK_RETRYABLE,
                        context={"chunk_id": request.chunk_id, "attempt": request.attempt},
                    )
                out.append(translated)
                billed += int(getattr(item, "billed_characters", 0) or 0)
                if detected is None:
                    lang = getattr(item, "detected_source_lang", None)
                    detected = str(lang) if lang else None
        latency_ms = int((time.perf_counter() - started) * 1000)
        return Ok(
            ProviderResponse(
                texts=out, chars_billed=billed, latency_ms=latency_ms, detected_source_lang=detected
            )
        )

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
