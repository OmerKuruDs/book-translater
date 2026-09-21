"""Overlay unit extraction: PDF lines -> ``OverlayBlock`` units (design doc 06, §3.6.1, §4.4).

Units are built from the *lines* of ``page.get_text("dict")`` (never from pymupdf
blocks, E6/E-39): lines are grouped greedily by vertical succession, every group
becomes one unit with the union of its line boxes as redaction/placement rect, and
overlapping neighbour rects are split so they are disjoint. Kinds, styles, alignment
and the translate decision are pure functions of ``cleanup.py``; this module only
reads the PDF (through ``pdfkit.api``) and assembles the value objects.

Display (block) mathematics is the one thing that is *not* grouped that way: the rows
of an equation are collected into a band that becomes a single kept unit
(``keep_reason="math"``), grouping never runs through a band and no translated rect is
allowed to reach into one - otherwise the redaction of the paragraph around an equation
wipes the equation off the page.

``check_modifiable`` (E-49) refuses encrypted PDFs and PDFs whose permission bits
forbid modification before any provider contact.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ..domain.models import content_hash_of
from ..domain.overlay import (
    BBox,
    OverlayAlignment,
    OverlayBlock,
    OverlayBlockKind,
    OverlayExtraction,
    OverlayPage,
    OverlayStyle,
    format_block_id,
)
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from ..pdfkit.api import (
    Matrix,
    close_document,
    doc_modifiable,
    doc_needs_password,
    doc_page_count,
    open_document,
    page_rotation,
    page_rotation_matrix,
    page_size,
    page_table_cells,
    silence_layout_hint,
    transform_rect,
)
from .base import ExtractOptions, FigureOptions, ProgressCallback, validate_pdf_path
from .cleanup import (
    MARKER_SIZE_RATIO,
    Line,
    RawBlock,
    RawDrawing,
    RawImage,
    RawPage,
    Span,
    body_font_size,
    collect_compounds,
    detect_alignment,
    detect_running_patterns,
    detect_style,
    ends_with_terminal,
    first_line_indent,
    group_lines,
    has_letters,
    in_band,
    is_footnote_marker,
    is_heading_candidate,
    is_page_number,
    is_running_line,
    is_short_band_candidate,
    join_lines,
    matches_caption_pattern,
    parse_list_marker,
    split_overlapping_rects,
    to_superscript,
    union_bbox,
)
from .pymupdf_extractor import detect_figure_regions, read_raw_page

__all__ = [
    "OverlayOptions",
    "check_modifiable",
    "extract_overlay",
    "overlay_signature",
]

log = logging.getLogger("book_translator.extractors.overlay")

_SHORT_BAND_MIN_PAGES = 2
_FIGURE_OVERLAP = 0.80  # §4.4 step 5: a line mostly inside a figure region is a label
_TABLE_REGION_OVERLAP = 0.5  # a "table" mostly inside a figure region is the figure itself
_ROW_TOLERANCE = 0.3  # same text row: |y0 difference| <= 0.3 x line height
_RUN_IN_GAP_EM = 2.0  # run-in join: horizontal gap between two lines of one row
_SAME_ROW_OVERLAP = 0.5  # CR-40: two units share a text row (vertical overlap / shorter height)
_ISOLATED_GAP_LINES = 1.5  # CR-40: a band line this far from the body looks like a header
HEADER_GUESS_WARNING = "header_guess"
"""Unit warning (CR-40): a short line in the header/footer band that looks like a running
header but matches no repeating pattern and shares its row with no page number. It is
translated like body text; the warning surfaces the judgement call."""

OVERLAP_KEPT_WARNING = "overlap_kept"
"""Unit warning: the rect of this unit overlapped a neighbour and could not be pulled off
it without being crushed below one line (:func:`cleanup.split_overlapping_rects`), so the
unit is kept with its source text (``keep_reason="no_bbox"``: the rect is unusable) rather
than printed over its neighbour. The warning tells that apart from a unit that never had a
box at all."""

_MIN_RECT_LINE_SCALE = 0.5
"""Smallest usable rect height, as a share of the unit's own shortest source line: the
exporter's floor scale (``pdf_overlay_exporter.DEFAULT_FLOOR_SCALE``), i.e. one line at
half the source font size. A rect cut back below this cannot hold a single line even at
the smallest font the exporter will set, so the unit is kept with its source text instead
(see :data:`OVERLAP_KEPT_WARNING`). Measured per unit, not per page: a footnote or a
superscript fragment is allowed to keep the small box it always had."""

MATH_KEEP_REASON = "math"
"""Keep reason of a display (block) math band: the band is one unit, is never sent and is
never redacted, so the source glyphs of the equation survive the overlay untouched."""

MATH_FONT_RE = re.compile(r"CM(MI|SY|EX)|MSAM|MSBM|Math", re.IGNORECASE)
"""Fonts that only carry mathematics, never prose. ``CMMI`` (math italic), ``CMSY``
(symbols), ``CMEX`` (the big brackets and operators of a display equation - the strongest
signal), ``MSAM``/``MSBM`` (AMS symbols) and anything named ``*Math*``. Deliberately *not*
``CMR``/``CMBX``: those are the Computer Modern text fonts and carry ordinary words.
Extend the alternation when a source uses another math-only family."""

EQUATION_NUMBER_RE = re.compile(r"^\(\d+(?:\.\d+)+\)$")
"""A display equation's number, alone on its line: ``(8.5)``, ``(8.39)``, ``(A.1.2)`` is
not matched on purpose - the chapter part is a number in every source seen so far, and a
bare ``(3)`` is far too close to a list marker to be used as a band signal."""

_MATH_WORD_RE = re.compile(r"[^\W\d_]{3,}")
"""A word of three or more letters: what a *text* font span must not hold for its line to
count as display math ("where Kk = diag(fk, fk, 1) is ..." stays prose, E-inline math)."""

_MATH_BAND_GAP_LINES = 0.6
"""Vertical gap (in body line heights) that still fuses two math line clusters into one
band: enough for the stacked rows of one display equation, which touch or overlap, and too
little to jump over a line of prose between two equations."""


@dataclass(frozen=True)
class OverlayOptions:
    """Overlay extraction settings (design doc 06, §3.6.1; v1 band values reused)."""

    translate_headers: bool = False  # FR-46
    keep_figure_text: bool = False  # Q23
    band_ratio: float = 0.12
    header_footer_page_ratio: float = 0.30
    line_gap_ratio: float = 0.5  # §4.4
    min_h_overlap: float = 0.30
    heading_size_ratio: float = 1.15
    footnote_size_ratio: float = 0.85
    detect_tables: bool = True  # find_tables per page (R24: the CR-21 toggle)


@dataclass
class _Unit:
    """Mutable unit under construction (one per line group / table cell)."""

    lines: List[Line]
    bbox: BBox
    kind: OverlayBlockKind
    text: str
    warnings: List[str] = field(default_factory=list)
    keep_reason: Optional[str] = None
    container: Optional[BBox] = None  # smallest drawing rect around a single-line unit
    rotated: bool = False


@dataclass
class _DocContext:
    options: OverlayOptions
    body_size: float
    patterns: Set[str]
    compounds: Set[str]
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# E-49: modifiability check
# --------------------------------------------------------------------------- #


def check_modifiable(pdf_path: Path) -> Result[None]:
    """Refuse encrypted / no-modify PDFs (``OVERLAY_PDF_RESTRICTED``, USER, exit 1)."""
    valid = validate_pdf_path(pdf_path)
    if isinstance(valid, Err):
        return valid
    try:
        doc = open_document(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - classified
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"cannot open PDF: {pdf_path.name}",
            ErrorScope.USER,
            cause=repr(exc),
        )
    try:
        if doc_needs_password(doc):
            return err(
                ErrorCode.OVERLAY_PDF_RESTRICTED,
                f"{pdf_path.name} is password protected; overlay mode needs a PDF that can be "
                "modified (decrypt it first or use --mode reflow)",
                ErrorScope.USER,
            )
        if not doc_modifiable(doc):
            return err(
                ErrorCode.OVERLAY_PDF_RESTRICTED,
                f"{pdf_path.name} is encrypted or does not permit modification (PDF permission "
                "bits); overlay mode cannot write the translation back - use --mode reflow",
                ErrorScope.USER,
            )
    except Exception as exc:  # noqa: BLE001 - boundary
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"cannot inspect PDF permissions: {pdf_path.name}",
            ErrorScope.USER,
            cause=repr(exc),
        )
    finally:
        close_document(doc)
    return Ok(None)


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #


def _area(bbox: BBox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _intersection(a: BBox, b: BBox) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _overlap_ratio(inner: BBox, container: BBox) -> float:
    area = _area(inner)
    return _intersection(inner, container) / area if area > 0 else 0.0


def _centre_inside(bbox: BBox, container: BBox) -> bool:
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    return container[0] <= cx <= container[2] and container[1] <= cy <= container[3]


def _rounded(bbox: BBox) -> BBox:
    return (round(bbox[0], 3), round(bbox[1], 3), round(bbox[2], 3), round(bbox[3], 3))


def _lines_of(raw: RawPage) -> List[Line]:
    return [ln for block in raw.blocks for ln in block.lines if ln.text]


def _line_text(line: Line) -> str:
    """Line text with footnote markers as Unicode superscript digits.

    A marker is a digits-only span that is flagged superscript or clearly smaller than
    the line ("dependencies." + "9" -> "dependencies.⁹"): the superscript characters
    survive translation unchanged and the exporter renders them as ``<sup>``. A line that
    is nothing but digits (page number, tick label) is left alone."""
    if len(line.spans) < 2:
        return line.text
    size = line.size
    parts: List[str] = []
    for span in line.spans:
        marker = span.text.strip()
        small = size > 0 and 0 < span.size <= MARKER_SIZE_RATIO * size
        if marker.isdigit() and is_footnote_marker(marker) and (span.superscript or small):
            parts.append(span.text.replace(marker, to_superscript(marker)))
        else:
            parts.append(span.text)
    return "".join(parts).strip()


def _is_upright(direction: Tuple[float, float]) -> bool:
    """Writing direction left-to-right in the page's visible orientation (+- ~2.5 degrees)."""
    return abs(direction[0] - 1.0) <= 1e-3 and abs(direction[1]) <= 0.045


def _rotate_dir(direction: Tuple[float, float], matrix: Matrix) -> Tuple[float, float]:
    a, b, c, d = matrix[0], matrix[1], matrix[2], matrix[3]
    x, y = direction
    return (round(a * x + c * y, 6), round(b * x + d * y, 6))


def _to_visible(raw: RawPage, matrix: Matrix, size: Tuple[float, float]) -> RawPage:
    """``raw`` of a page stored with ``/Rotate`` mapped into the orientation the reader
    sees (CR-53). MuPDF reports text, images and drawings in the unrotated page space, where
    the lines of an upright-looking page run vertically; every rule of this module (line
    grouping, bands, alignment) assumes the visible orientation. Tables come from
    ``find_tables``, which already answers in visible coordinates."""

    def box(bbox: BBox) -> BBox:
        return transform_rect(bbox, matrix)

    blocks = tuple(
        RawBlock(
            block.page,
            box(block.bbox),
            tuple(
                Line(
                    tuple(
                        Span(sp.text, sp.size, sp.font, sp.bold, sp.superscript, sp.mono,
                             box(sp.bbox), sp.italic, sp.color)
                        for sp in line.spans
                    ),
                    box(line.bbox),
                    _rotate_dir(line.dir, matrix),
                )
                for line in block.lines
            ),
        )
        for block in raw.blocks
    )
    return RawPage(
        raw.number,
        size[0],
        size[1],
        blocks,
        tuple(RawImage(img.page, box(img.bbox)) for img in raw.images),
        raw.tables,
        tuple(
            RawDrawing(box(d.bbox), d.kind, d.item_count, d.fill, d.stroke, d.width)
            for d in raw.drawings
        ),
        raw.link_boxes,
    )


def _is_type3(line: Line) -> bool:
    return any(sp.font.startswith("T3") for sp in line.spans if sp.text.strip())


# --------------------------------------------------------------------------- #
# Page processing
# --------------------------------------------------------------------------- #


def _read_tables(page: Any, raw: RawPage, regions: Sequence[BBox], ctx: _DocContext) -> List[BBox]:
    """Cell rects of the page tables (>= 2 rows and columns) outside the figure regions."""
    if not ctx.options.detect_tables:
        return []
    try:
        found = page_table_cells(page)
    except Exception as exc:  # noqa: BLE001 - best effort like v1
        ctx.warnings.append(f"page {raw.number}: table detection failed ({type(exc).__name__})")
        return []
    cells: List[BBox] = []
    for table_bbox, rows, cols, table_cells in found:
        if rows < 2 or cols < 2:
            continue
        table_area = _area(table_bbox)
        if table_area <= 0 or any(
            _intersection(table_bbox, region) / table_area >= _TABLE_REGION_OVERLAP
            for region in regions
        ):
            continue
        cells.extend(cell for cell in table_cells if cell is not None and _area(cell) > 0)
    return cells


def _row_join(prev: Line, line: Line, unit_bbox: BBox, raw: RawPage, band_ratio: float) -> bool:
    """Run-in join: ``line`` continues the single-line unit ``prev`` on the same text row
    (a bold "Figure 1.12" label followed by the caption text, which may continue on the
    following lines; a "1.3" number followed by its heading). Inside the header/footer
    band numbers-only lines never join ("1.3" / "Book overview" / "19" of a running
    header stay three units, E6)."""
    height = max(prev.height, line.height, 1.0)
    if abs(line.bbox[1] - prev.bbox[1]) > _ROW_TOLERANCE * height:
        return False
    if abs(line.size - prev.size) > 1.0:
        return False
    if in_band(prev.bbox, raw.height, band_ratio) and (
        not has_letters(prev.text) or not has_letters(line.text)
    ):
        return False
    gap = line.bbox[0] - unit_bbox[2]
    return 0.0 <= gap <= _RUN_IN_GAP_EM * max(line.size, 1.0)


def _merge_row_groups(groups: List[List[Line]], raw: RawPage, ctx: _DocContext) -> List[List[Line]]:
    """Fold run-in continuations (same row, small gap) into the preceding group."""
    merged: List[List[Line]] = []
    boxes: List[BBox] = []
    band = ctx.options.band_ratio
    for group in groups:
        if merged and len(merged[-1]) == 1 and _row_join(
            merged[-1][-1], group[0], boxes[-1], raw, band
        ):
            merged[-1].extend(group)
            for ln in group:
                boxes[-1] = union_bbox(boxes[-1], ln.bbox)
            continue
        merged.append(list(group))
        box = group[0].bbox
        for ln in group[1:]:
            box = union_bbox(box, ln.bbox)
        boxes.append(box)
    return merged


def _row_boxes(lines: Sequence[Line]) -> List[BBox]:
    """Line boxes with run-in lines of one text row unioned (alignment input)."""
    rows: List[BBox] = []
    last: Optional[Line] = None
    for line in lines:
        if last is not None and abs(line.bbox[1] - last.bbox[1]) <= _ROW_TOLERANCE * max(
            last.height, line.height, 1.0
        ):
            rows[-1] = union_bbox(rows[-1], line.bbox)
        else:
            rows.append(line.bbox)
        last = line
    return rows


def _smallest_drawing_around(raw: RawPage, bbox: BBox) -> Optional[BBox]:
    best: Optional[BBox] = None
    for drawing in raw.drawings:
        d = drawing.bbox
        if d[0] - 1.0 <= bbox[0] and d[1] - 1.0 <= bbox[1] and d[2] + 1.0 >= bbox[2] and (
            d[3] + 1.0 >= bbox[3]
        ):
            if best is None or drawing.area < _area(best):
                best = d
    return best


def _classify(
    unit: _Unit,
    raw: RawPage,
    page_body: float,
    ctx: _DocContext,
    *,
    in_figure: bool,
    in_table: bool,
    has_next: bool,
    row_anchor: bool = False,
) -> OverlayBlockKind:
    """§4.4 step 5 in order: band kinds, page number, figure label, table cell, footnote,
    heading, caption, body.

    A band line is HEADER/FOOTER only when it matches a repeating running pattern (the v1
    rule) or is a short line sharing its text row with a page number / running-pattern
    line (``row_anchor``: "1.3  Book overview  19"). A one-off short line in the band is
    ordinary content and stays translatable (CR-40)."""
    options = ctx.options
    if unit.keep_reason == MATH_KEEP_REASON:
        # a display math band is body content by definition; the size/position rules below
        # would happily call a bottom-of-page equation a footnote
        return OverlayBlockKind.BODY
    text = unit.text
    size = max((ln.size for ln in unit.lines), default=0.0)
    if in_band(unit.bbox, raw.height, options.band_ratio):
        if is_page_number(text):
            return OverlayBlockKind.PAGE_NUMBER
        running = _is_running(unit, ctx)
        if running or (row_anchor and _is_short_band_unit(unit, ctx)):
            centre = (unit.bbox[1] + unit.bbox[3]) / 2.0
            return (
                OverlayBlockKind.HEADER
                if centre < raw.height / 2.0
                else OverlayBlockKind.FOOTER
            )
    if in_figure:
        return OverlayBlockKind.FIGURE_LABEL
    if in_table:
        return OverlayBlockKind.TABLE_CELL
    if (
        page_body > 0
        and size <= page_body * options.footnote_size_ratio
        and unit.bbox[1] >= raw.height * (2.0 / 3.0)
    ):
        return OverlayBlockKind.FOOTNOTE
    # a first-line-indented paragraph that merely opens with "Figure 1.12 shows ..." is
    # running text, not a caption
    if matches_caption_pattern(text) and first_line_indent(_row_boxes(unit.lines)) <= 0.0:
        return OverlayBlockKind.CAPTION
    bold = all(ln.bold for ln in unit.lines)
    if is_heading_candidate(
        [ln.text for ln in unit.lines],
        size,
        bold,
        page_body,
        options.heading_size_ratio,
        followed_by_paragraph=has_next,
    ):
        return OverlayBlockKind.HEADING
    return OverlayBlockKind.BODY


def _is_running(unit: _Unit, ctx: _DocContext) -> bool:
    return any(is_running_line(ln.text, ctx.patterns) for ln in unit.lines)


def _is_short_band_unit(unit: _Unit, ctx: _DocContext) -> bool:
    size = max((ln.size for ln in unit.lines), default=0.0)
    return len(unit.lines) == 1 and is_short_band_candidate(unit.text, size, ctx.body_size)


def _same_row(a: BBox, b: BBox) -> bool:
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    shorter = min(a[3] - a[1], b[3] - b[1])
    return shorter > 0 and overlap / shorter >= _SAME_ROW_OVERLAP


def _row_anchors(units: Sequence[_Unit], raw: RawPage, ctx: _DocContext) -> List[bool]:
    """Per unit: does it share its text row with a page number or running-pattern unit?"""
    band = ctx.options.band_ratio
    anchors = [
        in_band(u.bbox, raw.height, band) and (is_page_number(u.text) or _is_running(u, ctx))
        for u in units
    ]
    return [
        any(
            anchors[j] and j != i and _same_row(unit.bbox, units[j].bbox)
            for j in range(len(units))
        )
        for i, unit in enumerate(units)
    ]


def _is_header_guess(index: int, units: Sequence[_Unit], raw: RawPage, ctx: _DocContext) -> bool:
    """A translatable short band line that stands apart from the page body (borderline)."""
    unit = units[index]
    if unit.kind not in (OverlayBlockKind.BODY, OverlayBlockKind.HEADING):
        return False
    if not in_band(unit.bbox, raw.height, ctx.options.band_ratio):
        return False
    if not _is_short_band_unit(unit, ctx):
        return False
    height = max(unit.bbox[3] - unit.bbox[1], 1.0)
    top = (unit.bbox[1] + unit.bbox[3]) / 2.0 < raw.height / 2.0
    gaps = [
        (other.bbox[1] - unit.bbox[3]) if top else (unit.bbox[1] - other.bbox[3])
        for j, other in enumerate(units)
        if j != index and not _same_row(unit.bbox, other.bbox)
    ]
    towards_body = [gap for gap in gaps if gap >= 0]
    return not towards_body or min(towards_body) > _ISOLATED_GAP_LINES * height


def _keep_reason(unit: _Unit, options: OverlayOptions) -> Optional[str]:
    """FR-46 / Q23 / §4.4 step 9: why a unit is never sent (``None`` -> translate)."""
    if unit.keep_reason is not None:  # rotated / no_bbox decided while reading
        return unit.keep_reason
    if unit.kind is OverlayBlockKind.PAGE_NUMBER:
        return "page_number"
    if unit.kind in (OverlayBlockKind.HEADER, OverlayBlockKind.FOOTER) and (
        not options.translate_headers
    ):
        return "header_footer"
    if unit.kind is OverlayBlockKind.FIGURE_LABEL and options.keep_figure_text:
        return "figure_text"
    if not has_letters(unit.text):
        return "no_letters"
    return None


@dataclass(frozen=True)
class _MathBand:
    """One display (block) math band of a page.

    ``y0``/``y1`` is the vertical range of the *math* lines of the band: it decides which
    other lines the band takes in, how the remaining lines are cut into segments and where
    the neighbouring rects have to stop. ``bbox`` is the rect of the finished unit: that
    same range, over the horizontal extent of the member lines (the equation number at the
    right margin is a member, so the rect reaches it)."""

    y0: float
    y1: float
    bbox: BBox
    lines: Tuple[Line, ...]


def _has_text_word(line: Line) -> bool:
    """True when a span in a *text* font holds a word of three or more letters: the line
    reads as prose, whatever mathematics it also carries."""
    return any(
        span.text.strip()
        and not MATH_FONT_RE.search(span.font)
        and _MATH_WORD_RE.search(span.text)
        for span in line.spans
    )


def _is_math_line(line: Line) -> bool:
    """True when ``line`` is a row of mathematics (measured rule).

    Two conditions: no span in a text font holds a word of three or more letters, and at
    least one non-blank span is set in a math-only font (:data:`MATH_FONT_RE`). The first
    condition is what keeps inline math out: a prose sentence with ``Kk = diag(fk, fk, 1)``
    in it still carries "where", "the", "camera" in the text font."""
    if _has_text_word(line):
        return False
    return any(MATH_FONT_RE.search(span.font) for span in line.spans if span.text.strip())


def _hangs_off_prose(line: Line, prose: Sequence[BBox]) -> bool:
    """True when ``line`` is a fragment of a prose row rather than a display row.

    A subscript or a superscript of inline math ("... covariance σ⁻²ₙ (9.37)") is reported
    as a line of its own, in a math font and without a single word - indistinguishable from
    a display row by fonts alone. Its *centre* however sits inside the box of the prose line
    it hangs off, while the centre of a real display row never does (the tall bracket of an
    equation may reach into the line above, but its middle is a full line below it)."""
    centre = (line.bbox[1] + line.bbox[3]) / 2.0
    return any(box[1] <= centre <= box[3] for box in prose)


def _body_line_height(lines: Sequence[Line], ctx: _DocContext) -> float:
    """Median line height of the page (the tall bracket rows are outnumbered by prose)."""
    heights = sorted(ln.height for ln in lines if ln.height > 0)
    if heights:
        return heights[len(heights) // 2]
    return max(ctx.body_size, 1.0) * 1.2


def _equation_numbers(free: Sequence[Line], raw: RawPage, ctx: _DocContext) -> List[Line]:
    """The equation numbers of one page: a line that is *nothing but* ``(8.5)`` and sits at
    the right text edge.

    A display equation set in the text fonts (``CMR``/``CMBX``: ``ELLS = ...``) carries no
    math-only font and is invisible to :func:`_is_math_line`; its number at the right margin
    is the one signal that survives. Both conditions are needed together - a reference to
    "(8.5)" in running prose is part of a longer line, and a short line that happens to look
    like a number somewhere in the text column is not an equation number.

    The right text edge is the *most common* line end of the body lines, not the widest
    one: a running head hangs out into the margin (docs/test-2.pdf p.34) and a full-width
    figure caption reaches past the text column (p.20), while justified body text ends on
    the margin line by line. Everything at or right of that edge counts, so a number set
    even further out still qualifies."""
    band = ctx.options.band_ratio
    ends = Counter(
        round(ln.bbox[2]) for ln in free if not in_band(ln.bbox, raw.height, band)
    )
    if not ends:
        return []
    right = float(max(ends, key=lambda value: (ends[value], value)))
    margin = max(ctx.body_size, 1.0)  # one em of slack: the glyph box of ")" vs the margin
    return [
        ln
        for ln in free
        if EQUATION_NUMBER_RE.match(_line_text(ln).strip()) and ln.bbox[2] >= right - margin
    ]


def _grow_over_math(
    ranges: List[List[float]], rows: Sequence[Line], prose: Sequence[BBox], gap_limit: float
) -> List[List[float]]:
    """Grow every cluster over the math rows it crosses, then merge the clusters again.

    The tall ``CMEX`` brackets and the sub/superscript fragments of a display equation are
    reported as lines of their own that reach far above and below the row the equation
    number sits on, and :func:`_hangs_off_prose` keeps them out of the seeds. Once a band
    exists they have to belong to it, or the rows they occupy end up as translated units
    painted over the equation. Growth runs over math rows only and is refused as soon as it
    would pull the centre of a *prose* line into the band that was not already in it - that
    is what stops a chain of inline fragments from swallowing a page."""
    for rng in ranges:
        growing = True
        while growing:
            growing = False
            for line in rows:
                y0, y1 = line.bbox[1], line.bbox[3]
                if y1 <= rng[0] or y0 >= rng[1]:  # no vertical contact with the band
                    continue
                if y0 >= rng[0] and y1 <= rng[1]:  # already inside
                    continue
                grown = [min(rng[0], y0), max(rng[1], y1)]
                if any(
                    grown[0] <= (box[1] + box[3]) / 2.0 <= grown[1]
                    and not rng[0] <= (box[1] + box[3]) / 2.0 <= rng[1]
                    for box in prose
                ):
                    continue
                rng[0], rng[1] = grown[0], grown[1]
                growing = True
    merged: List[List[float]] = []
    for rng in sorted(ranges):
        if merged and rng[0] - merged[-1][1] <= gap_limit:
            merged[-1][1] = max(merged[-1][1], rng[1])
        else:
            merged.append([rng[0], rng[1]])
    return merged


def _math_bands(free: Sequence[Line], raw: RawPage, ctx: _DocContext) -> List[_MathBand]:
    """Display math bands of one page, in reading order.

    Seed rows are the math lines (:func:`_is_math_line`) that do not hang off a prose row
    plus the equation numbers (:func:`_equation_numbers`); they are clustered by vertical
    gap (:data:`_MATH_BAND_GAP_LINES`) and each cluster then grows over the math rows it
    crosses (:func:`_grow_over_math`). Every cluster finally takes in *every* free line
    whose vertical centre falls inside it, so the equation number at the right margin and
    the bare digits of a matrix row join the band they belong to. Taking lines in never
    grows the range, so one band can never cascade down a page."""
    prose = [ln.bbox for ln in free if _has_text_word(ln)]
    rows = [ln for ln in free if _is_math_line(ln)]
    seeds = [ln for ln in rows if not _hangs_off_prose(ln, prose)]
    seen = {id(ln) for ln in seeds}
    seeds.extend(ln for ln in _equation_numbers(free, raw, ctx) if id(ln) not in seen)
    if not seeds:
        return []
    gap_limit = _MATH_BAND_GAP_LINES * _body_line_height(free, ctx)
    ranges: List[List[float]] = []  # [y0, y1] per cluster, kept disjoint and ordered
    for line in sorted(seeds, key=lambda ln: (ln.bbox[1], ln.bbox[0])):
        if ranges and line.bbox[1] - ranges[-1][1] <= gap_limit:
            ranges[-1][1] = max(ranges[-1][1], line.bbox[3])
        else:
            ranges.append([line.bbox[1], line.bbox[3]])
    ranges = _grow_over_math(ranges, rows, prose, gap_limit)
    bands: List[_MathBand] = []
    for y0, y1 in ranges:
        members = [ln for ln in free if y0 <= (ln.bbox[1] + ln.bbox[3]) / 2.0 <= y1]
        if not members:  # unreachable: every math line of the cluster sits in its range
            continue
        members.sort(key=lambda ln: (ln.bbox[1], ln.bbox[0]))
        x0 = min(ln.bbox[0] for ln in members)
        x1 = max(ln.bbox[2] for ln in members)
        # the rect keeps the *cluster* range vertically, not the union of the member boxes:
        # so "centre inside the band" and "inside the band rect" mean the same thing, and
        # every other unit of the page can be pulled clear of it (:func:`_clip_off_bands`)
        bands.append(_MathBand(y0, y1, (x0, y0, x1, y1), tuple(members)))
    return bands


def _clip_off_bands(units: Sequence[_Unit], bands: Sequence[_MathBand]) -> None:
    """Pull every other unit rect out of the math bands (in place).

    A paragraph rect may still reach into a band by a few points, because the tall bracket
    of an equation starts beside the last line of the paragraph above it. Redacting that
    rect would shave the top off the bracket, so the rect gives way - the band never moves
    and is never painted. A unit that cannot be freed (its own centre lies in the band) is
    left alone rather than collapsed to nothing."""
    for unit in units:
        if unit.keep_reason == MATH_KEEP_REASON:
            continue
        x0, y0, x1, y1 = unit.bbox
        centre = (y0 + y1) / 2.0
        for band in bands:
            if x1 <= band.bbox[0] or x0 >= band.bbox[2] or y1 <= band.y0 or y0 >= band.y1:
                continue
            if centre <= band.y0:
                y1 = min(y1, band.y0)
            elif centre >= band.y1:
                y0 = max(y0, band.y1)
        if y1 > y0:
            unit.bbox = (x0, y0, x1, y1)


def _math_unit(band: _MathBand) -> _Unit:
    """The single kept unit of a band. The text is a plain space join: it is never sent and
    never re-rendered, so the hyphen-rejoin heuristics of :func:`join_lines` (and their
    warnings) would only add noise on glyph soup like ``x1 y1 1``."""
    text = " ".join(t for t in (_line_text(ln) for ln in band.lines) if t)
    return _Unit(
        list(band.lines),
        band.bbox,
        OverlayBlockKind.BODY,
        text,
        keep_reason=MATH_KEEP_REASON,
    )


def _band_segments(free: Sequence[Line], bands: Sequence[_MathBand]) -> List[List[Line]]:
    """The lines left over, cut into one segment per gap between the bands.

    Grouping must never run *through* a band: a paragraph above an equation and one below
    it would otherwise land in one group whose box covers the equation, and redacting that
    box is exactly what wipes display math off the page."""
    members = {id(ln) for band in bands for ln in band.lines}
    rest = [ln for ln in free if id(ln) not in members]
    if not bands:
        return [rest] if rest else []
    segments: List[List[Line]] = [[] for _ in range(len(bands) + 1)]
    for line in rest:
        centre = (line.bbox[1] + line.bbox[3]) / 2.0
        segments[sum(1 for band in bands if band.y1 < centre)].append(line)
    return [segment for segment in segments if segment]


def _build_units(
    raw: RawPage, page: Any, regions: Sequence[BBox], ctx: _DocContext
) -> Tuple[List[_Unit], List[bool], List[bool]]:
    """Units of one page in reading order plus their (in_figure, in_table) flags."""
    lines = _lines_of(raw)
    units: List[_Unit] = []
    flags_figure: List[bool] = []
    flags_table: List[bool] = []

    def add(unit: _Unit, in_figure: bool, in_table: bool) -> None:
        units.append(unit)
        flags_figure.append(in_figure)
        flags_table.append(in_table)

    plain: List[Line] = []
    for line in lines:
        if not _is_upright(line.dir):
            add(_Unit([line], line.bbox, OverlayBlockKind.BODY, line.text, keep_reason="rotated",
                      rotated=True), False, False)
        elif _area(line.bbox) <= 0 or _is_type3(line):
            add(_Unit([line], line.bbox, OverlayBlockKind.BODY, line.text,
                      keep_reason="no_bbox"), False, False)
        else:
            plain.append(line)

    # Figure regions first (labels never group with prose), then table cells, then the rest.
    by_region: Dict[int, List[Line]] = {}
    rest: List[Line] = []
    for line in plain:
        owner = next(
            (
                i
                for i, region in enumerate(regions)
                if _overlap_ratio(line.bbox, region) >= _FIGURE_OVERLAP
            ),
            None,
        )
        if owner is None:
            rest.append(line)
        else:
            by_region.setdefault(owner, []).append(line)
    cells = _read_tables(page, raw, regions, ctx)
    by_cell: Dict[int, List[Line]] = {}
    free: List[Line] = []
    for line in rest:
        owner = next((i for i, cell in enumerate(cells) if _centre_inside(line.bbox, cell)), None)
        if owner is None:
            free.append(line)
        else:
            by_cell.setdefault(owner, []).append(line)

    def make(group: Sequence[Line], bbox: Optional[BBox] = None) -> _Unit:
        text, join_warnings = join_lines([_line_text(ln) for ln in group], ctx.compounds)
        box = bbox
        if box is None:
            box = group[0].bbox
            for ln in group[1:]:
                box = union_bbox(box, ln.bbox)
        unit = _Unit(list(group), box, OverlayBlockKind.BODY, text)
        if join_warnings:
            unit.warnings.append("ambiguous_hyphen")
        return unit

    for index in sorted(by_region):
        for group in _merge_row_groups(_group(by_region[index], ctx), raw, ctx):
            unit = make(group)
            if len(group) == 1:
                unit.container = _smallest_drawing_around(raw, unit.bbox)
            add(unit, True, False)
    for index in sorted(by_cell):
        add(make(sorted(by_cell[index], key=lambda ln: (ln.bbox[1], ln.bbox[0])), cells[index]),
            False, True)
    # Display math bands next: one kept unit each, and the lines they leave behind are
    # grouped per segment so that no translated paragraph box ever spans a band.
    # a band reaching into a table cell would fight the cell for the same rows; the cell
    # path owns them, so that band is dropped and its lines fall back to plain grouping
    bands = [
        band
        for band in _math_bands(free, raw, ctx)
        if not any(_intersection(band.bbox, cell) > 0 for cell in cells)
    ]
    for band in bands:
        add(_math_unit(band), False, False)
    for segment in _band_segments(free, bands):
        for group in _merge_row_groups(_group(segment, ctx, paragraphs=True), raw, ctx):
            add(make(group), False, False)
    _clip_off_bands(units, bands)
    return units, flags_figure, flags_table


def _group(
    lines: Sequence[Line], ctx: _DocContext, *, paragraphs: bool = False
) -> List[List[Line]]:
    """Line groups; ``paragraphs`` (running text only - never figure labels or table
    cells) also splits a group at first-line indents / short last lines."""
    return group_lines(
        lines,
        line_gap_ratio=ctx.options.line_gap_ratio,
        min_h_overlap=ctx.options.min_h_overlap,
        paragraphs=paragraphs,
    )


def _rotation_of(page: Any) -> int:
    try:
        return page_rotation(page)
    except Exception:  # noqa: BLE001 - a page without a readable /Rotate is treated as upright
        return 0


def _min_rect_height(unit: _Unit) -> float:
    """The height below which this unit's rect becomes unusable (§4.4 step 4).

    One line of the unit at the exporter's floor scale (:data:`_MIN_RECT_LINE_SCALE`),
    measured on its *shortest* source line: a paragraph whose rows are 13 pt apart needs
    6.5 pt to still show a line, a 6 pt superscript fragment needs 3."""
    heights = [ln.height for ln in unit.lines if ln.height > 0]
    if not heights:
        return 0.0
    return min(heights) * _MIN_RECT_LINE_SCALE


def _page_body_size(raw: RawPage, fallback: float) -> float:
    size = body_font_size(
        (sp.size, len(sp.text.strip())) for b in raw.blocks for ln in b.lines for sp in ln.spans
    )
    return size if size > 0 else fallback


def _process_page(
    page: Any,
    raw: RawPage,
    regions: Sequence[BBox],
    ctx: _DocContext,
    first_unit_id: int,
) -> Tuple[OverlayPage, List[OverlayBlock]]:
    width, height = raw.width, raw.height
    rotation = _rotation_of(page)
    page_body = _page_body_size(raw, ctx.body_size)
    if raw.char_count == 0:
        log.info(
            "overlay_page_skipped:%d:no_text_layer", raw.number,
            extra={"event": "overlay_page_skipped", "page": raw.number},
        )
        return (
            OverlayPage(raw.number, width, height, rotation, False, 0, 0, "no_text_layer",
                        page_body),
            [],
        )
    units, in_figure, in_table = _build_units(raw, page, regions, ctx)
    order = sorted(
        range(len(units)),
        key=lambda i: (round(units[i].bbox[1], 3), round(units[i].bbox[0], 3), i),
    )
    units = [units[i] for i in order]
    in_figure = [in_figure[i] for i in order]
    in_table = [in_table[i] for i in order]
    anchors = _row_anchors(units, raw, ctx)
    for index, unit in enumerate(units):
        unit.kind = _classify(
            unit, raw, page_body, ctx,
            in_figure=in_figure[index], in_table=in_table[index],
            has_next=index + 1 < len(units),
            row_anchor=anchors[index],
        )
        unit.keep_reason = _keep_reason(unit, ctx.options)
    for index, unit in enumerate(units):
        if unit.keep_reason is None and _is_header_guess(index, units, raw, ctx):
            unit.warnings.append(HEADER_GUESS_WARNING)

    rects, unpaintable = split_overlapping_rects(
        [u.bbox for u in units],
        painted=[u.keep_reason is None for u in units],
        min_heights=[_min_rect_height(u) for u in units],
    )
    for index in unpaintable:
        # the rect could not be pulled off its neighbour without being crushed: the unit
        # keeps its source glyphs (and lands on the review list) instead of printing a
        # translation over the neighbour's text
        units[index].keep_reason = "no_bbox"
        units[index].warnings.append(OVERLAP_KEPT_WARNING)
    image_boxes = [img.bbox for img in raw.images]
    # CR-47: only translatable body text takes part in the fragment (split paragraph) test
    body_indexes = [
        i
        for i, u in enumerate(units)
        if u.kind is OverlayBlockKind.BODY and u.keep_reason is None
    ]
    blocks: List[OverlayBlock] = []
    for index, unit in enumerate(units):
        rect = _rounded(rects[index])
        style: OverlayStyle = detect_style(unit.lines)
        alignment: OverlayAlignment = detect_alignment(_row_boxes(unit.lines), unit.container)
        fragment = False
        if index in body_indexes:
            position = body_indexes.index(index)
            if position == len(body_indexes) - 1:
                fragment = not ends_with_terminal(unit.text)
            else:
                nxt = units[body_indexes[position + 1]].text
                fragment = bool(nxt) and nxt[0].islower()
            # the continuation half (first body text of a page / column starting in lower
            # case) is tagged as well, so both halves reach the review list (E-42)
            if unit.text[:1].islower() and parse_list_marker(unit.text) is None:
                fragment = True
        keep = unit.keep_reason
        block_id = format_block_id(raw.number, index + 1)
        for warning in unit.warnings:
            ctx.warnings.append(f"{block_id}:{warning}")
        blocks.append(
            OverlayBlock(
                unit_id=first_unit_id + index,
                block_id=block_id,
                page=raw.number,
                index_on_page=index + 1,
                kind=unit.kind,
                bbox=rect,
                line_boxes=tuple(_rounded(ln.bbox) for ln in unit.lines),
                style=style,
                alignment=alignment,
                source_text=unit.text,
                content_hash=content_hash_of(unit.text),
                char_count=len(unit.text),
                translate=keep is None,
                keep_reason=keep,
                fragment=fragment,
                over_image=any(_intersection(rect, box) > 0 for box in image_boxes),
                warnings=tuple(unit.warnings),
            )
        )
    translatable = sum(1 for b in blocks if b.translate)
    info = OverlayPage(
        page=raw.number,
        width=width,
        height=height,
        rotation=rotation,
        has_text_layer=True,
        block_count=len(blocks),
        translatable_count=translatable,
        skip_reason=None,
        body_size=page_body,
    )
    return info, blocks


# --------------------------------------------------------------------------- #
# Document level
# --------------------------------------------------------------------------- #


def overlay_signature(blocks: Sequence[OverlayBlock], options: OverlayOptions) -> str:
    """sha256 over ``(block_id, bbox, source_text, translate)`` in order, prefixed by the
    two options that change translatability (design doc 06, §3.6.3)."""
    digest = hashlib.sha256()
    digest.update(
        f"overlay:v1:headers={int(options.translate_headers)}:"
        f"figure_text={int(options.keep_figure_text)}\n".encode("utf-8")
    )
    for block in blocks:
        x0, y0, x1, y1 = block.bbox
        digest.update(
            f"{block.block_id}|{x0:.3f},{y0:.3f},{x1:.3f},{y1:.3f}|{int(block.translate)}|"
            .encode("utf-8")
        )
        digest.update(block.source_text.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _band_texts(raw: RawPage, band_ratio: float) -> List[str]:
    return [ln.text for ln in _lines_of(raw) if in_band(ln.bbox, raw.height, band_ratio)]


def _short_band_texts(raw: RawPage, band_ratio: float, body_size: float) -> List[str]:
    return [
        ln.text
        for b in raw.blocks
        if len(b.lines) == 1
        for ln in b.lines
        if in_band(ln.bbox, raw.height, band_ratio)
        and is_short_band_candidate(ln.text, ln.size, body_size)
    ]


def extract_overlay(
    pdf_path: Path,
    options: OverlayOptions,
    figure_regions: Optional[Mapping[int, Sequence[BBox]]],
    progress: Optional[ProgressCallback] = None,
    figure_options: Optional[FigureOptions] = None,
) -> Result[OverlayExtraction]:
    """Units and pages of ``pdf_path`` (design doc 06, §4.4); never raises.

    ``figure_regions`` maps page numbers (1-based) to the figure rects of ``figures.json``;
    lines mostly inside a region become ``FIGURE_LABEL`` units. ``None`` (no inventory, e.g.
    ``translate --mode overlay`` on a fresh directory) runs the F1 detector on the fly with
    ``figure_options`` - the same regions an ``extract`` run would record.
    """
    valid = validate_pdf_path(pdf_path)
    if isinstance(valid, Err):
        return valid
    try:
        return _extract(pdf_path, options, figure_regions, progress, figure_options)
    except Exception as exc:  # noqa: BLE001 - boundary
        return err(
            ErrorCode.EXTRACTION_FAILED,
            f"overlay extraction failed on {pdf_path.name}: {type(exc).__name__}",
            ErrorScope.JOB_FATAL,
            cause=repr(exc)[:500],
        )


def _extract(
    pdf_path: Path,
    options: OverlayOptions,
    figure_regions: Optional[Mapping[int, Sequence[BBox]]],
    progress: Optional[ProgressCallback],
    figure_options: Optional[FigureOptions],
) -> Result[OverlayExtraction]:
    silence_layout_hint()
    warnings: List[str] = []
    try:
        doc = open_document(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - classified
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"cannot open PDF: {pdf_path.name}",
            ErrorScope.USER,
            cause=repr(exc),
        )
    try:
        if doc_needs_password(doc):
            return err(
                ErrorCode.OVERLAY_PDF_RESTRICTED,
                f"PDF is password protected: {pdf_path.name}",
                ErrorScope.USER,
            )
        total = doc_page_count(doc)
        if total <= 0:
            return err(
                ErrorCode.INPUT_UNREADABLE, f"PDF has no pages: {pdf_path.name}", ErrorScope.USER
            )
        raw_pages: List[RawPage] = []
        rotated: Dict[int, Matrix] = {}  # page number -> unrotated space -> visible orientation
        for index in range(total):
            source_page = doc[index]
            raw_page = read_raw_page(source_page, index + 1, warnings, read_drawings=True)
            if _rotation_of(source_page):
                # CR-53: judge direction, bands and alignment in the orientation the reader
                # sees; the stored rects are visible-space and the renderer maps them back.
                matrix = page_rotation_matrix(source_page)
                raw_page = _to_visible(raw_page, matrix, page_size(source_page))
                rotated[raw_page.number] = matrix
            raw_pages.append(raw_page)
        body_size = body_font_size(
            (sp.size, len(sp.text.strip()))
            for p in raw_pages
            for b in p.blocks
            for ln in b.lines
            for sp in ln.spans
        )
        patterns = detect_running_patterns(
            [_band_texts(p, options.band_ratio) for p in raw_pages],
            options.header_footer_page_ratio,
        )
        patterns |= detect_running_patterns(
            [_short_band_texts(p, options.band_ratio, body_size) for p in raw_pages],
            options.header_footer_page_ratio,
            min_pages=max(_SHORT_BAND_MIN_PAGES, len(raw_pages) if len(raw_pages) < 2 else 2),
        )
        compounds = collect_compounds(ln.text for p in raw_pages for ln in _lines_of(p))
        ctx = _DocContext(options, body_size, patterns, compounds, warnings=warnings)
        inventory_regions = figure_regions is not None  # ``figures.json``: unrotated space
        if figure_regions is None:
            detector_options = ExtractOptions(
                header_footer_page_ratio=options.header_footer_page_ratio,
                band_ratio=options.band_ratio,
                figures=figure_options or FigureOptions(),
            )
            figure_regions = detect_figure_regions(
                raw_pages, detector_options, body_size, patterns, compounds, warnings
            )
        pages: List[OverlayPage] = []
        blocks: List[OverlayBlock] = []
        for index, raw in enumerate(raw_pages):
            regions: List[BBox] = [
                (float(r[0]), float(r[1]), float(r[2]), float(r[3]))
                for r in figure_regions.get(raw.number, ())
            ]
            if inventory_regions and raw.number in rotated:
                regions = [transform_rect(r, rotated[raw.number]) for r in regions]
            info, page_blocks = _process_page(
                doc[index], raw, regions, ctx, first_unit_id=len(blocks) + 1
            )
            pages.append(info)
            blocks.extend(page_blocks)
            if progress is not None:
                progress(index + 1, total)
    finally:
        close_document(doc)
    signature = overlay_signature(blocks, options)
    log.info(
        "overlay_extracted: %d units on %d pages (%d translatable)",
        len(blocks),
        len(pages),
        sum(1 for b in blocks if b.translate),
        extra={
            "event": "overlay_extracted",
            "units": len(blocks),
            "pages": len(pages),
            "skipped": sum(1 for p in pages if p.skip_reason),
        },
    )
    return Ok(
        OverlayExtraction(
            pages=tuple(pages),
            blocks=tuple(blocks),
            overlay_sha256=signature,
            warnings=tuple(warnings),
        )
    )
