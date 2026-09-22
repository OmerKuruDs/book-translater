"""Base class for prompt-driven (LLM) providers (design doc 02, 2.3 and 7.5).

No vendor implementation ships in v1; a vendor subclass implements
``create`` and ``complete``. The glossary is injected into the prompt
(strategy ``PROMPT``) and placeholders use sentinel tokens ``⟦n⟧``.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from typing import ClassVar, List, Optional, Sequence

from ..domain.models import EffectiveGlossary, GlossaryStrategy
from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err
from .base import (
    BaseTranslator,
    GlossaryBinding,
    ProviderResponse,
    TranslationRequest,
    TranslatorCapabilities,
)

__all__ = ["BaseLLMTranslator", "MAX_PROMPT_TERMS", "build_prompt"]

MAX_PROMPT_TERMS = 200

_INSTRUCTIONS = (
    "You are a professional literary translator. Translate the following English text "
    "into Turkish.\n"
    "Rules:\n"
    "- Return only the translation, nothing else: no notes, no quotes, no explanations.\n"
    "- Keep every placeholder token of the form ⟦n⟧ (for example ⟦1⟧, ⟦2⟧) exactly as it "
    "is, in the same position relative to the surrounding words; never translate, drop, "
    "duplicate or renumber it.\n"
    "- Keep Markdown inline formatting (**bold**, *italic*) on the corresponding words.\n"
    "- Do not merge or split lines; translate each input text on its own."
)


def build_prompt(texts: Sequence[str], glossary: EffectiveGlossary) -> str:
    """Instructions + terminology block limited to the entries that occur in ``texts``."""
    joined = "\n".join(texts)
    # Case-folded, because this check decides whether a term is *shown* to the model
    # at all. Matching exactly dropped every term that opened a sentence or a heading:
    # measured live, of three glossary terms only the one sitting mid-sentence reached
    # the prompt, and only that one survived. The pair is still listed in its glossary
    # spelling, which is the form the model is asked to produce.
    haystack = joined.casefold()
    terms: List[str] = []
    for entry in glossary.entries:  # already longest-source-first
        if entry.source and entry.target and entry.source.casefold() in haystack:
            terms.append(f"{entry.source} ⇒ {entry.target}")
            if len(terms) >= MAX_PROMPT_TERMS:
                break
    parts = [_INSTRUCTIONS]
    if terms:
        parts.append(
            "Terminology (use these Turkish terms exactly, inflected as Turkish grammar "
            "requires):\n" + "\n".join(terms)
        )
    numbered = "\n".join(f"[{i}] {text}" for i, text in enumerate(texts, start=1))
    parts.append(
        "Text to translate (one item per line, keep the [n] labels at the start of each line):\n"
        + numbered
    )
    return "\n\n".join(parts)


def _parse_numbered(response: str, count: int) -> Optional[List[str]]:
    """Read ``[n] text`` lines back; ``None`` when a label is missing or out of order."""
    lines = [line for line in response.strip().splitlines() if line.strip()]
    if count == 1 and len(lines) == 1 and not lines[0].startswith("[1]"):
        return [lines[0].strip()]
    out: List[str] = []
    for line in lines:
        label = f"[{len(out) + 1}]"
        if line.startswith(label):
            out.append(line[len(label) :].strip())
        elif out:
            out[-1] = out[-1] + "\n" + line.strip()
        else:
            return None
    return out if len(out) == count else None


class BaseLLMTranslator(BaseTranslator):
    """Prompt-based provider; subclasses implement ``create`` and ``complete``."""

    name: ClassVar[str] = "llm"
    capabilities: ClassVar[TranslatorCapabilities] = TranslatorCapabilities(
        supports_glossary=False,
        supports_batch=True,
        max_chars_per_request=6_000,
        max_texts_per_request=40,
        supports_context=True,
        supports_tag_protection=False,
        glossary_in_prompt=True,
    )

    def __init__(self) -> None:
        self._glossary: Optional[EffectiveGlossary] = None

    @abstractmethod
    async def complete(self, prompt: str) -> Result[str]:
        """One completion round-trip; errors are returned classified, never raised."""

    async def prepare(self, glossary: EffectiveGlossary, run_id: str) -> Result[GlossaryBinding]:
        self._glossary = glossary
        strategy = GlossaryStrategy.PROMPT if glossary.entries else GlossaryStrategy.NONE
        return Ok(GlossaryBinding(strategy, None, glossary.glossary_hash))

    def prompt_for(self, texts: Sequence[str]) -> str:
        glossary = self._glossary or EffectiveGlossary(entries=(), glossary_hash="")
        return build_prompt(texts, glossary)

    async def translate(
        self, texts: Sequence[str], request: TranslationRequest
    ) -> Result[ProviderResponse]:
        started = time.perf_counter()
        if not texts:
            return Ok(ProviderResponse(texts=[], chars_billed=0, latency_ms=0))
        prompt = self.prompt_for(texts)
        if request.context and self.capabilities.supports_context:
            prompt = f"Context (do not translate):\n{request.context}\n\n{prompt}"
        completion = await self.complete(prompt)
        if isinstance(completion, Ok):
            parsed = _parse_numbered(completion.value, len(texts))
            pairs = zip(texts, parsed or [], strict=False)
            if parsed is None or any(s.strip() and not t.strip() for s, t in pairs):
                return err(
                    ErrorCode.PROVIDER_EMPTY_RESPONSE,
                    "LLM response could not be mapped back onto the input texts",
                    ErrorScope.CHUNK_RETRYABLE,
                    context={"chunk_id": request.chunk_id, "attempt": request.attempt},
                )
            latency_ms = int((time.perf_counter() - started) * 1000)
            return Ok(
                ProviderResponse(texts=parsed, chars_billed=len(prompt), latency_ms=latency_ms)
            )
        return completion
