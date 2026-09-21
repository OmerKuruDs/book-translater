"""Google Cloud Translation v2 (Basic) provider over REST (design doc 02, 2.3, 5.3).

Why v2 and not v3
-----------------
``GOOGLE_CLOUD_API`` holds an API key (``AIza...``). Cloud Translation **v3**
(``translate.googleapis.com/v3``) does not accept API keys - it wants a service account
or ADC - so this provider calls the **v2 (Basic)** endpoint
``https://translation.googleapis.com/language/translate/v2``, which does. The price of
that choice is that v2 has no glossary support at all (``supports_glossary=False``);
glossaries are a v3 feature.

Limits (documented, not guessed)
--------------------------------
* **Maximum request size: 100 000 bytes** for Cloud Translation - Basic; Google also
  recommends staying near 5 000 code points per request for latency.
  https://docs.cloud.google.com/translate/quotas ("Content size limits")
* **Maximum 128 text segments (``q``) per request**; above that the API answers
  ``400 "Too many text segments"``.
  https://github.com/googleapis/google-cloud-python/issues/5425
:data:`GoogleTranslator.capabilities` therefore uses 128 texts and a *character* budget of
20 000: the documented cap is in bytes, and 20 000 characters cannot exceed 80 000 UTF-8
bytes even if every single one of them were a 4-byte codepoint, which leaves room for the
JSON envelope. English/Turkish prose runs at ~1.05 bytes per character, so the real
payloads are far below the cap.

Tag protection
--------------
Requests are sent with ``format=html``: v2 then translates only the text nodes and passes
markup through, which is exactly what the ``<x id="n"/>`` placeholders of
:mod:`translators.protect` need. ``supports_tag_protection=True`` therefore selects the
``"xml"`` :data:`~translators.protect.ProtectMode` for this provider, the same mode DeepL
uses - the sentinel mode (``⟦1⟧``) would travel through the NMT model as ordinary text and
can come back spaced or dropped. In HTML mode Google escapes ``&``, ``<``, ``>``
and ``'`` in its answer, so every returned string has to be unescaped - but *how much*
depends on the protect mode of the request (:func:`_unescape_answer`): in ``"xml"`` mode
the payload went out already escaped and ``&amp;`` / ``&lt;`` / ``&gt;`` belong to
:func:`~translators.protect.restore`, which unescapes exactly one level. Undoing them here
as well would turn a book's literal ``&amp;`` into a bare ``&``.

Accounting
----------
v2 does not report billed characters. ``chars_billed`` is the character count of the
payload actually sent (after control-character stripping), which is what Google bills:
"you are charged for the total number of characters you send, including markup".
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from typing import Any, Callable, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

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
from .protect import ProtectMode, strip_control_chars

__all__ = [
    "ENDPOINT",
    "GoogleTranslator",
    "classify_google_exception",
    "classify_google_status",
    "scrub_secret",
]

log = logging.getLogger("book_translator.translators.google")

ENDPOINT = "https://translation.googleapis.com/language/translate/v2"

ClientFactory = Callable[[], Any]
"""Builds the HTTP client. Injectable so tests never touch the network (the key is not
handed to the factory: it is attached per request by the translator itself)."""

_TIMEOUT_S = 60.0

_BAD_REQUEST_STATUSES = frozenset({400, 404, 405, 413, 414, 415, 422})

# 403 carries the real meaning in ``error.errors[].reason`` (lower-cased here).
_QUOTA_REASONS = frozenset({"quotaexceeded", "dailylimitexceeded", "limitexceeded"})
_RATE_REASONS = frozenset(
    {"ratelimitexceeded", "userratelimitexceeded", "userratelimitexceededunreg"}
)
_AUTH_REASONS = frozenset(
    {
        "accessnotconfigured",
        "authenticationerror",
        "forbidden",
        "ipreferrerblocked",
        "keyexpired",
        "keyinvalid",
        "unauthorized",
        "api_key_invalid",
        "permission_denied",
    }
)

_KEY_PARAM = re.compile(r"((?:\?|&)key=)[^&\s\"']+")
_API_KEY_SHAPE = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")


def scrub_secret(text: str, secret: Optional[str] = None) -> str:
    """Remove an API key from ``text`` (CR-01: no credential in a log or an error).

    Three passes, because a key reaches a message by three routes: as the literal
    ``secret``, inside the ``?key=`` query of a URL httpx puts into its exception text,
    and as a bare ``AIza...`` token echoed by a proxy.
    """
    out = _KEY_PARAM.sub(r"\1***", text)
    if secret:
        out = out.replace(secret, "***")
    return _API_KEY_SHAPE.sub("***", out)


def _redact(exc: BaseException, secret: Optional[str] = None) -> str:
    return scrub_secret(f"{type(exc).__name__}: {str(exc)[:200]}", secret)


_XML_ESCAPED = frozenset({"amp", "lt", "gt"})
_CHAR_REF = re.compile(r"&(#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")


def _unescape_answer(raw: str, mode: ProtectMode) -> str:
    """Google's ``format=html`` escaping undone, down to the level the caller expects.

    ``"sentinel"``: nothing was escaped on the way out, so everything comes back
    (``html.unescape``). ``"xml"``: the payload left already escaped, so ``&amp;``,
    ``&lt;`` and ``&gt;`` are handed on untouched - :func:`~translators.protect.restore`
    resolves those, once. Only the references Google adds on its own (``&#39;`` for an
    apostrophe, ``&quot;``) are resolved here.
    """
    if mode != "xml":
        return html.unescape(raw)
    return _CHAR_REF.sub(
        lambda m: m.group(0) if m.group(1) in _XML_ESCAPED else html.unescape(m.group(0)),
        raw,
    )


def _default_client_factory() -> Any:
    """Real client with transport retries disabled (design 2.3: the orchestrator retries)."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(_TIMEOUT_S),
        transport=httpx.AsyncHTTPTransport(retries=0),
        follow_redirects=False,
    )


def _language(code: str) -> str:
    """``"EN"`` / ``"EN-GB"`` -> ``"en"``: v2 wants a plain ISO-639-1 code.

    The pipeline only ever asks for EN -> TR, so the region subtag is dropped rather than
    mapped (Google's regional codes such as ``zh-CN`` are out of scope here).
    """
    return code.split("-")[0].strip().lower()


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None  # HTTP-date form: let the backoff policy pick the delay
    return value if value >= 0 else None


def _error_fields(body: Any) -> Tuple[str, str]:
    """``(reason, message)`` out of a Google error envelope; both may be empty."""
    if not isinstance(body, dict):
        return "", ""
    error = body.get("error")
    if not isinstance(error, dict):
        return "", str(body.get("message", ""))[:200]
    message = str(error.get("message", ""))[:200]
    reason = ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = str(errors[0].get("reason", ""))
    if not reason:
        details = error.get("details")
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict) and item.get("reason"):
                    reason = str(item["reason"])
                    break
    if not reason:
        reason = str(error.get("status", ""))
    return reason, message


def classify_google_status(
    status: int,
    body: Any,
    headers: Optional[Mapping[str, str]] = None,
    secret: Optional[str] = None,
) -> AppError:
    """Map an HTTP error answer to the error table of design doc 02, 5.3.

    v2 reports both "the quota is gone" and "you are going too fast" as HTTP 403; only
    ``error.errors[].reason`` tells them apart, and the difference matters: a quota is
    ``JOB_FATAL`` (and the trigger for the automatic fallback), a rate limit is
    ``CHUNK_RETRYABLE``.
    """
    reason, message = _error_fields(body)
    key = reason.replace("-", "").replace(" ", "").lower()
    detail = scrub_secret(message, secret)
    suffix = f": {detail}" if detail else ""
    context: Dict[str, Any] = {"http_status": status}
    if reason:
        context["reason"] = reason
    if status == 429 or key in _RATE_REASONS:
        return AppError(
            ErrorCode.PROVIDER_RATE_LIMITED,
            f"Google rate limit reached (HTTP {status}){suffix}",
            ErrorScope.CHUNK_RETRYABLE,
            retry_after_s=_retry_after(headers or {}),
            context=context,
        )
    if key in _QUOTA_REASONS:
        return AppError(
            ErrorCode.PROVIDER_QUOTA,
            f"Google Cloud Translation quota exhausted (HTTP {status}){suffix}; "
            "raise the quota in the Cloud console or wait for the next period",
            ErrorScope.JOB_FATAL,
            context=context,
        )
    if key in _AUTH_REASONS or status == 401:
        return AppError(
            ErrorCode.PROVIDER_AUTH,
            f"Google rejected the API key (HTTP {status}){suffix}; check GOOGLE_CLOUD_API "
            "and that the Cloud Translation API is enabled for the project",
            ErrorScope.JOB_FATAL,
            context=context,
        )
    if status == 403:
        # An unknown 403 reason is a permission problem far more often than a quota one.
        return AppError(
            ErrorCode.PROVIDER_AUTH,
            f"Google refused the request (HTTP 403){suffix}",
            ErrorScope.JOB_FATAL,
            context=context,
        )
    if status in _BAD_REQUEST_STATUSES:
        return AppError(
            ErrorCode.PROVIDER_BAD_REQUEST,
            f"Google rejected the request (HTTP {status}){suffix}",
            ErrorScope.CHUNK_FATAL,
            context=context,
        )
    return AppError(
        ErrorCode.PROVIDER_TRANSIENT,
        f"Google request failed (HTTP {status}){suffix}",
        ErrorScope.CHUNK_RETRYABLE,
        context=context,
    )


def classify_google_exception(exc: BaseException, secret: Optional[str] = None) -> AppError:
    """Map a transport-level failure to the error table (nothing here is fatal)."""
    cause = _redact(exc, secret)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return AppError(
            ErrorCode.PROVIDER_TRANSIENT,
            "Google request timed out",
            ErrorScope.CHUNK_RETRYABLE,
            cause=cause,
        )
    if isinstance(exc, (httpx.HTTPError, ConnectionError, OSError)):
        return AppError(
            ErrorCode.PROVIDER_TRANSIENT,
            "connection to Google Cloud Translation failed",
            ErrorScope.CHUNK_RETRYABLE,
            cause=cause,
        )
    return AppError(
        ErrorCode.INTERNAL,
        "unexpected error while calling Google Cloud Translation",
        ErrorScope.CHUNK_FATAL,
        cause=cause,
    )


class GoogleTranslator(BaseTranslator):
    """Cloud Translation v2 (Basic). One HTTP round trip per batch, no internal retry."""

    name: ClassVar[str] = "google"
    capabilities: ClassVar[TranslatorCapabilities] = TranslatorCapabilities(
        supports_glossary=False,  # glossaries are a v3 feature; v2 has none
        supports_batch=True,
        max_chars_per_request=20_000,  # docs: 100 000 *bytes* per request (see module docs)
        max_texts_per_request=128,  # docs/issue 5425: "Too many text segments" above 128
        supports_context=False,  # v2 has no context field
        supports_tag_protection=True,  # via format=html -> ProtectMode "xml"
    )
    client_factory: ClassVar[ClientFactory] = staticmethod(_default_client_factory)

    def __init__(self, client: Any, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    @classmethod
    def create(
        cls, settings: Settings, *, client_factory: Optional[ClientFactory] = None
    ) -> Result[BaseTranslator]:
        if settings.google_cloud_api is None:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                "GOOGLE_CLOUD_API is not set (environment variable or .env)",
                ErrorScope.USER,
            )
        key = settings.google_cloud_api.get_secret_value()
        factory = client_factory if client_factory is not None else cls.client_factory
        try:
            client = factory()
        except Exception as exc:  # noqa: BLE001 - fail-fast, classified here
            return err(
                ErrorCode.PROVIDER_CONFIG,
                "could not construct the Google HTTP client",
                ErrorScope.USER,
                cause=_redact(exc, key),
            )
        return Ok(cls(client, key))

    # -- glossary ------------------------------------------------------------- #

    async def prepare(self, glossary: EffectiveGlossary, run_id: str) -> Result[GlossaryBinding]:
        """v2 has no glossary endpoint: the binding is always ``NONE``.

        Nothing is sent and nothing fails here; the glossary post-check in
        ``pipeline/glossary_check.py`` still reports ``glossary_miss:<term>`` per unit, so a
        dropped term stays visible in the summary.
        """
        if glossary.entries:
            log.info(
                "Google Cloud Translation v2 has no glossary support; %d terms are "
                "checked after translation instead of being bound",
                len(glossary.entries),
                extra={"event": "glossary_unsupported"},
            )
        return Ok(GlossaryBinding(GlossaryStrategy.NONE, None, glossary.glossary_hash))

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

    async def _post(self, payload: Dict[str, Any]) -> Any:
        return await self._client.post(
            ENDPOINT,
            params={"key": self._api_key},
            json=payload,
            headers={"Accept": "application/json"},
        )

    def _fail(self, error: AppError, request: TranslationRequest) -> Result[ProviderResponse]:
        return err(
            error.code,
            error.message,
            error.scope,
            retry_after_s=error.retry_after_s,
            context={
                "chunk_id": request.chunk_id,
                "attempt": request.attempt,
                **dict(error.context),
            },
            cause=error.cause,
        )

    async def translate(
        self, texts: Sequence[str], request: TranslationRequest
    ) -> Result[ProviderResponse]:
        started = time.perf_counter()
        if not texts:
            return Ok(ProviderResponse(texts=[], chars_billed=0, latency_ms=0))
        # Control characters are stripped for the same reason as in the DeepL provider:
        # ``format=html`` parses the payload, and one C0 character fails the whole request.
        payload_texts = [strip_control_chars(text) for text in texts]
        out: List[str] = []
        billed = 0
        detected: Optional[str] = None
        for batch in self._batches(payload_texts):
            body: Dict[str, Any] = {
                "q": batch,
                "source": _language(request.source_lang),
                "target": _language(request.target_lang),
                "format": "html",
            }
            try:
                response = await self._post(body)
                status = int(response.status_code)
                parsed: Any = None
                if status != 200:
                    try:
                        parsed = response.json()
                    except Exception:  # noqa: BLE001 - a non-JSON error body is still an error
                        parsed = None
                    return self._fail(
                        classify_google_status(
                            status, parsed, getattr(response, "headers", {}) or {}, self._api_key
                        ),
                        request,
                    )
                parsed = response.json()
            except Exception as exc:  # noqa: BLE001 - classified, never re-raised
                return self._fail(classify_google_exception(exc, self._api_key), request)
            items = _translations(parsed)
            if items is None or len(items) != len(batch):
                count = "no" if items is None else str(len(items))
                return err(
                    ErrorCode.PROVIDER_EMPTY_RESPONSE,
                    f"Google returned {count} translations for {len(batch)} inputs",
                    ErrorScope.CHUNK_RETRYABLE,
                    context={"chunk_id": request.chunk_id, "attempt": request.attempt},
                )
            for source, item in zip(batch, items, strict=True):
                raw = item.get("translatedText")
                if not isinstance(raw, str):
                    return err(
                        ErrorCode.PROVIDER_EMPTY_RESPONSE,
                        "Google returned a translation without text",
                        ErrorScope.CHUNK_RETRYABLE,
                        context={"chunk_id": request.chunk_id, "attempt": request.attempt},
                    )
                # format=html means the answer is HTML-escaped ("&#39;", "&amp;").
                translated = _unescape_answer(raw, request.protect_mode)
                if source.strip() and not translated.strip():
                    return err(
                        ErrorCode.PROVIDER_EMPTY_RESPONSE,
                        "Google returned an empty translation",
                        ErrorScope.CHUNK_RETRYABLE,
                        context={"chunk_id": request.chunk_id, "attempt": request.attempt},
                    )
                out.append(translated)
                billed += len(source)  # v2 reports no count; Google bills what we send
                if detected is None:
                    lang = item.get("detectedSourceLanguage")
                    detected = str(lang).upper() if lang else None
        latency_ms = int((time.perf_counter() - started) * 1000)
        return Ok(
            ProviderResponse(
                texts=out, chars_billed=billed, latency_ms=latency_ms, detected_source_lang=detected
            )
        )

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is None:
            return None
        result = close()
        if asyncio.iscoroutine(result):
            await result
        return None


def _translations(body: Any) -> Optional[List[Dict[str, Any]]]:
    """``{"data": {"translations": [...]}}`` -> the list, or ``None`` when malformed."""
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    items = data.get("translations")
    if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
        return None
    return [dict(item) for item in items]
