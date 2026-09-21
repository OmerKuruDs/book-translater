"""Automatic provider fallback when the primary runs out of quota.

User decision: *switch automatically, leave a warning*. A book run that dies half way
through because the DeepL character quota ran out is the worst outcome - the paid work
already done stays in the database, but nothing new gets translated until someone comes
back to the terminal. So the primary provider is wrapped:

    FallbackTranslator(primary=DeepLTranslator, secondary=GoogleTranslator)

``translate`` sends to the primary until it answers ``PROVIDER_QUOTA`` once; from then on
every request of this run goes to the secondary. The orchestrator's translation loop is
untouched - it still sees one ``BaseTranslator`` - and the switch is a property of the
wrapper, which makes it trivial to unit test.

Four rules make the wrapper safe:

1. **Only a quota error switches.** Auth, bad request, empty response, rate limit and
   network errors keep their existing meaning (stop, or back off and retry the chunk). A
   rate limit that switched providers would abandon DeepL over a two-second hiccup.
2. **The switch is permanent for the run.** Retrying the primary per unit would pay for a
   failed round trip on every single one of them. A new run starts on the primary again,
   which is what the user wants after topping the quota up.
3. **Every unit translated by the secondary carries warnings.** ``provider_fallback:a->b``
   always, plus ``glossary_unavailable:<secondary>`` when the primary had a native
   glossary bound and the secondary cannot take one. Silent degradation of a bound
   glossary is exactly the kind of thing that only shows up in the finished book.
4. **The response says who produced it.** :class:`FallbackResponse` carries the provider
   name so the orchestrator records the *real* provider per unit in
   ``chunks.provider`` / ``overlay_blocks.provider`` instead of one name for the whole run.

The wrapper is not in the provider registry: it is composed by the orchestrator from two
registered providers (``--provider`` and ``--fallback-provider``).
"""

from __future__ import annotations

import dataclasses
import logging
from contextlib import suppress
from typing import ClassVar, List, Optional, Sequence, Tuple

from ..config import Settings
from ..domain.models import EffectiveGlossary, GlossaryStrategy
from ..domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, Result, err
from .base import (
    BaseTranslator,
    GlossaryBinding,
    ProviderResponse,
    TranslationRequest,
    TranslatorCapabilities,
)

__all__ = ["FallbackResponse", "FallbackTranslator", "merge_capabilities"]

log = logging.getLogger("book_translator.translators.fallback")


@dataclasses.dataclass(frozen=True)
class FallbackResponse(ProviderResponse):
    """A :class:`ProviderResponse` that also names the provider that produced it.

    ``ProviderResponse`` is the interface every provider returns and stays as it is; this
    subclass only adds the two fields the wrapper knows about. Callers that do not care
    (every adapter in ``pipeline/units.py``) treat it as a plain response.
    """

    provider: str = ""
    warnings: Tuple[str, ...] = ()


def merge_capabilities(
    primary: TranslatorCapabilities, secondary: TranslatorCapabilities
) -> TranslatorCapabilities:
    """The capabilities a payload must satisfy to be sendable to *either* provider.

    Batch sizes are the minimum of the two and the optional features are the conjunction,
    because a unit that was built for the primary may end up at the secondary mid-run and
    must not have to be re-chunked or re-protected at that moment.

    ``supports_glossary`` is the *primary's*: the glossary is bound once, before any
    translation, and it is bound on the primary. What happens to it after a switch is
    reported as a warning, not as a capability.
    """
    return TranslatorCapabilities(
        supports_glossary=primary.supports_glossary,
        supports_batch=primary.supports_batch and secondary.supports_batch,
        max_chars_per_request=min(
            primary.max_chars_per_request, secondary.max_chars_per_request
        ),
        max_texts_per_request=min(
            primary.max_texts_per_request, secondary.max_texts_per_request
        ),
        supports_context=primary.supports_context and secondary.supports_context,
        supports_tag_protection=(
            primary.supports_tag_protection and secondary.supports_tag_protection
        ),
    )


class FallbackTranslator(BaseTranslator):
    """``primary`` until it reports ``PROVIDER_QUOTA``, then ``secondary`` for good."""

    name: ClassVar[str] = "fallback"

    def __init__(self, primary: BaseTranslator, secondary: BaseTranslator) -> None:
        self._primary = primary
        self._secondary = secondary
        self._switched = False
        self._warnings: Tuple[str, ...] = ()
        self._glossary_bound = False
        self._secondary_binding: Optional[GlossaryBinding] = None
        self.quota_error: Optional[AppError] = None
        # ``capabilities`` is a ClassVar on the interface because every other provider has
        # one fixed set; this one depends on the pair, so it is shadowed per instance.
        self.capabilities = merge_capabilities(  # type: ignore[misc]
            primary.capabilities, secondary.capabilities
        )

    # -- construction --------------------------------------------------------- #

    @classmethod
    def create(cls, settings: Settings) -> Result[BaseTranslator]:
        """Not reachable through ``--provider``: the wrapper needs two providers."""
        return err(
            ErrorCode.PROVIDER_CONFIG,
            "the fallback wrapper is composed from two providers; select them with "
            "--provider and --fallback-provider",
            ErrorScope.USER,
        )

    # -- introspection (used by the orchestrator for the provider column) ------ #

    @property
    def primary_name(self) -> str:
        return self._primary.name

    @property
    def secondary_name(self) -> str:
        return self._secondary.name

    @property
    def switched(self) -> bool:
        return self._switched

    @property
    def active_provider(self) -> str:
        """Name of the provider a request would go to right now."""
        return self._secondary.name if self._switched else self._primary.name

    # -- glossary ------------------------------------------------------------- #

    async def prepare(self, glossary: EffectiveGlossary, run_id: str) -> Result[GlossaryBinding]:
        """Bind on both providers up front so a mid-run switch needs no extra round trip.

        The binding returned (and recorded on the job) is the primary's: that is the one
        the run starts with. A secondary that cannot bind fails here, before anything is
        sent and paid for.
        """
        prepared = await self._primary.prepare(glossary, run_id)
        if isinstance(prepared, Err):
            return prepared
        binding = prepared.value
        second = await self._secondary.prepare(glossary, run_id)
        if isinstance(second, Err):
            return second
        self._secondary_binding = second.value
        self._glossary_bound = binding.strategy is not GlossaryStrategy.NONE
        return Ok(binding)

    # -- translation ---------------------------------------------------------- #

    def _switch(self, error: AppError) -> None:
        self._switched = True
        self.quota_error = error
        warnings: List[str] = [f"provider_fallback:{self._primary.name}->{self._secondary.name}"]
        if self._glossary_bound and not self._secondary.capabilities.supports_glossary:
            warnings.append(f"glossary_unavailable:{self._secondary.name}")
        self._warnings = tuple(warnings)
        log.warning(
            "%s reported an exhausted quota; every remaining unit of this run goes to %s "
            "(%s)",
            self._primary.name,
            self._secondary.name,
            error.message,
            extra={"event": "provider_fallback"},
        )
        if f"glossary_unavailable:{self._secondary.name}" in self._warnings:
            log.warning(
                "the native glossary bound on %s does not carry over to %s; the terms are "
                "only post-checked from here on",
                self._primary.name,
                self._secondary.name,
                extra={"event": "glossary_unavailable"},
            )

    def _secondary_request(self, request: TranslationRequest) -> TranslationRequest:
        """The same request, minus what the secondary cannot honour."""
        glossary = (
            self._secondary_binding if self._secondary.capabilities.supports_glossary else None
        )
        context = request.context if self._secondary.capabilities.supports_context else None
        return dataclasses.replace(request, glossary=glossary, context=context)

    @staticmethod
    def _tag(
        result: Result[ProviderResponse], provider: str, warnings: Tuple[str, ...]
    ) -> Result[ProviderResponse]:
        if isinstance(result, Err):
            return result
        source = result.value
        tagged: ProviderResponse = FallbackResponse(
            texts=source.texts,
            chars_billed=source.chars_billed,
            latency_ms=source.latency_ms,
            detected_source_lang=source.detected_source_lang,
            provider=provider,
            warnings=warnings,
        )
        return Ok(tagged)

    async def translate(
        self, texts: Sequence[str], request: TranslationRequest
    ) -> Result[ProviderResponse]:
        if not self._switched:
            result = await self._primary.translate(texts, request)
            if not (
                isinstance(result, Err) and result.error.code is ErrorCode.PROVIDER_QUOTA
            ):
                return self._tag(result, self._primary.name, ())
            self._switch(result.error)
        second = await self._secondary.translate(texts, self._secondary_request(request))
        return self._tag(second, self._secondary.name, self._warnings)

    async def aclose(self) -> None:
        for translator in (self._primary, self._secondary):
            with suppress(Exception):
                await translator.aclose()
