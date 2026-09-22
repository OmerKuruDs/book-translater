"""Gemini provider over the Generative Language REST API (design doc 02, 2.3 and 7.5).

Why an LLM at all
-----------------
DeepL and Cloud Translation v2 are NMT engines: the glossary is either a native
feature (DeepL) or absent (v2). Gemini is prompt-driven, so the terminology the
book needs travels *in the prompt* - :class:`~translators.llm_base.BaseLLMTranslator`
already builds that block and this module only has to make the call. That is why
``GlossaryStrategy.PROMPT`` is what a Gemini run reports, and why a glossary works
here without any provider-side upload.

Endpoint and credential
-----------------------
``POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent``
with the key in the ``x-goog-api-key`` header, not in the query string: a URL ends up
in proxy logs and in httpx exception text, a header does not. The key shape is the
same ``AIza...`` as Cloud Translation's, so :func:`~translators.google_translate.scrub_secret`
is reused rather than re-implemented - one redaction, tested once.

Thinking budget
---------------
The 2.5 models reason before answering and bill that reasoning as output tokens.
Translation does not need it, and output tokens are the expensive half of the bill,
so ``thinkingConfig.thinkingBudget`` is sent as ``0`` by default
(``BOOK_TRANSLATOR_GEMINI_THINKING_BUDGET``). A model that does not accept the field
answers HTTP 400; set the budget to ``-1`` to omit it entirely.

Rate limit versus quota
-----------------------
Both arrive as HTTP 429 ``RESOURCE_EXHAUSTED``. The difference decides whether the run
backs off or hands over to the fallback provider, so it is read from the
``QuotaFailure`` detail: a violation whose quota id mentions a *per day* limit is the
free tier's daily cap and will not clear by waiting a few seconds
(:data:`ErrorCode.PROVIDER_QUOTA`, arms the fallback); anything else is
per-minute pressure (:data:`ErrorCode.PROVIDER_RATE_LIMITED`, back off and retry).

Accounting
----------
``usageMetadata`` reports tokens, not characters, and the pipeline counts characters.
``chars_billed`` is therefore the length of the prompt actually sent, the same
convention the Google provider uses; the token counts go to the log.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, ClassVar, Dict, List, Mapping, Optional, Tuple

import httpx

from ..config import Settings
from ..domain.result import AppError, ErrorCode, ErrorScope, Ok, Result, err
from .base import BaseTranslator, TranslatorCapabilities
from .google_translate import scrub_secret
from .llm_base import BaseLLMTranslator

__all__ = [
    "DEFAULT_MODEL",
    "ENDPOINT_TEMPLATE",
    "GeminiTranslator",
    "classify_gemini_exception",
    "classify_gemini_status",
    "endpoint_for",
]

log = logging.getLogger("book_translator.translators.gemini")

ENDPOINT_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

DEFAULT_MODEL = "gemini-3.8-flash"
"""Overridden with ``BOOK_TRANSLATOR_GEMINI_MODEL``.

Measured over 120 units of one book against ``gemini-3.5-flash-lite`` and
``gemini-3.1-pro-preview``: all three keep the glossary (95-99% of terms) and none
shortens a paragraph, but the other two capitalise mid-sentence three to four times as
often, and flash-lite also wraps terms in Markdown markers that split Turkish suffixes
off their stem. Pro was worse than flash and several times the price.

Pinned rather than the ``gemini-flash-latest`` alias on purpose. An alias moves, and an
output then has no record of what produced it - a 35-page run here turned out to be a
mix of three models. A stale pin fails loudly (``gemini-2.5-flash`` began answering
"no longer available to new users" within an hour of being the default), which is the
better failure: anything the ``models`` endpoint lists is accepted, and an unknown name
comes back as the provider's own 404."""

ClientFactory = Callable[[], Any]
"""Builds the HTTP client. Injectable so tests never touch the network (the key is not
handed to the factory: it is attached per request by the translator itself)."""

_TIMEOUT_S = 120.0  # a batch of prose through a reasoning model is slower than NMT

_BAD_REQUEST_STATUSES = frozenset({400, 404, 405, 413, 414, 415, 422})

#: Longest wait still treated as a rate limit rather than an exhausted quota. The free
#: tier asked for 45 seconds; anything beyond a few minutes is not a speed bump.
_MAX_WAITABLE_DELAY_S = 300.0

_RETRY_DELAY = re.compile(r"^(?P<value>\d+(?:\.\d+)?)s$")

#: ``finishReason`` values that mean "there is no usable answer in this response".
_REFUSED_FINISH = frozenset({"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"})


def endpoint_for(model: str) -> str:
    return ENDPOINT_TEMPLATE.format(model=model)


def _redact(exc: BaseException, secret: Optional[str] = None) -> str:
    return scrub_secret(f"{type(exc).__name__}: {str(exc)[:200]}", secret)


def _error_fields(body: Any) -> Tuple[str, str, List[Mapping[str, Any]]]:
    """``(status, message, details)`` from a Generative Language error envelope."""
    if not isinstance(body, Mapping):
        return "", "", []
    error = body.get("error")
    if not isinstance(error, Mapping):
        return "", "", []
    status = error.get("status")
    message = error.get("message")
    details = error.get("details")
    return (
        status if isinstance(status, str) else "",
        message if isinstance(message, str) else "",
        [d for d in details if isinstance(d, Mapping)] if isinstance(details, list) else [],
    )


def _retry_delay(
    details: List[Mapping[str, Any]], headers: Optional[Mapping[str, str]]
) -> Optional[float]:
    """Seconds the API asks us to wait: ``RetryInfo.retryDelay`` first, then ``Retry-After``."""
    for detail in details:
        raw = detail.get("retryDelay")
        if isinstance(raw, str):
            match = _RETRY_DELAY.match(raw.strip())
            if match is not None:
                return float(match["value"])
    value = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    if value is not None:
        try:
            return float(str(value).strip())
        except ValueError:
            return None
    return None


def _waitable(delay: Optional[float]) -> bool:
    """Is this 429 something a run can simply wait out?

    ``quotaId`` cannot answer it. Captured from the free tier: a violation named
    ``GenerateRequestsPerDayPerProjectPerModel-FreeTier`` came with ``retryDelay: 45s``
    and cleared within the minute, so the name says "per day" while the window is far
    shorter. The delay the API asks for is what it will actually honour.

    No delay at all means the API declined to promise recovery; that is treated as an
    exhausted quota, which pauses the job rather than hammering it. The long-delay side
    of this has not been observed live - the free tier never asked for more than a
    minute during testing.
    """
    return delay is not None and delay <= _MAX_WAITABLE_DELAY_S


def classify_gemini_status(
    status: int,
    body: Any,
    headers: Optional[Mapping[str, str]] = None,
    secret: Optional[str] = None,
) -> AppError:
    """Map an HTTP error answer to the error table of design doc 02, 5.3."""
    api_status, message, details = _error_fields(body)
    detail = scrub_secret(message, secret)
    suffix = f": {detail}" if detail else ""
    context: Dict[str, Any] = {"http_status": status}
    if api_status:
        context["api_status"] = api_status

    if status == 402:
        # Observed live: HTTP 402 also carries ``RESOURCE_EXHAUSTED``, so the 429 branch
        # below would call an empty wallet a rate limit and retry it twenty times. No
        # amount of backing off tops up an account.
        return AppError(
            ErrorCode.PROVIDER_QUOTA,
            f"Gemini billing credits are depleted (HTTP 402){suffix}",
            ErrorScope.JOB_FATAL,
            context=context,
        )
    if status == 429 or api_status == "RESOURCE_EXHAUSTED":
        delay = _retry_delay(details, headers)
        if _waitable(delay):
            return AppError(
                ErrorCode.PROVIDER_RATE_LIMITED,
                f"Gemini rate limit reached (HTTP {status}){suffix}",
                ErrorScope.CHUNK_RETRYABLE,
                retry_after_s=delay,
                context=context,
            )
        return AppError(
            ErrorCode.PROVIDER_QUOTA,
            f"Gemini quota exhausted (HTTP {status}){suffix}; "
            "raise the limit for the project or wait for the next period",
            ErrorScope.JOB_FATAL,
            retry_after_s=delay,
            context=context,
        )
    if status in (401, 403) or api_status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return AppError(
            ErrorCode.PROVIDER_AUTH,
            f"Gemini rejected the API key (HTTP {status}){suffix}; check GEMINI_API_KEY "
            "and that the Generative Language API is enabled for the project",
            ErrorScope.JOB_FATAL,
            context=context,
        )
    if status in _BAD_REQUEST_STATUSES:
        return AppError(
            ErrorCode.PROVIDER_BAD_REQUEST,
            f"Gemini rejected the request (HTTP {status}){suffix}",
            ErrorScope.CHUNK_FATAL,
            context=context,
        )
    return AppError(
        ErrorCode.PROVIDER_TRANSIENT,
        f"Gemini request failed (HTTP {status}){suffix}",
        ErrorScope.CHUNK_RETRYABLE,
        context=context,
    )


def classify_gemini_exception(exc: BaseException, secret: Optional[str] = None) -> AppError:
    """Map a transport-level failure to the error table (nothing here is fatal)."""
    cause = _redact(exc, secret)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return AppError(
            ErrorCode.PROVIDER_TRANSIENT,
            "Gemini request timed out",
            ErrorScope.CHUNK_RETRYABLE,
            cause=cause,
        )
    if isinstance(exc, (httpx.HTTPError, ConnectionError, OSError)):
        return AppError(
            ErrorCode.PROVIDER_TRANSIENT,
            "connection to Gemini failed",
            ErrorScope.CHUNK_RETRYABLE,
            cause=cause,
        )
    return AppError(
        ErrorCode.INTERNAL,
        "unexpected error while calling Gemini",
        ErrorScope.CHUNK_FATAL,
        cause=cause,
    )


def _default_client_factory() -> Any:
    return httpx.AsyncClient(timeout=_TIMEOUT_S)


def _answer_text(body: Any) -> Result[str]:
    """The candidate's text, or a classified error explaining why there is none."""
    if not isinstance(body, Mapping):
        return err(
            ErrorCode.PROVIDER_EMPTY_RESPONSE,
            "Gemini returned a body that is not a JSON object",
            ErrorScope.CHUNK_RETRYABLE,
        )
    feedback = body.get("promptFeedback")
    if isinstance(feedback, Mapping) and feedback.get("blockReason"):
        # The prompt itself was refused: sending it again changes nothing.
        return err(
            ErrorCode.PROVIDER_BAD_REQUEST,
            f"Gemini blocked the prompt ({feedback.get('blockReason')})",
            ErrorScope.CHUNK_FATAL,
        )
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return err(
            ErrorCode.PROVIDER_EMPTY_RESPONSE,
            "Gemini returned no candidate",
            ErrorScope.CHUNK_RETRYABLE,
        )
    candidate = candidates[0]
    if not isinstance(candidate, Mapping):
        return err(
            ErrorCode.PROVIDER_EMPTY_RESPONSE,
            "Gemini returned a malformed candidate",
            ErrorScope.CHUNK_RETRYABLE,
        )
    finish = candidate.get("finishReason")
    if isinstance(finish, str) and finish in _REFUSED_FINISH:
        return err(
            ErrorCode.PROVIDER_BAD_REQUEST,
            f"Gemini refused to answer this text ({finish})",
            ErrorScope.CHUNK_FATAL,
        )
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, Mapping) else None
    pieces = (
        [p["text"] for p in parts if isinstance(p, Mapping) and isinstance(p.get("text"), str)]
        if isinstance(parts, list)
        else []
    )
    text = "".join(pieces)
    if not text.strip():
        # MAX_TOKENS lands here: the answer was cut off before any usable line.
        reason = finish if isinstance(finish, str) else "no finishReason"
        return err(
            ErrorCode.PROVIDER_EMPTY_RESPONSE,
            f"Gemini returned an empty answer ({reason})",
            ErrorScope.CHUNK_RETRYABLE,
        )
    return Ok(text)


class GeminiTranslator(BaseLLMTranslator):
    """Prompt-driven provider. One HTTP round trip per batch, no internal retry."""

    name: ClassVar[str] = "gemini"
    capabilities: ClassVar[TranslatorCapabilities] = TranslatorCapabilities(
        supports_glossary=False,  # no upload step: the terms ride in the prompt
        supports_batch=True,
        # The instruction block plus up to 200 glossary terms is a fixed ~5 kB overhead
        # per call. Larger batches amortise it, but a mis-numbered answer costs the whole
        # batch, so this stays near the NMT batch size rather than filling the context.
        max_chars_per_request=8_000,
        max_texts_per_request=40,
        supports_context=True,
        supports_tag_protection=False,  # sentinel placeholders (⟦n⟧), not XML tags
        glossary_in_prompt=True,  # the terminology block is part of every request
    )
    client_factory: ClassVar[ClientFactory] = staticmethod(_default_client_factory)

    def __init__(
        self, client: Any, api_key: str, model: str, thinking_budget: int = 0
    ) -> None:
        super().__init__()
        self._client = client
        self._api_key = api_key
        self._model = model
        self._thinking_budget = thinking_budget
        self._reported_model: Optional[str] = None

    @classmethod
    def create(
        cls, settings: Settings, *, client_factory: Optional[ClientFactory] = None
    ) -> Result[BaseTranslator]:
        if settings.gemini_api_key is None:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                "GEMINI_API_KEY is not set (environment variable or .env)",
                ErrorScope.USER,
            )
        key = settings.gemini_api_key.get_secret_value()
        factory = client_factory if client_factory is not None else cls.client_factory
        try:
            client = factory()
        except Exception as exc:  # noqa: BLE001 - fail-fast, classified here
            return err(
                ErrorCode.PROVIDER_CONFIG,
                "could not construct the Gemini HTTP client",
                ErrorScope.USER,
                cause=_redact(exc, key),
            )
        return Ok(cls(client, key, settings.gemini_model, settings.gemini_thinking_budget))

    def _payload(self, prompt: str) -> Dict[str, Any]:
        generation: Dict[str, Any] = {
            "temperature": 0.0,  # a translation should not vary between retries
            "candidateCount": 1,
        }
        if self._thinking_budget >= 0:
            generation["thinkingConfig"] = {"thinkingBudget": self._thinking_budget}
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }

    def _note_model(self, body: Any) -> None:
        """Record, once per run, the model the API actually served.

        ``modelVersion`` resolves the alias, so it answers "what produced this text?"
        where the requested name cannot. Logged at INFO because that question is asked
        after the fact, of a finished output, from the run log.
        """
        served = body.get("modelVersion") if isinstance(body, Mapping) else None
        if not isinstance(served, str) or served == self._reported_model:
            return
        self._reported_model = served
        log.info(
            "gemini model: %s (requested %s)",
            served,
            self._model,
            extra={"event": "provider_model"},
        )

    async def complete(self, prompt: str) -> Result[str]:
        started = time.perf_counter()
        try:
            response = await self._client.post(
                endpoint_for(self._model),
                json=self._payload(prompt),
                headers={
                    "x-goog-api-key": self._api_key,  # not the query string: URLs get logged
                    "Accept": "application/json",
                },
            )
        except Exception as exc:  # noqa: BLE001 - transport failures are classified
            error = classify_gemini_exception(exc, self._api_key)
            return err(error.code, error.message, error.scope, cause=error.cause)

        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body is still an answer to classify
            body = None

        if response.status_code >= 400:
            error = classify_gemini_status(
                response.status_code, body, dict(response.headers), self._api_key
            )
            return err(
                error.code,
                error.message,
                error.scope,
                retry_after_s=error.retry_after_s,
                context=dict(error.context),
            )

        answer = _answer_text(body)
        if isinstance(answer, Ok):
            self._note_model(body)
            usage = body.get("usageMetadata") if isinstance(body, Mapping) else None
            if isinstance(usage, Mapping):
                log.debug(
                    "gemini usage: prompt=%s candidates=%s thoughts=%s total=%s in %d ms",
                    usage.get("promptTokenCount"),
                    usage.get("candidatesTokenCount"),
                    usage.get("thoughtsTokenCount"),
                    usage.get("totalTokenCount"),
                    int((time.perf_counter() - started) * 1000),
                    extra={"event": "gemini_usage"},
                )
        return answer
