"""Backoff, adaptive limiter and translator providers (design doc 02, 5.3, 5.4, 7.4, 7.5)."""

from __future__ import annotations

import asyncio
import dataclasses
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
from book_translator.domain.result import Err, ErrorCode, ErrorScope, Ok, Result, unwrap
from book_translator.pipeline.backoff import AdaptiveLimiter, BackoffPolicy, compute_delay
from book_translator.translators.base import (
    BaseTranslator,
    GlossaryBinding,
    TranslationRequest,
    get_translator,
    list_translators,
)
from book_translator.translators.deepl_translator import (
    DeepLTranslator,
    classify_deepl_exception,
    glossary_name_for,
)
from book_translator.translators.llm_base import MAX_PROMPT_TERMS, BaseLLMTranslator, build_prompt
from book_translator.translators.local_nmt import LocalNMTTranslator, apply_post_replace

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


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


class FakeDeepLClient:
    """Stand-in for ``deepl.Translator``; raises real SDK exception classes."""

    def __init__(self, raise_exc: Optional[BaseException] = None) -> None:
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []
        self.glossaries: List[FakeGlossaryInfo] = []
        self.created: List[Dict[str, Any]] = []
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
        return FakeGlossaryInfo("g-new", name, True)

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
    assert list_translators() == ["deepl", "local"]
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
    client = FakeDeepLClient()
    g = glossary(("Dragon", "Ejderha"), (" Sword ", "Kılıç"), ("Same", "Same"), ("Empty", " "))
    binding = unwrap(await make_deepl(client).prepare(g, "run"))
    assert binding.strategy == GlossaryStrategy.NATIVE
    assert binding.provider_glossary_id == "g-new"
    assert client.created == [
        {"name": glossary_name_for("h" * 64), "source": "EN", "target": "TR",
         "entries": {"Dragon": "Ejderha", "Sword": "Kılıç"}}
    ]


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
    from book_translator.translators.protect import protect, strip_control_chars

    dirty = "Matrix" + chr(0x14) + " 1" + chr(0x15) + " with " + chr(0) + "a gap" + chr(1)
    assert strip_control_chars(dirty) == "Matrix 1 with a gap"
    # tab, newline and carriage return are valid XML and must survive
    plain = "a" + chr(9) + "b" + chr(10) + "c" + chr(13) + "d"
    assert strip_control_chars(plain) == plain
    for mode in ("xml", "sentinel"):
        out, _mapping = protect(dirty, mode)
        assert not any(ord(c) < 0x20 and c not in plain for c in out)


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
