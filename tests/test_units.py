"""Unit adapters and overlay batching (design doc 06, §3.6.2, §4.6; E-52)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

import pytest

from book_translator.database.repository import (
    ChunkRepository,
    JobRecord,
    OverlayBlockRepository,
)
from book_translator.database.session import Database
from book_translator.domain.models import (
    EffectiveGlossary,
    GlossaryEntry,
    GlossaryStatus,
    GlossaryStrategy,
    content_hash_of,
)
from book_translator.domain.overlay import (
    OverlayAlignment,
    OverlayBlock,
    OverlayBlockKind,
    OverlayPage,
    OverlayStyle,
    format_block_id,
)
from book_translator.domain.result import Err, ErrorCode, ErrorScope, Ok, unwrap
from book_translator.pipeline.overlay_batch import (
    apportion,
    batch_pages,
    context_window,
    max_units_for,
)
from book_translator.pipeline.units import (
    ChunkUnitAdapter,
    FinishContext,
    OverlayUnitAdapter,
    OverlayUnitRepository,
    UnitAdapter,
    UnitRepository,
    verbatim_result,
)
from book_translator.translators.base import ProviderResponse
from tests.conftest import make_chunk

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
STYLE = OverlayStyle(font_size=10.0, bold=False, italic=False, family="serif", color="#000000")
NO_GLOSSARY = EffectiveGlossary(entries=(), glossary_hash="0" * 64)


def block(unit_id: int, page: int, index: int, text: str, *, translate: bool = True,
          kind: OverlayBlockKind = OverlayBlockKind.BODY) -> OverlayBlock:
    return OverlayBlock(
        unit_id=unit_id, block_id=format_block_id(page, index), page=page, index_on_page=index,
        kind=kind, bbox=(40.0, 40.0 + index * 20, 300.0, 55.0 + index * 20),
        line_boxes=((40.0, 40.0 + index * 20, 300.0, 55.0 + index * 20),), style=STYLE,
        alignment=OverlayAlignment.LEFT, source_text=text, content_hash=content_hash_of(text),
        char_count=len(text), translate=translate,
        keep_reason=None if translate else "header_footer", fragment=False, over_image=False,
    )


def page(number: int, blocks: Sequence[OverlayBlock]) -> OverlayPage:
    own = [b for b in blocks if b.page == number]
    return OverlayPage(number, 400.0, 600.0, 0, True, len(own),
                       sum(1 for b in own if b.translate), None, 10.0)


def finish_ctx(occurrences: Optional[Dict[int, int]] = None, *, strict: bool = False,
               glossary: EffectiveGlossary = NO_GLOSSARY) -> FinishContext:
    return FinishContext(
        provider="fake", strategy=GlossaryStrategy.NONE, glossary=glossary,
        strict_glossary=strict, latency_ms=12, attempt=1,
        empty_occurrences=dict(occurrences or {}),
    )


@pytest.fixture
def overlay_repo(mem_db: Database, job: JobRecord) -> OverlayBlockRepository:
    """3 pages: a dense page (2 x 600 chars), a sparse page, a page with a kept header."""
    blocks = [
        block(1, 1, 1, "Running header", translate=False, kind=OverlayBlockKind.HEADER),
        block(2, 1, 2, "A" * 600 + "."),
        block(3, 1, 3, "B" * 600 + "."),
        block(4, 2, 1, "Short caption on page two."),
        block(5, 3, 1, "Another short unit on page three."),
        block(6, 3, 2, "The last unit of the document."),
    ]
    repo = OverlayBlockRepository(mem_db, "run-units", None)
    assert unwrap(repo.replace_all(job.id, blocks, pages=[page(n, blocks) for n in (1, 2, 3)])) == 6
    return repo


# ---------------------------------------------------------------------------
# Protocol typing (structural): both repositories and both adapters fit
# ---------------------------------------------------------------------------


def test_repositories_and_adapters_satisfy_the_protocols(
    mem_db: Database, overlay_repo: OverlayBlockRepository
) -> None:
    chunk_repo: UnitRepository = ChunkRepository(mem_db, "r", None)  # type: ignore[type-arg]
    unit_repo: OverlayUnitRepository = overlay_repo
    reflow: UnitAdapter = ChunkUnitAdapter(chunk_repo, "sentinel", False)  # type: ignore[type-arg]
    overlay: UnitAdapter = OverlayUnitAdapter(  # type: ignore[type-arg]
        unit_repo, "sentinel", supports_context=True, max_texts_per_request=50
    )
    assert (reflow.name, overlay.name) == ("reflow", "overlay")
    assert reflow.repo is chunk_repo and overlay.repo is overlay_repo
    for name in ("claim", "group", "build_request", "finish", "dry_run_chars", "unit_id"):
        assert callable(getattr(reflow, name)) and callable(getattr(overlay, name))


# ---------------------------------------------------------------------------
# Overlay adapter: batch claim, context window, 1:1 finish
# ---------------------------------------------------------------------------


def test_batch_claim_stops_at_the_page_boundary(
    overlay_repo: OverlayBlockRepository, job: JobRecord
) -> None:
    adapter = OverlayUnitAdapter(overlay_repo, "sentinel", supports_context=False,
                                 max_texts_per_request=50, min_chars=1000)
    first = unwrap(adapter.claim(job.id, 4, None, NOW, "run-units"))
    assert [u.unit_id for u in first] == [1, 2, 3]  # page 1 reaches 1 000 chars
    assert adapter.group(first) == [first]  # one claim == one batch == one request
    second = unwrap(adapter.claim(job.id, 4, None, NOW, "run-units"))
    assert [u.unit_id for u in second] == [4, 5, 6]  # sparse pages share a request
    assert unwrap(adapter.claim(job.id, 4, None, NOW, "run-units")) == []
    assert adapter.group([]) == []


def test_claim_respects_permits_the_limit_and_the_provider_cap(
    overlay_repo: OverlayBlockRepository, job: JobRecord
) -> None:
    adapter = OverlayUnitAdapter(overlay_repo, "sentinel", supports_context=False,
                                 max_texts_per_request=2, min_chars=1000)
    assert unwrap(adapter.claim(job.id, 0, None, NOW, "r")) == []  # no free permit
    assert unwrap(adapter.claim(job.id, 3, 0, NOW, "r")) == []  # --limit exhausted
    capped = unwrap(adapter.claim(job.id, 3, None, NOW, "r"))
    assert [u.unit_id for u in capped] == [1, 2]  # provider max texts per request
    limited = unwrap(adapter.claim(job.id, 3, 1, NOW, "r"))
    assert [u.unit_id for u in limited] == [3]  # --limit counts units
    assert max_units_for(50) == 50 and max_units_for(500) == 50 and max_units_for(50, 7) == 7


def test_build_request_sends_unit_texts_with_a_page_context(
    overlay_repo: OverlayBlockRepository, job: JobRecord
) -> None:
    adapter = OverlayUnitAdapter(overlay_repo, "sentinel", supports_context=True,
                                 max_texts_per_request=50, context_chars=4000)
    units = [u for u in unwrap(adapter.claim(job.id, 1, None, NOW, "r")) if u.translate]
    batch = unwrap(adapter.build_request(units, job.id))
    assert batch.unit_ids == (2, 3) and batch.counts == (1, 1)
    assert batch.texts == tuple(u.source_text for u in units)
    assert batch.chars == sum(len(t) for t in batch.texts)  # context is never billed
    assert batch.context is not None and len(batch.context) <= 4000
    assert "Running header" not in batch.context  # only translatable page text
    assert batch.texts[0] in batch.context
    no_context = OverlayUnitAdapter(overlay_repo, "sentinel", supports_context=False,
                                    max_texts_per_request=50)
    assert unwrap(no_context.build_request(units, job.id)).context is None
    assert adapter.dry_run_chars(units[0]) == len(units[0].source_text)


def test_context_window_is_capped_and_centred() -> None:
    texts = {1: [f"sentence number {i} of page one." for i in range(400)], 2: ["page two text."]}
    unit = texts[1][200]
    window = context_window(texts, [1, 2], [unit], limit=4000)
    assert len(window) <= 4000 and unit in window
    assert texts[1][0] not in window and "page two text." not in window
    short = context_window({1: ["alpha", "beta"], 2: ["gamma"]}, [2, 1, 1], ["beta"], limit=4000)
    assert short == "alpha\nbeta\ngamma"
    assert context_window(texts, [1], [unit], limit=0) == ""
    assert batch_pages([3, 1, 3, 2]) == (1, 2, 3)
    assert apportion(100, [10, 30, 0]) == (25, 75, 0) and apportion(7, [1, 1, 1]) == (2, 2, 3)
    assert apportion(0, [5]) == (0,) and apportion(5, []) == ()


def test_finish_is_one_to_one_with_per_unit_checks(
    overlay_repo: OverlayBlockRepository, job: JobRecord
) -> None:
    glossary = EffectiveGlossary(
        entries=(GlossaryEntry("caption", "altyazı", 3, GlossaryStatus.APPROVED, "user"),),
        glossary_hash="1" * 64,
    )
    adapter = OverlayUnitAdapter(overlay_repo, "sentinel", supports_context=False,
                                 max_texts_per_request=50, min_chars=0)
    units = [block(4, 2, 1, "Short caption on page two."), block(5, 3, 1, "Another unit here.")]
    batch = unwrap(adapter.build_request(units, job.id))
    response = ProviderResponse(texts=["Kısa başlık.", "Başka bir birim."], chars_billed=44,
                                latency_ms=5)
    results = adapter.finish(batch, response, finish_ctx(strict=True, glossary=glossary))
    first, second = (unwrap(r) for r in results)
    assert (first.chunk_id, second.chunk_id) == (4, 5)
    assert first.translated_text == "Kısa başlık." and second.translated_text == "Başka bir birim."
    assert first.warnings == ("glossary_miss:caption",) and first.review_flag  # per-unit check
    assert second.warnings == () and not second.review_flag
    assert first.chars_sent == len(units[0].source_text)
    assert first.chars_billed + second.chars_billed == 44
    assert first.attempts == 1 and first.latency_ms == 12

    mismatch = adapter.finish(batch, ProviderResponse(["only one"], 8, 1), finish_ctx())
    assert len(mismatch) == 2
    for result in mismatch:
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.PROVIDER_EMPTY_RESPONSE
        assert result.error.scope is ErrorScope.CHUNK_RETRYABLE


def test_empty_rule_is_applied_per_unit(
    overlay_repo: OverlayBlockRepository, job: JobRecord
) -> None:
    """E-52: first empty answer -> that unit retries; second -> kept untranslated + review."""
    adapter = OverlayUnitAdapter(overlay_repo, "sentinel", supports_context=False,
                                 max_texts_per_request=50)
    units = [block(4, 2, 1, "Short caption on page two."), block(5, 3, 1, "Another unit here.")]
    batch = unwrap(adapter.build_request(units, job.id))
    response = ProviderResponse(texts=["", "Başka bir birim."], chars_billed=20, latency_ms=1)

    first_try = adapter.finish(batch, response, finish_ctx())
    assert isinstance(first_try[0], Err)
    assert first_try[0].error.code is ErrorCode.PROVIDER_EMPTY_RESPONSE
    assert isinstance(first_try[1], Ok) and not first_try[1].value.review_flag

    second_try = adapter.finish(batch, response, finish_ctx({4: 1}))
    kept = unwrap(second_try[0])
    assert kept.translated_text == units[0].source_text and kept.review_flag
    assert "provider_empty" in kept.warnings
    assert any(w.startswith("empty_response:0") for w in kept.warnings)
    assert not unwrap(second_try[1]).review_flag


def test_kept_units_complete_verbatim() -> None:
    result = verbatim_result(7, "Running header", "fake")
    assert (result.chunk_id, result.translated_text, result.chars_sent) == (7, "Running header", 0)
    assert result.attempts == 0 and result.glossary_strategy is GlossaryStrategy.NONE


# ---------------------------------------------------------------------------
# Reflow adapter: v1 behaviour unchanged
# ---------------------------------------------------------------------------


def test_chunk_adapter_reproduces_the_v1_chunk_flow(mem_db: Database, job: JobRecord) -> None:
    repo = ChunkRepository(mem_db, "run-units", None)
    chunks = [make_chunk(1, "# Title\n\nFirst paragraph here.\n\n"),
              make_chunk(2, "```\ncode\n```\n\n", translatable=False),
              make_chunk(3, "Second paragraph.\n\n")]
    unwrap(repo.replace_all(job.id, chunks))
    adapter = ChunkUnitAdapter(repo, "sentinel", supports_context=False)
    claimed = unwrap(adapter.claim(job.id, 5, 2, NOW, "run-units"))
    assert [c.chunk_id for c in claimed] == [1, 2]  # min(permits, --limit)
    assert adapter.group(claimed) == [[claimed[0]], [claimed[1]]]  # one chunk per request
    assert adapter.is_translatable(claimed[0]) and not adapter.is_translatable(claimed[1])

    batch = unwrap(adapter.build_request([claimed[0]], job.id))
    assert batch.unit_ids == (1,) and batch.texts == ("Title", "First paragraph here.")
    assert batch.context is None and batch.chars == len("Title") + len("First paragraph here.")
    translated: List[str] = ["Başlık", "İlk paragraf burada."]
    (result,) = adapter.finish(batch, ProviderResponse(translated, 26, 3), finish_ctx())
    done = unwrap(result)
    assert done.translated_text == "# Başlık\n\nİlk paragraf burada.\n\n"  # markers re-applied
    assert done.chars_sent == batch.chars and done.chars_billed == 26 and not done.review_flag

    (empty,) = adapter.finish(batch, ProviderResponse(["", ""], 0, 1), finish_ctx())
    assert isinstance(empty, Err) and empty.error.code is ErrorCode.PROVIDER_EMPTY_RESPONSE
    (lenient,) = adapter.finish(batch, ProviderResponse(["", ""], 0, 1), finish_ctx({1: 1}))
    kept = unwrap(lenient)
    assert kept.review_flag and kept.translated_text == chunks[0].source_text
    (short,) = adapter.finish(batch, ProviderResponse(["x"], 1, 1), finish_ctx())
    assert isinstance(short, Err) and "1 texts for 2 segments" in short.error.message
    assert adapter.dry_run_chars(claimed[0]) == batch.chars
