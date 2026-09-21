"""Chunker, segmenter and inline protection (design doc 02, sections 4 and 11)."""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from typing import List, Tuple

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from book_translator.domain.bt_syntax import (
    format_image_line,
    format_legend_marker,
    is_image_line,
    is_legend_marker,
)
from book_translator.domain.models import (
    Block,
    BlockKind,
    Chunk,
    ChunkKind,
    ChunkStatus,
    content_hash_of,
)
from book_translator.domain.result import Err, ErrorCode, Ok, unwrap
from book_translator.pipeline.assembler import assemble
from book_translator.pipeline.chunker import (
    ChunkResult,
    chunk,
    heading_outline,
    legend_image_id,
    parse_blocks,
)
from book_translator.pipeline.segmenter import reassemble, segment
from book_translator.translators.protect import (
    escape_markup,
    protect,
    restore,
    restore_lenient,
    unescape_markup,
)

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

SENTENCE = "Kırmızı ejderha şehrin üzerinde uçtu ve herkes ona baktı. "


def paragraph(n_sentences: int, marker: str = "") -> str:
    return (marker + SENTENCE * n_sentences).rstrip() + "\n"


def doc(*blocks: str) -> str:
    return "\n".join(blocks)


def kinds(blocks: List[Block]) -> List[BlockKind]:
    return [b.kind for b in blocks]


def spans(md: str, blocks: List[Block]) -> List[str]:
    return [md[b.start : b.end] for b in blocks]


def run(md: str, min_chars: int = 100, max_chars: int = 150, **kw: int) -> ChunkResult:
    return unwrap(chunk(md, min_chars=min_chars, max_chars=max_chars, **kw))


def assert_tiling(md: str, blocks: List[Block]) -> None:
    if not md:
        assert blocks == []
        return
    assert blocks[0].start == 0
    assert blocks[-1].end == len(md)
    for left, right in zip(blocks, blocks[1:], strict=False):
        assert left.end == right.start
        assert left.end > left.start
    assert [b.block_id for b in blocks] == list(range(1, len(blocks) + 1))


def assert_consistent(md: str, result: ChunkResult) -> None:
    chunks = result.chunks
    assert "".join(c.source_text for c in chunks) == md
    for index, c in enumerate(chunks, start=1):
        assert c.chunk_id == index and c.order == index
        assert c.char_count == len(c.source_text)
        assert c.content_hash == content_hash_of(c.source_text)
        if c.kind in (ChunkKind.CODE, ChunkKind.IMAGE, ChunkKind.HR):
            assert c.translatable is False


# --------------------------------------------------------------------------- #
# 4.2 block parser
# --------------------------------------------------------------------------- #


def test_empty_document_has_no_blocks_and_no_chunks() -> None:
    assert parse_blocks("") == []
    result = unwrap(chunk(""))
    assert result.chunks == [] and result.blocks == []


def test_blocks_tile_document_and_trailing_blank_lines_attach() -> None:
    md = "# Title\n\n\nHello.\n\n- a\n- b\n\n\n"
    blocks = parse_blocks(md)
    assert_tiling(md, blocks)
    assert spans(md, blocks) == ["# Title\n\n\n", "Hello.\n\n", "- a\n- b\n\n\n"]


def test_leading_blank_lines_form_a_blank_block() -> None:
    md = "\n\nHello.\n"
    blocks = parse_blocks(md)
    assert_tiling(md, blocks)
    assert kinds(blocks) == [BlockKind.BLANK, BlockKind.PARAGRAPH]
    assert blocks[0].translatable is False


def test_every_block_kind_is_recognised() -> None:
    md = doc(
        "## Heading\n",
        "A paragraph.\n",
        "- item\n  - nested\n1. numbered\n",
        "| a | b |\n|---|---|\n| 1 | 2 |\n",
        "```python\nx = 1\n\n# not a heading\n```\n",
        "> quoted\n> more\n",
        "[^1]: A footnote.\n",
        "<!-- image:3 -->\n",
        "---\n",
        "Last.\n",
    )
    blocks = parse_blocks(md)
    assert_tiling(md, blocks)
    assert kinds(blocks) == [
        BlockKind.HEADING,
        BlockKind.PARAGRAPH,
        BlockKind.LIST,
        BlockKind.TABLE,
        BlockKind.CODE,
        BlockKind.BLOCKQUOTE,
        BlockKind.FOOTNOTE,
        BlockKind.IMAGE,
        BlockKind.HR,
        BlockKind.PARAGRAPH,
    ]
    assert blocks[0].level == 2
    assert blocks[2].level == 1  # deepest list nesting
    assert [b.translatable for b in blocks] == [
        True, True, True, True, False, True, True, False, False, True
    ]
    assert heading_outline(md) == [(2, "Heading")]


def test_unterminated_fence_runs_to_eof_with_warning() -> None:
    md = "Intro.\n\n```\ncode\n\n# still code\n"
    result = run(md)
    assert kinds(result.blocks) == [BlockKind.PARAGRAPH, BlockKind.CODE]
    assert result.blocks[-1].end == len(md)
    assert any("unterminated" in w for w in result.warnings)
    assert result.chunks[-1].kind == ChunkKind.CODE and not result.chunks[-1].translatable


def test_no_trailing_newline_is_still_lossless() -> None:
    md = "# T\n\nNo newline at end"
    result = run(md)
    assert_tiling(md, result.blocks)
    assert_consistent(md, result)


def test_tilde_fence_and_indented_continuation_lines() -> None:
    md = "~~~\n- not a list\n~~~\n\n- item one\n  continues here\n- item two\n"
    blocks = parse_blocks(md)
    assert kinds(blocks) == [BlockKind.CODE, BlockKind.LIST]
    assert_tiling(md, blocks)


# --------------------------------------------------------------------------- #
# 4.3 packing
# --------------------------------------------------------------------------- #


def test_packing_bounds_default_case() -> None:
    md = doc(*[paragraph(2) for _ in range(30)])  # ~112 chars per block
    result = run(md, min_chars=300, max_chars=450)
    assert_consistent(md, result)
    for c in result.chunks[:-1]:
        assert 300 <= c.char_count <= 450, (c.char_count, c.warnings)
        assert c.kind == ChunkKind.TEXT
    assert len(result.chunks) > 1


def test_heading_stays_with_next_paragraph() -> None:
    md = doc(paragraph(3), "# Chapter 2\n", paragraph(3))
    result = run(md, min_chars=150, max_chars=220)
    assert_consistent(md, result)
    assert len(result.chunks) == 2
    assert result.chunks[0].source_text == paragraph(3) + "\n"
    assert result.chunks[1].source_text.startswith("# Chapter 2\n\n")
    assert result.chunks[1].heading_path == ("Chapter 2",)


def test_consecutive_headings_move_together() -> None:
    md = doc(paragraph(3), "# Part\n", "## Chapter\n", paragraph(3))
    result = run(md, min_chars=150, max_chars=240)
    assert_consistent(md, result)
    assert len(result.chunks) == 2
    assert result.chunks[1].source_text.startswith("# Part\n\n## Chapter\n\n")
    assert result.chunks[1].heading_path == ("Part", "Chapter")


def test_heading_at_end_of_document_becomes_heading_chunk() -> None:
    md = doc(paragraph(2), "## The End\n")
    result = run(md, min_chars=50, max_chars=100)
    assert_consistent(md, result)
    assert result.chunks[-1].kind == ChunkKind.HEADING
    assert result.chunks[-1].source_text == "## The End\n"
    assert result.chunks[-1].translatable is True


def test_heading_before_code_block_is_stranded_with_warning() -> None:
    md = doc(paragraph(2), "## Code\n", "```\nx\n```\n", paragraph(1))
    result = run(md, min_chars=50, max_chars=400)
    assert_consistent(md, result)
    stranded = [c for c in result.chunks if c.source_text.endswith("## Code\n\n")]
    assert len(stranded) == 1
    assert any(w.startswith("heading_stranded") for w in stranded[0].warnings)


def test_code_fence_is_one_non_translatable_chunk_regardless_of_size() -> None:
    body = "".join(f"line {i} = {i * 2}\n" for i in range(200))
    md = doc(paragraph(1), "```py\n" + body + "```\n", paragraph(1))
    result = run(md, min_chars=100, max_chars=150)
    assert_consistent(md, result)
    code = [c for c in result.chunks if c.kind == ChunkKind.CODE]
    assert len(code) == 1
    assert code[0].translatable is False
    assert code[0].parent_block_id is None
    assert code[0].source_text == "```py\n" + body + "```\n\n"


def test_image_and_hr_are_own_chunks() -> None:
    md = doc(paragraph(1), "<!-- image:1 -->\n", "---\n", paragraph(1))
    result = run(md, min_chars=10, max_chars=500)
    assert [c.kind for c in result.chunks] == [
        ChunkKind.TEXT, ChunkKind.IMAGE, ChunkKind.HR, ChunkKind.TEXT
    ]
    assert [c.translatable for c in result.chunks] == [True, False, False, True]


def test_table_never_shares_a_chunk_with_prose() -> None:
    table = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    md = doc(paragraph(1), table, paragraph(1))
    result = run(md, min_chars=10, max_chars=1000)
    assert [c.kind for c in result.chunks] == [ChunkKind.TEXT, ChunkKind.TABLE, ChunkKind.TEXT]
    assert result.chunks[1].source_text == table + "\n"


def test_list_may_share_a_chunk_with_prose() -> None:
    md = doc("Intro.\n", "- a\n- b\n", "Outro.\n")
    result = run(md, min_chars=10, max_chars=1000)
    assert result.chunks[0].source_text == "Intro.\n\n- a\n- b\n\n"
    assert result.chunks[0].kind == ChunkKind.TEXT


def test_single_block_chunk_kinds() -> None:
    md = doc("- a\n- b\n", "[^1]: note\n")
    result = run(md, min_chars=5, max_chars=1000)
    assert [c.kind for c in result.chunks] == [ChunkKind.LIST, ChunkKind.FOOTNOTE]


def test_provider_limit_lowers_effective_max() -> None:
    md = doc(*[paragraph(1) for _ in range(10)])
    result = run(md, min_chars=100, max_chars=1000, provider_limit=120)
    assert all(c.char_count <= 120 for c in result.chunks if c.translatable)
    assert any("provider limit" in w for w in result.warnings)


def test_undersize_deviation_is_flagged() -> None:
    md = doc(paragraph(1), "```\nx\n```\n", paragraph(1))
    result = run(md, min_chars=100, max_chars=150)
    first = result.chunks[0]
    assert first.char_count < 100
    assert any(w.startswith("undersize:") for w in first.warnings)


def test_heading_path_tracks_h1_to_h3_only() -> None:
    md = doc(
        "# One\n", paragraph(1), "## Two\n", "#### Four\n", paragraph(1), "# Uno\n", paragraph(1)
    )
    result = run(md, min_chars=10, max_chars=200)
    paths = [c.heading_path for c in result.chunks]
    assert paths[0] == ("One",)
    assert ("One", "Two") in paths
    assert paths[-1] == ("Uno",)


# --------------------------------------------------------------------------- #
# 4.4 oversize split
# --------------------------------------------------------------------------- #


def sub_chunks_of(result: ChunkResult, block_id: int) -> List[Chunk]:
    subs = [c for c in result.chunks if c.parent_block_id == block_id]
    assert [c.sub_index for c in subs] == list(range(len(subs)))
    assert all(c.sub_count == len(subs) for c in subs)
    return subs


def test_oversize_paragraph_splits_at_sentences_with_parent_linkage() -> None:
    long = paragraph(12)  # ~660 chars
    md = doc(paragraph(1), long, paragraph(1))
    result = run(md, min_chars=100, max_chars=150)
    assert_consistent(md, result)
    block = next(b for b in result.blocks if b.end - b.start > 150)
    subs = sub_chunks_of(result, block.block_id)
    assert len(subs) >= 4
    assert "".join(c.source_text for c in subs) == md[block.start : block.end]
    for c in subs:
        assert c.char_count <= 150
        assert c.kind == ChunkKind.TEXT
        assert c.source_text.endswith(" ") or c is subs[-1]  # cut after the sentence space
        assert not c.source_text.startswith(" ")


def test_single_sentence_over_limit_is_split_at_whitespace_with_warning() -> None:
    words = "kelime " * 60
    md = words.rstrip() + "\n"
    result = run(md, min_chars=50, max_chars=100)
    assert_consistent(md, result)
    assert all(c.char_count <= 100 for c in result.chunks)
    assert all(c.parent_block_id == 1 for c in result.chunks)
    assert any("split at whitespace" in w for w in result.warnings)


def test_unbreakable_run_exceeds_limit_and_is_flagged() -> None:
    md = "x" * 250 + "\n"
    result = run(md, min_chars=50, max_chars=100)
    assert_consistent(md, result)
    assert result.chunks[0].char_count == 100
    assert "oversize:indivisible" not in result.chunks[0].warnings
    assert len(result.chunks) == 3


def test_oversize_list_splits_at_top_level_items_keeping_nested() -> None:
    items = "".join(f"- item {i} " + "text " * 8 + f"\n  - nested {i}\n" for i in range(12))
    md = items
    result = run(md, min_chars=80, max_chars=120)
    assert_consistent(md, result)
    subs = sub_chunks_of(result, 1)
    assert len(subs) > 1
    for c in subs:
        assert c.kind == ChunkKind.LIST
        assert c.source_text.startswith("- item")
        assert c.source_text.count("- item") == c.source_text.count("  - nested")


def test_oversize_table_splits_by_rows_header_only_in_first() -> None:
    header = "| Name | Value |\n|------|-------|\n"
    rows = "".join(f"| satır {i} | değer {i} |\n" for i in range(30))
    md = header + rows
    result = run(md, min_chars=80, max_chars=120)
    assert_consistent(md, result)
    subs = sub_chunks_of(result, 1)
    assert len(subs) > 1
    assert subs[0].source_text.startswith(header)
    for c in subs[1:]:
        assert "Name" not in c.source_text and "----" not in c.source_text
        assert c.source_text.startswith("| satır")
        assert c.kind == ChunkKind.TABLE
    assert all(c.char_count <= 120 for c in subs)


def test_oversize_blockquote_and_footnote_split_as_prose() -> None:
    quote = "> " + SENTENCE * 8 + "\n"
    note = "[^1]: " + SENTENCE * 8 + "\n"
    md = doc(quote, note)
    result = run(md, min_chars=100, max_chars=150)
    assert_consistent(md, result)
    assert len(sub_chunks_of(result, 1)) > 1
    subs = sub_chunks_of(result, 2)
    assert len(subs) > 1 and all(c.kind == ChunkKind.FOOTNOTE for c in subs)


def test_chunk_integrity_error_type() -> None:
    result = chunk("abc\n", min_chars=0, max_chars=0)
    assert isinstance(result, Err) and result.error.code == ErrorCode.PROVIDER_CONFIG


# --------------------------------------------------------------------------- #
# 4.5 hypothesis property
# --------------------------------------------------------------------------- #

_LETTERS = "abcdefghijklmnopqrstuvwxyzçğıöşüÇĞİÖŞÜABCDEFGHIJKLMNOPQRSTUVWXYZ"
_word = st.text(alphabet=_LETTERS, min_size=1, max_size=9)
_words = st.lists(_word, min_size=1, max_size=12).map(" ".join)
_sentence = st.tuples(_words, st.sampled_from([".", "!", "?", "…", '."', ".)", ""])).map(
    lambda t: t[0] + t[1]
)
_sentences = st.lists(_sentence, min_size=1, max_size=40).map(" ".join)


def _heading(level: int, title: str) -> str:
    return "#" * level + " " + title + "\n"


_block = st.one_of(
    st.tuples(st.integers(1, 6), _words).map(lambda t: _heading(*t)),
    _sentences.map(lambda s: s + "\n"),
    st.lists(
        st.tuples(st.sampled_from(["- ", "* ", "1. ", "  - "]), _sentence), min_size=1, max_size=15
    ).map(lambda items: "".join(m + s + "\n" for m, s in items)),
    st.lists(st.lists(_word, min_size=1, max_size=3), min_size=2, max_size=15).map(
        lambda rows: "| " + " | ".join(rows[0]) + " |\n|"
        + "---|" * len(rows[0])
        + "\n"
        + "".join("| " + " | ".join(r) + " |\n" for r in rows[1:])
    ),
    st.lists(_words, min_size=0, max_size=8).map(
        lambda ls: "```\n" + "".join(x + "\n" for x in ls) + "```\n"
    ),
    st.lists(_sentence, min_size=1, max_size=6).map(
        lambda ls: "".join("> " + x + "\n" for x in ls)
    ),
    st.tuples(st.integers(1, 99), _sentences).map(lambda t: f"[^{t[0]}]: {t[1]}\n"),
    st.integers(1, 50).map(lambda n: f"<!-- image:{n} -->\n"),
    # v1.1 (design doc 06, 8.2): image line with a file, legend marker + label list
    st.integers(1, 50).map(lambda n: format_image_line(n, f"images/p{n:03d}-f01.png") + "\n"),
    st.tuples(
        st.integers(1, 50),
        st.integers(0, 5),
        st.lists(st.lists(_word, min_size=1, max_size=4).map(" ".join), min_size=1, max_size=8),
    ).map(
        lambda t: format_legend_marker(t[0], t[1]) + "\n" + "".join(f"- {x}\n" for x in t[2])
    ),
    st.just("---\n"),
)


@st.composite
def bt_markdown(draw: st.DrawFn) -> Tuple[str, int, int]:
    max_chars = draw(st.integers(40, 300))
    min_chars = draw(st.integers(10, max_chars))
    blocks = draw(st.lists(_block, min_size=0, max_size=25))
    separator = draw(st.sampled_from(["\n", "\n\n"]))
    md = separator.join(blocks)
    if draw(st.booleans()):
        md = "\n" * draw(st.integers(1, 2)) + md
    if md and draw(st.booleans()):
        md = md.rstrip("\n")
    if len(md) > 5 * max_chars:
        md = md[: 5 * max_chars]
    return md, min_chars, max_chars


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(bt_markdown())
def test_property_lossless_tiling_and_bounds(case: Tuple[str, int, int]) -> None:
    md, min_chars, max_chars = case
    blocks = parse_blocks(md)
    assert_tiling(md, blocks)

    result = unwrap(chunk(md, min_chars=min_chars, max_chars=max_chars))
    assert result.blocks == blocks
    assert_consistent(md, result)

    ends = {b.end: b for b in blocks}
    position = 0
    for c in result.chunks:
        position += c.char_count
        is_last = c is result.chunks[-1]
        is_sub = c.parent_block_id is not None
        if is_sub:
            assert c.sub_index is not None and c.sub_count is not None
        if c.translatable and not is_sub and not is_last and c.char_count < min_chars:
            assert any(w.startswith("undersize:") for w in c.warnings), c
        if c.translatable and c.char_count > max_chars:
            assert is_sub or any(w.startswith("oversize:") for w in c.warnings), c
        if is_sub:
            assert c.char_count <= max_chars or "oversize:indivisible" in c.warnings
        last_block = ends.get(position)
        if not is_sub:
            assert last_block is not None, "chunk boundary must coincide with a block boundary"
        ends_with_heading = last_block is not None and last_block.kind == BlockKind.HEADING
        if ends_with_heading and not is_sub and not is_last:
            assert any(w.startswith("heading_stranded") for w in c.warnings), c

    for block in blocks:
        subs = [c for c in result.chunks if c.parent_block_id == block.block_id]
        if subs:
            assert [c.sub_index for c in subs] == list(range(len(subs)))
            assert "".join(c.source_text for c in subs) == md[block.start : block.end]
            assert block.translatable and block.kind != BlockKind.CODE

    for c in result.chunks:
        seg = segment(c.source_text)
        assert reassemble(seg, [s.text for s in seg.segments]) == c.source_text
        for s in seg.segments:
            assert "\n" not in s.text and s.text == s.text.strip()


# --------------------------------------------------------------------------- #
# 4.6 segmenter
# --------------------------------------------------------------------------- #


def test_segmenter_extracts_only_human_text() -> None:
    text = (
        "## Başlık\n\nBir paragraf.\n\n- ilk madde\n  - iç madde\n3. üçüncü\n\n"
        "| A | B |\n|---|---|\n| bir |  |\n| iki | üç |\n\n> alıntı\n>\n> devam\n\n"
        "[^7]: dipnot metni\n\n```\nkod\n```\n\n<!-- image:2 -->\n\n---\n\nSon.\n"
    )
    seg = segment(text)
    assert [s.text for s in seg.segments] == [
        "Başlık", "Bir paragraf.", "ilk madde", "iç madde", "üçüncü",
        "A", "B", "bir", "iki", "üç", "alıntı", "devam", "dipnot metni", "Son.",
    ]
    assert seg.segments[0].prefix == "## " and seg.segments[0].suffix == "\n\n"
    assert seg.segments[2].prefix == "- " and seg.segments[2].suffix == "\n"
    assert seg.segments[3].prefix == "  - " and seg.segments[3].suffix == "\n"
    assert seg.segments[4].prefix == "3. " and seg.segments[4].suffix == "\n\n"
    assert seg.segments[5].prefix == "| " and seg.segments[5].suffix == ""
    assert seg.segments[6].prefix == " | " and seg.segments[6].suffix == " |\n"
    assert seg.segments[7].prefix == "|---|---|\n| " and seg.segments[7].suffix == " |  |\n"
    assert seg.segments[10].prefix == "> "
    assert seg.segments[12].prefix == "[^7]: "
    assert "```\nkod\n```" in seg.segments[13].prefix  # code kept locally
    assert reassemble(seg, [s.text for s in seg.segments]) == text
    translated = reassemble(seg, [f"TR:{s.text}" for s in seg.segments])
    assert translated.startswith("## TR:Başlık\n\nTR:Bir paragraf.\n\n- TR:ilk madde\n")
    assert "| TR:bir |  |\n" in translated
    assert "```\nkod\n```" in translated


def test_segmenter_non_translatable_only_chunk_has_no_segments() -> None:
    text = "```\nx = 1\n```\n\n"
    seg = segment(text)
    assert seg.segments == [] and seg.tail == text
    assert reassemble(seg, []) == text


def test_reassemble_rejects_wrong_count() -> None:
    seg = segment("Merhaba.\n")
    with pytest.raises(ValueError):
        reassemble(seg, [])


# --------------------------------------------------------------------------- #
# protect / restore
# --------------------------------------------------------------------------- #

PROTECT_SAMPLE = (
    "Use `git status` (see [docs](https://example.com/a_(b)) or https://x.y/z?q=1, "
    "and `code` again)[^3]."
)


@pytest.mark.parametrize("mode", ["xml", "sentinel"])
def test_protect_masks_code_urls_links_and_restores(mode: str) -> None:
    masked, mapping = protect(PROTECT_SAMPLE, mode)  # type: ignore[arg-type]
    assert set(mapping.values()) == {
        "`git status`", "(https://example.com/a_(b))", "https://x.y/z?q=1", "`code`", "[^3]"
    }
    assert "[docs]" in masked and "https://" not in masked and "`" not in masked
    if mode == "xml":
        assert '<x id="1"/>' in masked
    else:
        assert "⟦1⟧" in masked
    assert unwrap(restore(masked, mapping, mode)) == PROTECT_SAMPLE  # type: ignore[arg-type]
    assert (
        unwrap(restore("çeviri " + masked, mapping, mode))  # type: ignore[arg-type]
        == "çeviri " + PROTECT_SAMPLE
    )


def test_restore_detects_missing_and_duplicated_placeholders() -> None:
    masked, mapping = protect("See `a` and `b`.", "sentinel")
    missing = restore(masked.replace("⟦2⟧", ""), mapping, "sentinel")
    assert isinstance(missing, Err)
    assert missing.error.code == ErrorCode.PROVIDER_EMPTY_RESPONSE
    duplicated = restore(masked + " ⟦1⟧", mapping, "sentinel")
    assert isinstance(duplicated, Err)


def test_restore_lenient_appends_missing_and_reports() -> None:
    masked, mapping = protect("See `a` and `b`.", "sentinel")
    text, warnings = restore_lenient(
        masked.replace("⟦2⟧", "").replace("⟦1⟧", "⟦1⟧ ⟦1⟧"), mapping, "sentinel"
    )
    assert warnings == ["placeholder_duplicated:1", "placeholder_missing:2"]
    assert text.endswith("`b`") and text.count("`a`") == 2


def test_restore_tolerates_xml_tag_variants() -> None:
    masked, mapping = protect("Run `ls` now.", "xml")
    variant = masked.replace('<x id="1"/>', "<x id='1'></x>")
    assert unwrap(restore(variant, mapping, "xml")) == "Run `ls` now."


def test_protect_no_spans_is_identity() -> None:
    text = "Sadece düz metin."
    masked, mapping = protect(text, "xml")
    assert masked == text and mapping == {}
    assert isinstance(restore(masked, mapping, "xml"), Ok)


@given(st.text(alphabet=_LETTERS + " `[]()<>&;:/.⟦⟧", max_size=80))
@settings(max_examples=200, deadline=None)
def test_protect_restore_round_trip_property(text: str) -> None:
    for mode in ("xml", "sentinel"):
        masked, mapping = protect(text, mode)  # type: ignore[arg-type]
        assert unwrap(restore(masked, mapping, mode)) == text  # type: ignore[arg-type]
        lenient, warnings = restore_lenient(masked, mapping, mode)  # type: ignore[arg-type]
        assert lenient == text and warnings == []
        if mode == "xml":
            ET.fromstring(f"<d>{masked}</d>")  # the payload is always well-formed XML


# --------------------------------------------------------------------------- #
# XML escaping in the "xml" protect mode (HTTP 400 on a bare "<")
# --------------------------------------------------------------------------- #

# Verbatim from the 364-page run: every one of these made DeepL answer HTTP 400 and took
# the whole batch down with it ("Contributors", "Running samples" and 41 more innocents).
XML_HOSTILE = [
    "5. CMake will have generated a Visual Studio solution file at "
    "<opencv_build_folder>/OpenCV.sln. Open it in Visual Studio.",
    "Unzip this file to any destination folder, which we will refer to as "
    "<opencv_contrib_unzip_destination>.",
    "Joe Minichino is an R&D labs engineer at Teamwork.",
    "The loop runs while a < b and stops when a > b.",
    "Tom & Jerry <-> cat & mouse",
]


@pytest.mark.parametrize("text", XML_HOSTILE)
def test_xml_mode_payload_is_well_formed_and_round_trips(text: str) -> None:
    masked, mapping = protect(text, "xml")
    ET.fromstring(f"<d>{masked}</d>")  # would raise ParseError before the escaping
    assert "&" not in masked.replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")
    assert unwrap(restore(masked, mapping, "xml")) == text


def test_xml_mode_escapes_only_outside_placeholders() -> None:
    masked, mapping = protect("Run `a < b` when x < y & z.", "xml")
    assert masked == 'Run <x id="1"/> when x &lt; y &amp; z.'
    assert mapping == {'<x id="1"/>': "`a < b`"}  # the span itself stays unescaped
    assert unwrap(restore(masked, mapping, "xml")) == "Run `a < b` when x < y & z."


def test_sentinel_mode_does_not_escape() -> None:
    masked, mapping = protect("x < y & z", "sentinel")
    assert masked == "x < y & z" and mapping == {}
    assert unwrap(restore(masked, mapping, "sentinel")) == "x < y & z"


def test_escaping_is_exactly_one_level() -> None:
    """A book that quotes HTML keeps its entities: no ``&amp;amp;`` and no bare ``&``."""
    text = "Write &amp; for & and &lt; for <."
    masked, _ = protect(text, "xml")
    assert masked == "Write &amp;amp; for &amp; and &amp;lt; for &lt;."
    assert unescape_markup(masked) == text
    assert unwrap(restore(masked, {}, "xml")) == text


def test_restore_does_not_resolve_a_literal_placeholder_in_the_source() -> None:
    """``<x id="1"/>`` written by the author travels escaped and stays text."""
    masked, mapping = protect('The tag <x id="1"/> and `code`.', "xml")
    assert masked == 'The tag &lt;x id="1"/&gt; and <x id="1"/>.'
    assert unwrap(restore(masked, mapping, "xml")) == 'The tag <x id="1"/> and `code`.'


def test_unescape_markup_resolves_provider_added_references_once() -> None:
    assert unescape_markup("it&#39;s") == "it's"
    assert unescape_markup("&amp;lt;") == "&lt;"
    assert unescape_markup("&amp;#39;") == "&#39;"
    assert escape_markup("&<>") == "&amp;&lt;&gt;"


def test_restore_lenient_unescapes_too() -> None:
    masked, mapping = protect("See `a` when x < y.", "xml")
    text, warnings = restore_lenient(masked.replace('<x id="1"/>', ""), mapping, "xml")
    assert warnings == ["placeholder_missing:1"]
    assert text == "See  when x < y. `a`"


# --------------------------------------------------------------------------- #
# fix round (docs/04_code_review.md)
# --------------------------------------------------------------------------- #


def test_fence_directly_after_a_paragraph_line_starts_a_code_block() -> None:
    """CR-04: fence state beats paragraph continuation (design 4.2 precedence)."""
    md = "Para\n```\ncode()\n```\n\nNext.\n"
    result = run(md)
    assert kinds(result.blocks) == [BlockKind.PARAGRAPH, BlockKind.CODE, BlockKind.PARAGRAPH]
    code = result.blocks[1]
    assert not code.translatable and md[code.start : code.end].startswith("```")
    assert "".join(spans(md, result.blocks)) == md
    code_chunks = [c for c in result.chunks if c.kind == ChunkKind.CODE]
    assert len(code_chunks) == 1 and not code_chunks[0].translatable
    assert "code()" in code_chunks[0].source_text
    assert all("code()" not in c.source_text for c in result.chunks if c.translatable)


# --------------------------------------------------------------------------- #
# v1.1 figures (design doc 06, C1-C4): image line with src, legend block, context
# --------------------------------------------------------------------------- #

LEGEND_DOC = (
    "# Figures\n\n"
    "Intro paragraph.\n\n"
    '<!-- image:3 src="images/p002-f01.png" -->\n\n'
    "Figure 3: The pipeline.\n\n"
    '<!-- legend:3 more="2" -->\n- Input\n- 1. Stage\n- Output\n\n'
    "After the figure.\n\n"
    "<!-- legend:9 -->\n\n"
    "Not a list.\n"
)
LEGEND_CHUNK = (
    'Figure 3: The pipeline.\n\n<!-- legend:3 more="2" -->\n- Input\n- 1. Stage\n- Output\n\n'
)


def test_image_line_with_src_is_an_image_block_and_own_chunk() -> None:
    result = run(LEGEND_DOC, min_chars=10, max_chars=1000)
    assert_consistent(LEGEND_DOC, result)
    images = [c for c in result.chunks if c.kind == ChunkKind.IMAGE]
    assert len(images) == 1 and images[0].translatable is False
    assert images[0].source_text == format_image_line(3, "images/p002-f01.png") + "\n\n"
    assert images[0].parent_block_id is None


def test_legend_marker_opens_a_list_block_and_orphan_marker_is_not_translatable() -> None:
    blocks = parse_blocks(LEGEND_DOC)
    assert_tiling(LEGEND_DOC, blocks)
    assert kinds(blocks) == [
        BlockKind.HEADING,
        BlockKind.PARAGRAPH,
        BlockKind.IMAGE,
        BlockKind.PARAGRAPH,
        BlockKind.LIST,
        BlockKind.PARAGRAPH,
        BlockKind.PARAGRAPH,
        BlockKind.PARAGRAPH,
    ]
    legend = blocks[4]
    assert spans(LEGEND_DOC, [legend]) == [
        '<!-- legend:3 more="2" -->\n- Input\n- 1. Stage\n- Output\n\n'
    ]
    assert legend.translatable and legend.level == 0
    assert legend_image_id(LEGEND_DOC, legend) == 3
    orphan = blocks[6]
    assert spans(LEGEND_DOC, [orphan]) == ["<!-- legend:9 -->\n\n"]
    assert orphan.translatable is False
    assert legend_image_id(LEGEND_DOC, orphan) is None
    assert [b.translatable for b in blocks] == [
        True, True, False, True, True, True, False, True
    ]


def test_legend_marker_ends_an_open_paragraph_list_or_quote() -> None:
    md = (
        "Prose line\n<!-- legend:1 -->\n- a\n\n"
        "- x\n<!-- legend:2 -->\n- b\n\n"
        "> q\n<!-- legend:3 -->\n- c\n"
    )
    blocks = parse_blocks(md)
    assert_tiling(md, blocks)
    assert kinds(blocks) == [
        BlockKind.PARAGRAPH, BlockKind.LIST, BlockKind.LIST, BlockKind.LIST,
        BlockKind.BLOCKQUOTE, BlockKind.LIST,
    ]
    assert [legend_image_id(md, b) for b in blocks] == [None, 1, None, 2, None, 3]


def test_legend_shares_a_chunk_only_with_its_caption() -> None:
    result = run(LEGEND_DOC, min_chars=10, max_chars=1000)
    assert_consistent(LEGEND_DOC, result)
    assert [c.source_text for c in result.chunks] == [
        "# Figures\n\nIntro paragraph.\n\n",
        '<!-- image:3 src="images/p002-f01.png" -->\n\n',
        LEGEND_CHUNK,
        "After the figure.\n\n",
        "<!-- legend:9 -->\n\n",
        "Not a list.\n",
    ]
    legend_chunk = result.chunks[2]
    assert legend_chunk.kind == ChunkKind.TEXT and legend_chunk.translatable
    assert "legend:3" in legend_chunk.warnings
    for c in result.chunks:
        if c is not legend_chunk:
            assert not any(w.startswith("legend:") for w in c.warnings)
    orphan = result.chunks[4]
    assert orphan.translatable is False and orphan.kind == ChunkKind.TEXT


def test_legend_without_caption_is_isolated_from_headings_and_lists() -> None:
    md = (
        "## Diagram\n\n<!-- legend:1 -->\n- a\n- b\n\n"
        "- other\n- list\n\n<!-- legend:2 -->\n- c\n\nTail.\n"
    )
    result = run(md, min_chars=100, max_chars=1000)
    assert_consistent(md, result)
    assert [c.source_text for c in result.chunks] == [
        "## Diagram\n\n",
        "<!-- legend:1 -->\n- a\n- b\n\n",
        "- other\n- list\n\n",
        "<!-- legend:2 -->\n- c\n\n",
        "Tail.\n",
    ]
    assert result.chunks[1].kind == ChunkKind.LIST and "legend:1" in result.chunks[1].warnings
    assert "legend:2" in result.chunks[3].warnings
    assert any(w.startswith("heading_stranded") for w in result.chunks[0].warnings)


def test_caption_waits_for_its_legend_even_when_min_chars_is_reached() -> None:
    caption = "Figure 1: " + SENTENCE * 2 + "\n"  # > min_chars on its own
    md = paragraph(1) + "\n" + caption + "\n<!-- legend:1 -->\n- a\n\n" + paragraph(1)
    result = run(md, min_chars=50, max_chars=1000)
    assert_consistent(md, result)
    legend = next(c for c in result.chunks if "legend:1" in c.warnings)
    assert legend.source_text == caption + "\n<!-- legend:1 -->\n- a\n\n"
    assert legend.kind == ChunkKind.TEXT


def test_oversize_caption_is_split_and_legend_stays_alone() -> None:
    caption = "Figure 1: " + SENTENCE * 6 + "\n"
    md = caption + "\n<!-- legend:1 -->\n- a\n- b\n"
    result = run(md, min_chars=50, max_chars=150)
    assert_consistent(md, result)
    legend = result.chunks[-1]
    assert legend.source_text == "<!-- legend:1 -->\n- a\n- b\n"
    assert "legend:1" in legend.warnings and legend.kind == ChunkKind.LIST
    assert all(c.parent_block_id == 1 for c in result.chunks[:-1]) and len(result.chunks) > 2


def test_segmenter_keeps_legend_marker_in_prefix_and_exposes_caption_context() -> None:
    seg = segment(LEGEND_CHUNK)
    assert [s.text for s in seg.segments] == [
        "Figure 3: The pipeline.", "Input", "1. Stage", "Output"
    ]
    assert seg.segments[0].suffix == "\n\n"
    assert seg.segments[1].prefix == '<!-- legend:3 more="2" -->\n- '
    assert seg.segments[2].prefix == "- " and seg.segments[3].suffix == "\n\n"
    assert seg.context == "Figure 3: The pipeline."
    assert reassemble(seg, [s.text for s in seg.segments]) == LEGEND_CHUNK
    translated = reassemble(seg, ["Şekil 3: Boru hattı.", "Girdi", "1. Aşama", "Çıktı"])
    assert translated == (
        'Şekil 3: Boru hattı.\n\n<!-- legend:3 more="2" -->\n- Girdi\n- 1. Aşama\n- Çıktı\n\n'
    )


def test_segmenter_context_is_none_without_caption_or_legend() -> None:
    alone = segment("<!-- legend:1 -->\n- a\n")
    assert [s.text for s in alone.segments] == ["a"] and alone.context is None
    assert alone.segments[0].prefix == "<!-- legend:1 -->\n- "
    orphan = segment("<!-- legend:9 -->\n\n")
    assert orphan.segments == [] and orphan.tail == "<!-- legend:9 -->\n\n"
    assert orphan.context is None
    assert segment("Just prose.\n").context is None
    heading = segment("## Title\n\n<!-- legend:2 -->\n- a\n")
    assert heading.context is None and [s.text for s in heading.segments] == ["Title", "a"]


def test_legend_chunk_is_one_provider_batch_with_the_caption_as_context() -> None:
    """Design doc 06, 8.2: one translate() payload ``[caption, *labels]``, context == caption."""
    md = (
        "Intro.\n\n"
        '<!-- image:1 src="images/p001-f01.png" -->\n\n'
        "Figure 1: Flow.\n\n"
        "<!-- legend:1 -->\n- Start\n- Stop\n\n"
        "Outro.\n"
    )
    result = run(md, min_chars=10, max_chars=1000)
    legend_chunks = [c for c in result.chunks if any(w.startswith("legend:") for w in c.warnings)]
    assert len(legend_chunks) == 1
    seg = segment(legend_chunks[0].source_text)
    payload = [protect(s.text, "xml")[0] for s in seg.segments]
    assert payload == ["Figure 1: Flow.", "Start", "Stop"]
    assert seg.context == "Figure 1: Flow."
    others = [segment(c.source_text) for c in result.chunks if c is not legend_chunks[0]]
    assert all(s.context is None for s in others)
    assert all("Start" not in t.text and "Stop" not in t.text for s in others for t in s.segments)
    assert all("images/" not in t.text for s in others for t in s.segments)


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(bt_markdown())
def test_property_legend_isolation_context_and_identity_assembly(
    case: Tuple[str, int, int],
) -> None:
    """Design doc 06, 8.2: legend chunk = [caption?, legend]; context; identity round trips."""
    md, min_chars, max_chars = case
    result = unwrap(chunk(md, min_chars=min_chars, max_chars=max_chars))
    assert_consistent(md, result)
    blocks = result.blocks
    position = 0
    for c in result.chunks:
        start, end = position, position + c.char_count
        position = end
        seg = segment(c.source_text)
        assert reassemble(seg, [s.text for s in seg.segments]) == c.source_text
        for s in seg.segments:  # the two comment lines are never provider texts
            assert not is_legend_marker(s.text) and not is_image_line(s.text)
        legend_warnings = [w for w in c.warnings if w.startswith("legend:")]
        if c.parent_block_id is not None:
            assert legend_warnings == []
            continue
        covered = [
            b for b in blocks if start <= b.start and b.end <= end and b.kind != BlockKind.BLANK
        ]
        legends = [b for b in covered if legend_image_id(md, b) is not None]
        if not legends:
            assert legend_warnings == [] and seg.context is None
            continue
        assert len(legends) == 1
        assert legend_warnings == [f"legend:{legend_image_id(md, legends[0])}"]
        covered_kinds = [b.kind for b in covered]
        assert covered_kinds in ([BlockKind.LIST], [BlockKind.PARAGRAPH, BlockKind.LIST])
        if covered_kinds == [BlockKind.PARAGRAPH, BlockKind.LIST]:
            assert covered[0].translatable
            assert seg.context == md[covered[0].start : covered[0].end].strip()
        else:
            assert seg.context is None
    if result.chunks:
        completed = [
            dataclasses.replace(c, status=ChunkStatus.COMPLETED, translated_text=c.source_text)
            for c in result.chunks
        ]
        doc = unwrap(assemble(
            completed, allow_partial=False, metadata={}, source_markdown=md, pair_legends=False
        ))
        assert doc.markdown == md
        assert not any(w.startswith("source_mismatch") for w in doc.warnings)


# --------------------------------------------------------------------------- #
# CR-58: strip_legends never alters fenced code (v1.1 fix round)
# --------------------------------------------------------------------------- #


def test_strip_legends_leaves_fenced_code_and_foreign_blank_runs_untouched() -> None:
    from book_translator.pipeline.figure_labels import (
        legend_ids,
        rewrite_image_sources,
        strip_legends,
    )

    code = "```python\na = 1\n\n\nb = 2\n\n\n\n<!-- legend:9 -->\n- code — kod\n```\n"
    tilde = "~~~\n```\n\n\n<!-- image:7 src=\"images/p009-f01.png\" -->\n~~~\n"
    doc = (
        "# Bölüm\n\n"
        '<!-- image:1 src="images/p002-f01.png" -->\n\n'
        "Şekil 1: Akış.\n\n"
        "<!-- legend:1 -->\n- Start — Başla\n- Stop — Dur\n\n"
        + code
        + "\nAra paragraf.\n\n\n\nÜç boş satırdan sonra.\n\n"
        + '<!-- image:2 src="images/p003-f01.png" -->\n\n'
        + "<!-- legend:2 -->\n- Keep — Koru\n\n"
        + tilde
        + "\nSon.\n"
    )
    stripped = strip_legends(doc)
    assert code in stripped and tilde in stripped  # byte-identical, blank lines included
    assert "\n\n\n\nÜç boş satırdan sonra." in stripped  # blank runs elsewhere are not collapsed
    assert "Başla" not in stripped and "Koru" not in stripped
    assert "Şekil 1: Akış.\n\n```python" in stripped  # one blank line at the removed block
    assert stripped.endswith("Son.\n")

    only_first = strip_legends(doc, [1])  # per figure (CR-57)
    assert "Başla" not in only_first and "- Keep — Koru" in only_first
    assert legend_ids(only_first) == [2] and legend_ids(doc) == [1, 2]
    assert strip_legends(doc, []) == doc

    rewritten = rewrite_image_sources(doc, {1: "images/p002-f01.tr.png", 7: "images/x.png"})
    assert '<!-- image:1 src="images/p002-f01.tr.png" -->' in rewritten
    assert '<!-- image:2 src="images/p003-f01.png" -->' in rewritten
    assert tilde in rewritten  # the image line inside the fence is code, not a figure
