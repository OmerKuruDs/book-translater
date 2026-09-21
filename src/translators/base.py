"""Translator interface and registry (design doc 02, section 2.3).

Every provider returns ``Result`` values classified per section 5.3 and never
retries internally; retry state lives in the database and is driven by the
orchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Dict, List, Optional, Sequence, Type

from ..config import Settings
from ..domain.models import EffectiveGlossary, GlossaryStrategy
from ..domain.result import ErrorCode, ErrorScope, Result, err

__all__ = [
    "BaseTranslator",
    "GlossaryBinding",
    "GlossaryStrategy",
    "ProviderResponse",
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

    async def aclose(self) -> None:
        """Release provider resources (default: nothing)."""
        return None


def _registry() -> Dict[str, Type[BaseTranslator]]:
    # Imported lazily: the concrete modules import this one.
    from .deepl_translator import DeepLTranslator
    from .local_nmt import LocalNMTTranslator

    return {DeepLTranslator.name: DeepLTranslator, LocalNMTTranslator.name: LocalNMTTranslator}


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
