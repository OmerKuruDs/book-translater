"""Default extractor: PyMuPDF page blocks -> cleaned BT-Markdown (design doc 02, 6.1).

The PDF-facing part is confined to ``_read_page`` and ``_render_figures``;
every heuristic is a pure function in ``cleanup.py`` / ``figures.py``.
``extract`` never raises: pymupdf exceptions are classified into ``Err``.

v1.1 (design doc 06, F1): with ``options.figures.enabled`` the page's vector
drawings and link boxes are read, figure regions are detected after the
header/footer, page-number and table decisions, their inner text is excluded
from the paragraph flow (FR-33), and each region is emitted as an image line
plus its caption paragraph and legend block. With figures disabled the output
is byte-identical to v1.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional, Sequence, Set, Tuple

from ..domain.bt_syntax import format_image_line, format_legend_marker
from ..domain.models import ExtractedDocument, FigureRecord, LabelBox, PageInfo
from ..domain.overlay import OverlayAlignment
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from ..pdfkit.api import (
    close_document,
    open_document,
    page_drawings,
    page_links,
    page_text_dict,
    rect_tuple,
    silence_layout_hint,
)
from .base import (
    BaseExtractor,
    ExtractOptions,
    FigureOptions,
    FigureSink,
    ProgressCallback,
    validate_pdf_path,
)
from .cleanup import (
    BBox,
    Line,
    RawBlock,
    RawDrawing,
    RawImage,
    RawPage,
    RawTable,
    Span,
    assign_heading_tiers,
    body_font_size,
    collect_compounds,
    color_hex,
    compute_source_profile,
    detect_alignment,
    detect_prefix_style,
    detect_running_patterns,
    detect_style,
    in_band,
    is_footnote_marker,
    is_heading_candidate,
    is_monospace_font,
    is_page_number,
    is_running_line,
    is_short_band_candidate,
    is_title_case,
    join_hyphenated,
    join_lines,
    list_level,
    looks_like_caption,
    normalize_pattern,
    numbered_heading_level,
    parse_footnote_definition,
    parse_list_marker,
    round_half,
    same_paragraph,
    should_merge_cross_page,
)
from .figure_render import figure_file_name, render_region
from .figures import BandInfo, FigureRegion, detect_figures, legend_labels
from .langdetect import detect_language
from .normalizer import ensure_bt_markdown

log = logging.getLogger("book_translator.extractors.pymupdf")

_FLAG_SUPERSCRIPT = 1
_FLAG_ITALIC = 2
_FLAG_MONO = 8
_FLAG_BOLD = 16
_MIN_IMAGE_AREA_RATIO = 0.01
_SHORT_BAND_MIN_PAGES = 2
_REGION_IMAGE_OVERLAP = 0.5  # a raster image mostly inside a region belongs to it

NO_TEXT_LAYER_MESSAGE = (
    "no extractable text; run OCR first or install the [marker] extra (--extractor marker)"
)


@dataclass
class _Element:
    """Intermediate page element in reading order (mutable; extractor-internal)."""

    kind: str  # heading | paragraph | list | code | table | image | legend
    page: int
    y0: float
    x0: float
    text: str = ""
    lines: List[str] = field(default_factory=list)
    items: List[Tuple[int, str, str]] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)
    size: float = 0.0
    bold: bool = False
    size_based: bool = False
    level: int = 0
    footnote_ids: List[int] = field(default_factory=list)
    line_height: float = 0.0
    first_on_page: bool = False
    # v1.1 figure fields
    src: Optional[str] = None  # image line ``src`` (None -> bare v1 token)
    sub: int = 0  # stable sub-order for elements sharing a position (image, caption, legend)
    caption: bool = False  # figure caption paragraph: never merged across pages
    labels: List[str] = field(default_factory=list)
    legend_more: int = 0


@dataclass
class _FootnoteDef:
    page: int
    marker: str
    text: str
    ident: Optional[int] = None


@dataclass
class _PendingFigure:
    """A detected region whose PNG is rendered after all pages are processed."""

    region: FigureRegion
    element: _Element
    image_id: int
    file: str
    labels: Tuple[str, ...]
    labels_more: int
    caption_present: bool
    label_boxes: Tuple[LabelBox, ...] = ()  # Addendum A


@dataclass
class _Context:
    options: ExtractOptions
    body_size: float
    patterns: Set[str]
    compounds: Set[str]
    warnings: List[str] = field(default_factory=list)
    removed_patterns: List[str] = field(default_factory=list)
    footnote_counter: int = 0
    image_counter: int = 0
    definitions: Dict[int, _FootnoteDef] = field(default_factory=dict)
    unmatched: List[_FootnoteDef] = field(default_factory=list)
    matched_any: bool = False
    pending_figures: List[_PendingFigure] = field(default_factory=list)
    figure_pages_skipped: List[Tuple[int, str]] = field(default_factory=list)

    def next_footnote_id(self) -> int:
        self.footnote_counter += 1
        return self.footnote_counter

    def next_image_id(self) -> int:
        self.image_counter += 1
        return self.image_counter


# ---------------------------------------------------------------------------
# PDF adapter (the only place touching pymupdf objects)
# ---------------------------------------------------------------------------


def _bbox(values: Sequence[float]) -> BBox:
    return (float(values[0]), float(values[1]), float(values[2]), float(values[3]))


def _colour(value: Any) -> Optional[Tuple[float, float, float]]:
    if value is None:
        return None
    try:
        channels = [float(v) for v in value]
    except (TypeError, ValueError):
        return None
    if len(channels) < 3:
        return None
    return (channels[0], channels[1], channels[2])


def _read_drawings(page: Any, warnings: List[str], number: int) -> List[RawDrawing]:
    drawings: List[RawDrawing] = []
    try:
        for item in page_drawings(page):
            rect = item.get("rect")
            if rect is None:
                continue
            width = item.get("width")
            drawings.append(
                RawDrawing(
                    bbox=rect_tuple(rect),
                    kind=str(item.get("type") or ""),
                    item_count=len(item.get("items") or ()),
                    fill=_colour(item.get("fill")),
                    stroke=_colour(item.get("color")),
                    width=float(width) if width is not None else None,
                )
            )
    except Exception as exc:  # noqa: BLE001 - drawings are best effort
        warnings.append(f"page {number}: drawing extraction failed ({type(exc).__name__})")
        return []
    return drawings


def _read_link_boxes(page: Any, warnings: List[str], number: int) -> List[BBox]:
    boxes: List[BBox] = []
    try:
        for link in page_links(page):
            rect = link.get("from")
            if rect is not None:
                boxes.append(rect_tuple(rect))
    except Exception as exc:  # noqa: BLE001 - links are best effort
        warnings.append(f"page {number}: link extraction failed ({type(exc).__name__})")
        return []
    return boxes


def _read_page(page: Any, number: int, warnings: List[str], read_drawings: bool = False) -> RawPage:
    data: Dict[str, Any] = page_text_dict(page)
    width = float(data.get("width") or page.rect.width)
    height = float(data.get("height") or page.rect.height)
    blocks: List[RawBlock] = []
    images: List[RawImage] = []
    for block in data.get("blocks", []):
        bbox = _bbox(block["bbox"])
        if block.get("type") == 1:
            images.append(RawImage(number, bbox))
            continue
        lines: List[Line] = []
        for line in block.get("lines", []):
            spans: List[Span] = []
            for span in line.get("spans", []):
                text = str(span.get("text", ""))
                if not text:
                    continue
                flags = int(span.get("flags", 0))
                font = str(span.get("font", ""))
                spans.append(
                    Span(
                        text=text,
                        size=float(span.get("size", 0.0)),
                        font=font,
                        bold=bool(flags & _FLAG_BOLD) or "bold" in font.lower(),
                        superscript=bool(flags & _FLAG_SUPERSCRIPT),
                        mono=bool(flags & _FLAG_MONO) or is_monospace_font(font),
                        bbox=_bbox(span["bbox"]),
                        italic=bool(flags & _FLAG_ITALIC) or "italic" in font.lower(),
                        color=color_hex(int(span.get("color", 0) or 0)),
                    )
                )
            if spans and "".join(s.text for s in spans).strip():
                direction = line.get("dir")
                dir_pair = (
                    (float(direction[0]), float(direction[1]))
                    if direction is not None
                    else (1.0, 0.0)
                )
                lines.append(Line(tuple(spans), _bbox(line["bbox"]), dir_pair))
        if lines:
            blocks.append(RawBlock(number, bbox, tuple(lines)))
    tables = _read_tables(page, number, warnings)
    drawings: List[RawDrawing] = []
    link_boxes: List[BBox] = []
    if read_drawings:
        drawings = _read_drawings(page, warnings, number)
        link_boxes = _read_link_boxes(page, warnings, number)
    return RawPage(
        number,
        width,
        height,
        tuple(blocks),
        tuple(images),
        tuple(tables),
        tuple(drawings),
        tuple(link_boxes),
    )


read_raw_page = _read_page
"""Public alias for the overlay extractor (same page adapter, same ``RawPage``)."""


def _read_tables(page: Any, number: int, warnings: List[str]) -> List[RawTable]:
    finder = getattr(page, "find_tables", None)
    if finder is None:
        return []
    tables: List[RawTable] = []
    try:
        found = finder()
        for table in found.tables:
            raw_rows = table.extract()
            rows: List[Tuple[str, ...]] = []
            for raw_row in raw_rows:
                cells = tuple(
                    " ".join(str(cell).split()) if cell is not None else "" for cell in raw_row
                )
                if any(cells):
                    rows.append(cells)
            if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                tables.append(RawTable(number, _bbox(table.bbox), tuple(rows)))
    except Exception as exc:  # noqa: BLE001 - table detection is best effort
        warnings.append(f"page {number}: table detection failed, kept as paragraphs ({exc!r})")
        return []
    return tables


# ---------------------------------------------------------------------------
# Page processing
# ---------------------------------------------------------------------------


def _inside(bbox: BBox, container: BBox, tolerance: float = 1.0) -> bool:
    cx = (bbox[0] + bbox[2]) / 2.0
    cy = (bbox[1] + bbox[3]) / 2.0
    return (
        container[0] - tolerance <= cx <= container[2] + tolerance
        and container[1] - tolerance <= cy <= container[3] + tolerance
    )


def _band_texts(raw: RawPage, band_ratio: float) -> List[str]:
    return [
        ln.text
        for b in raw.blocks
        for ln in b.lines
        if in_band(ln.bbox, raw.height, band_ratio) and ln.text
    ]


def _short_band_texts(raw: RawPage, band_ratio: float, body_size: float) -> List[str]:
    return [
        ln.text
        for b in raw.blocks
        if len(b.lines) == 1
        for ln in b.lines
        if in_band(ln.bbox, raw.height, band_ratio)
        and is_short_band_candidate(ln.text, ln.size, body_size)
    ]


def _keep_line(raw: RawPage, line: Line, ctx: _Context) -> bool:
    """Drop running headers/footers, page numbers, and text inside tables."""
    text = line.text
    if not text:
        return False
    if any(_inside(line.bbox, t.bbox) for t in raw.tables):
        return False
    if in_band(line.bbox, raw.height, ctx.options.band_ratio):
        if is_page_number(text):
            return False
        if is_running_line(text, ctx.patterns):
            pattern = normalize_pattern(text)
            if pattern not in ctx.removed_patterns:
                ctx.removed_patterns.append(pattern)
            return False
    return True


def _is_footnote_def_line(raw: RawPage, line: Line, ctx: _Context) -> bool:
    if ctx.body_size <= 0 or line.size > ctx.body_size * ctx.options.footnote_size_ratio:
        return False
    if line.bbox[1] < raw.height * (2.0 / 3.0):
        return False
    first = line.spans[0]
    if first.superscript and is_footnote_marker(first.text):
        return True
    return parse_footnote_definition(line.text) is not None


def _footnote_def(line: Line) -> Tuple[str, str]:
    first = line.spans[0]
    if first.superscript and is_footnote_marker(first.text):
        rest = "".join(s.text for s in line.spans[1:]).strip()
        return first.text.strip(), rest
    parsed = parse_footnote_definition(line.text)
    if parsed is None:
        return "", line.text
    return parsed


def _line_text_with_refs(
    line: Line, defs: Dict[str, _FootnoteDef], ctx: _Context, refs: List[int]
) -> str:
    """Line text where matched superscript markers become ``[^k]`` references."""
    parts: List[str] = []
    for span in line.spans:
        marker = span.text.strip()
        if span.superscript and is_footnote_marker(marker) and marker in defs:
            definition = defs[marker]
            if definition.ident is None:
                definition.ident = ctx.next_footnote_id()
                ctx.definitions[definition.ident] = definition
                ctx.matched_any = True
            refs.append(definition.ident)
            parts.append(f"[^{definition.ident}]")
        else:
            parts.append(span.text)
    return "".join(parts).strip()


def _code_lines(lines: Sequence[Line]) -> List[str]:
    min_x0 = min(ln.bbox[0] for ln in lines)
    result: List[str] = []
    for ln in lines:
        raw_text = "".join(s.text for s in ln.spans).rstrip()
        if raw_text and not raw_text[0].isspace():
            char_width = max(ln.size, 1.0) * 0.6
            indent = int((ln.bbox[0] - min_x0) / char_width + 0.5)
            raw_text = " " * indent + raw_text
        result.append(raw_text)
    return result


def _split_large_runs(lines: Sequence[Line], ctx: _Context) -> List[Tuple[bool, List[Line]]]:
    """Partition a block into runs of heading-sized vs body-sized lines."""
    threshold = ctx.body_size * ctx.options.heading_size_ratio
    runs: List[Tuple[bool, List[Line]]] = []
    for ln in lines:
        large = ctx.body_size > 0 and ln.size >= threshold
        if runs and runs[-1][0] == large:
            runs[-1][1].append(ln)
        else:
            runs.append((large, [ln]))
    return runs


def _band_info(raw: RawPage, removed: Sequence[BBox], band_ratio: float) -> BandInfo:
    """Header/footer geometry from the lines steps 4-5 removed on this page."""
    middle = raw.height / 2.0
    header_bottom = 0.0
    footer_top = raw.height
    for box in removed:
        centre = (box[1] + box[3]) / 2.0
        if centre < middle:
            header_bottom = max(header_bottom, box[3])
        else:
            footer_top = min(footer_top, box[1])
    return BandInfo(header_bottom, footer_top, tuple(removed), band_ratio)


def _detect_page_figures(
    raw: RawPage, kept_lines: Sequence[Line], removed: Sequence[BBox], ctx: _Context
) -> List[FigureRegion]:
    """Step 1 (page exclusion) + ``detect_figures`` (steps 2-13) for one page."""
    figure_options = ctx.options.figures
    if not figure_options.enabled:
        return []
    if raw.number in figure_options.exclude_pages:
        ctx.figure_pages_skipped.append((raw.number, "excluded"))
        return []
    band = _band_info(raw, removed, ctx.options.band_ratio)
    notes: List[str] = []
    regions = detect_figures(
        raw, kept_lines, band, ctx.body_size, figure_options, frozenset(ctx.compounds), notes
    )
    for note in notes:
        if note not in ctx.warnings:  # the overlay pass may detect the same page again
            ctx.warnings.append(note)
    for region in regions:
        log.info(
            "figure detected on page %d (%s, %d paths, %d images, %d labels)",
            raw.number,
            region.kind.value,
            region.member_drawings,
            region.member_images,
            len(region.labels),
            extra={
                "event": "figure_detected",
                "page": raw.number,
                "kind": region.kind.value,
                "bbox": [round(v, 1) for v in region.bbox],
                "labels": len(region.labels),
                "caption": region.caption_position,
            },
        )
    return regions


def detect_figure_regions(
    raw_pages: Sequence[RawPage],
    options: ExtractOptions,
    body_size: float,
    patterns: Set[str],
    compounds: Set[str],
    notes: Optional[List[str]] = None,
) -> Dict[int, List[BBox]]:
    """Figure rects per page without rendering anything (overlay pass without ``figures.json``).

    Runs the same pass-1 line filter and ``detect_figures`` call as ``_process_page``, so
    the regions equal the ``region`` values an ``extract`` run would write to the inventory.

    CR-86: pass ``notes`` to collect the detector's rejection notes
    (``figure_page_dropped:*``, ``figure_candidate_rejected:*``). Without it they are
    dropped with the throwaway context and the overlay pass reports no reason at all.
    """
    ctx = _Context(options, body_size, set(patterns), set(compounds),
                   warnings=notes if notes is not None else [])
    regions: Dict[int, List[BBox]] = {}
    for raw in raw_pages:
        kept_lines: List[Line] = []
        removed_boxes: List[BBox] = []
        for block in raw.blocks:
            for line in block.lines:
                if _keep_line(raw, line, ctx):
                    kept_lines.append(line)
                elif line.text and in_band(line.bbox, raw.height, options.band_ratio):
                    removed_boxes.append(line.bbox)
        found = _detect_page_figures(raw, kept_lines, removed_boxes, ctx)
        if found:
            regions[raw.number] = [region.bbox for region in found]
    return regions


def _figure_elements(
    raw: RawPage,
    regions: Sequence[FigureRegion],
    kept_lines: Sequence[Line],
    defs: Dict[str, _FootnoteDef],
    ctx: _Context,
) -> List[_Element]:
    """Image line + caption paragraph + legend block per region, in reading order."""
    figure_options = ctx.options.figures
    elements: List[_Element] = []
    for region in regions:
        image_id = ctx.next_image_id()
        file_name = figure_file_name(region.page, region.index_on_page)
        src = f"{figure_options.images_dir_name}/{file_name}"
        y0, x0 = region.bbox[1], region.bbox[0]
        image = _Element("image", raw.number, y0, x0, text=str(image_id), src=src, sub=0)
        elements.append(image)

        caption_present = False
        if region.caption_line_indexes:
            refs: List[int] = []
            caption_lines = [kept_lines[i] for i in region.caption_line_indexes]
            text, warnings = join_lines(
                [_line_text_with_refs(ln, defs, ctx, refs) for ln in caption_lines],
                ctx.compounds,
            )
            ctx.warnings.extend(f"page {raw.number}: {w}" for w in warnings)
            if text:
                caption_present = True
                elements.append(
                    _Element(
                        "paragraph",
                        raw.number,
                        y0,
                        x0,
                        text=text,
                        footnote_ids=refs,
                        line_height=max(ln.height for ln in caption_lines),
                        sub=1,
                        caption=True,
                    )
                )

        labels, more = legend_labels(region, figure_options.legend_limit)
        if not figure_options.legend:
            labels, more = (), 0
        if more > 0:
            warning = f"legend_truncated:{raw.number}:{image_id}"
            ctx.warnings.append(warning)
            log.warning(
                "legend truncated on page %d (%d labels dropped)",
                raw.number,
                more,
                extra={"event": "legend_truncated", "page": raw.number, "dropped": more},
            )
        if labels:
            elements.append(
                _Element(
                    "legend",
                    raw.number,
                    y0,
                    x0,
                    text=str(image_id),
                    labels=list(labels),
                    legend_more=more,
                    sub=2,
                )
            )
        ctx.pending_figures.append(
            _PendingFigure(
                region=region,
                element=image,
                image_id=image_id,
                file=src,
                labels=labels,
                labels_more=more,
                caption_present=caption_present,
                label_boxes=_label_boxes(raw, region, kept_lines, labels),
            )
        )
    return elements


def _label_boxes(
    raw: RawPage, region: FigureRegion, kept_lines: Sequence[Line], labels: Sequence[str]
) -> Tuple[LabelBox, ...]:
    """Geometry + style of every emitted legend label (Addendum A; ``figures.json``).

    Uses the overlay style detection of ``cleanup.py`` so the translated label is painted
    back with the properties the source used. A label that was filtered out of the legend
    (ticks, duplicates, over the limit) has no box.
    """
    by_text = dict(zip(region.labels, region.label_groups, strict=False))
    boxes: List[LabelBox] = []
    for label in labels:
        group = by_text.get(label)
        if not group:
            continue
        lines = [kept_lines[i] for i in group]
        line_boxes = [ln.bbox for ln in lines]
        bbox = (
            min(b[0] for b in line_boxes),
            min(b[1] for b in line_boxes),
            max(b[2] for b in line_boxes),
            max(b[3] for b in line_boxes),
        )
        style = detect_style(lines)
        container = _smallest_drawing_around(raw, bbox)
        alignment = detect_alignment(line_boxes, container)
        boxes.append(
            LabelBox(
                text=label,
                bbox=(round(bbox[0], 3), round(bbox[1], 3), round(bbox[2], 3), round(bbox[3], 3)),
                font_size=style.font_size,
                bold=style.bold,
                italic=style.italic,
                family=style.family,
                color=style.color,
                alignment=_ALIGNMENT_NAMES[alignment],
                prefix_style=detect_prefix_style(lines),
            )
        )
    return tuple(boxes)


_ALIGNMENT_NAMES: Dict[OverlayAlignment, Literal["left", "center", "right", "justify"]] = {
    OverlayAlignment.LEFT: "left",
    OverlayAlignment.CENTER: "center",
    OverlayAlignment.RIGHT: "right",
    OverlayAlignment.JUSTIFY: "justify",
}


def _smallest_drawing_around(raw: RawPage, bbox: BBox) -> Optional[BBox]:
    """The smallest drawing rect (a box of the diagram) containing ``bbox``, if any."""
    best: Optional[BBox] = None
    for drawing in raw.drawings:
        d = drawing.bbox
        contains = (
            d[0] - 1.0 <= bbox[0]
            and d[1] - 1.0 <= bbox[1]
            and d[2] + 1.0 >= bbox[2]
            and d[3] + 1.0 >= bbox[3]
        )
        if contains and (best is None or drawing.area < (best[2] - best[0]) * (best[3] - best[1])):
            best = d
    return best


def _process_page(raw: RawPage, ctx: _Context) -> Tuple[List[_Element], bool, int]:
    """Turn one page into ordered elements; returns ``(elements, skipped, figure_count)``."""
    # Pass 1: lines surviving the header/footer/page-number/table rules (steps 4-5, 13).
    kept_lines: List[Line] = []
    removed_boxes: List[BBox] = []
    for block in raw.blocks:
        for line in block.lines:
            if _keep_line(raw, line, ctx):
                kept_lines.append(line)
            elif line.text and in_band(line.bbox, raw.height, ctx.options.band_ratio):
                removed_boxes.append(line.bbox)

    # Figure detection (design doc 06, §4.1) and FR-33 exclusion.
    regions = _detect_page_figures(raw, kept_lines, removed_boxes, ctx)
    excluded: Set[int] = set()
    for region in regions:
        excluded.update(region.inner_lines)
        excluded.update(region.caption_line_indexes)
    excluded_lines = {id(kept_lines[i]) for i in excluded}

    # Pass 2: footnote definitions and the kept blocks of the paragraph flow.
    defs_by_marker: Dict[str, _FootnoteDef] = {}
    page_defs: List[_FootnoteDef] = []
    kept_blocks: List[List[Line]] = []
    kept_ids = {id(ln) for ln in kept_lines}
    for block in raw.blocks:
        kept: List[Line] = []
        for line in block.lines:
            if id(line) not in kept_ids or id(line) in excluded_lines:
                continue
            if _is_footnote_def_line(raw, line, ctx):
                marker, text = _footnote_def(line)
                if marker:
                    definition = _FootnoteDef(raw.number, marker, text)
                    page_defs.append(definition)
                    defs_by_marker.setdefault(marker, definition)
                elif page_defs:
                    page_defs[-1].text = f"{page_defs[-1].text} {text}".strip()
                continue
            kept.append(line)
        if kept:
            kept_blocks.append(kept)

    if not kept_blocks and not raw.tables and not regions:
        return [], True, 0

    elements: List[_Element] = []
    if regions:
        elements.extend(_figure_elements(raw, regions, kept_lines, defs_by_marker, ctx))
    region_boxes = [r.bbox for r in regions]
    image_boxes = [
        img.bbox
        for img in raw.images
        if (img.bbox[2] - img.bbox[0]) * (img.bbox[3] - img.bbox[1])
        >= _MIN_IMAGE_AREA_RATIO * raw.width * raw.height
        and not _in_region(img.bbox, region_boxes)
    ]
    if ctx.options.keep_images_as_placeholders:
        for box in image_boxes:
            elements.append(
                _Element("image", raw.number, box[1], box[0], text=str(ctx.next_image_id()))
            )
    for table in raw.tables:
        elements.append(
            _Element(
                "table",
                raw.number,
                table.bbox[1],
                table.bbox[0],
                rows=[list(r) for r in table.rows],
            )
        )

    for index, lines in enumerate(kept_blocks):
        next_lines = kept_blocks[index + 1] if index + 1 < len(kept_blocks) else None
        elements.extend(_classify_block(raw, lines, next_lines, image_boxes, defs_by_marker, ctx))

    elements.sort(key=lambda e: (round(e.y0, 1), round(e.x0, 1), e.sub))
    _merge_adjacent(elements)
    text_elements = [e for e in elements if e.kind in ("heading", "paragraph")]
    if text_elements:
        text_elements[0].first_on_page = True

    for definition in page_defs:
        if definition.ident is None:
            definition.ident = ctx.next_footnote_id()
            ctx.definitions[definition.ident] = definition
            ctx.unmatched.append(definition)
    return elements, False, len(regions)


def _in_region(bbox: BBox, regions: Sequence[BBox]) -> bool:
    area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
    if area <= 0:
        return False
    for region in regions:
        overlap = max(0.0, min(bbox[2], region[2]) - max(bbox[0], region[0])) * max(
            0.0, min(bbox[3], region[3]) - max(bbox[1], region[1])
        )
        if overlap / area >= _REGION_IMAGE_OVERLAP:
            return True
    return False


def _classify_block(
    raw: RawPage,
    lines: List[Line],
    next_lines: Optional[List[Line]],
    image_boxes: List[BBox],
    defs: Dict[str, _FootnoteDef],
    ctx: _Context,
) -> List[_Element]:
    page = raw.number
    if len(lines) >= 2 and all(ln.mono for ln in lines):
        return [
            _Element(
                "code", page, lines[0].bbox[1], lines[0].bbox[0], lines=_code_lines(lines)
            )
        ]
    if parse_list_marker(lines[0].text) is not None:
        return [_list_element(lines, page, ctx)]

    block_x0 = min(ln.bbox[0] for ln in lines)
    block_text = " ".join(ln.text for ln in lines)
    below_image = any(
        0 <= lines[0].bbox[1] - box[3] <= 1.5 * max(lines[0].height, 1.0) for box in image_boxes
    )
    caption = below_image and looks_like_caption(block_text)
    next_is_paragraph = bool(
        next_lines
        and parse_list_marker(next_lines[0].text) is None
        and (ctx.body_size <= 0 or next_lines[0].size < ctx.body_size * 1.15)
    )

    elements: List[_Element] = []
    for large, run in _split_large_runs(lines, ctx):
        texts = [ln.text for ln in run]
        size = round_half(max(ln.size for ln in run))
        bold = all(ln.bold for ln in run)
        heading = (
            not caption
            and is_heading_candidate(
                texts,
                size,
                bold,
                ctx.body_size,
                ctx.options.heading_size_ratio,
                followed_by_paragraph=next_is_paragraph or len(lines) > len(run),
            )
        )
        if heading:
            refs: List[int] = []
            text, _ = join_lines([_line_text_with_refs(ln, defs, ctx, refs) for ln in run], set())
            elements.append(
                _Element(
                    "heading",
                    page,
                    run[0].bbox[1],
                    run[0].bbox[0],
                    text=text,
                    size=size,
                    bold=bold,
                    size_based=large,
                    line_height=max(ln.height for ln in run),
                )
            )
            continue
        paragraphs: List[List[Line]] = []
        for ln in run:
            if paragraphs and same_paragraph(paragraphs[-1][-1], ln, block_x0):
                paragraphs[-1].append(ln)
            else:
                paragraphs.append([ln])
        for para in paragraphs:
            refs = []
            joined, warnings = join_lines(
                [_line_text_with_refs(ln, defs, ctx, refs) for ln in para], ctx.compounds
            )
            ctx.warnings.extend(f"page {page}: {w}" for w in warnings)
            if joined:
                elements.append(
                    _Element(
                        "paragraph",
                        page,
                        para[0].bbox[1],
                        para[0].bbox[0],
                        text=joined,
                        footnote_ids=refs,
                        line_height=max(ln.height for ln in para),
                    )
                )
    return elements


def _list_element(lines: List[Line], page: int, ctx: _Context) -> _Element:
    marker_lines = [ln for ln in lines if parse_list_marker(ln.text) is not None]
    base_x0 = min(ln.bbox[0] for ln in marker_lines)
    items: List[Tuple[int, str, str]] = []
    for ln in lines:
        parsed = parse_list_marker(ln.text)
        if parsed is None:
            if items:
                level, marker, text = items[-1]
                joined, _ = join_hyphenated(text, ln.text, ctx.compounds)
                items[-1] = (level, marker, joined)
            continue
        level = list_level(ln.bbox[0], base_x0, ln.size)
        items.append((level, parsed[0], parsed[1]))
    return _Element("list", page, lines[0].bbox[1], lines[0].bbox[0], items=items)


def _merge_adjacent(elements: List[_Element]) -> None:
    """Merge consecutive list blocks and consecutive code blocks on one page."""
    index = 1
    while index < len(elements):
        prev, cur = elements[index - 1], elements[index]
        if prev.kind == cur.kind == "list":
            prev.items.extend(cur.items)
            del elements[index]
            continue
        if prev.kind == cur.kind == "code":
            prev.lines.extend(cur.lines)
            del elements[index]
            continue
        index += 1


# ---------------------------------------------------------------------------
# Document-level passes
# ---------------------------------------------------------------------------


def _merge_cross_page(pages: List[List[_Element]], ctx: _Context) -> None:
    previous: Optional[List[_Element]] = None
    for elements in pages:
        if not elements:
            continue
        if previous:
            last, first = previous[-1], elements[0]
            if (
                last.kind == "paragraph"
                and first.kind == "paragraph"
                and not last.caption
                and not first.caption
                and should_merge_cross_page(last.text, first.text)
            ):
                merged, warning = join_hyphenated(last.text, first.text, ctx.compounds)
                if warning:
                    ctx.warnings.append(f"page {first.page}: {warning}")
                last.text = merged
                last.footnote_ids.extend(first.footnote_ids)
                del elements[0]
                if not elements:
                    continue
        previous = elements


def _assign_heading_levels(pages: List[List[_Element]], ctx: _Context) -> None:
    headings = [e for elements in pages for e in elements if e.kind == "heading"]
    tiers = assign_heading_tiers(h.size for h in headings if h.size_based)
    bold_level = min(3, len(tiers) + 1)
    for elements in pages:
        for index, element in enumerate(elements):
            if element.kind != "heading":
                continue
            level = tiers.get(element.size, bold_level) if element.size_based else bold_level
            numbered = numbered_heading_level(element.text)
            if numbered is not None:
                level = numbered
            elif element.first_on_page and element.size_based and is_title_case(element.text):
                gap = _gap_below(elements, index)
                if gap is not None and gap >= 3.0 * max(element.line_height, 1.0):
                    level = 1
            element.level = level


def _gap_below(elements: List[_Element], index: int) -> Optional[float]:
    current = elements[index]
    for follower in elements[index + 1 :]:
        if follower.kind in ("heading", "paragraph", "list", "code", "table"):
            return follower.y0 - (current.y0 + current.line_height)
    return None


def _table_markdown(rows: List[List[str]]) -> str:
    width = max(len(r) for r in rows)

    def cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ").strip()

    def render(row: List[str]) -> str:
        padded = list(row) + [""] * (width - len(row))
        return "| " + " | ".join(cell(c) for c in padded) + " |"

    header = render(rows[0])
    delimiter = "|" + "|".join([" --- "] * width) + "|"
    body = [render(r) for r in rows[1:]]
    return "\n".join([header, delimiter, *body])


def _serialize(pages: List[List[_Element]], ctx: _Context) -> str:
    blocks: List[str] = []
    pending_notes: List[_FootnoteDef] = []
    unmatched_by_page: Dict[int, List[_FootnoteDef]] = {}
    for definition in ctx.unmatched:
        unmatched_by_page.setdefault(definition.page, []).append(definition)

    def flush_notes() -> None:
        if not pending_notes:
            return
        blocks.append("## Notes")
        for definition in pending_notes:
            blocks.append(f"[^{definition.ident}]: {definition.text}")
        pending_notes.clear()

    for elements in pages:
        for element in elements:
            if element.kind == "heading":
                if element.level == 1:
                    flush_notes()
                blocks.append(f"{'#' * element.level} {element.text}")
            elif element.kind == "paragraph":
                blocks.append(element.text)
                for ident in element.footnote_ids:
                    definition = ctx.definitions[ident]
                    blocks.append(f"[^{ident}]: {definition.text}")
            elif element.kind == "list":
                blocks.append(
                    "\n".join(
                        f"{'  ' * level}{marker} {text}" for level, marker, text in element.items
                    )
                )
            elif element.kind == "code":
                blocks.append("\n".join(["```", *element.lines, "```"]))
            elif element.kind == "table":
                blocks.append(_table_markdown(element.rows))
            elif element.kind == "image":
                blocks.append(format_image_line(int(element.text), element.src))
            elif element.kind == "legend":
                marker_line = format_legend_marker(int(element.text), element.legend_more)
                blocks.append(
                    "\n".join([marker_line, *(f"- {label}" for label in element.labels)])
                )
        if elements:
            pending_notes.extend(unmatched_by_page.get(elements[0].page, []))
    flush_notes()
    return ensure_bt_markdown("\n\n".join(blocks))


# ---------------------------------------------------------------------------
# Figure rendering (after all pages are processed; §4.2)
# ---------------------------------------------------------------------------


def _render_figures(
    pdf_path: Path, ctx: _Context, figure_sink: Optional[FigureSink]
) -> Result[List[FigureRecord]]:
    """Render every pending region, hand the PNG to the sink, build the records."""
    records: List[FigureRecord] = []
    if not ctx.pending_figures:
        return Ok(records)
    figure_options: FigureOptions = ctx.options.figures
    try:
        doc = open_document(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - classified
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"cannot re-open PDF for figure rendering: {pdf_path.name}",
            ErrorScope.USER,
            cause=repr(exc),
        )
    try:
        for pending in ctx.pending_figures:
            region = pending.region
            rendered = render_region(doc[region.page - 1], region.bbox, figure_options)
            if isinstance(rendered, Err):
                # Degrade to the v1 placeholder for this figure; the run continues.
                pending.element.src = None
                warning = f"figure_render_failed:{pending.file.rsplit('/', 1)[-1]}"
                ctx.warnings.append(warning)
                log.warning(
                    "%s: %s",
                    warning,
                    rendered.error.message,
                    extra={"event": "figure_render_failed", "page": region.page},
                )
                continue
            figure = rendered.value
            warnings = tuple(region.warnings) + tuple(figure.warnings)
            record = FigureRecord(
                image_id=pending.image_id,
                page=region.page,
                index_on_page=region.index_on_page,
                kind=region.kind,
                region=region.bbox,
                file=pending.file,
                dpi=figure.dpi,
                width_px=figure.width_px,
                height_px=figure.height_px,
                bytes=len(figure.png),
                dpi_reduced=figure.dpi_reduced,
                label_count=len(pending.labels),
                labels=pending.labels,
                labels_more=pending.labels_more,
                caption_present=pending.caption_present,
                warnings=warnings,
                label_boxes=pending.label_boxes,
            )
            if figure.dpi_reduced or figure.warnings:
                log.warning(
                    "figure %s limited: dpi %d -> %d (%d bytes)",
                    pending.file,
                    figure_options.dpi,
                    figure.dpi,
                    len(figure.png),
                    extra={
                        "event": "figure_limited",
                        "page": region.page,
                        "dpi": figure.dpi,
                        "bytes": len(figure.png),
                    },
                )
            if figure_sink is not None:
                written = figure_sink(record, figure.png)
                if isinstance(written, Err):
                    return written
            log.info(
                "figure rendered: %s (%dx%d px @ %d dpi, %d bytes)",
                pending.file,
                figure.width_px,
                figure.height_px,
                figure.dpi,
                len(figure.png),
                extra={
                    "event": "figure_rendered",
                    "page": region.page,
                    "file": pending.file,
                    "dpi": figure.dpi,
                    "bytes": len(figure.png),
                },
            )
            records.append(record)
    except Exception as exc:  # noqa: BLE001 - boundary
        return err(
            ErrorCode.EXTRACTION_FAILED,
            f"figure rendering failed on {pdf_path.name}: {type(exc).__name__}",
            ErrorScope.JOB_FATAL,
            cause=repr(exc),
        )
    finally:
        close_document(doc)
    return Ok(records)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


class PyMuPDFExtractor(BaseExtractor):
    name: ClassVar[str] = "pymupdf"
    supports_ocr: ClassVar[bool] = False
    supports_footnotes: ClassVar[bool] = True

    @classmethod
    def is_available(cls) -> bool:
        return True  # hard dependency

    def extract(
        self,
        pdf_path: Path,
        options: ExtractOptions,
        progress: Optional[ProgressCallback] = None,
        figure_sink: Optional[FigureSink] = None,
    ) -> Result[ExtractedDocument]:
        validation = validate_pdf_path(pdf_path)
        if isinstance(validation, Err):
            return validation
        try:
            return self._extract(pdf_path, options, progress, figure_sink)
        except Exception as exc:  # noqa: BLE001 - boundary: never raise across layers
            return err(
                ErrorCode.EXTRACTION_FAILED,
                f"pymupdf extraction failed on {pdf_path.name}: {type(exc).__name__}",
                ErrorScope.JOB_FATAL,
                cause=repr(exc),
            )

    def _extract(
        self,
        pdf_path: Path,
        options: ExtractOptions,
        progress: Optional[ProgressCallback],
        figure_sink: Optional[FigureSink],
    ) -> Result[ExtractedDocument]:
        silence_layout_hint()
        warnings: List[str] = []
        try:
            doc = open_document(str(pdf_path))
        except Exception as exc:  # noqa: BLE001 - classified below
            return err(
                ErrorCode.INPUT_UNREADABLE,
                f"cannot open PDF: {pdf_path.name}",
                ErrorScope.USER,
                cause=repr(exc),
            )
        try:
            if doc.needs_pass:
                return err(
                    ErrorCode.INPUT_UNREADABLE,
                    f"PDF is password protected: {pdf_path.name}",
                    ErrorScope.USER,
                )
            total = int(doc.page_count)
            if total <= 0:
                return err(
                    ErrorCode.INPUT_UNREADABLE,
                    f"PDF has no pages: {pdf_path.name}",
                    ErrorScope.USER,
                )
            raw_pages: List[RawPage] = []
            for index in range(total):
                raw_pages.append(
                    _read_page(
                        doc[index], index + 1, warnings, read_drawings=options.figures.enabled
                    )
                )
                if progress is not None:
                    progress(index + 1, total)
        finally:
            close_document(doc)

        char_counts = [p.char_count for p in raw_pages]
        if statistics.median(char_counts) < options.min_chars_per_page:
            return err(
                ErrorCode.NO_TEXT_LAYER,
                NO_TEXT_LAYER_MESSAGE,
                ErrorScope.USER,
                context={"median_chars_per_page": statistics.median(char_counts)},
            )

        body_size = body_font_size(
            (s.size, len(s.text.strip()))
            for p in raw_pages
            for b in p.blocks
            for ln in b.lines
            for s in ln.spans
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
        compounds = collect_compounds(
            ln.text for p in raw_pages for b in p.blocks for ln in b.lines
        )
        ctx = _Context(options, body_size, patterns, compounds, warnings=warnings)

        pages: List[List[_Element]] = []
        page_infos: List[PageInfo] = []
        for raw in raw_pages:
            elements, skipped, figure_count = _process_page(raw, ctx)
            pages.append(elements)
            page_infos.append(
                PageInfo(
                    number=raw.number,
                    char_count=raw.char_count,
                    skipped=skipped,
                    had_images=len(raw.images),
                    figures=figure_count,
                )
            )
        rendered = _render_figures(pdf_path, ctx, figure_sink)
        if isinstance(rendered, Err):
            return rendered
        _merge_cross_page(pages, ctx)
        _assign_heading_levels(pages, ctx)
        markdown = _serialize(pages, ctx)
        language, confidence = detect_language(markdown)
        profile = compute_source_profile(raw_pages, body_size)

        if ctx.matched_any:
            footnote_mode: Literal["markdown", "notes_section", "none"] = "markdown"
        elif ctx.unmatched:
            footnote_mode = "notes_section"
        else:
            footnote_mode = "none"

        return Ok(
            ExtractedDocument(
                markdown=markdown,
                extractor_name=self.name,
                fallback_used=False,
                pages=page_infos,
                detected_language=language,
                language_confidence=confidence,
                removed_headers_footers=list(ctx.removed_patterns),
                footnote_mode=footnote_mode,
                image_count=ctx.image_counter,
                warnings=list(ctx.warnings),
                figures=tuple(rendered.value),
                figure_pages_skipped=tuple(ctx.figure_pages_skipped),
                source_profile=profile,
            )
        )
