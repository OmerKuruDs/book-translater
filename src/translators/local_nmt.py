"""Offline NMT provider (CTranslate2 + SentencePiece), ``[local]`` extra.

The optional packages are probed with ``importlib`` at ``create`` time; this
module imports cleanly without them (NFR-09). The glossary is applied with the
``POST_REPLACE`` strategy (design doc 02, 7.5) through the pure function
:func:`apply_post_replace`.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import re
import time
from pathlib import Path
from typing import Any, Callable, ClassVar, List, Optional, Sequence, Tuple

from ..config import Settings
from ..domain.models import EffectiveGlossary, GlossaryStrategy
from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err
from .base import (
    BaseTranslator,
    GlossaryBinding,
    ProviderResponse,
    TranslationRequest,
    TranslatorCapabilities,
)

__all__ = ["LocalNMTTranslator", "apply_post_replace", "missing_local_packages"]

REQUIRED_PACKAGES: Tuple[str, ...] = ("ctranslate2", "sentencepiece")
REQUIRED_MODEL_FILES: Tuple[str, ...] = ("model.bin", "source.spm", "target.spm")

EngineFactory = Callable[[Path, str], Any]


def missing_local_packages() -> List[str]:
    """Names of the ``[local]`` packages that cannot be imported."""
    return [name for name in REQUIRED_PACKAGES if importlib.util.find_spec(name) is None]


def _download_hint(model_dir: Path, model: str) -> str:
    return (
        f"install the extra with `pip install \"book-translator[local]\"` and convert the model "
        f"with `ct2-transformers-converter --model Helsinki-NLP/{model} "
        f"--output_dir \"{model_dir}\" --copy_files source.spm target.spm`"
    )


# --------------------------------------------------------------------------- #
# 7.5 POST_REPLACE
# --------------------------------------------------------------------------- #


def _boundary_pattern(term: str) -> re.Pattern[str]:
    left = r"(?<!\w)" if term[:1].isalnum() or term[:1] == "_" else ""
    right = r"(?!\w)" if term[-1:].isalnum() or term[-1:] == "_" else ""
    return re.compile(left + re.escape(term) + right)


def apply_post_replace(
    text: str, glossary: EffectiveGlossary, source_text: Optional[str] = None
) -> Tuple[str, List[str]]:
    """Enforce glossary targets on a translated segment (design doc 02, 7.5).

    Entries are applied longest source first, word-boundary and case-sensitive: an
    untranslated ``source`` left in ``text`` is replaced with ``target``. When the
    original ``source_text`` is given, only entries whose source occurs in it are
    considered, and ``glossary_miss:<source>`` is reported for each such entry whose
    source and target are both absent from the output. Without ``source_text`` every
    entry is a replacement candidate and no miss can be diagnosed.
    """
    warnings: List[str] = []
    entries = sorted(
        (e for e in glossary.entries if e.source.strip() and e.target.strip()),
        key=lambda e: len(e.source),
        reverse=True,
    )
    if source_text is not None:
        entries = [e for e in entries if _boundary_pattern(e.source).search(source_text)]
    if not entries:
        return text, warnings
    targets = {e.source: e.target for e in entries}
    counts = {e.source: 0 for e in entries}
    # One pass over a longest-first alternation: a replacement is never re-scanned, so
    # "York" cannot match inside the target already written for "New York".
    combined = re.compile("|".join(f"(?:{_boundary_pattern(e.source).pattern})" for e in entries))

    def substitute(match: re.Match[str]) -> str:
        source = match.group(0)
        counts[source] += 1
        return targets[source]

    replaced = combined.sub(substitute, text)
    for entry in entries:
        if counts[entry.source] == 0 and entry.target not in replaced:
            warnings.append(f"glossary_miss:{entry.source}")
    return replaced, warnings


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class _CTranslate2Engine:
    """Thin wrapper: SentencePiece tokenize → CTranslate2 translate_batch → detokenize."""

    def __init__(self, model_dir: Path, device: str) -> None:
        ctranslate2 = importlib.import_module("ctranslate2")
        sentencepiece = importlib.import_module("sentencepiece")
        self._translator = ctranslate2.Translator(str(model_dir), device=device)
        processor = sentencepiece.SentencePieceProcessor
        self._source = processor(model_file=str(model_dir / "source.spm"))
        self._target = processor(model_file=str(model_dir / "target.spm"))

    def translate_batch(self, texts: Sequence[str]) -> List[str]:
        tokens = [list(self._source.encode(text, out_type=str)) + ["</s>"] for text in texts]
        results = self._translator.translate_batch(tokens, beam_size=4, max_batch_size=16)
        out: List[str] = []
        for result in results:
            hypothesis = [t for t in result.hypotheses[0] if t != "</s>"]
            out.append(str(self._target.decode(hypothesis)))
        return out


def _default_engine_factory(model_dir: Path, device: str) -> Any:
    return _CTranslate2Engine(model_dir, device)


class LocalNMTTranslator(BaseTranslator):
    name: ClassVar[str] = "local"
    capabilities: ClassVar[TranslatorCapabilities] = TranslatorCapabilities(
        supports_glossary=False,
        supports_batch=True,
        max_chars_per_request=4_000,
        max_texts_per_request=32,
        supports_context=False,
        supports_tag_protection=False,
    )
    engine_factory: ClassVar[EngineFactory] = staticmethod(_default_engine_factory)

    def __init__(self, engine: Any, model_dir: Path) -> None:
        self._engine = engine
        self._model_dir = model_dir
        self._glossary: Optional[EffectiveGlossary] = None

    @classmethod
    def create(
        cls, settings: Settings, *, engine_factory: Optional[EngineFactory] = None
    ) -> Result[BaseTranslator]:
        model_dir = settings.local_model_dir / settings.local_model
        hint = _download_hint(model_dir, settings.local_model)
        missing = missing_local_packages()
        if missing:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                f"local provider needs the packages {', '.join(missing)}; {hint}",
                ErrorScope.USER,
                context={"missing_packages": missing},
            )
        absent = [name for name in REQUIRED_MODEL_FILES if not (model_dir / name).is_file()]
        if absent:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                f"model files missing under {model_dir}: {', '.join(absent)}; {hint}",
                ErrorScope.USER,
                context={"model_dir": str(model_dir), "missing_files": absent},
            )
        factory = engine_factory if engine_factory is not None else cls.engine_factory
        try:
            engine = factory(model_dir, settings.local_device)
        except Exception as exc:  # noqa: BLE001 - model load failure is a config error (5.3)
            return err(
                ErrorCode.PROVIDER_CONFIG,
                f"could not load the local model from {model_dir}: {type(exc).__name__}",
                ErrorScope.USER,
                cause=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        return Ok(cls(engine, model_dir))

    async def prepare(self, glossary: EffectiveGlossary, run_id: str) -> Result[GlossaryBinding]:
        self._glossary = glossary
        return Ok(GlossaryBinding(GlossaryStrategy.POST_REPLACE, None, glossary.glossary_hash))

    async def translate(
        self, texts: Sequence[str], request: TranslationRequest
    ) -> Result[ProviderResponse]:
        started = time.perf_counter()
        if not texts:
            return Ok(ProviderResponse(texts=[], chars_billed=0, latency_ms=0))
        try:
            results = await asyncio.to_thread(self._engine.translate_batch, list(texts))
        except (MemoryError, RuntimeError, OSError) as exc:
            return err(
                ErrorCode.PROVIDER_TRANSIENT,
                f"local model inference failed: {type(exc).__name__}",
                ErrorScope.CHUNK_RETRYABLE,
                context={"chunk_id": request.chunk_id, "attempt": request.attempt},
                cause=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        except Exception as exc:  # noqa: BLE001 - never raise across the boundary
            return err(
                ErrorCode.INTERNAL,
                f"unexpected local model error: {type(exc).__name__}",
                ErrorScope.CHUNK_FATAL,
                context={"chunk_id": request.chunk_id, "attempt": request.attempt},
                cause=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
        items = list(results)
        if len(items) != len(texts):
            return err(
                ErrorCode.PROVIDER_EMPTY_RESPONSE,
                f"local model returned {len(items)} texts for {len(texts)} inputs",
                ErrorScope.CHUNK_RETRYABLE,
                context={"chunk_id": request.chunk_id, "attempt": request.attempt},
            )
        out: List[str] = []
        for source, translated in zip(texts, items, strict=True):
            if not isinstance(translated, str) or (source.strip() and not translated.strip()):
                return err(
                    ErrorCode.PROVIDER_EMPTY_RESPONSE,
                    "local model returned an empty translation",
                    ErrorScope.CHUNK_RETRYABLE,
                    context={"chunk_id": request.chunk_id, "attempt": request.attempt},
                )
            if self._glossary is not None:
                translated, _ = apply_post_replace(translated, self._glossary, source)
            out.append(translated)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return Ok(ProviderResponse(texts=out, chars_billed=0, latency_ms=latency_ms))
