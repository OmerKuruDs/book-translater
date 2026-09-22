"""Translator interface and registry (design doc 02, section 2.3).

Every provider returns ``Result`` values classified per section 5.3 and never
retries internally; retry state lives in the database and is driven by the
orchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Dict, List, Optional, Sequence, Tuple, Type

from ..config import Settings
from ..domain.models import EffectiveGlossary, GlossaryStrategy
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from .protect import ProtectMode

__all__ = [
    "BaseTranslator",
    "GlossaryBinding",
    "GlossaryCleanup",
    "GlossaryStrategy",
    "ProviderGlossary",
    "ProviderResponse",
    "RemovedGlossary",
    "TranslationRequest",
    "TranslatorCapabilities",
    "get_translator",
    "list_translators",
]


@dataclass(frozen=True)
class TranslatorCapabilities:
    supports_glossary: bool  # native provider glossary
    supports_batch: bool  # list of texts in one request
    max_chars_per_request: int  # sum of payload chars per request
    max_texts_per_request: int
    supports_context: bool  # read-only context field (LLM providers)
    supports_tag_protection: bool  # XML tag ignore (DeepL) vs sentinel tokens
    # A prompt-driven provider has no glossary endpoint but still applies the terms,
    # because they are written into the prompt. Without this the two cases look
    # identical from outside, and "no native glossary" reads as "no glossary".
    glossary_in_prompt: bool = False


@dataclass(frozen=True)
class GlossaryBinding:
    strategy: GlossaryStrategy
    provider_glossary_id: Optional[str]  # DeepL glossary id
    glossary_hash: str  # sha256 of normalized approved entries


@dataclass(frozen=True)
class TranslationRequest:
    job_id: str
    run_id: str
    chunk_id: int
    attempt: int
    source_lang: str = "EN"
    target_lang: str = "TR"
    glossary: Optional[GlossaryBinding] = None
    context: Optional[str] = None  # only used when supports_context
    protect_mode: ProtectMode = "xml"
    """How the payload was escaped by :mod:`translators.protect`.

    ``"xml"`` means ``&``, ``<`` and ``>`` in ``texts`` are already character references
    and the answer is expected to come back the same way - the provider must not resolve
    them, :func:`~translators.protect.restore` does that. A provider whose wire format
    escapes more than those three (Google's ``format=html`` answers ``&#39;``) uses this
    to decide how much of its own escaping to undo.
    """


@dataclass(frozen=True)
class ProviderGlossary:
    """One glossary stored on the provider side, as the cleanup sees it."""

    glossary_id: str
    name: str
    entries: int
    # The tool's own glossary hash, parsed back out of ``name``. ``None`` means the
    # glossary was *not* created by this tool - the cleanup never deletes those.
    tool_hash: Optional[str] = None


@dataclass(frozen=True)
class RemovedGlossary:
    name: str
    entries: int


@dataclass(frozen=True)
class GlossaryCleanup:
    """What ``cleanup_glossaries`` did; deletion is destructive, so it reports precisely."""

    provider: str
    supported: bool = True  # False: this provider keeps no glossaries on its side
    deleted: Tuple[RemovedGlossary, ...] = ()
    kept_current: Optional[str] = None  # the glossary of this run, never deleted
    kept_foreign: int = 0  # glossaries not created by this tool, left untouched
    failed: Tuple[str, ...] = ()  # "<name>: <reason>" per glossary that would not delete


@dataclass(frozen=True)
class ProviderResponse:
    texts: List[str]  # same length/order as input
    chars_billed: int
    latency_ms: int
    detected_source_lang: Optional[str] = None


class BaseTranslator(ABC):
    """One provider. ``translate`` performs exactly one round-trip per batch."""

    name: ClassVar[str]  # "deepl" | "local" | "llm:<vendor>"
    capabilities: ClassVar[TranslatorCapabilities]

    @classmethod
    @abstractmethod
    def create(cls, settings: Settings) -> Result[BaseTranslator]:
        """Fail-fast construction: key present, model files present; no network."""

    @abstractmethod
    async def prepare(self, glossary: EffectiveGlossary, run_id: str) -> Result[GlossaryBinding]:
        """Bind the effective glossary once per run and report the strategy used."""

    @abstractmethod
    async def translate(
        self, texts: Sequence[str], request: TranslationRequest
    ) -> Result[ProviderResponse]:
        """Translate ``texts`` (one chunk's segments); never retries internally."""

    # -- provider-side glossary administration (optional) --------------------- #
    #
    # Only providers that *store* glossaries implement these two; the default says
    # "not supported" so a provider without them needs no code at all.

    async def list_provider_glossaries(self) -> Result[List[ProviderGlossary]]:
        """Every glossary this account holds, tool-owned or not (default: unsupported)."""
        return self._no_glossary_store()

    async def delete_provider_glossary(self, glossary_id: str) -> Result[None]:
        """Delete one provider-side glossary by id (default: unsupported)."""
        return self._no_glossary_store()

    def _no_glossary_store(self) -> Err:
        return err(
            ErrorCode.PROVIDER_CONFIG,
            f"provider {self.name!r} keeps no glossaries on its side",
            ErrorScope.USER,
        )

    async def cleanup_glossaries(
        self, *, keep_hash: Optional[str] = None
    ) -> Result[GlossaryCleanup]:
        """Delete the glossaries *this tool* left on the provider, except the current one.

        Provider-agnostic policy, so every provider inherits the same safety rules:

        * a glossary whose ``tool_hash`` is ``None`` was created by the user themselves
          and is never touched - only :meth:`list_provider_glossaries` decides ownership;
        * the glossary matching ``keep_hash`` (the run being prepared) is kept, so a
          cleanup right before a translation does not delete the glossary it needs;
        * one failed deletion does not stop the others - it is reported instead.
        """
        if not self.capabilities.supports_glossary:
            return Ok(GlossaryCleanup(self.name, supported=False))
        listed = await self.list_provider_glossaries()
        if isinstance(listed, Err):
            return listed
        deleted: List[RemovedGlossary] = []
        failed: List[str] = []
        kept_current: Optional[str] = None
        kept_foreign = 0
        for item in listed.value:
            if not item.tool_hash:
                kept_foreign += 1
                continue
            if keep_hash is not None and keep_hash.startswith(item.tool_hash):
                kept_current = item.name
                continue
            removed = await self.delete_provider_glossary(item.glossary_id)
            if isinstance(removed, Err):
                failed.append(f"{item.name}: {removed.error.message}")
                continue
            deleted.append(RemovedGlossary(item.name, item.entries))
        return Ok(
            GlossaryCleanup(
                provider=self.name,
                supported=True,
                deleted=tuple(deleted),
                kept_current=kept_current,
                kept_foreign=kept_foreign,
                failed=tuple(failed),
            )
        )

    async def aclose(self) -> None:
        """Release provider resources (default: nothing)."""
        return None


def _registry() -> Dict[str, Type[BaseTranslator]]:
    # Imported lazily: the concrete modules import this one.
    from .deepl_translator import DeepLTranslator
    from .gemini import GeminiTranslator
    from .google_translate import GoogleTranslator
    from .local_nmt import LocalNMTTranslator

    return {
        DeepLTranslator.name: DeepLTranslator,
        GeminiTranslator.name: GeminiTranslator,
        GoogleTranslator.name: GoogleTranslator,
        LocalNMTTranslator.name: LocalNMTTranslator,
    }


def list_translators() -> List[str]:
    """Names accepted by ``--provider`` (read-only listing for the CLI)."""
    return sorted(_registry())


def get_translator(name: str, settings: Settings) -> Result[BaseTranslator]:
    """Instantiate the provider ``name`` via its ``create`` (fail-fast checks included)."""
    cls = _registry().get(name)
    if cls is None:
        return err(
            ErrorCode.PROVIDER_CONFIG,
            f"unknown provider {name!r}; available: {', '.join(list_translators())}",
            ErrorScope.USER,
        )
    return cls.create(settings)
