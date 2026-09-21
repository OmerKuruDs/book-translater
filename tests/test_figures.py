"""Pure figure detector tests on hand-built ``RawPage`` fixtures (design doc 06, §4.1, §4.3, §8)."""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

import pytest

from book_translator.config import parse_page_ranges
from book_translator.domain.models import FigureKind
from book_translator.domain.result import Err, Ok
from book_translator.extractors.base import FigureOptions
from book_translator.extractors.cleanup import (
    BBox,
    Line,
    RawBlock,
    RawDrawing,
    RawImage,
    RawPage,
    RawTable,
    Span,
    is_label_like,
    matches_caption_pattern,
)
from book_translator.extractors.figure_render import dpi_ladder, figure_file_name
from book_translator.extractors.figures import (
    BandInfo,
    FigureRegion,
    detect_figures,
    is_decorative,
    legend_labels,
    rect_gap,
)

PAGE_W, PAGE_H = 400.0, 600.0
BODY = 10.0
OPTS = FigureOptions()
BAND = BandInfo(header_bottom=0.0, footer_top=PAGE_H, removed_line_boxes=())
BLACK = (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def line(
    text: str, x0: float, y0: float, *, size: float = BODY, width: Optional[float] = None
) -> Line:
    w = width if width is not None else max(4.0, len(text) * size * 0.5)
    bbox: BBox = (x0, y0, x0 + w, y0 + size * 1.2)
    return Line((Span(text, size, "Helvetica", False, False, False, bbox),), bbox)


def block(*lines: Line, page: int = 1) -> RawBlock:
    bbox = (
        min(ln.bbox[0] for ln in lines),
        min(ln.bbox[1] for ln in lines),
        max(ln.bbox[2] for ln in lines),
        max(ln.bbox[3] for ln in lines),
    )
    return RawBlock(page, bbox, tuple(lines))


def drawing(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    kind: str = "s",
    items: int = 1,
    fill: Optional[Tuple[float, float, float]] = None,
    stroke: Optional[Tuple[float, float, float]] = BLACK,
    width: Optional[float] = 1.0,
) -> RawDrawing:
    return RawDrawing((x0, y0, x1, y1), kind, items, fill, stroke, width)


def diagram(x: float, y: float, w: float, h: float, boxes: int = 3) -> List[RawDrawing]:
    """Stacked boxes joined by connectors: ``2 * boxes - 1`` paths, all touching."""
    out: List[RawDrawing] = []
    box_h = h / (boxes * 2 - 1)
    for i in range(boxes):
        top = y + i * 2 * box_h
        out.append(drawing(x, top, x + w, top + box_h, kind="fs", fill=(1, 1, 1)))
        if i < boxes - 1:
            out.append(drawing(x + w / 2 - 1, top + box_h, x + w / 2 + 1, top + 2 * box_h))
    return out


def page(
    blocks: Sequence[RawBlock] = (),
    drawings: Sequence[RawDrawing] = (),
    images: Sequence[RawImage] = (),
    tables: Sequence[RawTable] = (),
    links: Sequence[BBox] = (),
    number: int = 1,
) -> RawPage:
    return RawPage(
        number, PAGE_W, PAGE_H, tuple(blocks), tuple(images), tuple(tables), tuple(drawings),
        tuple(links),
    )


def kept(raw: RawPage) -> List[Line]:
    return [ln for b in raw.blocks for ln in b.lines]


def detect(
    raw: RawPage, options: FigureOptions = OPTS, band: BandInfo = BAND, body: float = BODY
) -> List[FigureRegion]:
    return detect_figures(raw, kept(raw), band, body, options)


def prose_block(y: float, n: int = 3, x: float = 40.0) -> RawBlock:
    texts = [f"Prose line number {i} of the paragraph that fills the column." for i in range(n)]
    return block(*[line(t, x, y + i * 12.0, width=320.0) for i, t in enumerate(texts)])


# ---------------------------------------------------------------------------
# Step 2: decorative filter (FR-30, E-24)
# ---------------------------------------------------------------------------


def test_decorative_rules_background_borders_bullets_links() -> None:
    raw = page(
        tables=(RawTable(1, (40, 300, 240, 340), (("a", "b"), ("c", "d"))),),
        links=((100.0, 200.0, 180.0, 212.0),),
    )
    rule = drawing(40, 100, 240, 100.5)  # 0.5 pt high stroke, nothing near it
    assert is_decorative(rule, raw, [rule], OPTS, BODY) == "rule"
    background = drawing(0, 0, PAGE_W, PAGE_H, kind="f", fill=(1, 1, 1), stroke=None)
    assert is_decorative(background, raw, [background], OPTS, BODY) == "background"
    white_fill = drawing(50, 50, 120, 90, kind="f", fill=(0.98, 0.98, 0.98), stroke=None)
    assert is_decorative(white_fill, raw, [white_fill], OPTS, BODY) == "background"
    border = drawing(40, 300, 240, 300.6)  # inside the table bbox
    assert is_decorative(border, raw, [border], OPTS, BODY) == "table_border"
    underline = drawing(100, 211, 180, 211.4)  # inside the link box (+1 pt)
    assert is_decorative(underline, raw, [underline], OPTS, BODY) == "link_box"
    bullets = [drawing(40, 120 + i * 14, 43, 123 + i * 14, kind="f", fill=BLACK) for i in range(3)]
    assert is_decorative(bullets[0], raw, bullets, OPTS, BODY) == "bullet"
    band_rule = drawing(40, 40, 360, 40.8)  # thin, inside the top 12 % band
    assert is_decorative(band_rule, raw, [band_rule], OPTS, BODY) == "band_rule"
    # A thin arrow shaft next to a real box is part of the figure, not a rule.
    box = drawing(100, 150, 200, 200, kind="fs", fill=(1, 1, 1))
    shaft = drawing(150, 200, 150.5, 240)
    assert is_decorative(shaft, raw, [box, shaft], OPTS, BODY) is None
    assert is_decorative(box, raw, [box, shaft], OPTS, BODY) is None


def test_page_with_only_decoration_has_no_figure(caplog: pytest.LogCaptureFixture) -> None:
    raw = page(
        blocks=[block(line("Running header", 40, 30)), prose_block(120)],
        drawings=[
            drawing(40, 44, 360, 44.5),  # rule under the header
            drawing(40, 560, 200, 560.4),  # footnote separator
            drawing(40, 133, 120, 133.3),  # underline
            *[drawing(40, 200 + i * 14, 43, 203 + i * 14, kind="f", fill=BLACK) for i in range(3)],
            drawing(40, 300, 240, 300.5),
            drawing(40, 340, 240, 340.5),
            drawing(40, 300, 40.5, 340),
            drawing(240, 300, 240.5, 340),
        ],
        tables=(RawTable(1, (40, 300, 240, 340), (("a", "b"), ("c", "d"))),),
    )
    with caplog.at_level(logging.INFO):
        assert detect(raw) == []


# ---------------------------------------------------------------------------
# Steps 3-5: candidates, clustering, acceptance
# ---------------------------------------------------------------------------


def test_cluster_thresholds_paths_and_area(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        few = detect(page(drawings=diagram(50, 100, 200, 150, boxes=2)))  # 3 paths
        assert few == []
        small = detect(page(drawings=diagram(50, 100, 30, 30, boxes=4)))  # 7 paths, 0.4 %
        assert small == []
    events = [getattr(r, "event", "") for r in caplog.records]
    assert events.count("figure_candidate_rejected") == 2
    assert any(getattr(r, "reason", "") == "few_paths" for r in caplog.records)
    assert any(getattr(r, "reason", "") == "small_area" for r in caplog.records)

    accepted = detect(page(drawings=diagram(50, 100, 200, 150, boxes=4)))  # 7 paths, 12.5 %
    assert len(accepted) == 1
    region = accepted[0]
    assert region.kind is FigureKind.VECTOR and region.member_drawings == 7
    assert region.bbox == (46.0, 96.0, 254.0, 254.0)  # union + 4 pt border


def test_raster_image_is_a_figure_from_one_percent_of_the_page() -> None:
    raw = page(images=[RawImage(1, (100, 100, 150, 150))])  # 2500 / 240000 = 1.04 %
    regions = detect(raw)
    assert len(regions) == 1 and regions[0].kind is FigureKind.RASTER
    tiny = page(images=[RawImage(1, (100, 100, 130, 130))])  # 0.4 %
    assert detect(tiny) == []


def test_merge_gap_joins_nearby_clusters_only() -> None:
    left = diagram(40, 100, 120, 150, boxes=3)
    right_near = diagram(170, 100, 120, 150, boxes=3)  # 10 pt away -> one figure
    assert len(detect(page(drawings=left + right_near))) == 1
    right_far = diagram(200, 100, 120, 150, boxes=3)  # 40 pt away -> two figures
    regions = detect(page(drawings=left + right_far))
    assert [r.index_on_page for r in regions] == [1, 2]
    assert regions[0].bbox[0] < regions[1].bbox[0]


def test_mixed_kind_when_a_raster_sits_inside_the_cluster() -> None:
    raw = page(
        drawings=diagram(40, 100, 150, 150), images=[RawImage(1, (195, 120, 260, 180))]
    )
    regions = detect(raw)
    assert len(regions) == 1
    assert regions[0].kind is FigureKind.MIXED
    assert regions[0].member_images == 1 and regions[0].member_drawings == 5


# ---------------------------------------------------------------------------
# Steps 6-7: inner text and label attachment (FR-33, E5)
# ---------------------------------------------------------------------------


def test_inner_labels_are_members_and_axis_titles_attach() -> None:
    drawings = diagram(60, 150, 200, 150, boxes=3)  # boxes at y 150-180, 210-240, 270-300
    inner = [
        block(line("1. Input", 70, 158)),
        block(line("2. Model", 70, 218)),
        block(line("3. Output", 70, 278)),
    ]
    axis = block(line("2D (what?)", 60, 130))  # 8 pt above the union: attached
    prose = prose_block(60)  # wide multi-line block far above: never absorbed
    near_prose = block(
        line("A whole sentence that is far too wide to be a label of a figure.", 40, 320,
             width=330.0)
    )
    raw = page(blocks=[prose, axis, *inner, near_prose], drawings=drawings)
    regions = detect(raw)
    assert len(regions) == 1
    region = regions[0]
    lines = kept(raw)
    member_texts = {lines[i].text for i in region.inner_lines}
    assert member_texts == {"1. Input", "2. Model", "3. Output", "2D (what?)"}
    assert region.labels == ("2D (what?)", "1. Input", "2. Model", "3. Output")
    assert region.bbox[1] <= 130.0 + 4.0  # grew to include the axis title
    assert region.bbox[3] < 320.0  # the wide sentence below is not part of it


def test_label_like_rules() -> None:
    assert is_label_like([line("Image formation", 40, 100)], BODY, 320.0)
    assert not is_label_like([line("Figure 3.1 A caption", 40, 100)], BODY, 320.0)
    assert not is_label_like([line("x" * 90, 40, 100, width=100.0)], BODY, 320.0)
    assert not is_label_like([line("wide", 40, 100, width=200.0)], BODY, 320.0)
    three = [line("a b", 40, 100 + i * 12) for i in range(3)]
    assert not is_label_like(three, BODY, 320.0)


def test_multi_line_inner_block_is_one_label_and_split_by_overlap() -> None:
    # E6: PyMuPDF merged two labels into one block whose lines do not overlap horizontally.
    drawings = diagram(40, 100, 300, 200, boxes=4)
    merged = block(
        line("13. 3D", 200, 110, width=40.0),
        line("reconstruction", 190, 122, width=70.0),
        line("14. Image-based", 60, 240, width=80.0),
        line("rendering", 70, 252, width=50.0),
    )
    raw = page(blocks=[merged], drawings=drawings)
    (region,) = detect(raw)
    assert region.labels == ("13. 3D reconstruction", "14. Image-based rendering")


# ---------------------------------------------------------------------------
# Step 8: body-band clip (FR-29, E-23)
# ---------------------------------------------------------------------------


def test_union_is_clipped_to_the_body_band_and_removed_lines() -> None:
    drawings = diagram(40, 40, 300, 200, boxes=3)  # reaches into the header band
    header_box: BBox = (40.0, 30.0, 200.0, 44.0)
    band = BandInfo(header_bottom=44.0, footer_top=PAGE_H, removed_line_boxes=(header_box,))
    (region,) = detect(page(drawings=drawings), band=band)
    assert region.bbox[1] >= 44.0
    # A leaked short band line (not removed by the v1 rules) is cut away too (A-13).
    leaked = block(line("1.3 Book overview", 40, 30))
    (region2,) = detect(page(blocks=[leaked], drawings=drawings))
    assert region2.bbox[1] >= leaked.bbox[3]
    assert not region2.inner_lines


# ---------------------------------------------------------------------------
# Step 9: caption association (FR-34, E-30)
# ---------------------------------------------------------------------------


def test_caption_below_is_preferred_then_above() -> None:
    drawings = diagram(40, 150, 300, 150, boxes=3)  # union y 150-300
    below = block(line("Figure 2.1 The caption below the drawing.", 40, 312))
    above = block(line("Table 2.1 The caption above the drawing.", 40, 120))
    (region,) = detect(page(blocks=[above, below], drawings=drawings))
    assert region.caption_position == "below"
    assert region.caption_line_indexes == (1,)
    assert region.bbox[3] < below.bbox[1]
    (region_above,) = detect(page(blocks=[above], drawings=drawings))
    assert region_above.caption_position == "above" and region_above.caption_line_indexes == (0,)
    assert region_above.bbox[1] > above.bbox[3]
    far = block(line("Figure 2.1 Too far away to be the caption.", 40, 400))
    (region_none,) = detect(page(blocks=[far], drawings=drawings))
    assert region_none.caption_position == "none" and region_none.caption_line_indexes == ()


def test_caption_pattern() -> None:
    captions = ("Figure 1.12 A", "Fig. 3 B", "Table 2.1", "Illustration IV", "Plate 7",
                "Listing 4.2")
    for text in captions:
        assert matches_caption_pattern(text), text
    for text in ("Figures are nice", "figure 1", "The Table", "Plateau"):
        assert not matches_caption_pattern(text), text


# ---------------------------------------------------------------------------
# Steps 10-11: table precedence, two figures, sub-figures (E-25, E-32)
# ---------------------------------------------------------------------------


def test_table_precedence_drops_the_cluster() -> None:
    drawings = diagram(40, 100, 200, 150, boxes=4)
    table = RawTable(1, (40, 100, 240, 250), (("a", "b"), ("c", "d")))
    # Drawings inside the table are decoration already; force one survivor outside.
    outside = diagram(250, 100, 100, 150, boxes=4)
    regions = detect(page(drawings=drawings + outside, tables=(table,)))
    assert len(regions) == 1 and regions[0].bbox[0] >= 246.0


def test_two_stacked_figures_with_prose_between_stay_separate() -> None:
    top = diagram(40, 60, 200, 120, boxes=3)
    caption_top = block(line("Figure 3.1 First diagram.", 40, 190))
    prose = prose_block(220)
    bottom = diagram(40, 300, 200, 120, boxes=3)
    caption_bottom = block(line("Figure 3.2 Second diagram.", 40, 430))
    raw = page(blocks=[caption_top, prose, caption_bottom], drawings=top + bottom)
    regions = detect(raw)
    assert [r.index_on_page for r in regions] == [1, 2]
    assert regions[0].caption_line_indexes == (0,)
    assert regions[1].caption_line_indexes == (4,)
    assert all("merged_by_caption" not in r.warnings for r in regions)


def test_subfigures_merge_when_one_caption_names_a_and_b() -> None:
    left = diagram(40, 100, 140, 150, boxes=3)
    right = diagram(210, 100, 140, 150, boxes=3)  # 30 pt gap: separate clusters
    caption = block(line("Figure 3.4 (a) the left part and (b) the right part.", 40, 262))
    (region,) = detect(page(blocks=[caption], drawings=left + right))
    assert "merged_by_caption" in region.warnings
    assert region.member_drawings == 10 and region.caption_position == "below"
    # Without the (a)/(b) markers the two clusters stay separate; the nearer keeps the caption.
    plain = block(line("Figure 3.4 Two things side by side.", 40, 262))
    regions = detect(page(blocks=[plain], drawings=left + right))
    assert len(regions) == 2
    assert sum(1 for r in regions if r.caption_line_indexes) == 1


# ---------------------------------------------------------------------------
# Steps 12-13 and non-figures (E-27, E-28, E-29)
# ---------------------------------------------------------------------------


def test_full_page_figure_without_prose() -> None:
    (region,) = detect(page(drawings=diagram(20, 20, 360, 500, boxes=5)))
    assert region.bbox == (16.0, 16.0, 384.0, 524.0)


def test_text_box_and_formula_are_not_figures(caplog: pytest.LogCaptureFixture) -> None:
    boxed = page(blocks=[prose_block(120)], drawings=[drawing(35, 115, 365, 160)])
    assert detect(boxed) == []
    glyphs = [drawing(150 + i * 10, 200, 158 + i * 10, 208, kind="f", fill=BLACK) for i in range(3)]
    with caplog.at_level(logging.INFO):
        assert detect(page(blocks=[prose_block(120)], drawings=glyphs)) == []
    assert any(getattr(r, "event", "") == "figure_candidate_rejected" for r in caplog.records)


def test_excluded_page_and_disabled_options() -> None:
    raw = page(drawings=diagram(50, 100, 200, 150, boxes=4), number=7)
    assert detect(raw, FigureOptions(exclude_pages=frozenset({7}))) == []
    assert detect(raw, FigureOptions(enabled=False)) == []
    assert len(detect(raw)) == 1


# ---------------------------------------------------------------------------
# Legend (§4.3), helpers
# ---------------------------------------------------------------------------


def _region(labels: Sequence[str]) -> FigureRegion:
    return FigureRegion(
        1, 1, FigureKind.VECTOR, (0, 0, 10, 10), 5, 0, (), tuple(labels), (), "none", ()
    )


def test_legend_labels_filter_dedupe_and_limit() -> None:
    labels = ["0", "1.5", "x", "Yes", "No", "Yes", " 10 % ", "Image formation", "-", "(3)"]
    kept_labels, more = legend_labels(_region(labels), 40)
    assert kept_labels == ("Yes", "No", "Image formation")
    assert more == 0
    limited, more = legend_labels(_region(labels), 2)
    assert limited == ("Yes", "No") and more == 1
    assert legend_labels(_region([]), 40) == ((), 0)


def test_rect_gap_and_naming_and_dpi_ladder() -> None:
    assert rect_gap((0, 0, 10, 10), (10, 0, 20, 10)) == 0.0
    assert rect_gap((0, 0, 10, 10), (15, 0, 20, 10)) == 5.0
    assert rect_gap((0, 0, 10, 10), (15, 20, 20, 30)) == 10.0
    assert figure_file_name(2, 1) == "p002-f01.png"
    assert figure_file_name(1000, 12) == "p1000-f12.png"
    assert dpi_ladder(200, 96) == [200, 150, 112, 96]
    assert dpi_ladder(96, 96) == [96]
    assert dpi_ladder(72, 96) == [72]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", frozenset()), ("2", frozenset({2})), ("2,5-7", frozenset({2, 5, 6, 7})),
     (" 1 , 3-3 ", frozenset({1, 3}))],
)
def test_parse_page_ranges(text: str, expected: frozenset[int]) -> None:
    result = parse_page_ranges(text)
    assert isinstance(result, Ok) and result.value == expected


@pytest.mark.parametrize("text", ["a", "0", "5-3", "2,,x", "1-"])
def test_parse_page_ranges_errors(text: str) -> None:
    result = parse_page_ranges(text)
    assert isinstance(result, Err) and "page range" in result.error.message


# ---------------------------------------------------------------------------
# Addendum A (2026-09-18): label groups and legend pairs (figure labels translated in place)
# ---------------------------------------------------------------------------

from book_translator.domain.models import FigureRecord, LabelBox  # noqa: E402
from book_translator.domain.overlay import LabelReplacement, OverlayAlignment  # noqa: E402
from book_translator.pipeline.figure_labels import (  # noqa: E402
    build_replacements,
    legend_pairs,
    strip_legends,
)


def test_label_groups_are_aligned_with_the_labels() -> None:
    drawings = diagram(40, 100, 300, 200, boxes=4)
    two = block(line("Image alignment", 60, 110, width=90.0),
                line("and stitching", 65, 122, width=80.0))
    single = block(line("Recognition", 60, 240, width=70.0))
    duplicate = block(line("Recognition", 200, 240, width=70.0))
    raw = page(blocks=[two, single, duplicate], drawings=drawings)
    (region,) = detect(raw)
    assert region.labels == ("Image alignment and stitching", "Recognition")
    assert len(region.label_groups) == len(region.labels)
    lines = kept(raw)
    texts = [[lines[i].text for i in group] for group in region.label_groups]
    assert texts == [["Image alignment", "and stitching"], ["Recognition"]]
    assert region.label_groups[1] == (2,)  # the first occurrence of a duplicate label


ASSEMBLED = (
    "# Bölüm\n\n"
    '<!-- image:1 src="images/p002-f01.png" -->\n\n'
    "Şekil 1: Akış.\n\n"
    '<!-- legend:1 more="2" -->\n'
    "- Start — Başla\n"
    "- 2\\. Step — 2\\. Adım\n"
    "- Stop — Dur\n"
    "\n*… ve 2 etiket daha*\n\n"
    "Sonraki paragraf.\n\n"
    "```\n<!-- legend:9 -->\n- code — kod\n```\n\n"
    "<!-- legend:3 -->\n- Unpaired label\n\n"
    "Son.\n"
)


def test_legend_pairs_read_the_assembled_document() -> None:
    pairs = legend_pairs(ASSEMBLED)
    assert pairs[1] == {"Start": "Başla", "2. Step": "2. Adım", "Stop": "Dur"}  # unescaped
    assert pairs[3] == {}  # an unpaired legend never paints a label
    assert 9 not in pairs  # inside a code fence
    assert legend_pairs("No legend here.\n") == {}


def test_strip_legends_removes_the_list_only() -> None:
    stripped = strip_legends(ASSEMBLED)
    assert "<!-- legend:1" not in stripped and "Başla" not in stripped
    assert "etiket daha" not in stripped and "Unpaired label" not in stripped
    assert "<!-- legend:9 -->\n- code — kod" in stripped  # code fences are untouched
    assert '<!-- image:1 src="images/p002-f01.png" -->\n\nŞekil 1: Akış.\n\nSonraki' in stripped
    assert "\n\n\n" not in stripped and stripped.endswith("Son.\n")
    assert strip_legends("Plain text.\n") == "Plain text.\n"


def test_build_replacements_maps_pairs_onto_label_boxes() -> None:
    def label(text: str, y: float, alignment: str = "center") -> LabelBox:
        return LabelBox(text, (10.0, y, 90.0, y + 14.0), 13.0, False, True, "serif", "#102030",
                        alignment)  # type: ignore[arg-type]

    record = FigureRecord(
        image_id=1, page=2, index_on_page=1, kind=FigureKind.VECTOR,
        region=(0.0, 0.0, 100.0, 100.0), file="images/p002-f01.png", dpi=200, width_px=278,
        height_px=278, bytes=10, dpi_reduced=False, label_count=3,
        labels=("Start", "2. Step", "Stop"), labels_more=0, caption_present=True, warnings=(),
        label_boxes=(label("Start", 10), label("2. Step", 30, "left"), label("Stop", 50)),
    )
    replacements = build_replacements(record, {"Start": "Başla", "2. Step": "2. Adım",
                                               "Stop": "Stop", "Other": "Diğer"})
    assert replacements == [
        LabelReplacement((10.0, 10.0, 90.0, 24.0), "Başla", 13.0, False, True, "serif",
                         "#102030", OverlayAlignment.CENTER),
        LabelReplacement((10.0, 30.0, 90.0, 44.0), "2. Adım", 13.0, False, True, "serif",
                         "#102030", OverlayAlignment.LEFT),
    ]  # an unchanged label ("Stop") is left alone; unknown pairs are ignored
    assert build_replacements(record, {}) == []


# ---------------------------------------------------------------------------
# CR-38: the detector never takes prose out of the paragraph flow
# ---------------------------------------------------------------------------


def paragraph(y: float, n: int, x: float = 50.0, width: float = 300.0) -> RawBlock:
    texts = [f"Line {i} of a paragraph whose prose fills the whole text column." for i in range(n)]
    return block(*[line(t, x, y + i * 12.0, width=width) for i, t in enumerate(texts)])


def shaded_box(x0: float, y0: float, x1: float, y1: float) -> List[RawDrawing]:
    """Tinted fill + four separate border strokes: the usual producer output of a note box."""
    return [
        drawing(x0, y0, x1, y1, kind="f", fill=(0.85, 0.9, 1.0), stroke=None),
        drawing(x0, y0, x1, y0 + 1.0),
        drawing(x1 - 1.0, y0, x1, y1),
        drawing(x0, y1 - 1.0, x1, y1),
        drawing(x0, y0, x0 + 1.0, y1),
    ]


def test_full_page_raster_under_the_text_is_background_not_a_figure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CR-38 (a): OCR "sandwich" / paper texture. Every line used to become a label."""
    scan = RawImage(1, (0.0, 0.0, PAGE_W, PAGE_H))
    raw = page(blocks=[paragraph(80, 6), paragraph(200, 6), paragraph(320, 6)], images=[scan])
    with caplog.at_level(logging.INFO):
        assert detect(raw) == []
    assert any(getattr(r, "reason", "") == "background_image" for r in caplog.records)
    # 60 % of the page is the limit; a smaller picture with a label on it stays a figure
    photo = RawImage(1, (40.0, 300.0, 360.0, 560.0))  # 34.7 %
    regions = detect(page(blocks=[paragraph(80, 6), block(line("Label", 60, 320))], images=[photo]))
    assert len(regions) == 1 and regions[0].labels == ("Label",)
    # a text-free full-page plate has nothing to swallow and is still a figure
    assert len(detect(page(images=[scan]))) == 1


def test_shaded_note_box_around_a_paragraph_is_not_a_figure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CR-38 (b), E-28: 5 paths around a 12-line paragraph; the text stays in the flow."""
    raw = page(
        blocks=[paragraph(60, 4), paragraph(228, 12), paragraph(430, 4)],
        drawings=shaded_box(40, 220, 380, 400),
    )
    with caplog.at_level(logging.INFO):
        assert detect(raw) == []
    rejected = [r for r in caplog.records if getattr(r, "event", "") == "figure_candidate_rejected"]
    assert [getattr(r, "reason", "") for r in rejected] == ["prose"]


def test_framed_page_keeps_every_paragraph() -> None:
    """CR-38: a tinted frame around the page body (63 % of the page, below the 90 % rule)."""
    blocks = [paragraph(90 + i * 84, 5) for i in range(5)]
    raw = page(blocks=blocks, drawings=shaded_box(30, 80, 390, 515))
    assert detect(raw) == []


def test_prose_inside_a_real_figure_is_handed_back_to_the_flow() -> None:
    """CR-38 (b): labels are members, a multi-line annotation is not."""
    note = paragraph(200, 3, x=60.0, width=150.0)  # 3 lines: not label-like
    raw = page(
        blocks=[block(line("Camera", 70, 110)), block(line("Sensor", 70, 330)), note],
        drawings=diagram(50, 100, 300, 260, boxes=4),
    )
    regions = detect(raw)
    assert len(regions) == 1
    region = regions[0]
    assert region.labels == ("Camera", "Sensor")
    lines_of_note = {i for i, ln in enumerate(kept(raw)) if ln.text.startswith("Line ")}
    assert lines_of_note and lines_of_note.isdisjoint(region.inner_lines)


def test_stacked_legend_block_is_still_label_text() -> None:
    """A chart legend stored as one text block (several short entries) is label text."""
    legend = block(*[line(name, 70, 120 + i * 12.0) for i, name in
                     enumerate(["Depth camera", "Laser range", "Stereo pair", "Monocular"])])
    raw = page(blocks=[legend], drawings=diagram(50, 100, 300, 260, boxes=4))
    regions = detect(raw)
    assert len(regions) == 1 and len(regions[0].inner_lines) == 4


def test_page_safety_net_drops_figures_that_own_the_page_text() -> None:
    """CR-38 (c): label-like lines everywhere (a framed index page): > 40 % of the page's
    characters would leave the flow -> no figure, one page-level note."""
    entries = [
        block(line(f"Index entry {i:03d}", 60.0 + 110.0 * (i % 3), 110.0 + 12.0 * (i // 3),
                   width=90.0))
        for i in range(90)
    ]
    outside = [paragraph(520, 3)]
    raw = page(blocks=[*entries, *outside], drawings=diagram(50, 100, 330, 390, boxes=4))
    notes: List[str] = []
    assert detect_figures(raw, kept(raw), BAND, BODY, OPTS, frozenset(), notes) == []
    assert notes == ["figure_page_dropped:1:text_heavy"]
    # the same drawing with a handful of labels is an ordinary figure, no note
    few = page(blocks=[*entries[:6], *outside], drawings=diagram(50, 100, 330, 390, boxes=4))
    notes = []
    assert len(detect_figures(few, kept(few), BAND, BODY, OPTS, frozenset(), notes)) == 1
    assert notes == []


# ---------------------------------------------------------------------------
# CR-83 / CR-84 / CR-85: the text-safety brakes must not eat real figures
# ---------------------------------------------------------------------------


def labelled_schematic(labels: int = 40) -> RawPage:
    """A full-page schematic: a grid of connected boxes with one short label in each, a
    caption, and no prose at all (doc 06: a full-page figure is supported "even with no
    prose"). Its labels are the page's whole text."""
    drawings: List[RawDrawing] = []
    blocks: List[RawBlock] = []
    for i in range(labels):
        x = 30.0 + (i % 4) * 88.0
        y = 80.0 + (i // 4) * 44.0
        drawings.append(drawing(x, y, x + 80.0, y + 28.0, kind="fs", fill=(1, 1, 1)))
        drawings.append(drawing(x + 40.0, y + 28.0, x + 41.0, y + 44.0))  # connector
        blocks.append(block(line(f"Sensor unit {i:02d}", x + 6.0, y + 8.0, width=60.0)))
    blocks.append(block(line("Figure 3.1 The whole system.", 30.0, 550.0, width=170.0)))
    return page(blocks=blocks, drawings=drawings)


def test_page_safety_net_keeps_a_label_dense_full_page_figure() -> None:
    """CR-83: the page *is* the figure. The net used to weigh the absorbed characters
    against every character of the page (560 of 588 = 95 %) and deleted all of them; a page
    with no prose has nothing to lose to a figure."""
    raw = labelled_schematic(40)
    notes: List[str] = []
    regions = detect_figures(raw, kept(raw), BAND, BODY, OPTS, frozenset(), notes)
    assert len(regions) == 1 and len(regions[0].labels) == 40
    assert notes == []
    # the old rule fired from ~30 labels on; none of these pages may lose its figure
    for count in (20, 30, 40):
        dense = labelled_schematic(count)
        assert len(detect_figures(dense, kept(dense), BAND, BODY, OPTS)) == 1


def test_page_safety_net_still_drops_a_scanned_column_the_figure_swallowed() -> None:
    """CR-83 keeps CR-38 (c): the lines of a narrow OCR column are short enough to pass the
    label test block by block, but they are the prose of the page and belong to the flow.
    The picture stays under the 60 % background rule, so only the page net can catch it."""
    ocr = "the committee met again in the spring and"  # 41 characters: no label is this long
    blocks = [paragraph(60, 3)]  # the body text of the page sets the body width
    for x in (45.0, 205.0):
        blocks.extend(block(line(ocr, x, 140.0 + i * 14.0, width=150.0)) for i in range(8))
    blocks.append(block(line("Figure 2.1 A scanned page.", 45.0, 420.0, width=150.0)))
    raw = page(blocks=blocks, images=[RawImage(1, (40.0, 130.0, 360.0, 400.0))])
    notes: List[str] = []
    assert detect_figures(raw, kept(raw), BAND, BODY, OPTS, frozenset(), notes) == []
    assert notes == ["figure_page_dropped:1:text_heavy"]


def test_a_diagram_with_one_annotation_box_stays_a_figure() -> None:
    """CR-84: labels are short by definition, so ``prose_chars > label_chars`` held for
    every diagram and the mixed rule was a bare 15 % area gate. One four-line note inside a
    250x160 flow chart (18 % of its union) used to reject the whole figure."""
    note = paragraph(150, 4, x=70.0, width=150.0)
    raw = page(
        blocks=[block(line("Camera", 60, 110)), block(line("Sensor", 60, 240)), note],
        drawings=diagram(50, 100, 250, 160, boxes=4),
    )
    notes: List[str] = []
    regions = detect_figures(raw, kept(raw), BAND, BODY, OPTS, frozenset(), notes)
    assert len(regions) == 1 and regions[0].labels == ("Camera", "Sensor")
    assert notes == []
    note_lines = {i for i, ln in enumerate(kept(raw)) if ln.text.startswith("Line ")}
    assert note_lines and note_lines.isdisjoint(regions[0].inner_lines)  # note stays in flow


def test_two_prose_blocks_inside_a_candidate_still_reject_it() -> None:
    """CR-84 keeps the brake for what it was written for - two multi-line prose blocks over
    15 % of the union are a note box - and CR-85 puts the rejection in the caller's notes."""
    raw = page(
        blocks=[
            block(line("Camera", 55, 105)),
            paragraph(130, 2, x=60.0, width=130.0),
            paragraph(190, 2, x=60.0, width=130.0),
        ],
        drawings=diagram(50, 100, 250, 160, boxes=4),
    )
    notes: List[str] = []
    assert detect_figures(raw, kept(raw), BAND, BODY, OPTS, frozenset(), notes) == []
    assert notes == ["figure_candidate_rejected:1:prose"]


def test_background_image_rejection_reaches_the_caller() -> None:
    """CR-85: the 60 % raster rule also costs real full-page plates that carry three lines
    of text, so the rejection is a business warning, not an INFO log line only."""
    scan = RawImage(1, (0.0, 0.0, PAGE_W, PAGE_H))
    raw = page(blocks=[paragraph(80, 6), paragraph(200, 6), paragraph(320, 6)], images=[scan])
    notes: List[str] = []
    assert detect_figures(raw, kept(raw), BAND, BODY, OPTS, frozenset(), notes) == []
    assert notes == ["figure_candidate_rejected:1:background_image"]


# ---------------------------------------------------------------------------
# CR-60: clustering is indexed, not n x n
# ---------------------------------------------------------------------------


def _brute_components(boxes: Sequence[BBox], reach: float) -> List[int]:
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if rect_gap(boxes[i], boxes[j]) <= reach:
                parent[max(find(i), find(j))] = min(find(i), find(j))
    return [find(i) for i in range(len(boxes))]


@pytest.mark.parametrize("reach", [0.0, 2.0, 12.0, 40.0])
def test_grid_clustering_equals_the_pairwise_scan(reach: float) -> None:
    import random

    from book_translator.extractors import figures as figures_module

    rng = random.Random(7)
    boxes: List[BBox] = []
    for _ in range(300):
        x, y = rng.uniform(0, 380), rng.uniform(0, 580)
        boxes.append((x, y, x + rng.uniform(0.5, 25), y + rng.uniform(0.5, 25)))
    boxes.append((0.0, 0.0, 400.0, 3.0))  # a page-wide rule spans many cells
    uf = figures_module._UnionFind(len(boxes))
    figures_module._Grid(boxes, reach).join_components(uf)
    assert [uf.find(i) for i in range(len(boxes))] == _brute_components(boxes, reach)
    grid = figures_module._Grid(boxes, reach)
    probe: BBox = (100.0, 100.0, 130.0, 120.0)
    assert grid.near(probe) == [i for i, b in enumerate(boxes) if rect_gap(probe, b) <= reach]


def test_five_thousand_paths_are_detected_well_under_a_second() -> None:
    """NFR-20 / CR-60: 5 000 drawings took 11 s with the pairwise scan."""
    import random
    import time

    rng = random.Random(1)
    paths = []
    for _ in range(5000):
        x, y = 60 + rng.random() * 280, 120 + rng.random() * 300
        paths.append(drawing(x, y, x + 3 + rng.random() * 10, y + 3 + rng.random() * 10))
    raw = page(drawings=paths)
    started = time.perf_counter()
    regions = detect(raw)
    elapsed = time.perf_counter() - started
    assert len(regions) == 1 and regions[0].member_drawings == 5000
    assert elapsed < 3.0, elapsed  # ~0.1 s here; the bound only guards against n x n
