"""Backoff, adaptive limiter and translator providers (design doc 02, 5.3, 5.4, 7.4, 7.5)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx
import pytest
from deepl import exceptions as deepl_exc
from pydantic import SecretStr

from book_translator.config import Settings
from book_translator.domain.models import (
    EffectiveGlossary,
    GlossaryEntry,
    GlossaryStatus,
    GlossaryStrategy,
)
from book_translator.domain.result import (
    AppError,
    Err,
    ErrorCode,
    ErrorScope,
    Ok,
    Result,
    unwrap,
)
from book_translator.pipeline.backoff import AdaptiveLimiter, BackoffPolicy, compute_delay
from book_translator.translators.base import (
    BaseTranslator,
    GlossaryBinding,
    ProviderResponse,
    TranslationRequest,
    get_translator,
    list_translators,
)
from book_translator.translators.deepl_translator import (
    DeepLTranslator,
    classify_deepl_exception,
    glossary_name_for,
)
from book_translator.translators.fallback import FallbackResponse, FallbackTranslator
from book_translator.translators.gemini import GeminiTranslator, endpoint_for
from book_translator.translators.google_translate import ENDPOINT, GoogleTranslator
from book_translator.translators.llm_base import MAX_PROMPT_TERMS, BaseLLMTranslator, build_prompt
from book_translator.translators.local_nmt import LocalNMTTranslator, apply_post_replace
from book_translator.translators.protect import protect, restore
from tests.conftest import DEFAULT_CAPABILITIES, FakeTranslator, Outcome

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


class ZeroRng(random.Random):
    """``uniform`` always returns the lower bound (the worst case for full jitter)."""

    def uniform(self, a: float, b: float) -> float:
        return a


class MaxRng(random.Random):
    """``uniform`` always returns the upper bound (deterministic worst case)."""

    def uniform(self, a: float, b: float) -> float:
        return b


class MinRng(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return a


def entry(source: str, target: str) -> GlossaryEntry:
    return GlossaryEntry(source, target, 1, GlossaryStatus.APPROVED, "user")


def glossary(*pairs: tuple[str, str]) -> EffectiveGlossary:
    entries = tuple(sorted((entry(s, t) for s, t in pairs), key=lambda e: -len(e.source)))
    return EffectiveGlossary(entries=entries, glossary_hash="h" * 64)


def settings_with_key(key: Optional[str] = "abc-123:fx", **kw: Any) -> Settings:
    return Settings(_env_file=None, deepl_api_key=key, **kw)  # type: ignore[call-arg]


def request(chunk_id: int = 1, binding: Optional[GlossaryBinding] = None) -> TranslationRequest:
    return TranslationRequest(
        job_id="j", run_id="r", chunk_id=chunk_id, attempt=0, glossary=binding
    )


@dataclass
class FakeTextResult:
    text: str
    detected_source_lang: str = "EN"
    billed_characters: int = 0


@dataclass
class FakeGlossaryInfo:
    glossary_id: str
    name: str
    ready: bool
    entry_count: int = 0


class FakeDeepLClient:
    """Stand-in for ``deepl.Translator``; raises real SDK exception classes."""

    def __init__(self, raise_exc: Optional[BaseException] = None) -> None:
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []
        self.glossaries: List[FakeGlossaryInfo] = []
        self.created: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self.create_exc: Optional[BaseException] = None  # raised by ``create_glossary`` only
        self.delete_exc: Dict[str, BaseException] = {}  # glossary_id -> failure
        self.responder: Optional[Any] = None
        self.closed = False

    def translate_text(self, text: Sequence[str], **kwargs: Any) -> List[FakeTextResult]:
        self.calls.append({"text": list(text), **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.responder is not None:
            return list(self.responder(list(text)))
        return [FakeTextResult("TR:" + t, billed_characters=len(t)) for t in text]

    def list_glossaries(self) -> List[FakeGlossaryInfo]:
        if self.raise_exc is not None:
            raise self.raise_exc
        return list(self.glossaries)

    def create_glossary(
        self, name: str, source_lang: str, target_lang: str, entries: Dict[str, str]
    ) -> FakeGlossaryInfo:
        self.created.append({"name": name, "source": source_lang, "target": target_lang,
                             "entries": entries})
        if self.create_exc is not None:
            raise self.create_exc
        return FakeGlossaryInfo("g-new", name, True, len(entries))

    def delete_glossary(self, glossary_id: str) -> None:
        failure = self.delete_exc.get(glossary_id)
        if failure is not None:
            raise failure
        self.deleted.append(glossary_id)
        self.glossaries = [g for g in self.glossaries if g.glossary_id != glossary_id]

    def close(self) -> None:
        self.closed = True


def make_deepl(client: FakeDeepLClient) -> DeepLTranslator:
    result = DeepLTranslator.create(settings_with_key(), client_factory=lambda k, u: client)
    translator = unwrap(result)
    assert isinstance(translator, DeepLTranslator)
    return translator


# --------------------------------------------------------------------------- #
# 5.4 backoff
# --------------------------------------------------------------------------- #


def test_compute_delay_floor_lifts_the_jitter() -> None:
    """Full jitter can draw ~0; under a rate limit that spends a retry without waiting."""
    kw = dict(base_s=2.0, factor=2.0, cap_s=60.0, retry_after_s=None, rng=ZeroRng())
    assert compute_delay(0, **kw) == 0.0  # type: ignore[arg-type]
    assert compute_delay(0, floor_s=5.0, **kw) == 5.0  # type: ignore[arg-type]
    # the floor never pushes a wait past the ceiling of the policy
    assert compute_delay(0, floor_s=900.0, **kw) == 60.0  # type: ignore[arg-type]
    # Retry-After still wins when it asks for more
    assert compute_delay(
        0, base_s=2.0, factor=2.0, cap_s=60.0, retry_after_s=30.0, floor_s=5.0, rng=ZeroRng()
    ) == 30.0


def test_rate_limit_budget_is_separate_from_the_failure_budget() -> None:
    policy = BackoffPolicy(max_retries=5, rate_limit_max_retries=20)
    assert policy.can_retry(4) and not policy.can_retry(5)
    assert policy.can_retry_rate_limited(19) and not policy.can_retry_rate_limited(20)
    # the rate-limit delay carries the floor, the ordinary one does not
    floored = BackoffPolicy(
        base_s=2.0, cap_s=60.0, rate_limit_min_delay_s=9.0, rng=ZeroRng()
    )
    assert floored.next_delay(0) == 0.0
    assert floored.next_rate_limit_delay(0) == 9.0


def test_compute_delay_exponential_with_full_jitter() -> None:
    kw = dict(base_s=2.0, factor=2.0, cap_s=60.0, retry_after_s=None)
    assert compute_delay(0, rng=MaxRng(), **kw) == 2.0
    assert compute_delay(1, rng=MaxRng(), **kw) == 4.0
    assert compute_delay(3, rng=MaxRng(), **kw) == 16.0
    assert compute_delay(3, rng=MinRng(), **kw) == 0.0
    rng = random.Random(42)
    for attempt in range(8):
        delay = compute_delay(attempt, rng=rng, **kw)
        assert 0.0 <= delay <= min(60.0, 2.0 * 2.0**attempt)


def test_compute_delay_is_capped() -> None:
    assert compute_delay(20, base_s=2.0, cap_s=60.0, retry_after_s=None, rng=MaxRng()) == 60.0


def test_compute_delay_retry_after_is_a_floor_even_above_cap() -> None:
    assert compute_delay(0, base_s=2.0, cap_s=60.0, retry_after_s=90.0, rng=MaxRng()) == 90.0
    assert compute_delay(5, base_s=2.0, cap_s=60.0, retry_after_s=1.0, rng=MaxRng()) == 60.0
    assert compute_delay(0, base_s=2.0, cap_s=60.0, retry_after_s=0.0, rng=MinRng()) == 0.0


def test_compute_delay_rejects_negative_attempt() -> None:
    with pytest.raises(ValueError):
        compute_delay(-1, base_s=1.0, cap_s=1.0, retry_after_s=None)


def test_backoff_policy_defaults_and_can_retry() -> None:
    policy = BackoffPolicy(rng=MaxRng())
    assert (policy.base_s, policy.factor, policy.cap_s, policy.max_retries) == (2.0, 2.0, 60.0, 5)
    assert policy.next_delay(2) == 8.0
    assert policy.next_delay(0, retry_after_s=30.0) == 30.0
    assert policy.can_retry(4) and not policy.can_retry(5)


# --------------------------------------------------------------------------- #
# 5.4 AdaptiveLimiter (AIMD)
# --------------------------------------------------------------------------- #


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_limiter_halves_on_rate_limit_down_to_one() -> None:
    limiter = AdaptiveLimiter(8, now=Clock())
    limiter.on_rate_limited()
    assert limiter.permits == 4
    limiter.on_rate_limited()
    limiter.on_rate_limited()
    assert limiter.permits == 1
    limiter.on_rate_limited()
    assert limiter.permits == 1
    assert limiter.changes == [("rate_limited", 4), ("rate_limited", 2), ("rate_limited", 1)]


def test_limiter_grows_after_twenty_successes_when_cooldown_elapsed() -> None:
    clock = Clock()
    events: List[tuple[str, int]] = []
    limiter = AdaptiveLimiter(8, now=clock, on_change=lambda e, p: events.append((e, p)))
    limiter.on_rate_limited()
    assert limiter.permits == 4
    for _ in range(20):
        limiter.on_success()
    assert limiter.permits == 4, "still inside the 30 s cooldown"
    clock.t += 30.0
    for _ in range(19):
        limiter.on_success()
    assert limiter.permits == 4, "the success streak restarted after the cooldown check"
    limiter.on_success()
    assert limiter.permits == 5
    for _ in range(20):
        limiter.on_success()
    assert limiter.permits == 6
    assert events[-2:] == [("grow", 5), ("grow", 6)]


def test_limiter_success_streak_resets_on_rate_limit_and_never_exceeds_max() -> None:
    clock = Clock()
    limiter = AdaptiveLimiter(2, now=clock)
    for _ in range(19):
        limiter.on_success()
    limiter.on_rate_limited()
    clock.t += 31.0
    for _ in range(19):
        limiter.on_success()
    assert limiter.permits == 1
    limiter.on_success()
    assert limiter.permits == 2
    for _ in range(40):
        limiter.on_success()
    assert limiter.permits == 2


def test_limiter_rejects_zero_permits() -> None:
    with pytest.raises(ValueError):
        AdaptiveLimiter(0)


async def test_limiter_acquire_release_and_reduction() -> None:
    limiter = AdaptiveLimiter(2, now=Clock())
    await limiter.acquire()
    await limiter.acquire()
    assert limiter.in_flight == 2
    third = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    assert not third.done()
    limiter.on_rate_limited()  # permits -> 1 while two are in flight
    limiter.release()
    await asyncio.sleep(0)
    assert not third.done(), "in-flight (1) is not below permits (1)"
    limiter.release()
    await asyncio.sleep(0)
    assert third.done() and limiter.in_flight == 1
    limiter.release()
    assert limiter.in_flight == 0
    with pytest.raises(RuntimeError):
        limiter.release()


async def test_limiter_growth_wakes_waiters_and_context_manager() -> None:
    clock = Clock()
    limiter = AdaptiveLimiter(2, now=clock)
    limiter.on_rate_limited()
    assert limiter.permits == 1
    entered: List[int] = []

    async def worker(i: int) -> None:
        async with limiter:
            entered.append(i)
            await asyncio.sleep(0.01)

    tasks = [asyncio.create_task(worker(i)) for i in range(3)]
    await asyncio.sleep(0)
    assert limiter.in_flight == 1
    clock.t += 31.0
    for _ in range(20):
        limiter.on_success()
    await asyncio.sleep(0)
    assert limiter.permits == 2 and limiter.in_flight == 2
    await asyncio.gather(*tasks)
    assert sorted(entered) == [0, 1, 2] and limiter.in_flight == 0


async def test_limiter_cancelled_waiter_does_not_leak_permit() -> None:
    limiter = AdaptiveLimiter(1, now=Clock())
    await limiter.acquire()
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    limiter.release()
    await limiter.acquire()  # permit is available again
    assert limiter.in_flight == 1


# --------------------------------------------------------------------------- #
# DeepL
# --------------------------------------------------------------------------- #


def test_registry_lists_and_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    assert list_translators() == ["deepl", "gemini", "google", "local"]
    result = get_translator("nope", settings_with_key())
    assert isinstance(result, Err) and result.error.code == ErrorCode.PROVIDER_CONFIG


def test_deepl_create_without_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    result = DeepLTranslator.create(settings_with_key(None))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_CONFIG
    assert result.error.scope == ErrorScope.USER


def test_deepl_create_uses_factory_with_key_and_url() -> None:
    seen: List[tuple[str, Optional[str]]] = []

    def factory(key: str, url: Optional[str]) -> FakeDeepLClient:
        seen.append((key, url))
        return FakeDeepLClient()

    settings = settings_with_key("k-1:fx", deepl_server_url="https://api-free.deepl.com")
    assert isinstance(DeepLTranslator.create(settings, client_factory=factory), Ok)
    assert seen == [("k-1:fx", "https://api-free.deepl.com")]
    assert isinstance(settings.deepl_api_key, SecretStr)
    assert "k-1" not in repr(settings)


def test_deepl_create_via_registry_uses_class_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = staticmethod(lambda k, u: FakeDeepLClient())
    monkeypatch.setattr(DeepLTranslator, "client_factory", factory)
    translator = unwrap(get_translator("deepl", settings_with_key()))
    assert isinstance(translator, DeepLTranslator)


@pytest.mark.parametrize(
    ("exc", "code", "scope"),
    [
        (deepl_exc.TooManyRequestsException("429", http_status_code=429),
         ErrorCode.PROVIDER_RATE_LIMITED, ErrorScope.CHUNK_RETRYABLE),
        (deepl_exc.QuotaExceededException("456", http_status_code=456),
         ErrorCode.PROVIDER_QUOTA, ErrorScope.JOB_FATAL),
        (deepl_exc.AuthorizationException("403", http_status_code=403),
         ErrorCode.PROVIDER_AUTH, ErrorScope.JOB_FATAL),
        (deepl_exc.ConnectionException("timeout", should_retry=True),
         ErrorCode.PROVIDER_TRANSIENT, ErrorScope.CHUNK_RETRYABLE),
        (deepl_exc.DeepLException("bad request", http_status_code=400),
         ErrorCode.PROVIDER_BAD_REQUEST, ErrorScope.CHUNK_FATAL),
        (deepl_exc.DeepLException("too large", http_status_code=413),
         ErrorCode.PROVIDER_BAD_REQUEST, ErrorScope.CHUNK_FATAL),
        (deepl_exc.GlossaryNotFoundException("gone", http_status_code=404),
         ErrorCode.PROVIDER_BAD_REQUEST, ErrorScope.CHUNK_FATAL),
        (deepl_exc.DeepLException("unavailable", should_retry=True, http_status_code=503),
         ErrorCode.PROVIDER_TRANSIENT, ErrorScope.CHUNK_RETRYABLE),
        (deepl_exc.DeepLException("server", http_status_code=500),
         ErrorCode.PROVIDER_TRANSIENT, ErrorScope.CHUNK_RETRYABLE),
        (TimeoutError("socket"), ErrorCode.PROVIDER_TRANSIENT, ErrorScope.CHUNK_RETRYABLE),
        (ValueError("bug"), ErrorCode.INTERNAL, ErrorScope.CHUNK_FATAL),
    ],
)
async def test_deepl_translate_classifies_exceptions(
    exc: BaseException, code: ErrorCode, scope: ErrorScope
) -> None:
    classified = classify_deepl_exception(exc)
    assert (classified.code, classified.scope) == (code, scope)
    translator = make_deepl(FakeDeepLClient(raise_exc=exc))
    result = await translator.translate(["Hello"], request(chunk_id=7))
    assert isinstance(result, Err)
    assert (result.error.code, result.error.scope) == (code, scope)
    assert result.error.context["chunk_id"] == 7
    assert "Hello" not in result.error.message


def test_deepl_rate_limit_carries_retry_after_when_present() -> None:
    exc = deepl_exc.TooManyRequestsException("429", http_status_code=429)
    assert classify_deepl_exception(exc).retry_after_s is None
    exc.retry_after = 12  # type: ignore[attr-defined]
    assert classify_deepl_exception(exc).retry_after_s == 12.0


async def test_deepl_translate_success_and_request_shape() -> None:
    client = FakeDeepLClient()
    translator = make_deepl(client)
    binding = GlossaryBinding(GlossaryStrategy.NATIVE, "g-1", "h" * 64)
    response = unwrap(await translator.translate(["a", "bb"], request(binding=binding)))
    assert response.texts == ["TR:a", "TR:bb"]
    assert response.chars_billed == 3
    assert response.detected_source_lang == "EN"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["text"] == ["a", "bb"]
    assert call["source_lang"] == "EN" and call["target_lang"] == "TR"
    assert call["glossary"] == "g-1"
    assert call["tag_handling"] == "xml" and call["ignore_tags"] == ["x"]
    assert call["preserve_formatting"] is True
    await translator.aclose()
    assert client.closed


async def test_deepl_translate_empty_input_makes_no_call() -> None:
    client = FakeDeepLClient()
    response = unwrap(await make_deepl(client).translate([], request()))
    assert response.texts == [] and client.calls == []


async def test_deepl_batches_by_text_count_and_chars() -> None:
    client = FakeDeepLClient()
    translator = make_deepl(client)
    texts = [f"t{i}" for i in range(120)]
    response = unwrap(await translator.translate(texts, request()))
    assert response.texts == ["TR:" + t for t in texts]
    assert [len(c["text"]) for c in client.calls] == [50, 50, 20]

    client.calls.clear()
    big = ["x" * 60_000, "y" * 50_000, "z"]
    unwrap(await translator.translate(big, request()))
    assert [len(c["text"]) for c in client.calls] == [1, 2]


@pytest.mark.parametrize(
    "responder",
    [
        lambda texts: [FakeTextResult("TR:" + t) for t in texts[:-1]],  # length mismatch
        lambda texts: [FakeTextResult("") for _ in texts],  # empty text
    ],
)
async def test_deepl_bad_response_is_empty_response_error(responder: Any) -> None:
    client = FakeDeepLClient()
    client.responder = responder
    result = await make_deepl(client).translate(["a", "b"], request())
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_EMPTY_RESPONSE
    assert result.error.scope == ErrorScope.CHUNK_RETRYABLE


async def test_deepl_prepare_empty_glossary_makes_no_call() -> None:
    client = FakeDeepLClient(raise_exc=RuntimeError("must not be called"))
    binding = unwrap(await make_deepl(client).prepare(glossary(), "run"))
    assert binding == GlossaryBinding(GlossaryStrategy.NONE, None, "h" * 64)


async def test_deepl_prepare_reuses_ready_glossary_with_same_name() -> None:
    client = FakeDeepLClient()
    name = glossary_name_for("h" * 64)
    assert name == "book-translator:" + "h" * 16
    client.glossaries = [
        FakeGlossaryInfo("g-old", name, ready=False),
        FakeGlossaryInfo("g-other", "book-translator:other", ready=True),
        FakeGlossaryInfo("g-ready", name, ready=True),
    ]
    binding = unwrap(await make_deepl(client).prepare(glossary(("Dragon", "Ejderha")), "run"))
    assert binding == GlossaryBinding(GlossaryStrategy.NATIVE, "g-ready", "h" * 64)
    assert client.created == []


async def test_deepl_prepare_creates_when_absent_with_clean_entries() -> None:
    """Empty targets are dropped, sources are trimmed - and an identity entry is *kept*.

    This test used to assert that ``("Same", "Same")`` is dropped as a no-op, following
    design doc 02 section 7.4. It is not a no-op: an identity entry is how a glossary says
    "leave this term in English", which is what a technical glossary is mostly for. Dropping
    them turned a 807-term terminology file into ``GlossaryStrategy.NONE`` and the terms came
    back translated."""
    client = FakeDeepLClient()
    g = glossary(("Dragon", "Ejderha"), (" Sword ", "Kılıç"), ("Same", "Same"), ("Empty", " "))
    binding = unwrap(await make_deepl(client).prepare(g, "run"))
    assert binding.strategy == GlossaryStrategy.NATIVE
    assert binding.provider_glossary_id == "g-new"
    assert client.created == [
        {"name": glossary_name_for("h" * 64), "source": "EN", "target": "TR",
         "entries": {"Dragon": "Ejderha", "Sword": "Kılıç", "Same": "Same"}}
    ]


async def test_a_glossary_of_only_identity_entries_still_binds() -> None:
    """The shape of the bulk of ``glossaries/terms.en-tr.json``: a term maps to itself."""
    client = FakeDeepLClient()
    g = glossary(("computer vision", "computer vision"), ("object detection", "object detection"))
    binding = unwrap(await make_deepl(client).prepare(g, "run"))
    assert binding.strategy == GlossaryStrategy.NATIVE  # not NONE
    assert client.created[0]["entries"] == {
        "computer vision": "computer vision", "object detection": "object detection",
    }


# --------------------------------------------------------------------------- #
# HTTP 456 means two different things (measured on a 484k/1M account)
# --------------------------------------------------------------------------- #

TOO_MANY_GLOSSARIES = (  # the exact text DeepL answered with
    "Quota for this billing period has been exceeded, message: Too many glossaries"
)


def quota_exc(message: str) -> deepl_exc.QuotaExceededException:
    return deepl_exc.QuotaExceededException(message, http_status_code=456)


def test_deepl_glossary_limit_is_not_reported_as_a_character_quota() -> None:
    """The account was at 484 353/1 000 000 characters; only the glossary count was full."""
    error = classify_deepl_exception(quota_exc(TOO_MANY_GLOSSARIES))

    assert error.code is ErrorCode.PROVIDER_GLOSSARY_LIMIT
    # PROVIDER_QUOTA would arm the automatic fallback: paying a second provider because a
    # glossary slot is missing is the wrong move (translators/fallback.py).
    assert error.code is not ErrorCode.PROVIDER_QUOTA
    assert error.scope is ErrorScope.USER  # the user can fix it right now
    assert "character quota exhausted" not in error.message
    assert "wait for the next period" not in error.message
    assert "Delete an existing glossary" in error.message
    assert "--cleanup-remote" in error.message
    assert error.context["http_status"] == 456


@pytest.mark.parametrize(
    "message",
    [
        "Quota for this billing period has been exceeded",
        "quota exceeded",
        "456",
    ],
)
def test_deepl_quota_without_a_glossary_word_keeps_the_character_reading(message: str) -> None:
    """DeepL may reword its text; anything that does not mention glossaries stays as it was."""
    error = classify_deepl_exception(quota_exc(message))

    assert error.code is ErrorCode.PROVIDER_QUOTA
    assert error.scope is ErrorScope.JOB_FATAL
    assert "character quota exhausted for this billing period" in error.message


def test_deepl_glossary_wording_is_matched_case_insensitively() -> None:
    error = classify_deepl_exception(quota_exc("Too Many GLOSSARIES for this account"))
    assert error.code is ErrorCode.PROVIDER_GLOSSARY_LIMIT


async def test_deepl_prepare_says_delete_a_glossary_not_wait_for_the_quota() -> None:
    """The message the user actually saw: a bind failure that told them to wait."""
    client = FakeDeepLClient()
    client.create_exc = quota_exc(TOO_MANY_GLOSSARIES)

    result = await make_deepl(client).prepare(glossary(("A", "B")), "run")

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.GLOSSARY_BIND_FAILED
    assert "Delete an existing glossary" in result.error.message
    assert "--cleanup-remote" in result.error.message
    assert "wait for the next period" not in result.error.message


# --------------------------------------------------------------------------- #
# --cleanup-remote: provider-side glossary administration
# --------------------------------------------------------------------------- #


def stocked_client() -> FakeDeepLClient:
    """Two stale tool glossaries, the current one, and two the user made themselves."""
    client = FakeDeepLClient()
    client.glossaries = [
        FakeGlossaryInfo("g-old1", glossary_name_for("a" * 64), True, 807),
        FakeGlossaryInfo("g-mine", "ai-ml terms", True, 12),
        FakeGlossaryInfo("g-current", glossary_name_for("h" * 64), True, 3),
        FakeGlossaryInfo("g-old2", glossary_name_for("b" * 64), True, 40),
        FakeGlossaryInfo("g-theirs", "book-translator (manual copy)", True, 1),
    ]
    return client


async def test_cleanup_deletes_only_the_tools_own_stale_glossaries() -> None:
    client = stocked_client()

    cleanup = unwrap(await make_deepl(client).cleanup_glossaries(keep_hash="h" * 64))

    assert client.deleted == ["g-old1", "g-old2"]  # nothing else was touched
    assert [(g.name, g.entries) for g in cleanup.deleted] == [
        (glossary_name_for("a" * 64), 807),
        (glossary_name_for("b" * 64), 40),
    ]
    assert cleanup.kept_current == glossary_name_for("h" * 64)
    assert cleanup.kept_foreign == 2  # "ai-ml terms" and the manually named one
    assert cleanup.failed == ()
    assert cleanup.supported is True


async def test_cleanup_never_deletes_a_glossary_the_user_made() -> None:
    """The safety rule: a name without the tool's prefix is off limits, whatever it says."""
    client = FakeDeepLClient()
    client.glossaries = [
        FakeGlossaryInfo("g-1", "book-translator", True, 5),  # prefix without the colon
        FakeGlossaryInfo("g-2", "Book-Translator:abc", True, 5),  # different case
        FakeGlossaryInfo("g-3", "my book-translator:abc", True, 5),  # prefix not at the start
        FakeGlossaryInfo("g-4", "book-translator:", True, 5),  # prefix, but no hash
    ]

    cleanup = unwrap(await make_deepl(client).cleanup_glossaries(keep_hash="h" * 64))

    assert client.deleted == []
    assert cleanup.deleted == () and cleanup.kept_foreign == 4


async def test_cleanup_without_a_current_hash_still_spares_foreign_glossaries() -> None:
    client = stocked_client()

    cleanup = unwrap(await make_deepl(client).cleanup_glossaries(keep_hash=None))

    assert client.deleted == ["g-old1", "g-current", "g-old2"]
    assert cleanup.kept_current is None and cleanup.kept_foreign == 2


async def test_cleanup_reports_when_there_was_nothing_to_delete() -> None:
    client = FakeDeepLClient()
    client.glossaries = [
        FakeGlossaryInfo("g-current", glossary_name_for("h" * 64), True, 3),
        FakeGlossaryInfo("g-mine", "ai-ml terms", True, 12),
    ]

    cleanup = unwrap(await make_deepl(client).cleanup_glossaries(keep_hash="h" * 64))

    assert cleanup.deleted == () and client.deleted == []
    assert cleanup.kept_current == glossary_name_for("h" * 64) and cleanup.kept_foreign == 1


async def test_cleanup_reports_a_failed_delete_and_carries_on() -> None:
    client = stocked_client()
    client.delete_exc["g-old1"] = deepl_exc.DeepLException("locked", http_status_code=400)

    cleanup = unwrap(await make_deepl(client).cleanup_glossaries(keep_hash="h" * 64))

    assert client.deleted == ["g-old2"]
    assert [g.name for g in cleanup.deleted] == [glossary_name_for("b" * 64)]
    assert len(cleanup.failed) == 1
    assert cleanup.failed[0].startswith(glossary_name_for("a" * 64) + ": ")
    assert "HTTP 400" in cleanup.failed[0]


async def test_cleanup_fails_cleanly_when_the_listing_fails() -> None:
    client = FakeDeepLClient(raise_exc=deepl_exc.AuthorizationException("403",
                                                                       http_status_code=403))

    result = await make_deepl(client).cleanup_glossaries(keep_hash="h" * 64)

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.PROVIDER_AUTH
    assert client.deleted == []


async def test_cleanup_is_a_no_op_for_a_provider_without_provider_side_glossaries() -> None:
    """``local``/``google``: the command says so instead of pretending it deleted something."""
    client = FakeHttpClient([])
    google = make_google(client)
    assert google.capabilities.supports_glossary is False

    cleanup = unwrap(await google.cleanup_glossaries(keep_hash="h" * 64))

    assert cleanup.supported is False
    assert cleanup.provider == "google" and cleanup.deleted == ()
    assert client.calls == []  # nothing was sent


async def test_the_default_glossary_admin_methods_report_unsupported() -> None:
    translator = FakeTranslator()
    listed = await translator.list_provider_glossaries()
    deleted = await translator.delete_provider_glossary("g-1")

    assert isinstance(listed, Err) and isinstance(deleted, Err)
    for error in (listed.error, deleted.error):
        assert error.code is ErrorCode.PROVIDER_CONFIG
        assert error.scope is ErrorScope.USER
        assert "keeps no glossaries" in error.message


async def test_deepl_prepare_failure_is_job_fatal() -> None:
    client = FakeDeepLClient(raise_exc=deepl_exc.DeepLException("boom", http_status_code=400))
    result = await make_deepl(client).prepare(glossary(("A", "B")), "run")
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.GLOSSARY_BIND_FAILED
    assert result.error.scope == ErrorScope.JOB_FATAL


# --------------------------------------------------------------------------- #
# 7.5 LLM prompt builder
# --------------------------------------------------------------------------- #


def test_build_prompt_contains_every_term_present_and_excludes_others() -> None:
    g = glossary(("Dragon", "Ejderha"), ("Sword", "Kılıç"), ("Castle", "Kale"))
    texts = ["The Dragon flew.", "A Sword ⟦1⟧ shone."]
    prompt = build_prompt(texts, g)
    assert "Dragon ⇒ Ejderha" in prompt and "Sword ⇒ Kılıç" in prompt
    assert "Castle" not in prompt
    assert "⟦n⟧" in prompt
    assert "[1] The Dragon flew." in prompt and "[2] A Sword ⟦1⟧ shone." in prompt


def test_a_term_opening_a_sentence_still_reaches_the_prompt() -> None:
    """A glossary term is only obeyed if the model is shown it, and the occurrence check
    used to be exact-case. Measured live against Gemini: of three terms in three
    sentences, the two that opened their sentence never entered the prompt and came back
    translated ("Bilgisayarli goru"); the one sitting mid-sentence entered and was kept.
    Case-folding the check took that run to three out of three."""
    g = glossary(("computer vision", "computer vision"), ("object detection", "nesne"))
    texts = [
        "Computer vision is a field.",  # capitalised: sentence start
        "OBJECT DETECTION in a heading.",  # a heading shouts
    ]

    prompt = build_prompt(texts, g)

    assert "computer vision \u21d2 computer vision" in prompt
    assert "object detection \u21d2 nesne" in prompt


def test_the_terminology_block_keeps_the_glossary_spelling() -> None:
    """Matching is case-insensitive; the pair shown is still the glossary's own, because
    that is the form the model is asked to produce."""
    g = glossary(("OpenCV", "OpenCV"))

    prompt = build_prompt(["opencv is a library."], g)

    assert "OpenCV \u21d2 OpenCV" in prompt


def test_an_absent_term_is_still_left_out() -> None:
    g = glossary(("Dragon", "Ejderha"), ("Castle", "Kale"))

    prompt = build_prompt(["The dragon flew."], g)

    assert "Dragon \u21d2 Ejderha" in prompt
    assert "Castle" not in prompt


def test_build_prompt_caps_terms_at_200() -> None:
    pairs = [(f"Term{i:03d}", f"Terim{i:03d}") for i in range(250)]
    g = glossary(*pairs)
    prompt = build_prompt([" ".join(s for s, _ in pairs)], g)
    assert prompt.count(" ⇒ ") == MAX_PROMPT_TERMS


class ScriptedLLM(BaseLLMTranslator):
    name = "llm:test"

    def __init__(self, reply: Result[str]) -> None:
        super().__init__()
        self.reply = reply
        self.prompts: List[str] = []

    @classmethod
    def create(cls, settings: Settings) -> Result[BaseTranslator]:
        return Ok(cls(Ok("")))

    async def complete(self, prompt: str) -> Result[str]:
        self.prompts.append(prompt)
        return self.reply


async def test_llm_translate_uses_prompt_strategy_and_maps_numbered_reply() -> None:
    llm = ScriptedLLM(Ok("[1] Ejderha uçtu.\n[2] Kılıç parladı."))
    binding = unwrap(await llm.prepare(glossary(("Dragon", "Ejderha")), "run"))
    assert binding.strategy == GlossaryStrategy.PROMPT
    response = unwrap(await llm.translate(["Dragon flew.", "Sword shone."], request()))
    assert response.texts == ["Ejderha uçtu.", "Kılıç parladı."]
    assert "Dragon ⇒ Ejderha" in llm.prompts[0]


async def test_llm_unmappable_reply_is_empty_response() -> None:
    llm = ScriptedLLM(Ok("only one line"))
    result = await llm.translate(["a", "b"], request())
    assert isinstance(result, Err) and result.error.code == ErrorCode.PROVIDER_EMPTY_RESPONSE


# --------------------------------------------------------------------------- #
# 7.5 local NMT
# --------------------------------------------------------------------------- #


def test_apply_post_replace_word_boundary_case_sensitive_longest_first() -> None:
    g = glossary(("York", "York'"), ("New York", "New York'a"), ("Dragon", "Ejderha"))
    text, warnings = apply_post_replace("Dragon and Dragons went to New York with dragon.", g)
    assert text == "Ejderha and Dragons went to New York'a with dragon."
    assert warnings == []


def test_apply_post_replace_reports_miss_only_with_source_text() -> None:
    g = glossary(("Dragon", "Ejderha"), ("Sword", "Kılıç"))
    source = "The Dragon and the Sword."
    text, warnings = apply_post_replace("Ejderha ve kılıç.", g, source)
    assert text == "Ejderha ve kılıç."
    assert warnings == ["glossary_miss:Sword"]
    text, warnings = apply_post_replace("Ejderha ve Sword.", g, source)
    assert text == "Ejderha ve Kılıç." and warnings == []
    text, warnings = apply_post_replace("Castle only.", g, "The Castle.")
    assert text == "Castle only." and warnings == []


def test_local_create_reports_missing_packages(tmp_path: Path) -> None:
    settings = settings_with_key(local_model_dir=tmp_path)
    result = LocalNMTTranslator.create(settings)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_CONFIG
    assert result.error.scope == ErrorScope.USER
    assert "ctranslate2" in result.error.message
    assert "book-translator[local]" in result.error.message


class FakeEngine:
    def __init__(self, raise_exc: Optional[BaseException] = None) -> None:
        self.raise_exc = raise_exc

    def translate_batch(self, texts: Sequence[str]) -> List[str]:
        if self.raise_exc is not None:
            raise self.raise_exc
        return ["Ejderha uçtu." if "Dragon" in t else "Sword parladı." for t in texts]


async def test_local_translate_applies_post_replace() -> None:
    translator = LocalNMTTranslator(FakeEngine(), Path("model"))
    binding = unwrap(await translator.prepare(glossary(("Sword", "Kılıç")), "run"))
    assert binding.strategy == GlossaryStrategy.POST_REPLACE
    response = unwrap(await translator.translate(["Dragon flew.", "Sword shone."], request()))
    assert response.texts == ["Ejderha uçtu.", "Kılıç parladı."]
    assert response.chars_billed == 0


async def test_local_translate_oom_is_transient() -> None:
    translator = LocalNMTTranslator(FakeEngine(MemoryError("cuda")), Path("model"))
    result = await translator.translate(["x"], request())
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_TRANSIENT
    assert result.error.scope == ErrorScope.CHUNK_RETRYABLE


def test_local_capabilities_shape() -> None:
    caps = LocalNMTTranslator.capabilities
    assert not caps.supports_glossary and not caps.supports_tag_protection
    assert DeepLTranslator.capabilities.supports_tag_protection
    assert DeepLTranslator.capabilities.max_texts_per_request == 50


# --------------------------------------------------------------------------- #
# v1.1 (design doc 06, C5): DeepL forwards the request context
# --------------------------------------------------------------------------- #


async def test_deepl_forwards_context_only_when_present() -> None:
    assert DeepLTranslator.capabilities.supports_context is True
    client = FakeDeepLClient()
    translator = make_deepl(client)
    legend_request = TranslationRequest(
        job_id="j", run_id="r", chunk_id=3, attempt=0, context="Figure 1: Flow."
    )
    response = unwrap(
        await translator.translate(["Figure 1: Flow.", "Start", "Stop"], legend_request)
    )
    assert response.texts == ["TR:Figure 1: Flow.", "TR:Start", "TR:Stop"]
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["text"] == ["Figure 1: Flow.", "Start", "Stop"]
    assert call["context"] == "Figure 1: Flow."
    assert call["tag_handling"] == "xml" and call["preserve_formatting"] is True

    client.calls.clear()
    unwrap(await translator.translate(["a"], request()))
    assert "context" not in client.calls[0]

    client.calls.clear()
    unwrap(await translator.translate(["a"], dataclasses.replace(request(), context="")))
    assert "context" not in client.calls[0]


async def test_deepl_context_is_repeated_on_every_batch() -> None:
    client = FakeDeepLClient()
    translator = make_deepl(client)
    texts = [f"t{i}" for i in range(60)]
    unwrap(await translator.translate(
        texts, TranslationRequest(job_id="j", run_id="r", chunk_id=1, attempt=0, context="ctx")
    ))
    assert [len(c["text"]) for c in client.calls] == [50, 10]
    assert all(c["context"] == "ctx" for c in client.calls)


def test_control_characters_never_reach_the_provider() -> None:
    """A PDF built from Computer Modern hands back C0 controls where math glyphs were.

    DeepL is called with ``tag_handling="xml"`` and rejects the whole request over a
    single one of them, so every unit of the batch fails - on a 48-page book that was
    194 failed units over the 20 that actually carried one."""
    from book_translator.translators.protect import strip_control_chars

    dirty = "Matrix" + chr(0x14) + " 1" + chr(0x15) + " with " + chr(0) + "a gap" + chr(1)
    assert strip_control_chars(dirty) == "Matrix 1 with a gap"
    # tab, newline and carriage return are valid XML and must survive
    plain = "a" + chr(9) + "b" + chr(10) + "c" + chr(13) + "d"
    assert strip_control_chars(plain) == plain
    for mode in ("xml", "sentinel"):
        out, _mapping = protect(dirty, mode)
        assert not any(ord(c) < 0x20 and c not in plain for c in out)


@pytest.mark.asyncio
async def test_deepl_payload_carries_no_bare_angle_bracket() -> None:
    """The real HTTP 400: ``<opencv_build_folder>`` is not a tag, it is prose.

    ``tag_handling="xml"`` makes DeepL parse the request; an unclosed ``<...>`` rejects the
    **whole** request, so 12-character headings such as "Contributors" failed for no other
    reason than sharing a batch with it (65 units on the 364-page run).
    """
    client = FakeDeepLClient()
    translator = make_deepl(client)
    sources = [
        "Contributors",
        "CMake generated <opencv_build_folder>/OpenCV.sln; open it.",
        "Joe Minichino is an R&D labs engineer at Teamwork.",
    ]
    payload, mappings = zip(*(protect(text, "xml") for text in sources), strict=True)

    result = await translator.translate(list(payload), request())

    assert isinstance(result, Ok)
    sent = client.calls[0]["text"]
    assert all("<" not in t.replace('<x id="', "") for t in sent)
    for text in sent:
        ET.fromstring(f"<d>{text}</d>")  # the request DeepL would have parsed
    # the fake echoes the payload back the way the XML mode does: still escaped
    answers = [t.removeprefix("TR:") for t in result.value.texts]
    assert [
        unwrap(restore(answer, mapping, "xml"))
        for answer, mapping in zip(answers, mappings, strict=True)
    ] == sources


@pytest.mark.asyncio
async def test_deepl_context_window_is_escaped_too() -> None:
    """The page text of an overlay batch is parsed as XML as well - escape it."""
    client = FakeDeepLClient()
    translator = make_deepl(client)

    result = await translator.translate(
        ["metin"],
        dataclasses.replace(request(), context="see <opencv_build_folder> & the README"),
    )

    assert isinstance(result, Ok)
    assert client.calls[0]["context"] == "see &lt;opencv_build_folder&gt; &amp; the README"
    ET.fromstring(f"<d>{client.calls[0]['context']}</d>")


@pytest.mark.asyncio
async def test_deepl_strips_control_characters_from_the_context_window() -> None:
    """The context window never goes through ``protect``; DeepL parses it the same way."""
    client = FakeDeepLClient()
    translator = make_deepl(client)
    result = await translator.translate(
        ["hello"],
        TranslationRequest(
            job_id="j", run_id="r", chunk_id=1, attempt=0,
            context="page" + chr(0x15) + " text" + chr(0x14),
        ),
    )
    assert isinstance(result, Ok)
    assert client.calls[0]["context"] == "page text"


# --------------------------------------------------------------------------- #
# Google Cloud Translation v2 (Basic)
# --------------------------------------------------------------------------- #


GOOGLE_KEY = "AIzaSyD-FAKE-key-for-tests-0123456789"


@dataclass
class FakeHttpResponse:
    """Minimal stand-in for ``httpx.Response`` (only what the provider reads)."""

    status_code: int
    payload: Any = None
    headers: Dict[str, str] = dataclasses.field(default_factory=dict)
    raises: Optional[BaseException] = None

    def json(self) -> Any:
        if self.raises is not None:
            raise self.raises
        return self.payload


class FakeHttpClient:
    """Async HTTP client that never leaves the process."""

    def __init__(
        self,
        responses: Optional[Sequence[FakeHttpResponse]] = None,
        raise_exc: Optional[BaseException] = None,
    ) -> None:
        self.responses = list(responses or [])
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []
        self.closed = False

    async def post(self, url: str, **kwargs: Any) -> FakeHttpResponse:
        self.calls.append({"url": url, **kwargs})
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.responses:
            return self.responses.pop(0)
        texts = list(kwargs["json"]["q"])
        return FakeHttpResponse(
            200,
            {
                "data": {
                    "translations": [
                        {"translatedText": "TR:" + t, "detectedSourceLanguage": "en"}
                        for t in texts
                    ]
                }
            },
        )

    async def aclose(self) -> None:
        self.closed = True


def google_settings(key: Optional[str] = GOOGLE_KEY, **kw: Any) -> Settings:
    return Settings(_env_file=None, google_cloud_api=key, **kw)  # type: ignore[call-arg]


def make_google(client: FakeHttpClient, key: Optional[str] = GOOGLE_KEY) -> GoogleTranslator:
    made = GoogleTranslator.create(google_settings(key), client_factory=lambda: client)
    translator = unwrap(made)
    assert isinstance(translator, GoogleTranslator)
    return translator


def google_error(status: int, reason: str, message: str = "boom") -> FakeHttpResponse:
    return FakeHttpResponse(
        status,
        {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}},
    )


async def test_google_translates_a_batch_and_reports_what_it_sent() -> None:
    client = FakeHttpClient()
    translator = make_google(client)

    response = unwrap(await translator.translate(["Hello", "World"], request()))

    assert response.texts == ["TR:Hello", "TR:World"]
    assert response.chars_billed == len("Hello") + len("World")  # v2 reports no count
    assert response.detected_source_lang == "EN"
    call = client.calls[0]
    assert call["url"] == ENDPOINT
    assert call["params"] == {"key": GOOGLE_KEY}
    assert call["json"] == {
        "q": ["Hello", "World"],
        "source": "en",
        "target": "tr",
        "format": "html",  # tag protection for the <x id="n"/> placeholders
    }


async def test_google_unescapes_only_its_own_references_in_xml_mode() -> None:
    """``format=html`` answers are escaped; only the part Google added is undone here.

    The payload left this process already escaped (``protect(..., "xml")``), so
    ``&amp;`` / ``&lt;`` / ``&gt;`` are handed on to ``restore``, which resolves exactly
    one level. Undoing them twice would turn a book's literal ``&amp;`` into a bare ``&``.
    ``&#39;`` and ``&quot;`` are Google's own doing and are resolved right here.
    """
    escaped = "Kitab&#39;in &amp; kalemin &lt;b&gt; ve &quot;tirnak&quot;"
    client = FakeHttpClient(
        [FakeHttpResponse(200, {"data": {"translations": [{"translatedText": escaped}]}})]
    )
    translator = make_google(client)

    response = unwrap(await translator.translate(["The book's & pen"], request()))

    assert response.texts == ["Kitab'in &amp; kalemin &lt;b&gt; ve \"tirnak\""]
    assert (
        unwrap(restore(response.texts[0], {}, "xml"))
        == "Kitab'in & kalemin <b> ve \"tirnak\""
    )


async def test_google_unescapes_everything_in_sentinel_mode() -> None:
    """Without tag protection nothing was escaped on the way out, so all of it comes back."""
    escaped = "Kitab&#39;in &amp; kalemi &lt;b&gt;"
    client = FakeHttpClient(
        [FakeHttpResponse(200, {"data": {"translations": [{"translatedText": escaped}]}})]
    )
    translator = make_google(client)

    response = unwrap(
        await translator.translate(
            ["The book's & pen"], dataclasses.replace(request(), protect_mode="sentinel")
        )
    )

    assert response.texts == ["Kitab'in & kalemi <b>"]


async def test_google_quota_is_job_fatal() -> None:
    client = FakeHttpClient([google_error(403, "quotaExceeded", "quota gone")])
    translator = make_google(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_QUOTA
    assert result.error.scope == ErrorScope.JOB_FATAL
    assert result.error.context["http_status"] == 403


async def test_google_403_rate_limit_is_retryable_not_a_quota() -> None:
    """v2 reports "too fast" and "quota gone" both as 403; only the reason separates them.

    Reading ``userRateLimitExceeded`` as a quota would make the automatic fallback
    abandon the primary provider over a two-second hiccup.
    """
    client = FakeHttpClient([google_error(403, "userRateLimitExceeded")])
    translator = make_google(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_RATE_LIMITED
    assert result.error.scope == ErrorScope.CHUNK_RETRYABLE


async def test_google_429_carries_retry_after() -> None:
    client = FakeHttpClient(
        [FakeHttpResponse(429, {"error": {"message": "slow down"}}, {"Retry-After": "7"})]
    )
    translator = make_google(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_RATE_LIMITED
    assert result.error.retry_after_s == 7.0


async def test_google_400_is_chunk_fatal() -> None:
    client = FakeHttpClient([google_error(400, "invalid", "Too many text segments")])
    translator = make_google(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_BAD_REQUEST
    assert result.error.scope == ErrorScope.CHUNK_FATAL
    assert "Too many text segments" in result.error.message


@pytest.mark.parametrize(
    ("status", "reason"),
    [(401, ""), (400, "API_KEY_INVALID"), (403, "accessNotConfigured"), (403, "somethingElse")],
)
async def test_google_auth_failures_stop_the_job(status: int, reason: str) -> None:
    client = FakeHttpClient([google_error(status, reason, "API key not valid")])
    translator = make_google(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_AUTH
    assert result.error.scope == ErrorScope.JOB_FATAL


async def test_google_transport_error_is_transient() -> None:
    client = FakeHttpClient(raise_exc=httpx.ConnectError("nope"))
    translator = make_google(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_TRANSIENT
    assert result.error.scope == ErrorScope.CHUNK_RETRYABLE


async def test_google_response_shape_is_verified() -> None:
    client = FakeHttpClient(
        [FakeHttpResponse(200, {"data": {"translations": [{"translatedText": "one"}]}})]
    )
    translator = make_google(client)

    result = await translator.translate(["a", "b"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_EMPTY_RESPONSE
    assert result.error.scope == ErrorScope.CHUNK_RETRYABLE


async def test_google_strips_control_characters_before_sending() -> None:
    """Same bug as the DeepL one: ``format=html`` parses the payload."""
    client = FakeHttpClient()
    translator = make_google(client)

    response = unwrap(await translator.translate(["Matrix" + chr(0x14) + " 1"], request()))

    assert client.calls[0]["json"]["q"] == ["Matrix 1"]
    assert response.chars_billed == len("Matrix 1")  # billed for what was actually sent


async def test_google_splits_batches_at_the_documented_limits() -> None:
    client = FakeHttpClient()
    translator = make_google(client)
    caps = GoogleTranslator.capabilities
    assert (caps.max_texts_per_request, caps.max_chars_per_request) == (128, 20_000)

    unwrap(await translator.translate([f"t{i}" for i in range(130)], request()))
    assert [len(c["json"]["q"]) for c in client.calls] == [128, 2]

    client.calls.clear()
    unwrap(await translator.translate(["x" * 12_000, "y" * 12_000], request()))
    assert [len(c["json"]["q"]) for c in client.calls] == [1, 1]


async def test_google_has_no_glossary_but_still_binds() -> None:
    translator = make_google(FakeHttpClient())
    binding = unwrap(await translator.prepare(glossary(("gouge", "gouge")), "run1"))
    assert binding.strategy is GlossaryStrategy.NONE
    assert binding.provider_glossary_id is None
    assert GoogleTranslator.capabilities.supports_glossary is False
    # format=html carries the <x id="n"/> placeholders, so the xml protect mode applies
    assert GoogleTranslator.capabilities.supports_tag_protection is True
    assert GoogleTranslator.capabilities.supports_context is False


def test_google_create_without_key_fails() -> None:
    result = GoogleTranslator.create(google_settings(None), client_factory=FakeHttpClient)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_CONFIG
    assert result.error.scope == ErrorScope.USER
    assert "GOOGLE_CLOUD_API" in result.error.message


async def test_google_never_leaks_the_api_key(caplog: pytest.LogCaptureFixture) -> None:
    """CR-01 for the Google key: httpx puts the request URL - key and all - into its
    exception text, and a proxy can echo the key back in an error body."""
    leaky = httpx.ConnectError(f"failed for {ENDPOINT}?key={GOOGLE_KEY}&x=1")
    client = FakeHttpClient(raise_exc=leaky)
    translator = make_google(client)

    with caplog.at_level("DEBUG"):
        result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    printed = "\n".join(
        [result.error.message, str(result.error.cause or ""), str(result.error), caplog.text]
    )
    assert GOOGLE_KEY not in printed and "AIza" not in printed
    assert "key=***" in str(result.error.cause)

    echoed = FakeHttpClient([google_error(403, "forbidden", f"bad key {GOOGLE_KEY}")])
    body_result = await make_google(echoed).translate(["a"], request())
    assert isinstance(body_result, Err)
    assert GOOGLE_KEY not in str(body_result.error)
    assert GOOGLE_KEY not in repr(google_settings())  # SecretStr repr


async def test_google_aclose_closes_the_client() -> None:
    client = FakeHttpClient()
    translator = make_google(client)
    await translator.aclose()
    assert client.closed is True


# --------------------------------------------------------------------------- #
# Automatic fallback wrapper (user decision: switch automatically, warn)
# --------------------------------------------------------------------------- #


def quota_script(*chunk_ids: int) -> Dict[int, Sequence[Outcome]]:
    return {chunk_id: ["err_456"] for chunk_id in chunk_ids}


def make_pair(
    *,
    primary_script: Optional[Dict[int, Sequence[Outcome]]] = None,
    secondary_glossary: bool = False,
) -> Tuple[FakeTranslator, FakeTranslator, FallbackTranslator]:
    primary = FakeTranslator(
        primary_script,
        capabilities=dataclasses.replace(
            DEFAULT_CAPABILITIES, supports_glossary=True, supports_context=True
        ),
        strategy=GlossaryStrategy.NATIVE,
    )
    primary.name = "primary"
    secondary = FakeTranslator(
        capabilities=dataclasses.replace(
            DEFAULT_CAPABILITIES,
            supports_glossary=secondary_glossary,
            max_texts_per_request=128,
            max_chars_per_request=20_000,
        )
    )
    secondary.name = "secondary"
    return primary, secondary, FallbackTranslator(primary, secondary)


async def test_fallback_switches_on_quota_and_names_the_provider() -> None:
    primary, secondary, translator = make_pair(primary_script=quota_script(1))
    unwrap(await translator.prepare(glossary(("gouge", "gouge")), "run1"))

    response = unwrap(await translator.translate(["a"], request(chunk_id=1)))

    assert isinstance(response, FallbackResponse)
    assert response.provider == "secondary"
    assert response.warnings == ("provider_fallback:primary->secondary",
                                 "glossary_unavailable:secondary")
    assert len(primary.calls) == 1 and len(secondary.calls) == 1
    assert translator.switched is True and translator.active_provider == "secondary"


async def test_fallback_is_permanent_for_the_run() -> None:
    """Retrying the primary per unit would pay for a failed round trip every time."""
    primary, secondary, translator = make_pair(primary_script=quota_script(1))
    unwrap(await translator.prepare(glossary(), "run1"))

    for chunk_id in (1, 2, 3):
        unwrap(await translator.translate([f"t{chunk_id}"], request(chunk_id=chunk_id)))

    assert len(primary.calls) == 1  # only the one that hit the quota
    assert [c[0] for c in secondary.calls] == [1, 2, 3]


async def test_fallback_tags_the_primary_response_too() -> None:
    primary, secondary, translator = make_pair()
    unwrap(await translator.prepare(glossary(), "run1"))

    response = unwrap(await translator.translate(["a"], request()))

    assert isinstance(response, FallbackResponse)
    assert response.provider == "primary" and response.warnings == ()
    assert not secondary.calls


@pytest.mark.parametrize("outcome", ["err_429", "err_403", "err_400", "err_5xx"])
async def test_only_a_quota_error_triggers_the_fallback(outcome: str) -> None:
    primary, secondary, translator = make_pair(primary_script={1: [outcome]})
    unwrap(await translator.prepare(glossary(), "run1"))

    result = await translator.translate(["a"], request(chunk_id=1))

    assert isinstance(result, Err)
    assert not secondary.calls
    assert translator.switched is False


async def test_fallback_warns_about_the_glossary_only_when_one_is_bound() -> None:
    primary, secondary, translator = make_pair(primary_script=quota_script(1))
    primary.strategy = GlossaryStrategy.NONE  # nothing bound: nothing to lose
    unwrap(await translator.prepare(glossary(), "run1"))

    response = unwrap(await translator.translate(["a"], request(chunk_id=1)))

    assert isinstance(response, FallbackResponse)
    assert response.warnings == ("provider_fallback:primary->secondary",)


async def test_fallback_keeps_the_glossary_warning_off_when_the_secondary_has_one() -> None:
    primary, secondary, translator = make_pair(
        primary_script=quota_script(1), secondary_glossary=True
    )
    unwrap(await translator.prepare(glossary(("gouge", "gouge")), "run1"))

    response = unwrap(await translator.translate(["a"], request(chunk_id=1)))

    assert isinstance(response, FallbackResponse)
    assert response.warnings == ("provider_fallback:primary->secondary",)


async def test_fallback_strips_what_the_secondary_cannot_take() -> None:
    primary, secondary, translator = make_pair(primary_script=quota_script(1))
    unwrap(await translator.prepare(glossary(("gouge", "gouge")), "run1"))
    binding = GlossaryBinding(GlossaryStrategy.NATIVE, "deepl-glossary-id", "h" * 64)
    sent = dataclasses.replace(request(chunk_id=1, binding=binding), context="page text")

    captured: List[TranslationRequest] = []
    original = secondary.translate

    async def spy(texts: Sequence[str], req: TranslationRequest) -> Result[ProviderResponse]:
        captured.append(req)
        return await original(texts, req)

    secondary.translate = spy  # type: ignore[method-assign]
    unwrap(await translator.translate(["a"], sent))

    assert captured[0].glossary is None  # a DeepL glossary id means nothing to Google
    assert captured[0].context is None


async def test_fallback_capabilities_are_the_common_ground() -> None:
    primary, secondary, translator = make_pair()
    caps = translator.capabilities
    assert caps.max_texts_per_request == min(
        primary.capabilities.max_texts_per_request, secondary.capabilities.max_texts_per_request
    )
    assert caps.max_chars_per_request == min(
        primary.capabilities.max_chars_per_request, secondary.capabilities.max_chars_per_request
    )
    assert caps.supports_context is False  # the secondary has none
    assert caps.supports_glossary is True  # the primary binds it


async def test_fallback_prepare_binds_both_and_closes_both() -> None:
    primary, secondary, translator = make_pair()

    binding = unwrap(await translator.prepare(glossary(("a", "b")), "run1"))

    assert binding.strategy is GlossaryStrategy.NATIVE  # the primary's
    assert len(primary.prepared) == 1 and len(secondary.prepared) == 1
    await translator.aclose()
    assert primary.closed and secondary.closed


async def test_fallback_prepare_fails_fast_when_the_secondary_cannot_bind() -> None:
    primary, secondary, translator = make_pair()
    secondary.prepare_error = AppError(
        ErrorCode.GLOSSARY_BIND_FAILED, "no", ErrorScope.JOB_FATAL
    )

    result = await translator.prepare(glossary(("a", "b")), "run1")

    assert isinstance(result, Err) and result.error.code == ErrorCode.GLOSSARY_BIND_FAILED


def test_fallback_is_not_reachable_through_the_registry() -> None:
    assert "fallback" not in list_translators()
    result = FallbackTranslator.create(settings_with_key())
    assert isinstance(result, Err) and result.error.code == ErrorCode.PROVIDER_CONFIG


# --------------------------------------------------------------------------- #
# automatic fallback resolution
# --------------------------------------------------------------------------- #


def test_a_configured_second_provider_arms_the_fallback_by_itself() -> None:
    """The user asked for this: a run must not stop half-way through a book because the
    primary's quota died. With a second provider's key present, the fallback is armed
    without a flag."""
    both = settings_with_key(google_cloud_api="AIza" + "x" * 35)
    assert both.effective_fallback_provider() == "google"


def test_without_a_second_key_there_is_no_fallback_and_no_error() -> None:
    """A user with one provider sees no change - and no complaint about a missing key."""
    only_deepl = settings_with_key()
    assert only_deepl.effective_fallback_provider() is None
    assert unwrap(only_deepl.validate_fallback_provider()) is None


def test_the_automatic_fallback_can_be_switched_off() -> None:
    both = settings_with_key(google_cloud_api="AIza" + "x" * 35, fallback_provider="none")
    assert both.effective_fallback_provider() is None
    assert unwrap(both.validate_fallback_provider()) is None


def test_an_explicit_fallback_wins_over_the_automatic_one() -> None:
    both = settings_with_key(google_cloud_api="AIza" + "x" * 35, fallback_provider="local")
    assert both.effective_fallback_provider() == "local"


def test_the_fallback_is_never_the_primary_provider() -> None:
    """Primary google: the automatic pick has to be the *other* configured provider."""
    google_first = settings_with_key(
        google_cloud_api="AIza" + "x" * 35, provider="google",
    )
    assert google_first.effective_fallback_provider() == "deepl"


def test_an_unknown_fallback_name_is_a_user_error() -> None:
    bad = settings_with_key(fallback_provider="nope")
    result = bad.validate_fallback_provider()
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.PROVIDER_CONFIG
    assert "nope" in result.error.message


# --------------------------------------------------------------------------- #
# Gemini (Generative Language API)
# --------------------------------------------------------------------------- #

GEMINI_KEY = "AIzaSyD-FAKE-gemini-key-0123456789ab"


def gemini_settings(key: Optional[str] = GEMINI_KEY, **kw: Any) -> Settings:
    return Settings(_env_file=None, gemini_api_key=key, **kw)  # type: ignore[call-arg]


def make_gemini(
    client: FakeHttpClient, key: Optional[str] = GEMINI_KEY, **kw: Any
) -> GeminiTranslator:
    made = GeminiTranslator.create(gemini_settings(key, **kw), client_factory=lambda: client)
    translator = unwrap(made)
    assert isinstance(translator, GeminiTranslator)
    return translator


def gemini_answer(text: str) -> FakeHttpResponse:
    return FakeHttpResponse(
        200,
        {
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4},
            "modelVersion": "gemini-3.8-flash",
        },
    )


def gemini_error(
    status: int, api_status: str, message: str = "boom", **extra: Any
) -> FakeHttpResponse:
    error: Dict[str, Any] = {"code": status, "message": message, "status": api_status}
    error.update(extra)
    return FakeHttpResponse(status, {"error": error})


async def test_gemini_translates_a_batch_and_sends_the_key_in_a_header() -> None:
    """A key in the query string reaches proxy logs and httpx exception text; a header
    does not. The numbered answer is mapped back onto the inputs in order."""
    client = FakeHttpClient([gemini_answer("[1] Merhaba\n[2] Dunya")])
    translator = make_gemini(client)

    response = unwrap(await translator.translate(["Hello", "World"], request()))

    assert response.texts == ["Merhaba", "Dunya"]
    call = client.calls[0]
    assert call["url"] == endpoint_for("gemini-3.8-flash")
    assert call["headers"]["x-goog-api-key"] == GEMINI_KEY
    assert "params" not in call and GEMINI_KEY not in call["url"]


async def test_gemini_carries_the_glossary_in_the_prompt() -> None:
    """The reason this provider is worth having: no upload step, the terms are simply
    part of every request - so a glossary works where Cloud Translation v2 has none."""
    client = FakeHttpClient([gemini_answer("[1] Bu bir computer vision kitabi")])
    translator = make_gemini(client)
    glossary = EffectiveGlossary(
        entries=(entry("computer vision", "computer vision"),),
        glossary_hash="c" * 64,
    )
    binding = unwrap(await translator.prepare(glossary, run_id="r"))
    assert binding.strategy is GlossaryStrategy.PROMPT

    await translator.translate(["This is a computer vision book"], request())

    prompt = client.calls[0]["json"]["contents"][0]["parts"][0]["text"]
    assert "computer vision" in prompt and "Terminology" in prompt


async def test_gemini_does_not_pay_for_reasoning_by_default() -> None:
    """Output tokens are the expensive half and a translation needs no deliberation."""
    client = FakeHttpClient([gemini_answer("[1] a")])
    translator = make_gemini(client)

    await translator.translate(["a"], request())

    generation = client.calls[0]["json"]["generationConfig"]
    assert generation["thinkingConfig"] == {"thinkingBudget": 0}
    assert generation["temperature"] == 0.0  # a retry must not produce a different text


async def test_a_negative_thinking_budget_omits_the_field_for_models_that_reject_it() -> None:
    client = FakeHttpClient([gemini_answer("[1] a")])
    translator = make_gemini(client, gemini_thinking_budget=-1)

    await translator.translate(["a"], request())

    assert "thinkingConfig" not in client.calls[0]["json"]["generationConfig"]


async def test_a_quota_that_promises_no_recovery_is_job_fatal() -> None:
    """Pressure that clears by waiting must not stop a book; an exhausted allowance must.
    With no retry delay offered, the API has not promised recovery, so the job pauses and
    the fallback provider can take the rest of the book."""
    client = FakeHttpClient(
        [
            gemini_error(
                429,
                "RESOURCE_EXHAUSTED",
                "quota exceeded",
                details=[
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}
                        ],
                    }
                ],
            )
        ]
    )
    translator = make_gemini(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_QUOTA
    assert result.error.scope == ErrorScope.JOB_FATAL


async def test_gemini_per_minute_limit_is_retryable_and_honours_the_asked_delay() -> None:
    client = FakeHttpClient(
        [
            gemini_error(
                429,
                "RESOURCE_EXHAUSTED",
                "too fast",
                details=[
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"},
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": "GenerateRequestsPerMinutePerProject"}],
                    },
                ],
            )
        ]
    )
    translator = make_gemini(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_RATE_LIMITED
    assert result.error.scope == ErrorScope.CHUNK_RETRYABLE
    assert result.error.retry_after_s == 21.0


async def test_gemini_rejects_a_bad_key_without_printing_it() -> None:
    client = FakeHttpClient([gemini_error(401, "UNAUTHENTICATED", "API key not valid")])
    translator = make_gemini(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_AUTH
    assert GEMINI_KEY not in str(result.error)


async def test_gemini_400_is_chunk_fatal_not_an_endless_retry() -> None:
    client = FakeHttpClient([gemini_error(400, "INVALID_ARGUMENT", "unknown field")])
    translator = make_gemini(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_BAD_REQUEST
    assert result.error.scope == ErrorScope.CHUNK_FATAL


async def test_a_refused_text_fails_its_chunk_instead_of_retrying_forever() -> None:
    """A safety refusal is deterministic: the same text would be refused again."""
    client = FakeHttpClient([FakeHttpResponse(200, {"candidates": [{"finishReason": "SAFETY"}]})])
    translator = make_gemini(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.scope == ErrorScope.CHUNK_FATAL


async def test_a_truncated_answer_is_retryable() -> None:
    client = FakeHttpClient(
        [FakeHttpResponse(200, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {}}]})]
    )
    translator = make_gemini(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_EMPTY_RESPONSE
    assert result.error.scope == ErrorScope.CHUNK_RETRYABLE


async def test_gemini_without_a_key_is_a_user_error() -> None:
    made = GeminiTranslator.create(
        gemini_settings(None), client_factory=lambda: FakeHttpClient()
    )
    assert isinstance(made, Err)
    assert made.error.code == ErrorCode.PROVIDER_CONFIG
    assert "GEMINI_API_KEY" in made.error.message


async def test_an_empty_wallet_stops_the_job_instead_of_being_retried() -> None:
    """The live body, verbatim: HTTP 402 arrives with ``RESOURCE_EXHAUSTED`` too, so the
    429 branch would have treated depleted credits as a rate limit and backed off twenty
    times over something no amount of waiting fixes."""
    client = FakeHttpClient(
        [
            FakeHttpResponse(
                402,
                {
                    "error": {
                        "code": 402,
                        "message": "Your prepayment credits are depleted.",
                        "status": "RESOURCE_EXHAUSTED",
                    }
                },
            )
        ]
    )
    translator = make_gemini(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_QUOTA
    assert result.error.scope == ErrorScope.JOB_FATAL
    assert "credits" in result.error.message


async def test_a_per_day_quota_id_with_a_short_delay_is_still_just_a_rate_limit() -> None:
    """Captured verbatim from the free tier. The violation is named
    ``GenerateRequestsPerDayPerProjectPerModel-FreeTier`` - and it cleared within the
    minute, exactly as its own ``retryDelay: 45s`` said it would. Reading the name
    instead of the delay stopped a 120-unit comparison run dead on a speed bump."""
    client = FakeHttpClient(
        [
            gemini_error(
                429,
                "RESOURCE_EXHAUSTED",
                "You exceeded your current quota",
                details=[
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaMetric": (
                                    "generativelanguage.googleapis.com/"
                                    "generate_content_free_tier_requests"
                                ),
                                "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                "quotaValue": "20",
                            }
                        ],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45s"},
                ],
            )
        ]
    )
    translator = make_gemini(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_RATE_LIMITED
    assert result.error.scope == ErrorScope.CHUNK_RETRYABLE
    assert result.error.retry_after_s == 45.0


async def test_a_delay_too_long_to_wait_out_is_treated_as_an_exhausted_quota() -> None:
    client = FakeHttpClient(
        [
            gemini_error(
                429,
                "RESOURCE_EXHAUSTED",
                "come back tomorrow",
                details=[
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "36000s"}
                ],
            )
        ]
    )
    translator = make_gemini(client)

    result = await translator.translate(["a"], request())

    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.PROVIDER_QUOTA
    assert result.error.scope == ErrorScope.JOB_FATAL


async def test_the_model_that_answered_is_recorded_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A finished translation has to be able to say what produced it.

    Nothing recorded the model, and a 35-page output turned out to be a mix of three:
    the only reason that could be reconstructed was that two runs happened to hit
    rate-limit errors whose text named the model. ``modelVersion`` resolves the alias,
    so it answers the question the requested name cannot.
    """
    answers = [gemini_answer("[1] bir"), gemini_answer("[2] iki")]
    client = FakeHttpClient(answers)
    translator = make_gemini(client, gemini_model="gemini-flash-latest")

    with caplog.at_level(logging.INFO, logger="book_translator.translators.gemini"):
        await translator.translate(["one"], request())
        await translator.translate(["two"], request(chunk_id=2))

    lines = [r for r in caplog.records if getattr(r, "event", None) == "provider_model"]
    assert len(lines) == 1, "the served model is logged once, not once per batch"
    assert "gemini-3.8-flash" in lines[0].getMessage()  # what answered
    assert "gemini-flash-latest" in lines[0].getMessage()  # what was asked for


async def test_a_model_switch_mid_run_is_recorded_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two different answers must not be silently attributed to one model."""
    first = gemini_answer("[1] bir")
    second = gemini_answer("[1] iki")
    second.payload["modelVersion"] = "gemini-3.9-flash"
    translator = make_gemini(FakeHttpClient([first, second]))

    with caplog.at_level(logging.INFO, logger="book_translator.translators.gemini"):
        await translator.translate(["one"], request())
        await translator.translate(["two"], request(chunk_id=2))

    served = [
        r.getMessage() for r in caplog.records if getattr(r, "event", None) == "provider_model"
    ]
    assert len(served) == 2
    assert "gemini-3.8-flash" in served[0] and "gemini-3.9-flash" in served[1]
