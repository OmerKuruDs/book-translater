"""Overlay unit extraction (design doc 06, §3.6.1, §4.4; BA US-18..US-21, E-39..E-49)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from book_translator.domain.models import content_hash_of
from book_translator.domain.overlay import (
    OverlayAlignment,
    OverlayBlock,
    OverlayBlockKind,
    OverlayExtraction,
    format_block_id,
)
from book_translator.domain.result import Err, ErrorCode, unwrap
from book_translator.extractors.base import ExtractOptions
from book_translator.extractors.cleanup import (
    BBox,
    Line,
    Span,
    detect_alignment,
    detect_prefix_style,
    detect_style,
    first_line_indent,
    font_family,
    group_lines,
    has_letters,
    split_overlapping_rects,
    split_paragraphs,
)
from book_translator.extractors.overlay_extractor import (
    OVERLAP_KEPT_WARNING,
    OverlayOptions,
    check_modifiable,
    extract_overlay,
    overlay_signature,
)
from book_translator.extractors.pymupdf_extractor import PyMuPDFExtractor
from tests.conftest import (
    OVERLAY_CENTERED,
    OVERLAY_FRAGMENT,
    OVERLAY_HEADER,
    OVERLAY_ITALIC,
    TEST_DOC_PDF,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _figure_regions(pdf: Path) -> Dict[int, List[BBox]]:
    doc = unwrap(PyMuPDFExtractor().extract(pdf, ExtractOptions(min_chars_per_page=0)))
    regions: Dict[int, List[BBox]] = {}
    for figure in doc.figures:
        regions.setdefault(figure.page, []).append(figure.region)
    return regions


MATH_DOC_PDF = TEST_DOC_PDF.parent / "test-2.pdf"
"""A LaTeX book page set (Szeliski, "Computer Vision") full of display mathematics."""


@pytest.fixture(scope="module")
def test_doc() -> OverlayExtraction:
    return unwrap(extract_overlay(TEST_DOC_PDF, OverlayOptions(), _figure_regions(TEST_DOC_PDF)))


@pytest.fixture(scope="module")
def math_doc() -> OverlayExtraction:
    return unwrap(extract_overlay(MATH_DOC_PDF, OverlayOptions(), None))


def _page(extraction: OverlayExtraction, page: int) -> List[OverlayBlock]:
    return [b for b in extraction.blocks if b.page == page]


def _by_text(blocks: Sequence[OverlayBlock], start: str) -> OverlayBlock:
    (found,) = [b for b in blocks if b.source_text.startswith(start)]
    return found


def _line(text: str, x0: float, y0: float, x1: float, *, size: float = 10.0,
          bold: bool = False, italic: bool = False, font: str = "Times") -> Line:
    bbox: BBox = (x0, y0, x1, y0 + size * 1.2)
    return Line((Span(text, size, font, bold, False, False, bbox, italic=italic),), bbox)


# ---------------------------------------------------------------------------
# docs/test_doc.pdf page 2 (E6): grouping, kinds, figure labels, caption
# ---------------------------------------------------------------------------


def test_page_two_unit_kinds(test_doc: OverlayExtraction) -> None:
    blocks = _page(test_doc, 2)
    kinds = Counter(b.kind for b in blocks)
    assert kinds == {
        OverlayBlockKind.FIGURE_LABEL: 15,
        OverlayBlockKind.HEADER: 2,
        OverlayBlockKind.PAGE_NUMBER: 1,
        OverlayBlockKind.CAPTION: 1,
    }
    assert [p.block_count for p in test_doc.pages] == [13, 19, 11]
    assert all(p.skip_reason is None and p.has_text_layer for p in test_doc.pages)


def test_boxes_13_and_14_are_two_units(test_doc: OverlayExtraction) -> None:
    """E6 / E-39: overlapping label lines of neighbouring boxes never merge into one unit."""
    blocks = _page(test_doc, 2)
    thirteen = _by_text(blocks, "13.")
    fourteen = _by_text(blocks, "14.")
    assert thirteen.source_text == "13. 3D reconstruction"
    assert fourteen.source_text == "14. Image-based rendering"
    assert thirteen.unit_id != fourteen.unit_id
    assert len(thirteen.line_boxes) == 2 and len(fourteen.line_boxes) == 2
    assert thirteen.style.prefix_style == ("13.", True, False)  # E-46 / Q22
    assert not thirteen.style.bold  # dominant style is the regular text
    # interleaved columns (4 / 3, 11 / 10) still give one unit per box
    assert _by_text(blocks, "4.").source_text == "4. Model fitting and optimization"
    assert _by_text(blocks, "11.").source_text == "11. Structure from motion and SLAM"
    assert _by_text(blocks, "10.").source_text == "10. Computational photography"


def test_running_header_is_three_units(test_doc: OverlayExtraction) -> None:
    blocks = _page(test_doc, 2)
    header = [(b.source_text, b.kind, b.translate, b.keep_reason) for b in blocks[:3]]
    assert header == [
        ("1.3", OverlayBlockKind.HEADER, False, "header_footer"),
        ("Book overview", OverlayBlockKind.HEADER, False, "header_footer"),
        ("19", OverlayBlockKind.PAGE_NUMBER, False, "page_number"),
    ]
    # the same "1.3" + "Book overview" pair in the body of page 1 is one heading unit
    heading = _by_text(_page(test_doc, 1), "1.3")
    assert heading.source_text == "1.3 Book overview"
    assert heading.kind is OverlayBlockKind.HEADING and heading.translate


def test_figure_labels_sit_inside_the_region_and_caption_is_one_unit(
    test_doc: OverlayExtraction,
) -> None:
    (region,) = _figure_regions(TEST_DOC_PDF)[2]
    labels = [b for b in _page(test_doc, 2) if b.kind is OverlayBlockKind.FIGURE_LABEL]
    for label in labels:
        assert region[0] <= label.bbox[0] and label.bbox[2] <= region[2], label.source_text
        assert region[1] <= label.bbox[1] and label.bbox[3] <= region[3], label.source_text
        assert label.translate and label.keep_reason is None
    boxed = _by_text(labels, "2. Image formation")
    assert boxed.alignment is OverlayAlignment.CENTER  # centred in its drawing box
    caption = _by_text(_page(test_doc, 2), "Figure 1.12")
    assert caption.kind is OverlayBlockKind.CAPTION
    assert caption.source_text.endswith("widely used in subsequent chapters.")
    assert "sub- sequent" not in caption.source_text and "-\n" not in caption.source_text
    assert caption.alignment is OverlayAlignment.JUSTIFY
    assert caption.bbox[1] >= region[3] - 1.0  # never inside the figure


def test_footnotes_and_fragments(test_doc: OverlayExtraction) -> None:
    footnote = _by_text(_page(test_doc, 1), "⁹For")  # the small leading digit is a marker
    assert footnote.kind is OverlayBlockKind.FOOTNOTE and footnote.translate  # E-44: inline digit
    last_body = [b for b in _page(test_doc, 3) if b.kind is OverlayBlockKind.BODY][-1]
    # E-42: the page ends in the middle of a sentence ("... the basic elements of")
    assert last_body.source_text.endswith("the basic elements of") and last_body.fragment


def test_unit_invariants_and_disjoint_rects(test_doc: OverlayExtraction) -> None:
    for position, block in enumerate(test_doc.blocks, start=1):
        assert block.unit_id == position
        assert block.block_id == format_block_id(block.page, block.index_on_page)
        assert block.char_count == len(block.source_text)
        assert block.content_hash == content_hash_of(block.source_text)
        assert block.translate == (block.keep_reason is None)
    for page in test_doc.pages:
        blocks = _page(test_doc, page.page)
        assert [b.index_on_page for b in blocks] == list(range(1, len(blocks) + 1))
        assert page.translatable_count == sum(1 for b in blocks if b.translate)
        for i, a in enumerate(blocks):
            for b in blocks[i + 1 :]:
                w = min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0])
                h = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
                assert w <= 0 or h <= 0.01, (a.block_id, b.block_id)  # R14: disjoint rects


def test_extraction_is_deterministic(test_doc: OverlayExtraction) -> None:
    again = unwrap(extract_overlay(TEST_DOC_PDF, OverlayOptions(), _figure_regions(TEST_DOC_PDF)))
    assert again == test_doc
    assert again.overlay_sha256 == test_doc.overlay_sha256
    assert overlay_signature(again.blocks, OverlayOptions()) == test_doc.overlay_sha256


def test_regions_are_detected_without_an_inventory(test_doc: OverlayExtraction) -> None:
    """``figure_regions=None`` (fresh ``translate --mode overlay``) equals the inventory run."""
    auto = unwrap(extract_overlay(TEST_DOC_PDF, OverlayOptions(), None))
    assert auto.overlay_sha256 == test_doc.overlay_sha256
    no_regions = unwrap(extract_overlay(TEST_DOC_PDF, OverlayOptions(), {}))
    assert not any(b.kind is OverlayBlockKind.FIGURE_LABEL for b in no_regions.blocks)


def test_translate_decision_follows_the_options() -> None:
    regions = _figure_regions(TEST_DOC_PDF)
    headers = unwrap(extract_overlay(TEST_DOC_PDF, OverlayOptions(translate_headers=True), regions))
    decisions = {b.source_text: (b.translate, b.keep_reason) for b in _page(headers, 2)[:3]}
    assert decisions == {
        "1.3": (False, "no_letters"),  # FR-46: no letter -> never
        "Book overview": (True, None),
        "19": (False, "page_number"),  # page numbers never
    }
    kept = unwrap(extract_overlay(TEST_DOC_PDF, OverlayOptions(keep_figure_text=True), regions))
    labels = [b for b in kept.blocks if b.kind is OverlayBlockKind.FIGURE_LABEL]
    assert len(labels) == 15
    assert all(not b.translate and b.keep_reason == "figure_text" for b in labels)
    default = unwrap(extract_overlay(TEST_DOC_PDF, OverlayOptions(), regions))
    assert len({headers.overlay_sha256, kept.overlay_sha256, default.overlay_sha256}) == 3


# ---------------------------------------------------------------------------
# overlay_styles fixture: style, alignment, prefix, hyphen, fragment, footnote
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def styles(overlay_pdfs: Dict[str, Path]) -> OverlayExtraction:
    return unwrap(extract_overlay(overlay_pdfs["overlay_styles"], OverlayOptions(), None))


def test_styles_and_alignment_on_the_fixture(styles: OverlayExtraction) -> None:
    page = _page(styles, 1)
    heading = _by_text(page, "Chapter One")
    assert heading.kind is OverlayBlockKind.HEADING
    assert heading.style.bold and heading.style.family == "sans" and heading.style.font_size == 16
    justified = _by_text(page, "The committee")
    assert justified.alignment is OverlayAlignment.JUSTIFY and len(justified.line_boxes) >= 4
    assert justified.style.family == "serif" and justified.style.color == "#000000"
    centred = _by_text(page, "A centred epigraph")
    assert centred.alignment is OverlayAlignment.CENTER
    assert centred.source_text == OVERLAY_CENTERED.replace("\n", " ")
    assert _by_text(page, "Signed by").alignment is OverlayAlignment.RIGHT
    italic = _by_text(page, OVERLAY_ITALIC[:12])
    assert italic.style.italic and not italic.style.bold
    assert italic.alignment is OverlayAlignment.LEFT


def test_prefix_hyphen_footnote_and_fragment(styles: OverlayExtraction) -> None:
    page = _page(styles, 1)
    item = _by_text(page, "1. First item")
    assert item.style.prefix_style == ("1.", True, False) and not item.style.bold
    hyphen = _by_text(page, "The investigation")
    assert hyphen.source_text == "The investigation was remarkable in every single respect."  # E-43
    footnote = _by_text(page, "1 A footnote")
    assert footnote.kind is OverlayBlockKind.FOOTNOTE and footnote.style.font_size == 7
    fragment = _by_text(page, OVERLAY_FRAGMENT)
    assert fragment.fragment  # E-42: no terminal punctuation at the end of the page
    continuation = _by_text(_page(styles, 2), "page and it ends")
    # CR-47: the continuation half (lower-case start) is tagged as well, so both halves of
    # the split paragraph reach the review list (there is no cross-page merge: own unit)
    assert continuation.fragment
    assert not _by_text(page, "The committee").fragment


def test_running_header_and_page_numbers_on_the_fixture(styles: OverlayExtraction) -> None:
    headers = [b for b in styles.blocks if b.source_text == OVERLAY_HEADER]
    assert len(headers) == 3
    assert all(b.kind is OverlayBlockKind.HEADER and not b.translate for b in headers)
    numbers = [b for b in styles.blocks if b.kind is OverlayBlockKind.PAGE_NUMBER]
    assert [b.source_text for b in numbers] == ["7", "8", "9"]
    assert all(b.kind is OverlayBlockKind.FOOTER or b.keep_reason == "page_number" for b in numbers)


def test_page_without_text_layer_is_skipped(overlay_pdfs: Dict[str, Path]) -> None:
    extraction = unwrap(extract_overlay(overlay_pdfs["overlay_no_text"], OverlayOptions(), None))
    assert [(p.page, p.skip_reason, p.has_text_layer, p.block_count) for p in extraction.pages] == [
        (1, None, True, 1),
        (2, "no_text_layer", False, 0),
        (3, None, True, 1),
    ]
    assert {b.page for b in extraction.blocks} == {1, 3}


def test_table_cells_are_one_unit_per_cell(overlay_pdfs: Dict[str, Path]) -> None:
    """E-45: ``find_tables`` cells; a number-only cell is kept (``no_letters``)."""
    extraction = unwrap(extract_overlay(overlay_pdfs["table_page"], OverlayOptions(), None))
    cells = [b for b in extraction.blocks if b.kind is OverlayBlockKind.TABLE_CELL]
    assert [(c.source_text, c.translate, c.keep_reason) for c in cells] == [
        ("Name", True, None),
        ("Value", True, None),
        ("alpha", True, None),
        ("1", False, "no_letters"),
    ]
    off = unwrap(
        extract_overlay(overlay_pdfs["table_page"], OverlayOptions(detect_tables=False), None)
    )
    assert not any(b.kind is OverlayBlockKind.TABLE_CELL for b in off.blocks)


def test_progress_callback_counts_pages(overlay_pdfs: Dict[str, Path]) -> None:
    seen: List[Tuple[int, int]] = []
    unwrap(
        extract_overlay(
            overlay_pdfs["overlay_styles"], OverlayOptions(), None,
            progress=lambda done, total: seen.append((done, total)),
        )
    )
    assert seen == [(1, 3), (2, 3), (3, 3)]


# ---------------------------------------------------------------------------
# E-49: check_modifiable
# ---------------------------------------------------------------------------


def test_check_modifiable(overlay_pdfs: Dict[str, Path], tmp_path: Path) -> None:
    assert unwrap(check_modifiable(TEST_DOC_PDF)) is None
    assert unwrap(check_modifiable(overlay_pdfs["overlay_styles"])) is None
    restricted = check_modifiable(overlay_pdfs["overlay_encrypted"])
    assert isinstance(restricted, Err)
    assert restricted.error.code is ErrorCode.OVERLAY_PDF_RESTRICTED
    assert "--mode reflow" in restricted.error.message
    missing = check_modifiable(tmp_path / "nope.pdf")
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.INPUT_NOT_FOUND
    bad = extract_overlay(tmp_path / "nope.pdf", OverlayOptions(), None)
    assert isinstance(bad, Err) and bad.error.code is ErrorCode.INPUT_NOT_FOUND


# ---------------------------------------------------------------------------
# Pure heuristics (cleanup.py)
# ---------------------------------------------------------------------------


def test_group_lines_vertical_succession_only() -> None:
    header = [_line("1.3", 57, 44, 70), _line("Book overview", 81, 44, 148),
              _line("19", 526, 44, 537)]
    assert [len(g) for g in group_lines(header)] == [1, 1, 1]
    # two label columns whose second lines overlap vertically by 0.4 pt (E6)
    labels = [
        _line("13. 3D", 416, 535.1, 455, size=12),
        _line("reconstruction", 397, 549.2, 471, size=12),
        _line("14. Image-based", 233, 560.1, 322, size=12),
        _line("rendering", 251, 574.2, 301, size=12),
    ]
    groups = group_lines(labels)
    assert [[ln.text for ln in g] for g in groups] == [
        ["13. 3D", "reconstruction"], ["14. Image-based", "rendering"],
    ]
    # a size change or a paragraph gap starts a new unit
    mixed = [_line("Heading", 40, 100, 200, size=16), _line("body text here", 40, 121, 300)]
    assert len(group_lines(mixed)) == 2
    gap = [_line("first paragraph", 40, 100, 300), _line("second paragraph", 40, 130, 300)]
    assert len(group_lines(gap)) == 2


def _paragraph(first: str, y0: float, full_lines: int, last_x1: float, *, indent: float = 16.0,
               last: str = "the end of it.") -> List[Line]:
    """An indented, justified paragraph: first line indented, full lines, short last line."""
    lines = [_line(first, 100.0 + indent, y0, 400.0)]
    for i in range(full_lines):
        lines.append(_line("continues across the full measure of the column", 100.0,
                           y0 + 13.0 * (i + 1), 400.0))
    lines.append(_line(last, 100.0, y0 + 13.0 * (full_lines + 1), last_x1))
    return lines


def test_paragraphs_split_at_first_line_indents() -> None:
    lines = (
        _paragraph("First paragraph opens here", 100.0, 2, 220.0)
        + _paragraph("Second paragraph opens here", 152.0, 1, 180.0)
        + _paragraph("Third paragraph opens here", 191.0, 3, 300.0)
    )
    assert len(group_lines(lines)) == 1  # no vertical gap: one group without the split
    groups = group_lines(lines, paragraphs=True)
    assert [len(g) for g in groups] == [4, 3, 5]
    assert [g[0].text for g in groups] == [
        "First paragraph opens here", "Second paragraph opens here", "Third paragraph opens here",
    ]
    for group in groups:
        boxes = [ln.bbox for ln in group]
        assert detect_alignment(boxes) is OverlayAlignment.JUSTIFY
        assert first_line_indent(boxes) == pytest.approx(16.0)


def test_short_last_line_handling() -> None:
    # the last line of a paragraph nearly reaches the right edge (gap < 2 em): the indent of
    # the next line together with the terminal punctuation still splits
    full_last = _paragraph("Opening line of the text", 100.0, 1, 396.0) + _paragraph(
        "Next paragraph starts here", 139.0, 1, 250.0)
    assert [len(g) for g in split_paragraphs(full_last)] == [3, 3]
    # block paragraphs (no indent): short last line + terminal punctuation + capital start
    block = _paragraph("Block paragraph one begins", 100.0, 2, 200.0, indent=0.0) + _paragraph(
        "Block paragraph two begins", 152.0, 2, 260.0, indent=0.0)
    assert [len(g) for g in split_paragraphs(block)] == [4, 4]
    # ... but a short line that does not end a sentence stays in its paragraph
    no_terminal = _paragraph("Block paragraph one begins", 100.0, 2, 200.0, indent=0.0,
                             last="a short line without an end") + _paragraph(
        "Block paragraph two begins", 152.0, 2, 260.0, indent=0.0)
    assert len(split_paragraphs(no_terminal)) == 1
    # ragged (left-aligned) text is never split at sentence ends
    ragged = [
        _line("Ragged text ends early.", 100.0, 100.0, 250.0),
        _line("Another sentence also ends early.", 100.0, 113.0, 300.0),
        _line("A third one that is short.", 100.0, 126.0, 270.0),
        _line("And the longest line of the whole block here", 100.0, 139.0, 400.0),
    ]
    assert len(split_paragraphs(ragged)) == 1
    # centred label lines and hanging list items keep their unit
    centred = [_line("14. Image-based", 233.0, 560.0, 322.0, size=12),
               _line("rendering.", 251.0, 574.0, 304.0, size=12),
               _line("And more text", 240.0, 588.0, 315.0, size=12)]
    assert len(split_paragraphs(centred)) == 1
    hanging = [_line("1. A list item that wraps.", 100.0, 100.0, 400.0),
               _line("Its continuation is indented", 114.0, 113.0, 300.0)]
    assert len(split_paragraphs(hanging)) == 1
    # alignment: the first-line indent is tolerated, two lines are not enough for JUSTIFY
    assert detect_alignment([(116.0, 0.0, 400.0, 12.0), (100.0, 13.0, 400.0, 25.0)]) is (
        OverlayAlignment.LEFT  # not RIGHT
    )
    assert detect_alignment(
        [(116.0, 0.0, 400.0, 12.0), (100.0, 13.0, 399.2, 25.0), (100.0, 26.0, 400.0, 38.0)]
    ) is OverlayAlignment.JUSTIFY
    assert first_line_indent([(280.0, 0.0, 400.0, 12.0), (100.0, 13.0, 400.0, 25.0)]) == 0.0


def test_test_doc_body_paragraphs_are_justified_units(test_doc: OverlayExtraction) -> None:
    for page_no, expected in ((1, 8), (3, 7)):
        bodies = [b for b in _page(test_doc, page_no) if b.kind is OverlayBlockKind.BODY]
        assert len(bodies) == expected
        assert all(b.alignment is OverlayAlignment.JUSTIFY for b in bodies)
    # an indented paragraph opening with "Figure 1.12 shows ..." is running text
    opener = _by_text(_page(test_doc, 1), "Figure 1.12 shows")
    assert opener.kind is OverlayBlockKind.BODY
    assert opener.line_boxes[0][0] - opener.bbox[0] == pytest.approx(15.9, abs=0.5)
    # the superscript footnote marker travels as a Unicode superscript digit
    assert opener.source_text.endswith(".\u2079")
    assert any(b.source_text.endswith(".\u00b9\u2070") for b in _page(test_doc, 3))


def _disjoint(a: BBox, b: BBox) -> bool:
    """No shared area worth a point: what two painted rects must never have."""
    return (
        min(a[2], b[2]) - max(a[0], b[0]) <= 0.5 or min(a[3], b[3]) - max(a[1], b[1]) <= 0.5
    )


def test_split_overlapping_rects() -> None:
    upper: BBox = (100.0, 100.0, 200.0, 114.5)
    lower: BBox = (100.0, 114.1, 200.0, 128.0)
    (a, b), red, dropped = split_overlapping_rects([upper, lower])
    assert a[3] == pytest.approx(114.3) and b[1] == pytest.approx(114.3) and dropped == []
    assert red == [upper, lower]  # two painted rects: redaction keeps the original extent
    side: BBox = (210.0, 100.0, 300.0, 114.5)
    # no x overlap: nothing to split, and redaction follows the placement rects
    assert split_overlapping_rects([upper, side]) == ([upper, side], [upper, side], [])


def test_split_overlapping_rects_splits_a_large_crossing() -> None:
    """A rect that crosses another one over most of its height is two texts printed on top
    of each other, not side-by-side text: it is split at the middle of the overlap like any
    other crossing. Before CR-overlap this case was left alone, which is the bug."""
    upper: BBox = (100.0, 100.0, 200.0, 114.5)
    stacked: BBox = (100.0, 105.0, 200.0, 119.0)
    (a, b), red, dropped = split_overlapping_rects([upper, stacked])
    assert a[3] == pytest.approx(109.75) and b[1] == pytest.approx(109.75)
    assert dropped == [] and _disjoint(a, b)
    assert red == [upper, stacked]


def test_split_overlapping_rects_shortens_the_container() -> None:
    """docs/test-2.pdf p.19: the paragraph rect (#44) swallows the display equation (#45),
    so it is cut back to where the equation begins - the side the contained rect sits on."""
    paragraph: BBox = (114.7, 444.6, 536.7, 513.4)
    equation: BBox = (218.5, 496.5, 296.5, 513.4)
    (cut, kept), red, dropped = split_overlapping_rects([paragraph, equation])
    assert cut == (114.7, 444.6, 536.7, pytest.approx(496.5))
    assert kept == equation and dropped == [] and _disjoint(cut, kept)
    # both are painted here, so the paragraph is still redacted over its whole extent
    assert red == [paragraph, equation]


def test_split_overlapping_rects_leaves_real_columns_alone() -> None:
    """Two columns of a table row overlap by a sliver of width: their rows are *not*
    touched (only the sliver is halved), or every table would lose its rows."""
    left: BBox = (57.0, 100.0, 280.0, 400.0)
    right: BBox = (278.0, 100.0, 500.0, 400.0)
    (a, b), red, dropped = split_overlapping_rects([left, right])
    assert (a[1], a[3]) == (100.0, 400.0) and (b[1], b[3]) == (100.0, 400.0)
    assert a[2] == pytest.approx(279.0) and b[0] == pytest.approx(279.0)
    assert dropped == [] and _disjoint(a, b)
    assert red == [left, right]


def test_split_overlapping_rects_trims_the_painted_side_of_a_sliver() -> None:
    """A painted rect beside a kept one: the sliver they share is taken off the painted
    rect alone, because the kept rect is the one whose glyphs stay on the page."""
    painted_rect: BBox = (57.0, 100.0, 280.0, 400.0)
    kept: BBox = (278.0, 100.0, 500.0, 400.0)
    rects, red, dropped = split_overlapping_rects([painted_rect, kept], painted=[True, False])
    assert rects == [(57.0, 100.0, pytest.approx(278.0), 400.0), kept] and dropped == []
    # a kept neighbour shortens the redaction rect too - its glyphs must survive
    assert red == rects


def test_split_overlapping_rects_never_moves_a_kept_rect() -> None:
    """A kept rect (a math band, a page number) is not painted and never gives way: the
    painted neighbour is the one that is cut back."""
    paragraph: BBox = (100.0, 100.0, 400.0, 200.0)
    band: BBox = (150.0, 160.0, 300.0, 200.0)
    rects, red, dropped = split_overlapping_rects(
        [paragraph, band], painted=[True, False], min_heights=[6.0, 6.0]
    )
    assert rects == [(100.0, 100.0, 400.0, pytest.approx(160.0)), band] and dropped == []
    assert red == rects  # the band is never redacted, so the paragraph stops above it


def test_split_overlapping_rects_keeps_the_smaller_unit_when_the_cut_would_crush() -> None:
    """Cutting the container would leave it below one line, so nothing is cut: the smaller
    rect is reported unpaintable and keeps its source text instead."""
    paragraph: BBox = (100.0, 100.0, 400.0, 130.0)
    fragment: BBox = (150.0, 102.0, 200.0, 129.0)
    rects, red, dropped = split_overlapping_rects(
        [paragraph, fragment], painted=[True, True], min_heights=[12.0, 12.0]
    )
    assert rects == [paragraph, fragment]  # untouched
    assert dropped == [1]  # the smaller of the two is the one that is not painted
    # the dropped unit is never redacted itself (it carries its own rect, never a wider
    # one); the paragraph cannot give way to it without exposing its own glyphs, so it
    # redacts exactly what it paints - the behaviour before the two rects were split
    assert red == [paragraph, fragment]


def test_redaction_rect_keeps_the_strip_a_painted_neighbour_took() -> None:
    """The bug this split exists for: a block shortened against a *painted* neighbour used
    to be redacted over the shortened rect, leaving its source glyphs in the strip that was
    cut away. The placement rect still gives way; the redaction rect does not."""
    upper: BBox = (115.0, 340.0, 325.0, 380.0)  # translated, cut back by the wide one
    lower: BBox = (115.0, 367.0, 537.0, 380.0)
    (pa, pb), (ra, rb), dropped = split_overlapping_rects(
        [upper, lower], painted=[True, True], min_heights=[6.0, 6.0]
    )
    assert dropped == [] and _disjoint(pa, pb)  # placement rects still pulled apart
    assert pa[3] < upper[3] or pb[1] > lower[1]
    assert (ra, rb) == (upper, lower)  # redaction covers every source glyph of both


def test_redaction_rect_still_gives_way_to_a_kept_unit() -> None:
    """A display equation is kept (``keep_reason="math"``), so its glyphs must survive. The
    redaction rect of the paragraph around it is cut back exactly like the placement rect -
    redacting the band would wipe the equation off the page."""
    paragraph: BBox = (114.7, 444.6, 536.7, 513.4)
    band: BBox = (218.5, 496.5, 296.5, 513.4)
    placement, redaction, dropped = split_overlapping_rects(
        [paragraph, band], painted=[True, False], min_heights=[6.0, 6.0]
    )
    assert dropped == []
    assert redaction == placement == [(114.7, 444.6, 536.7, pytest.approx(496.5)), band]
    assert _disjoint(redaction[0], band)


def test_redaction_rect_widens_only_against_painted_neighbours() -> None:
    """Both at once: a kept band above and a painted neighbour below. The placement rect
    stops at both; the redaction rect stops at the band only."""
    band: BBox = (100.0, 100.0, 400.0, 120.0)  # kept
    paragraph: BBox = (100.0, 110.0, 400.0, 200.0)  # painted, overlaps both
    neighbour: BBox = (100.0, 190.0, 400.0, 240.0)  # painted
    placement, redaction, dropped = split_overlapping_rects(
        [band, paragraph, neighbour], painted=[False, True, True], min_heights=[6.0, 6.0, 6.0]
    )
    assert dropped == []
    assert placement[1] == (100.0, pytest.approx(120.0), 400.0, pytest.approx(195.0))
    assert redaction[1] == (100.0, pytest.approx(120.0), 400.0, 200.0)
    assert redaction[0] == placement[0] == band  # the kept unit is never redacted


def test_alignment_rules() -> None:
    left = [(40.0, 0.0, 300.0, 12.0), (40.0, 12.0, 250.0, 24.0), (40.0, 24.0, 280.0, 36.0)]
    assert detect_alignment(left) is OverlayAlignment.LEFT
    justified = [(40.0, float(i * 12), 300.0, float(i * 12 + 12)) for i in range(3)]
    justified.append((40.0, 36.0, 180.0, 48.0))
    assert detect_alignment(justified) is OverlayAlignment.JUSTIFY
    # three lines are enough: two full lines + the short last one
    assert detect_alignment(justified[:2] + [justified[-1]]) is OverlayAlignment.JUSTIFY
    assert detect_alignment(justified[:1] + [justified[-1]]) is OverlayAlignment.LEFT  # < 3 lines
    centred = [(100.0, 0.0, 300.0, 12.0), (150.0, 12.0, 250.0, 24.0)]
    assert detect_alignment(centred) is OverlayAlignment.CENTER
    right = [(100.0, 0.0, 300.0, 12.0), (180.0, 12.0, 300.0, 24.0)]
    assert detect_alignment(right) is OverlayAlignment.RIGHT
    single: BBox = (120.0, 10.0, 180.0, 22.0)
    assert detect_alignment([single], (100.0, 0.0, 200.0, 30.0)) is OverlayAlignment.CENTER
    assert detect_alignment([single], (110.0, 0.0, 300.0, 30.0)) is OverlayAlignment.LEFT
    assert detect_alignment([single]) is OverlayAlignment.LEFT
    assert detect_alignment([]) is OverlayAlignment.LEFT


def test_style_and_prefix_detection() -> None:
    bbox: BBox = (0.0, 0.0, 100.0, 12.0)
    line = Line(
        (
            Span("13.", 13.0, "Times,Bold", True, False, False, bbox, color="#112233"),
            Span(" 3D reconstruction", 13.0, "Times", False, False, False, bbox, color="#112233"),
        ),
        bbox,
    )
    style = detect_style([line])
    assert (style.font_size, style.bold, style.italic) == (13.0, False, False)
    assert style.family == "serif"
    assert style.color == "#112233" and style.prefix_style == ("13.", True, False)
    assert detect_prefix_style([_line("No prefix here", 0, 0, 90)]) is None
    long_prefix = Line(
        (Span("Figure 1.12", 10, "X-Bold", True, False, False, bbox),
         Span(" caption", 10, "X", False, False, False, bbox)),
        bbox,
    )
    assert detect_prefix_style([long_prefix]) is None  # prefix longer than six characters
    assert font_family("Arial-BoldMT") == "sans" and font_family("Courier New") == "mono"
    assert font_family("NimbusRomNo9L-Regu") == "serif"
    assert detect_style([_line("x", 0, 0, 5, italic=True)]).italic
    assert has_letters("Şekil 3") and not has_letters("1.3") and not has_letters("© 19")


# ---------------------------------------------------------------------------
# v1.1 review: CR-40 (band lines), CR-41 (lists), CR-47 (fragments), CR-53 (rotated pages)
# ---------------------------------------------------------------------------

_WORDS = (
    "geometry appearance images pipeline stages interact subtle reconstruction process careful "
    "evaluation matters practical deployment camera sensors robust estimation methods features"
).split()


def _word_lines(count: int, seed: int) -> List[str]:
    import random

    rng = random.Random(seed)
    lines: List[str] = []
    for _ in range(count):
        text = ""
        while len(text) < 40:
            text += rng.choice(_WORDS) + " "
        lines.append(text.strip())
    return lines


def build_band_one_off(path: Path) -> Path:
    """Four pages with a repeating header + page number; page 1 also holds "Key idea", a
    short line that occurs once, inside the top 12 % band (y = 79 of 842)."""
    import pymupdf

    doc = pymupdf.open()
    for number in range(1, 5):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 50), "My Running Header", fontsize=9, fontname="helv")
        page.insert_text((500, 50), str(number), fontsize=9, fontname="helv")
        if number == 1:
            page.insert_text((72, 90), "Key idea", fontsize=10, fontname="helv")
        for index, text in enumerate(_word_lines(6, number)):
            page.insert_text((72, 140 + index * 13), text + ".", fontsize=10, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


def build_column_orphans(path: Path) -> Path:
    """Two columns per page; each column ends with a one-line orphan (the first line of the
    next paragraph) at y = 748 of 842 - inside the bottom band, different on every page."""
    import pymupdf

    doc = pymupdf.open()
    for number in range(3):
        page = doc.new_page(width=595, height=842)
        for column_x in (72, 310):
            lines = _word_lines(44, number * 10 + column_x)
            lines[-2] += "."
            lines[-1] = " ".join(lines[-1].split()[:4]).capitalize()
            for index, text in enumerate(lines):
                y = 190 + index * 13 + (10 if index == len(lines) - 1 else 0)
                page.insert_text((column_x, y), text, fontsize=10, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


def test_one_off_short_line_in_the_band_stays_translatable(tmp_path: Path) -> None:
    """CR-40: only a repeating pattern (or a row shared with the page number) makes a
    header. "Key idea" used to be ``header / translate=False`` and was listed nowhere."""
    extraction = unwrap(extract_overlay(build_band_one_off(tmp_path / "band.pdf"),
                                        OverlayOptions(), {}))
    key_idea = _by_text(_page(extraction, 1), "Key idea")
    assert key_idea.translate and key_idea.keep_reason is None
    assert key_idea.kind in (OverlayBlockKind.BODY, OverlayBlockKind.HEADING)
    assert key_idea.warnings == ("header_guess",)  # the judgement call is visible
    assert f"{key_idea.block_id}:header_guess" in extraction.warnings
    for number in range(1, 5):  # the real running header and the page numbers are kept
        header = _by_text(_page(extraction, number), "My Running Header")
        assert header.kind is OverlayBlockKind.HEADER and header.keep_reason == "header_footer"
        assert _by_text(_page(extraction, number), str(number)).kind is OverlayBlockKind.PAGE_NUMBER


def test_column_bottom_orphan_lines_are_body_text(tmp_path: Path) -> None:
    """CR-40: six different single lines at the bottom of the columns were ``footer``."""
    extraction = unwrap(extract_overlay(build_column_orphans(tmp_path / "cols.pdf"),
                                        OverlayOptions(), {}))
    footers = [b for b in extraction.blocks if b.kind is OverlayBlockKind.FOOTER]
    assert footers == []
    orphans = [b for b in extraction.blocks if len(b.line_boxes) == 1 and b.bbox[1] > 740]
    assert len(orphans) == 6
    assert all(b.translate and b.kind is OverlayBlockKind.BODY for b in orphans)
    assert all("header_guess" not in b.warnings for b in orphans)  # they sit on their column


def test_test_doc_page_two_header_shares_its_row_with_the_page_number(
    test_doc: OverlayExtraction,
) -> None:
    """CR-40 keeps the "1.3 / Book overview / 19" row: not a repeating pattern, but the
    short lines share their text row with the page number."""
    top = [b for b in _page(test_doc, 2) if b.bbox[3] < 80]
    assert [(b.kind, b.source_text) for b in top] == [
        (OverlayBlockKind.HEADER, "1.3"),
        (OverlayBlockKind.HEADER, "Book overview"),
        (OverlayBlockKind.PAGE_NUMBER, "19"),
    ]
    assert all(not b.translate for b in top)
    assert not [w for w in test_doc.warnings if w.endswith("header_guess")]


def build_list_page(path: Path) -> Path:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 200.0
    for text in [
        "The following steps are required for calibration of the camera:",
        "• Capture a set of checkerboard images from many angles.",
        "• Detect the corners in every image with subpixel accuracy.",
        "• Solve for the intrinsic parameters and the distortion.",
        "1. First numbered step of the second procedure here.",
        "2. Second numbered step of the second procedure here.",
    ]:
        page.insert_text((72, y), text, fontsize=10, fontname="helv")
        y += 13
    doc.save(str(path))
    doc.close()
    return path


def test_list_items_are_one_unit_each(tmp_path: Path) -> None:
    """CR-41: intro sentence + 3 bullets + 2 ordinals used to be one BODY unit."""
    extraction = unwrap(extract_overlay(build_list_page(tmp_path / "list.pdf"),
                                        OverlayOptions(), {}))
    texts = [b.source_text for b in extraction.blocks]
    assert len(texts) == 6
    assert texts[0].startswith("The following steps") and texts[0].endswith("camera:")
    assert all(text[0] in "•·" for text in texts[1:4])  # helv stores the bullet as U+00B7
    assert texts[4].startswith("1. First") and texts[5].startswith("2. Second")
    assert all(len(b.line_boxes) == 1 and b.translate for b in extraction.blocks)


def test_split_paragraphs_list_markers_and_hanging_lines() -> None:
    """CR-41: a marker line opens a unit; wrapped (hanging) lines stay with their item; a
    number that merely starts a wrapped prose line does not split the paragraph."""
    intro = _mk_line("These are the steps of the procedure:", 72.0, 100.0, width=300.0)
    first = _mk_line("• a first item that is long enough to wrap onto", 72.0, 113.0, width=300.0)
    hang1 = _mk_line("a second line and even", 84.0, 126.0, width=288.0)
    hang2 = _mk_line("a third line.", 84.0, 139.0, width=80.0)
    second = _mk_line("• second item", 72.0, 152.0, width=90.0)
    third = _mk_line("3) third item", 72.0, 165.0, width=90.0)
    parts = split_paragraphs([intro, first, hang1, hang2, second, third])
    assert [[ln.text for ln in part] for part in parts] == [
        [intro.text], [first.text, hang1.text, hang2.text], [second.text], [third.text],
    ]
    # "12. The" at the start of a wrapped line of running prose is not a list item
    a = _mk_line("The details are given in the long discussion of Figure", 72.0, 300.0, width=300.0)
    b = _mk_line("12. The remaining lines of the paragraph continue here", 72.0, 313.0, width=300.0)
    c = _mk_line("until the paragraph finally ends.", 72.0, 326.0, width=180.0)
    assert len(split_paragraphs([a, b, c])) == 1


def _mk_line(text: str, x0: float, y0: float, *, width: float, size: float = 10.0) -> Line:
    bbox: BBox = (x0, y0, x0 + width, y0 + size * 1.2)
    return Line((Span(text, size, "Helvetica", False, False, False, bbox),), bbox)


def test_both_halves_of_a_split_paragraph_are_fragments(test_doc: OverlayExtraction) -> None:
    """CR-47: the paragraph that runs from page 1 to page 3 is tagged on both sides, and a
    unit that is never translated is never a fragment."""
    first_half = [b for b in _page(test_doc, 1) if b.kind is OverlayBlockKind.BODY][-1]
    assert first_half.source_text.endswith("still others as") and first_half.fragment
    continuation = [b for b in _page(test_doc, 3) if b.kind is OverlayBlockKind.BODY][0]
    assert continuation.source_text.startswith("open-ended research problems")
    assert continuation.fragment
    complete = _by_text(_page(test_doc, 3), "If the students or curriculum")
    assert not complete.fragment
    assert not [b.block_id for b in test_doc.blocks if b.fragment and not b.translate]


def build_rotated_page(path: Path, rotation: int) -> Path:
    """A landscape-stored page (``/Rotate``) whose text reads upright for the reader."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=400)
    page.set_rotation(rotation)
    for index, y in enumerate((80, 95, 110, 125)):
        page.insert_text(
            pymupdf.Point(50, y) * page.derotation_matrix,
            f"Upright body text line {index} of a landscape-stored page, long enough.",
            fontsize=10, rotate=rotation,
        )
    doc.save(str(path))
    doc.close()
    return path


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page_text_is_translatable_in_visible_coordinates(
    rotation: int, tmp_path: Path
) -> None:
    """CR-53: every line of such a page used to be ``keep_reason="rotated"``."""
    pdf = build_rotated_page(tmp_path / f"rot{rotation}.pdf", rotation)
    extraction = unwrap(extract_overlay(pdf, OverlayOptions(), {}))
    (info,) = extraction.pages
    assert info.rotation == rotation and info.skip_reason is None
    assert (info.width, info.height) == ((400.0, 600.0) if rotation != 180 else (600.0, 400.0))
    (unit,) = extraction.blocks
    assert unit.translate and unit.keep_reason is None and len(unit.line_boxes) == 4
    assert unit.source_text.startswith("Upright body text line 0")
    # stored in the orientation the reader sees: a wide box near the top left
    assert unit.bbox == pytest.approx((50.0, 69.0, 340.0, 128.0), abs=2.0)


def test_genuinely_rotated_line_on_an_upright_page_is_still_kept(tmp_path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 200), "Ordinary body text on an upright page.", fontsize=10)
    page.insert_text((40, 500), "Rotated axis label", fontsize=10, rotate=90)
    pdf = tmp_path / "axis.pdf"
    doc.save(str(pdf))
    doc.close()
    extraction = unwrap(extract_overlay(pdf, OverlayOptions(), {}))
    axis = _by_text(extraction.blocks, "Rotated axis label")
    assert not axis.translate and axis.keep_reason == "rotated" and not axis.fragment
    assert _by_text(extraction.blocks, "Ordinary body").translate


def test_label_boxes_carry_the_bold_running_number(tmp_path: Path) -> None:
    """CR-97: ``_label_boxes`` records the prefix style of a label, so the figure PNG can
    reproduce the bold "2." that the overlay PDF already keeps (E-46 / Q22)."""
    from book_translator.domain.models import FigureRecord
    from book_translator.domain.result import Ok
    from book_translator.extractors.base import FigureOptions

    written: List[Path] = []

    def sink(record: FigureRecord, png: bytes) -> object:
        target = tmp_path / Path(*record.file.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png)
        written.append(target)
        return Ok(target)

    doc = unwrap(PyMuPDFExtractor().extract(
        TEST_DOC_PDF,
        ExtractOptions(figures=FigureOptions(), min_chars_per_page=0),
        figure_sink=sink,
    ))
    boxes = {box.text: box for figure in doc.figures for box in figure.label_boxes}
    assert written and boxes
    numbered = boxes["2. Image formation"]
    assert numbered.prefix_style == ("2.", True, False)
    assert not numbered.bold  # the dominant run is regular; only the number is bold
    assert boxes["2D (what?)"].prefix_style is None  # no running number, no prefix


# ---------------------------------------------------------------------------
# Display (block) math bands: one kept unit per equation, never grouped through
# ---------------------------------------------------------------------------


def _span(text: str, bbox: BBox, font: str, size: float) -> Span:
    return Span(text, size, font, False, False, False, bbox)


def _math_line(
    text: str, x0: float, y0: float, x1: float, y1: float, *, font: str = "CMMI10"
) -> Line:
    """One line in a single font, with the box given outright: display math rows are far
    taller than their font size (a CMEX bracket spans the whole equation)."""
    bbox: BBox = (x0, y0, x1, y1)
    return Line((_span(text, bbox, font, 10.0),), bbox)


def _prose_line(text: str, x0: float, y0: float, x1: float) -> Line:
    return _line(text, x0, y0, x1, font="NimbusRomNo9L-Regu")


def _math_page(lines: Sequence[Line]) -> Tuple[Any, Any]:
    """A ``RawPage`` holding ``lines`` plus the context ``_build_units`` needs."""
    from book_translator.extractors.cleanup import RawBlock, RawPage
    from book_translator.extractors.overlay_extractor import _DocContext

    box = lines[0].bbox
    for line in lines[1:]:
        box = (min(box[0], line.bbox[0]), min(box[1], line.bbox[1]),
               max(box[2], line.bbox[2]), max(box[3], line.bbox[3]))
    raw = RawPage(1, 595.0, 790.0, (RawBlock(1, box, tuple(lines)),), (), ())
    ctx = _DocContext(OverlayOptions(detect_tables=False), 10.0, set(), set())
    return raw, ctx


def _units_of(lines: Sequence[Line]) -> List[Any]:
    from book_translator.extractors.overlay_extractor import _build_units

    raw, ctx = _math_page(lines)
    units, _figure, _table = _build_units(raw, None, [], ctx)  # type: ignore[arg-type]
    return sorted(units, key=lambda u: (u.bbox[1], u.bbox[0]))


def test_inline_math_in_a_prose_line_is_no_band() -> None:
    """The measured rule: a line with a word of 3+ letters in a text font is prose, however
    much mathematics it carries. "where Kk = diag(fk, fk, 1) is ..." must stay translatable
    (docs/test-2.pdf p.30, unit #715 - the paragraph that used to swallow equation 8.39)."""
    from book_translator.extractors.overlay_extractor import _is_math_line, _math_bands

    inline = Line(
        (
            _span("where K", (57.0, 268.0, 100.0, 281.0), "NimbusRomNo9L-Regu", 10.0),
            _span("k", (100.0, 271.0, 104.0, 281.0), "CMMI7", 7.0),
            _span(" = diag(f", (104.0, 268.0, 150.0, 281.0), "CMMI10", 10.0),
            _span(") is the camera intrinsic", (150.0, 268.0, 300.0, 281.0),
                  "NimbusRomNo9L-Regu", 10.0),
        ),
        (57.0, 268.0, 300.0, 281.0),
    )
    assert not _is_math_line(inline)
    raw, ctx = _math_page([inline])
    assert _math_bands([inline], raw, ctx) == []
    (unit,) = _units_of([inline])
    assert unit.keep_reason is None and unit.kind is OverlayBlockKind.BODY


def test_display_equation_is_one_kept_math_unit_with_its_number() -> None:
    """Every row of the equation, and the ``(8.39)`` at the right margin, end up in one
    unit that is never sent - so nothing inside it is translated and nothing redacts it."""
    rows = [
        _math_line("", 156.9, 298.8, 164.0, 338.4, font="CMEX10"),
        _math_line("x1", 164.0, 307.0, 174.3, 318.4),
        _math_line("y1", 164.4, 320.4, 173.8, 331.8),
        _math_line("1", 166.7, 333.8, 172.0, 344.4, font="CMR10"),
        _math_line("", 174.8, 298.8, 181.9, 338.4, font="CMEX10"),
        _math_line("R10", 249.6, 317.9, 276.0, 357.5, font="CMEX10"),
    ]
    number = _prose_line("(8.39)", 453.5, 318.5, 479.2)
    units = _units_of([*rows, number])
    assert len(units) == 1
    (band,) = units
    assert band.keep_reason == "math" and band.kind is OverlayBlockKind.BODY
    assert len(band.lines) == len(rows) + 1  # the equation number joined the band
    assert "(8.39)" in band.text
    assert band.bbox[0] <= 156.9 and band.bbox[2] >= 479.2  # the rect reaches the number
    # the bare "1" of the matrix row has no math font of its own; the band still keeps it
    assert any(ln.text == "1" for ln in band.lines)


def test_paragraphs_around_a_band_are_separate_units_that_never_cross_it() -> None:
    """The regression that motivated all of this: the paragraph above an equation and the
    one below it used to become a single group whose rect covered the equation, and the
    redaction of that rect wiped the display math off the page."""
    above = [
        _prose_line("where the simplified camera intrinsic matrix holds", 57.0, 267.9, 479.0),
        _prose_line("and the pixels are indexed from the image centre so", 57.0, 281.2, 479.0),
    ]
    band_rows = [
        _math_line("", 156.9, 298.8, 164.0, 338.4, font="CMEX10"),
        _math_line("x1", 164.0, 307.0, 174.3, 318.4),
        _math_line("", 174.8, 317.9, 181.9, 357.5, font="CMEX10"),
    ]
    below = [
        _prose_line("which reveals the simplicity of the mapping and", 57.0, 403.5, 479.0),
        _prose_line("makes the three-parameter motion model explicit.", 57.0, 416.9, 479.0),
    ]
    units = _units_of([*above, *band_rows, *below])
    bands = [u for u in units if u.keep_reason == "math"]
    prose = [u for u in units if u.keep_reason is None]
    assert len(bands) == 1 and len(prose) == 2  # never one group across the band
    assert prose[0].text.startswith("where the simplified")
    assert prose[1].text.startswith("which reveals")
    for unit in prose:
        for band in bands:
            assert (
                unit.bbox[3] <= band.bbox[1]
                or unit.bbox[1] >= band.bbox[3]
                or unit.bbox[2] <= band.bbox[0]
                or unit.bbox[0] >= band.bbox[2]
            ), "a translated rect reaching into a math band would redact the equation"


def test_math_band_on_the_real_document(math_doc: OverlayExtraction) -> None:
    """docs/test-2.pdf p.30 (Szeliski p.415): equations 8.39-8.42 survive as kept units and
    no translated rect on the page intersects one of them."""
    page = _page(math_doc, 30)
    bands = [b for b in page if b.keep_reason == "math"]
    assert len(bands) >= 2
    assert all(b.kind is OverlayBlockKind.BODY and not b.translate for b in bands)
    assert any("(8.39)" in b.source_text for b in bands)
    crossing = [
        (unit.block_id, band.block_id)
        for unit in page
        if unit.translate
        for band in bands
        if not (
            unit.bbox[3] <= band.bbox[1]
            or unit.bbox[1] >= band.bbox[3]
            or unit.bbox[2] <= band.bbox[0]
            or unit.bbox[0] >= band.bbox[2]
        )
    ]
    assert crossing == []


def _text_font_equation(number: str = "(8.5)", number_x0: float = 453.5) -> List[Line]:
    """docs/test-2.pdf p.19 (Szeliski p.404), equation 8.5: three prose rows over a display
    equation whose left half (``ELLS =``) is set in the *text* font ``CMR10``, so the row
    reads as prose and the band can only be found through the number at the right margin."""
    rows = [
        _prose_line("where J is the Jacobian of the transformation f", 114.7, 444.6, 479.0),
        _prose_line("p (see Table 8.1). In this case a linear regression", 114.7, 458.0, 479.0),
        _prose_line("can be formulated as", 114.7, 471.3, 479.0),
        _math_line("ELLS =", 163.7, 498.5, 198.4, 510.7, font="CMR10"),
        _math_line("∥J(xi)p −∆xi∥2", 218.5, 496.5, 296.5, 516.6,
                   font="CMSY10"),
    ]
    if number:
        rows.append(_prose_line(number, number_x0, 496.6, number_x0 + 25.5))
    return rows


def test_equation_number_at_the_right_margin_makes_a_band() -> None:
    """CR-overlap/2: ``(8.5)`` alone on its line and flush with the right text edge marks a
    display row that no font rule can see, because the equation is set in ``CMR``/``CMBX``."""
    from book_translator.extractors.overlay_extractor import _math_bands

    without = _text_font_equation(number="")
    raw, ctx = _math_page(without)
    assert _math_bands(without, raw, ctx) == []  # font rule alone: the row stays prose

    lines = _text_font_equation()
    raw, ctx = _math_page(lines)
    (band,) = _math_bands(lines, raw, ctx)
    assert {line.text for line in band.lines} == {"ELLS =", "∥J(xi)p −∆xi∥2",
                                                 "(8.5)"}
    units = _units_of(lines)
    (kept,) = [u for u in units if u.keep_reason == "math"]
    assert "(8.5)" in kept.text
    prose = [u for u in units if u.keep_reason is None]
    assert prose and all(u.bbox[3] <= band.y0 or u.bbox[1] >= band.y1 for u in prose)


def test_an_equation_reference_in_running_text_makes_no_band() -> None:
    """The false positive the rule has to avoid: "(8.5)" inside a sentence, and a bare
    "(8.5)" that sits in the text column instead of at the right margin."""
    from book_translator.extractors.overlay_extractor import _math_bands

    reference = [
        _prose_line("the residual of (8.5) is measured in pixels", 114.7, 444.6, 479.0),
        _prose_line("and the weights of (8.5) follow from it", 114.7, 458.0, 479.0),
        _prose_line("which is what Section 8.1 calls the error", 114.7, 471.3, 479.0),
    ]
    raw, ctx = _math_page(reference)
    assert _math_bands(reference, raw, ctx) == []
    indented = _text_font_equation(number_x0=250.0)  # a "(8.5)" nowhere near the margin
    raw, ctx = _math_page(indented)
    assert _math_bands(indented, raw, ctx) == []


def test_no_painted_rect_overlaps_another_on_the_real_document(
    math_doc: OverlayExtraction,
) -> None:
    """§4.4 step 4 as an invariant over docs/test-2.pdf (47 pages of display mathematics):
    no rect that is painted shares area with any other rect of its page. Page 19 (Szeliski
    p.404) is the page the invariant was written for - equation 8.5 inside the paragraph
    that flows around it."""
    per_page: Dict[int, List[OverlayBlock]] = {}
    for block in math_doc.blocks:
        per_page.setdefault(block.page, []).append(block)
    overlaps = [
        (a.block_id, b.block_id)
        for blocks in per_page.values()
        for index, a in enumerate(blocks)
        for b in blocks[index + 1 :]
        if (a.translate or b.translate) and not _disjoint(a.bbox, b.bbox)
    ]
    assert overlaps == []
    assert any(b.keep_reason == "math" and "(8.5)" in b.source_text for b in per_page[19])
    # the fallback of the invariant: a rect that could not be freed keeps its source text
    crushed = [b for b in math_doc.blocks if OVERLAP_KEPT_WARNING in b.warnings]
    assert crushed, "the fallback path is part of what keeps the invariant on this document"
    assert all(not b.translate and b.keep_reason == "no_bbox" for b in crushed)


def test_no_redaction_rect_reaches_a_kept_unit_on_the_real_document(
    math_doc: OverlayExtraction,
) -> None:
    """The companion invariant of the one above, for the *redaction* rect (docs/test-2.pdf).

    Every translated unit is cleared over at least its placement rect - otherwise its own
    English glyphs stay under the Turkish text - and never over a unit that keeps its
    source text, so no math band or page number is wiped. Page 19 (Szeliski p.404) is the
    page the split was written for: unit #33 there is shortened against #32 and must still
    be redacted over its original rows."""
    per_page: Dict[int, List[OverlayBlock]] = {}
    for block in math_doc.blocks:
        per_page.setdefault(block.page, []).append(block)
    widened: List[str] = []
    for blocks in per_page.values():
        for block in blocks:
            if not block.translate:
                assert block.redact_bbox is None, block.block_id
                continue
            rect = block.redact_bbox or block.bbox
            assert rect[0] <= block.bbox[0] and rect[1] <= block.bbox[1], block.block_id
            assert rect[2] >= block.bbox[2] and rect[3] >= block.bbox[3], block.block_id
            if block.redact_bbox is not None:
                widened.append(block.block_id)
            for other in blocks:
                if other is block or other.translate:
                    continue
                assert _disjoint(rect, other.bbox), f"{block.block_id} redacts {other.block_id}"
    assert widened, "no unit was shortened against a painted neighbour - the split is untested"
    page_19 = {b.block_id: b for b in per_page[19]}
    shortened = page_19["p019-b033"]
    assert shortened.redact_bbox is not None and shortened.redact_bbox[1] < shortened.bbox[1]


def test_a_document_without_math_fonts_has_no_bands(test_doc: OverlayExtraction) -> None:
    """No math font on the page -> no band, and the unit set is the one v1.1 always had."""
    assert not [b for b in test_doc.blocks if b.keep_reason == "math"]
    assert len(test_doc.blocks) == 43
    assert sum(1 for b in test_doc.blocks if b.translate) == 34


# ---------------------------------------------------------------------------
# Dot-leader (table of contents) rows: one record per row, never down the columns
# ---------------------------------------------------------------------------


def _toc_row(number: str, title: str, page: str, y0: float) -> List[Line]:
    """One contents row as the PDF reports it: three fragments on one baseline, the middle
    one carrying the dot leader (docs/test-2.pdf p.16, Szeliski p.401)."""
    return [
        _prose_line(number, 73.1, y0, 86.4),
        _prose_line(f"{title} " + ". " * 20, 97.5, y0, 452.6),
        _prose_line(page, 463.3, y0, 479.2),
    ]


def test_contents_rows_are_one_unit_per_row() -> None:
    """The bug: grouping runs down the columns, so the whole page-number column became one
    unit and three headings landed in another. Every row is its own record instead."""
    lines = [
        line
        for index, (number, title, page) in enumerate(
            [
                ("8.1", "Pairwise alignment", "403"),
                ("8.2", "Image stitching", "411"),
                ("8.3", "Global alignment", "421"),
                ("8.4", "Compositing", "426"),
                ("8.5", "Additional reading", "437"),
            ]
        )
        for line in _toc_row(number, title, page, 194.8 + index * 13.4)
    ]
    units = _units_of(lines)
    # the number joins the heading beside it (same row, run-in gap); the page number does not
    headings = [u for u in units if has_letters(u.text)]
    assert [u.text.split(" .")[0] for u in headings] == [
        "8.1 Pairwise alignment",
        "8.2 Image stitching",
        "8.3 Global alignment",
        "8.4 Compositing",
        "8.5 Additional reading",
    ]
    numbers = [u for u in units if not has_letters(u.text)]
    assert [u.text for u in numbers] == ["403", "411", "421", "426", "437"]
    # nothing spans two rows: every unit is one line tall, and no page number stacked up
    for unit in units:
        assert len(unit.lines) == 1 or {round(ln.bbox[1], 3) for ln in unit.lines} == {
            round(unit.lines[0].bbox[1], 3)
        }, unit.text
        assert unit.bbox[3] - unit.bbox[1] < 13.4, unit.text


def test_a_paragraph_page_is_not_touched_by_the_leader_rule() -> None:
    """Regression guard: no dot leader on the page -> the ordinary vertical grouping, so a
    paragraph is still one unit."""
    lines = [
        _prose_line("The alignment of two images is estimated from the", 73.1, 194.8, 452.6),
        _prose_line("correspondences between them, which the matcher", 73.1, 208.2, 452.6),
        _prose_line("produces from the detected feature points.", 73.1, 221.6, 400.0),
    ]
    (unit,) = _units_of(lines)
    assert len(unit.lines) == 3 and unit.keep_reason is None
    assert unit.text.startswith("The alignment") and unit.text.endswith("points.")


def test_an_ellipsis_inside_a_sentence_is_no_leader() -> None:
    """Three dots are an ellipsis, not a leader: the paragraph keeps its grouping. Four in
    a row are a leader, which is the line the constant draws."""
    from book_translator.extractors.overlay_extractor import LEADER_DOTS_RE

    assert not LEADER_DOTS_RE.search("we wait ... and then act")
    assert not LEADER_DOTS_RE.search("the ratio is 8.1.2 on that page")
    assert LEADER_DOTS_RE.search("Pairwise alignment . . . . 403")
    assert LEADER_DOTS_RE.search("Pairwise alignment....403")
    lines = [
        _prose_line("The matcher keeps the best hypothesis ... and then", 73.1, 194.8, 452.6),
        _prose_line("refines it over the remaining correspondences.", 73.1, 208.2, 420.0),
    ]
    (unit,) = _units_of(lines)
    assert len(unit.lines) == 2


def test_contents_page_of_the_real_document(math_doc: OverlayExtraction) -> None:
    """docs/test-2.pdf p.16 (Szeliski p.401): no unit spans two contents rows.

    Before the leader rule this page held ``'403 403 405 406 ...'`` (the whole right-hand
    column as one unit, 321 pt tall), ``'Blending Additional reading Exercises'`` and
    ``'8.5 8.6'``. Every contents unit is one text row now."""
    blocks = _page(math_doc, 16)
    contents = [b for b in blocks if 190.0 <= b.bbox[1] <= 520.0]
    assert len(contents) == 48  # 24 rows, two units each: the heading and its page number
    for block in contents:
        assert block.bbox[3] - block.bbox[1] <= 13.4, block.source_text
        # a unit may hold several fragments, but never two of them from different rows
        assert len({round(box[1], 3) for box in block.line_boxes}) == 1, block.source_text
    stacked = [b for b in blocks if b.source_text.startswith("403 403")]
    assert not stacked
    assert not [b for b in blocks if b.source_text == "8.5 8.6"]
    assert not [b for b in blocks if b.source_text.startswith("Blending Additional reading")]
    headings = [b for b in contents if b.translate]
    assert _by_text(headings, "8.1 Pairwise").source_text.startswith("8.1 Pairwise alignment .")
    assert _by_text(headings, "8.6 Exercises").source_text == "8.6 Exercises"
    # the page numbers stay in their own column, kept and aligned at the right margin
    kept = [b for b in contents if not b.translate]
    assert all(b.bbox[2] >= 452.0 for b in kept)
