"""Translation units through the v1 runner: repository/adapter Protocols (design doc 06, §3.6.2).

The runner (``orchestrator._run_job`` / ``_worker``) is parameterised over a
:class:`UnitAdapter` that knows how to claim units, turn a batch of units into one
provider request and turn the response back into one ``TranslationResult`` per unit.

* :class:`ChunkUnitAdapter` reproduces the v1 reflow behaviour exactly: one chunk per
  batch, segments + placeholder protection, the empty/truncation counter rule, lenient
  restore on the second occurrence, ``reassemble`` and the glossary/structure check.
* :class:`OverlayUnitAdapter` claims a page-oriented batch (``claim_pending_batch``),
  sends N unit texts in one request with a page-text context window, and finishes 1:1
  with the same counter rule, glossary check and review flag per unit (E-52).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    TypeVar,
)

from ..domain.models import (
    Chunk,
    EffectiveGlossary,
    GlossaryStrategy,
    StatusCounts,
    TranslationResult,
)
from ..domain.overlay import OverlayBlock, OverlayPage
from ..domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, Result, err
from ..translators.base import ProviderResponse
from ..translators.protect import ProtectMode, protect, restore, restore_lenient
from .glossary_check import check_chunk
from .overlay_batch import (
    DEFAULT_CONTEXT_CHARS,
    DEFAULT_MIN_CHARS,
    apportion,
    batch_pages,
    context_window,
    max_units_for,
)
from .segmenter import Segmented, reassemble, segment

__all__ = [
    "ChunkUnitAdapter",
    "FinishContext",
    "OverlayUnitAdapter",
    "OverlayUnitRepository",
    "UnitAdapter",
    "UnitBatch",
    "UnitRepository",
    "empty_error",
    "is_empty",
    "is_truncated",
]

U = TypeVar("U")

EMPTY_PREFIX = ErrorCode.PROVIDER_EMPTY_RESPONSE.value
TRUNCATION_RATIO = 0.2
TRUNCATION_MIN_CHARS = 40
EMPTY_LENIENT_AT = 2  # second occurrence -> review-flag path
EMPTY_FAIL_AT = 3  # third occurrence of an unsalvageable empty response -> FAILED


def is_empty(source: str, translated: str) -> bool:
    return bool(source.strip()) and not translated.strip()


def is_truncated(source: str, translated: str) -> bool:
    return len(source) > TRUNCATION_MIN_CHARS and len(translated) < TRUNCATION_RATIO * len(source)


def empty_error(unit_id: int, attempt: int, detail: str) -> Err:
    return err(
        ErrorCode.PROVIDER_EMPTY_RESPONSE,
        f"{EMPTY_PREFIX}: {detail}",
        ErrorScope.CHUNK_RETRYABLE,
        context={"chunk_id": unit_id, "attempt": attempt},
    )


# --------------------------------------------------------------------------- #
# Protocols
# --------------------------------------------------------------------------- #


class UnitRepository(Protocol[U]):
    """The v1 ``ChunkRepository`` surface with ``U`` instead of ``Chunk``.

    Satisfied structurally by ``database.repository.ChunkRepository`` (``U = Chunk``)
    and ``OverlayBlockRepository`` (``U = OverlayBlock``).
    """

    run_id: str
    glossary_hash: Optional[str]

    def requeue_processing(self, job_id: str) -> Result[int]: ...

    def claim_pending(
        self, job_id: str, limit: int, now: datetime, run_id: str
    ) -> Result[List[U]]: ...

    def complete(self, job_id: str, unit_id: int, result: TranslationResult) -> Result[bool]: ...

    def reschedule(
        self, job_id: str, unit_id: int, error: AppError, next_attempt_at: datetime
    ) -> Result[bool]: ...

    def fail(self, job_id: str, unit_id: int, error: AppError) -> Result[bool]: ...

    def release(self, job_id: str, unit_ids: Sequence[int]) -> Result[int]: ...

    def reset_failed(self, job_id: str) -> Result[int]: ...

    def counts(self, job_id: str, now: Optional[datetime] = None) -> Result[StatusCounts]: ...

    def earliest_next_attempt(self, job_id: str) -> Result[Optional[datetime]]: ...

    def iter_ordered(self, job_id: str, batch_size: int = 200) -> Iterator[U]: ...

    def totals(self, job_id: str) -> Result[Any]: ...

    def failed_ids(self, job_id: str) -> Result[List[int]]: ...

    def review_ids(self, job_id: str) -> Result[List[int]]: ...

    def count_completed_with_other_glossary(
        self, job_id: str, glossary_hash: str
    ) -> Result[int]: ...

    def count_completed_by_run(self, run_id: str) -> Result[Tuple[int, int]]: ...


class OverlayUnitRepository(UnitRepository[OverlayBlock], Protocol):
    """``UnitRepository[OverlayBlock]`` plus the overlay-specific reads (doc 07, §9.1)."""

    def claim_pending_batch(
        self, job_id: str, max_units: int, min_chars: int, now: datetime, run_id: str
    ) -> Result[List[OverlayBlock]]: ...

    def page_texts(self, job_id: str, pages: Sequence[int]) -> Result[Dict[int, List[str]]]: ...

    def page_progress(self, job_id: str) -> Result[Tuple[int, int]]: ...

    def iter_page(self, job_id: str, page: int) -> Iterator[OverlayBlock]: ...

    def pages(self, job_id: str) -> Result[List[OverlayPage]]: ...


@dataclass(frozen=True)
class UnitBatch:
    """One provider request: the protected payload texts of one or more units."""

    unit_ids: Tuple[int, ...]
    texts: Tuple[str, ...]  # protected payload texts (what the provider receives)
    context: Optional[str]  # read-only context (only when the provider supports it)
    chars: int  # payload chars (context excluded, NFR-23)
    mappings: Tuple[Dict[str, str], ...]  # placeholder map per text
    counts: Tuple[int, ...]  # texts contributed by each unit (aligned with unit_ids)
    sources: Tuple[str, ...]  # source text per unit
    segmented: Tuple[Optional[Segmented], ...]  # per unit; reflow only


@dataclass(frozen=True)
class FinishContext:
    """What the adapter needs to turn a provider response into results."""

    provider: str
    strategy: GlossaryStrategy
    glossary: EffectiveGlossary
    strict_glossary: bool
    latency_ms: int
    attempt: int
    empty_occurrences: Mapping[int, int]  # unit id -> empty responses seen before this one


class UnitAdapter(Protocol[U]):
    """How the runner treats one unit type (design doc 06, §3.6.2)."""

    name: str  # "reflow" | "overlay"

    @property
    def repo(self) -> UnitRepository[U]: ...

    def unit_id(self, unit: U) -> int: ...

    def is_translatable(self, unit: U) -> bool: ...

    def source_text(self, unit: U) -> str: ...

    def retry_count(self, unit: U) -> int: ...

    def last_error(self, unit: U) -> Optional[str]: ...

    def claim(
        self, job_id: str, permits: int, unit_cap: Optional[int], now: datetime, run_id: str
    ) -> Result[List[U]]: ...

    def group(self, units: Sequence[U]) -> List[List[U]]: ...

    def build_request(self, units: Sequence[U], job_id: str) -> Result[UnitBatch]: ...

    def finish(
        self, batch: UnitBatch, response: ProviderResponse, ctx: FinishContext
    ) -> List[Result[TranslationResult]]: ...

    def dry_run_chars(self, unit: U) -> int: ...


# --------------------------------------------------------------------------- #
# Shared post-processing (the v1 rules, applied per unit)
# --------------------------------------------------------------------------- #


def _verbatim(
    unit_id: int, source: str, provider: str, warnings: Tuple[str, ...]
) -> TranslationResult:
    return TranslationResult(
        chunk_id=unit_id,
        translated_text=source,
        provider=provider,
        chars_sent=0,
        chars_billed=0,
        latency_ms=0,
        attempts=0,
        glossary_strategy=GlossaryStrategy.NONE,
        warnings=warnings,
        review_flag=False,
    )


def _restore_texts(
    unit_id: int,
    attempt: int,
    protected: Sequence[str],
    texts: Sequence[str],
    mappings: Sequence[Dict[str, str]],
    occurrence: int,
) -> Tuple[Optional[Err], List[str], List[str], bool]:
    """v1 empty/truncation/placeholder rule for the texts of one unit.

    Returns ``(error, restored_texts, warnings, review)``: first occurrence of an issue ->
    ``error`` (retryable ``PROVIDER_EMPTY_RESPONSE``); second occurrence -> lenient
    restore, warnings and ``review=True``; no issue -> restored texts.
    """
    issues: List[str] = []
    for index, (source, translated) in enumerate(zip(protected, texts, strict=True)):
        if is_empty(source, translated):
            issues.append(f"empty_response:{index}")
        elif is_truncated(source, translated):
            issues.append(f"truncated_response:{index}")
    restored: List[str] = []
    placeholder_issue = False
    for translated, mapping in zip(texts, mappings, strict=True):
        fixed = restore(translated, mapping)
        if isinstance(fixed, Ok):
            restored.append(fixed.value)
        else:
            placeholder_issue = True
            restored.append(translated)
    if not issues and not placeholder_issue:
        return None, restored, [], False
    if occurrence < EMPTY_LENIENT_AT:
        detail = "; ".join(issues) if issues else "placeholders altered by the provider"
        return empty_error(unit_id, attempt, detail), [], [], False
    warnings: List[str] = []
    restored = []
    for index, (source, translated, mapping) in enumerate(
        zip(protected, texts, mappings, strict=True)
    ):
        if is_empty(source, translated):
            translated = source
            warnings.append(f"empty_response:{index}:untranslated")
        elif is_truncated(source, translated):
            warnings.append(f"truncated_response:{index}")
        text, lenient_warnings = restore_lenient(translated, mapping)
        warnings.extend(lenient_warnings)
        restored.append(text)
    return None, restored, warnings, True


def _check(
    source: str, translated: str, ctx: FinishContext, warnings: List[str]
) -> bool:
    check_warnings, check_review = check_chunk(
        source, translated, ctx.glossary, strict=ctx.strict_glossary
    )
    warnings.extend(check_warnings)
    return check_review


# --------------------------------------------------------------------------- #
# Reflow adapter (v1 behaviour, unchanged results)
# --------------------------------------------------------------------------- #


class ChunkUnitAdapter:
    """One chunk per batch; segments + placeholder protection; v1 empty rule."""

    name = "reflow"

    def __init__(
        self, repo: UnitRepository[Chunk], mode: ProtectMode, supports_context: bool
    ) -> None:
        self._repo = repo
        self.mode = mode
        self.supports_context = supports_context

    @property
    def repo(self) -> UnitRepository[Chunk]:
        return self._repo

    def unit_id(self, unit: Chunk) -> int:
        return unit.chunk_id

    def is_translatable(self, unit: Chunk) -> bool:
        return unit.translatable

    def source_text(self, unit: Chunk) -> str:
        return unit.source_text

    def retry_count(self, unit: Chunk) -> int:
        return unit.retry_count

    def last_error(self, unit: Chunk) -> Optional[str]:
        return unit.last_error

    def claim(
        self, job_id: str, permits: int, unit_cap: Optional[int], now: datetime, run_id: str
    ) -> Result[List[Chunk]]:
        limit = permits if unit_cap is None else min(permits, unit_cap)
        if limit <= 0:
            return Ok([])
        return self._repo.claim_pending(job_id, limit, now, run_id)

    def group(self, units: Sequence[Chunk]) -> List[List[Chunk]]:
        return [[unit] for unit in units]

    def build_request(self, units: Sequence[Chunk], job_id: str) -> Result[UnitBatch]:
        (chunk,) = tuple(units)
        segmented = segment(chunk.source_text)
        protected: List[str] = []
        mappings: List[Dict[str, str]] = []
        for seg in segmented.segments:
            text, mapping = protect(seg.text, self.mode)
            protected.append(text)
            mappings.append(mapping)
        context = getattr(segmented, "context", None) if self.supports_context else None
        return Ok(
            UnitBatch(
                unit_ids=(chunk.chunk_id,),
                texts=tuple(protected),
                context=context,
                chars=sum(len(t) for t in protected),
                mappings=tuple(mappings),
                counts=(len(protected),),
                sources=(chunk.source_text,),
                segmented=(segmented,),
            )
        )

    def finish(
        self, batch: UnitBatch, response: ProviderResponse, ctx: FinishContext
    ) -> List[Result[TranslationResult]]:
        (unit_id,) = batch.unit_ids
        (source,) = batch.sources
        segmented = batch.segmented[0]
        if not batch.texts or segmented is None:
            return [Ok(_verbatim(unit_id, source, ctx.provider, ("no_segments",)))]
        texts = list(response.texts)
        if len(texts) != len(batch.texts):
            return [
                empty_error(
                    unit_id,
                    ctx.attempt,
                    f"provider returned {len(texts)} texts for {len(batch.texts)} segments",
                )
            ]
        occurrence = ctx.empty_occurrences.get(unit_id, 0) + 1
        error, restored, warnings, review = _restore_texts(
            unit_id, ctx.attempt, batch.texts, texts, batch.mappings, occurrence
        )
        if error is not None:
            return [error]
        translated_text = reassemble(segmented, restored)
        check_review = _check(source, translated_text, ctx, warnings)
        return [
            Ok(
                TranslationResult(
                    chunk_id=unit_id,
                    translated_text=translated_text,
                    provider=ctx.provider,
                    chars_sent=batch.chars,
                    chars_billed=response.chars_billed,
                    latency_ms=ctx.latency_ms,
                    attempts=ctx.attempt,
                    glossary_strategy=ctx.strategy,
                    warnings=tuple(warnings),
                    review_flag=review or check_review,
                )
            )
        ]

    def dry_run_chars(self, unit: Chunk) -> int:
        segments = segment(unit.source_text).segments
        return sum(len(protect(seg.text, self.mode)[0]) for seg in segments)


# --------------------------------------------------------------------------- #
# Overlay adapter
# --------------------------------------------------------------------------- #


class OverlayUnitAdapter:
    """N units -> N texts + page context; 1:1 finish with per-unit checks (§4.6)."""

    name = "overlay"

    def __init__(
        self,
        repo: OverlayUnitRepository,
        mode: ProtectMode,
        *,
        supports_context: bool,
        max_texts_per_request: int,
        min_chars: int = DEFAULT_MIN_CHARS,
        context_chars: int = DEFAULT_CONTEXT_CHARS,
    ) -> None:
        self._repo = repo
        self.mode = mode
        self.supports_context = supports_context
        self.max_texts_per_request = max_texts_per_request
        self.min_chars = min_chars
        self.context_chars = context_chars

    @property
    def repo(self) -> UnitRepository[OverlayBlock]:
        return self._repo

    def unit_id(self, unit: OverlayBlock) -> int:
        return unit.unit_id

    def is_translatable(self, unit: OverlayBlock) -> bool:
        return unit.translate

    def source_text(self, unit: OverlayBlock) -> str:
        return unit.source_text

    def retry_count(self, unit: OverlayBlock) -> int:
        return unit.retry_count

    def last_error(self, unit: OverlayBlock) -> Optional[str]:
        return unit.last_error

    def claim(
        self, job_id: str, permits: int, unit_cap: Optional[int], now: datetime, run_id: str
    ) -> Result[List[OverlayBlock]]:
        """One batch per free permit (the runner loops while permits are free)."""
        if permits <= 0:
            return Ok([])
        max_units = max_units_for(self.max_texts_per_request, unit_cap)
        if max_units <= 0:
            return Ok([])
        return self._repo.claim_pending_batch(job_id, max_units, self.min_chars, now, run_id)

    def group(self, units: Sequence[OverlayBlock]) -> List[List[OverlayBlock]]:
        return [list(units)] if units else []

    def build_request(self, units: Sequence[OverlayBlock], job_id: str) -> Result[UnitBatch]:
        protected: List[str] = []
        mappings: List[Dict[str, str]] = []
        for unit in units:
            text, mapping = protect(unit.source_text, self.mode)
            protected.append(text)
            mappings.append(mapping)
        context: Optional[str] = None
        if self.supports_context and units:
            pages = batch_pages([u.page for u in units])
            texts = self._repo.page_texts(job_id, pages)
            if isinstance(texts, Err):
                return texts
            context = context_window(
                texts.value, pages, [u.source_text for u in units], self.context_chars
            ) or None
        return Ok(
            UnitBatch(
                unit_ids=tuple(u.unit_id for u in units),
                texts=tuple(protected),
                context=context,
                chars=sum(len(t) for t in protected),
                mappings=tuple(mappings),
                counts=tuple(1 for _ in units),
                sources=tuple(u.source_text for u in units),
                segmented=tuple(None for _ in units),
            )
        )

    def finish(
        self, batch: UnitBatch, response: ProviderResponse, ctx: FinishContext
    ) -> List[Result[TranslationResult]]:
        texts = list(response.texts)
        if len(texts) != len(batch.texts):
            detail = f"provider returned {len(texts)} texts for {len(batch.texts)} units"
            return [empty_error(unit_id, ctx.attempt, detail) for unit_id in batch.unit_ids]
        billed = apportion(response.chars_billed, [len(t) for t in batch.texts])
        results: List[Result[TranslationResult]] = []
        for index, unit_id in enumerate(batch.unit_ids):
            source = batch.sources[index]
            occurrence = ctx.empty_occurrences.get(unit_id, 0) + 1
            error, restored, warnings, review = _restore_texts(
                unit_id,
                ctx.attempt,
                [batch.texts[index]],
                [texts[index]],
                [batch.mappings[index]],
                occurrence,
            )
            if error is not None:
                results.append(error)
                continue
            translated = restored[0]
            if review:
                warnings.append("provider_empty")  # review reason of E-52
            check_review = _check(source, translated, ctx, warnings)
            results.append(
                Ok(
                    TranslationResult(
                        chunk_id=unit_id,
                        translated_text=translated,
                        provider=ctx.provider,
                        chars_sent=len(batch.texts[index]),
                        chars_billed=billed[index],
                        latency_ms=ctx.latency_ms,
                        attempts=ctx.attempt,
                        glossary_strategy=ctx.strategy,
                        warnings=tuple(warnings),
                        review_flag=review or check_review,
                    )
                )
            )
        return results

    def dry_run_chars(self, unit: OverlayBlock) -> int:
        return len(protect(unit.source_text, self.mode)[0])


def verbatim_result(unit_id: int, source: str, provider: str) -> TranslationResult:
    """Completion record of a unit that is never sent (v1 §5.2 step 1)."""
    return _verbatim(unit_id, source, provider, ())


def iter_units(repo: UnitRepository[U], job_id: str) -> Iterable[U]:
    return repo.iter_ordered(job_id)
