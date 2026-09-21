"""Pure figure-region detector: ``RawPage`` -> ``FigureRegion`` list (design doc 06, §4.1, §4.3).

No pymupdf import and no I/O: everything works on the frozen dataclasses of
``cleanup.py`` so the rules can be unit-tested on hand-built pages. The
extractor calls ``detect_figures`` per page after the running header/footer,
page-number and table decisions of v1 §6.1 steps 4-5 and 13.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Literal, Optional, Sequence, Set, Tuple

from ..domain.models import FigureKind
from .base import FigureOptions
from .cleanup import (
    BBox,
    Line,
    RawDrawing,
    RawPage,
    in_band,
    is_label_like,
    is_short_band_candidate,
    join_lines,
    matches_caption_pattern,
)

__all__ = [
    "BandInfo",
    "FigureKind",
    "FigureRegion",
    "detect_figures",
    "is_decorative",
    "is_label_like",
    "legend_labels",
    "rect_gap",
]

log = logging.getLogger("book_translator.extractors.figures")

_MIN_IMAGE_AREA_RATIO = 0.01  # v1 ``_MIN_IMAGE_AREA_RATIO`` (raster candidates, AC US-14/1)
_BACKGROUND_AREA_RATIO = 0.90
# CR-38: a raster this large with page text on top of it is a scan / paper texture, not a figure.
_BACKGROUND_IMAGE_AREA_RATIO = 0.60
_BACKGROUND_IMAGE_TEXT_LINES = 3
# CR-38: prose inside a candidate. A cluster is rejected when prose covers this share of its
# union, or a smaller share while the prose clearly outweighs the label text. CR-84: "clearly"
# means several times the label characters *and* more than one multi-line block - labels are
# short by definition, so the bare comparison was a 15 % area gate on real diagrams.
_PROSE_AREA_REJECT_RATIO = 0.35
_PROSE_AREA_MIXED_RATIO = 0.15
_PROSE_MIXED_CHAR_FACTOR = 2.0
_PROSE_MIXED_MIN_BLOCKS = 2
# CR-38 safety net, narrowed by CR-83: the figures of a page may own the page's *text* (a
# full-page schematic is labels and nothing else, doc 06 §4.1 "even with no prose"), but never
# its *prose*. The net fires when prose left the paragraph flow - measured against the prose
# that stayed behind, not against the whole page - or when the absorbed text fills the figures
# themselves (a framed index page is a text page with a border, not a picture with labels).
_PAGE_ABSORB_MAX_RATIO = 0.40
_PAGE_ABSORB_MIN_CHARS = 400
_PAGE_LABEL_MAX_CHARS = 40
_PAGE_TEXT_FILL_RATIO = 0.50
# A stacked legend (one text block, several short entries) is still label text.
_STACK_MAX_LINES = 8
_STACK_MAX_LINE_CHARS = 30
_STACK_MAX_LINE_WORDS = 3
# CR-60: spatial grid for neighbour queries.
_GRID_MIN_CELL_PT = 4.0
_GRID_MAX_CELLS_PER_BOX = 4096
_WHITE_CHANNEL = 0.95
_THIN_PT = 2.0
_TABLE_BORDER_TOLERANCE_PT = 2.0
_LINK_BOX_TOLERANCE_PT = 1.0
_BULLET_SIZE_RATIO = 0.6
_LABEL_ATTACH_ROUNDS = 3
_CAPTION_GAP_LINES = 3.0
_SUBFIGURE_GAP_LINES = 3.0
_TABLE_OVERLAP_RATIO = 0.5
_CLIP_MARGIN_PT = 2.0
_DEFAULT_LINE_HEIGHT = 12.0
_SUBFIGURE_MARKERS = ("(a)", "(b)")
_NUMERIC_LABEL_RE = re.compile(r"^\W*\d[\d\W]*$")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Public value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FigureRegion:
    """One detected figure on a page (design doc 06, §3.2)."""

    page: int
    index_on_page: int  # 1-based, reading order
    kind: FigureKind
    bbox: BBox  # final region incl. border, clipped to the body band
    member_drawings: int
    member_images: int
    inner_lines: Tuple[int, ...]  # indexes into the page's kept lines (FR-33 exclusion)
    labels: Tuple[str, ...]  # distinct label texts in reading order (before legend filtering)
    caption_line_indexes: Tuple[int, ...]  # caption block lines (never inside bbox)
    caption_position: Literal["below", "above", "none"]
    warnings: Tuple[str, ...]
    # Addendum A: kept-line indexes per label, aligned with ``labels`` (same order, same
    # de-duplication) so the extractor can record geometry/style per label.
    label_groups: Tuple[Tuple[int, ...], ...] = ()


@dataclass(frozen=True)
class BandInfo:
    """What v1 §6.1 steps 4-5 decided for this page (header/footer geometry)."""

    header_bottom: float  # y1 of the lowest removed header/page-number line above the body, or 0
    footer_top: float  # y0 of the highest removed footer/page-number line below the body, or height
    removed_line_boxes: Tuple[BBox, ...]
    band_ratio: float = 0.12  # v1 ``ExtractOptions.band_ratio`` (band decoration rule)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _area(bbox: BBox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _union(a: BBox, b: BBox) -> BBox:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _intersection_area(a: BBox, b: BBox) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _expand(bbox: BBox, margin: float) -> BBox:
    return (bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin)


def _contains(container: BBox, inner: BBox) -> bool:
    return (
        inner[0] >= container[0]
        and inner[1] >= container[1]
        and inner[2] <= container[2]
        and inner[3] <= container[3]
    )


def rect_gap(a: BBox, b: BBox) -> float:
    """Separating distance of two boxes: ``max(dx, dy)``; 0 when they overlap or touch."""
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return max(dx, dy)


def _is_thin(drawing: RawDrawing) -> bool:
    width = drawing.bbox[2] - drawing.bbox[0]
    height = drawing.bbox[3] - drawing.bbox[1]
    return height < _THIN_PT or width < _THIN_PT


def _is_small(drawing: RawDrawing, body_size: float) -> bool:
    """Bullet/glyph sized: both sides at most 0.6 x the body font size."""
    return body_size > 0 and (
        (drawing.bbox[2] - drawing.bbox[0]) <= _BULLET_SIZE_RATIO * body_size
        and (drawing.bbox[3] - drawing.bbox[1]) <= _BULLET_SIZE_RATIO * body_size
    )


def _is_background(drawing: RawDrawing, page_area: float) -> bool:
    if page_area > 0 and drawing.area >= _BACKGROUND_AREA_RATIO * page_area:
        return True
    return (
        drawing.kind == "f"
        and drawing.fill is not None
        and all(channel >= _WHITE_CHANNEL for channel in drawing.fill)
        and drawing.item_count <= 1
    )


# ---------------------------------------------------------------------------
# Step 2: decorative filter
# ---------------------------------------------------------------------------


class _Grid:
    """Uniform grid over bounding boxes: neighbour queries and gap clustering without the
    n x n scan (CR-60).

    Boxes spanning very many cells go to a short ``large`` list that is checked linearly, so
    one page-wide rectangle cannot blow the index up.
    """

    def __init__(self, boxes: Sequence[BBox], reach: float) -> None:
        self._boxes = boxes
        self._reach = max(0.0, reach)
        self._cell = max(_GRID_MIN_CELL_PT, self._reach)
        self._cells: Dict[Tuple[int, int], List[int]] = {}
        self._large: List[int] = []
        for index, box in enumerate(boxes):
            cx0, cy0, cx1, cy1 = self._span(box, 0.0)
            if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > _GRID_MAX_CELLS_PER_BOX:
                self._large.append(index)
                continue
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    self._cells.setdefault((cx, cy), []).append(index)

    def _span(self, box: BBox, margin: float) -> Tuple[int, int, int, int]:
        cell = self._cell
        return (
            int((box[0] - margin) // cell),
            int((box[1] - margin) // cell),
            int((box[2] + margin) // cell),
            int((box[3] + margin) // cell),
        )

    def near(self, box: BBox) -> List[int]:
        """Indexes of the boxes whose ``rect_gap`` to ``box`` is at most ``reach`` (sorted)."""
        found: Set[int] = set(self._large)
        cx0, cy0, cx1, cy1 = self._span(box, self._reach)
        if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > _GRID_MAX_CELLS_PER_BOX:
            found.update(range(len(self._boxes)))
        else:
            for cx in range(cx0, cx1 + 1):
                for cy in range(cy0, cy1 + 1):
                    found.update(self._cells.get((cx, cy), ()))
        return sorted(i for i in found if rect_gap(box, self._boxes[i]) <= self._reach)

    def join_components(self, uf: "_UnionFind") -> None:
        """Join every pair with ``rect_gap <= reach`` (same components as the pairwise scan).

        With ``cell == reach`` two boxes touching one cell are within reach by construction,
        so a cell is chained without a single distance test, and two neighbouring cells need
        only one successful pair. Two boxes within reach always touch the same or two
        neighbouring cells (their column and row ranges are at most one cell apart).
        """
        boxes, reach = self._boxes, self._reach
        exact = self._cell <= reach
        for members in self._cells.values():
            if exact:
                for other in members[1:]:
                    uf.join(members[0], other)
            else:
                self._join_pairs(uf, members, members)
        for (cx, cy), members in self._cells.items():
            for dx, dy in ((1, -1), (1, 0), (1, 1), (0, 1)):
                others = self._cells.get((cx + dx, cy + dy))
                if not others:
                    continue
                if exact:
                    if uf.find(members[0]) == uf.find(others[0]):
                        continue
                    self._join_pairs(uf, members, others, first_only=True)
                else:
                    self._join_pairs(uf, members, others)
        for index in self._large:
            for other in range(len(boxes)):
                if other != index and rect_gap(boxes[index], boxes[other]) <= reach:
                    uf.join(index, other)

    def _join_pairs(
        self, uf: "_UnionFind", left: Sequence[int], right: Sequence[int], first_only: bool = False
    ) -> None:
        boxes, reach = self._boxes, self._reach
        for a in left:
            for b in right:
                if a == b or uf.find(a) == uf.find(b):
                    continue
                if rect_gap(boxes[a], boxes[b]) <= reach:
                    uf.join(a, b)
                    if first_only:
                        return


def is_decorative(
    drawing: RawDrawing,
    raw: RawPage,
    neighbours: Sequence[RawDrawing],
    options: FigureOptions,
    body_size: float = 0.0,
    band_ratio: float = 0.12,
) -> Optional[str]:
    """Reason a drawing is decoration (FR-30, E-24), or ``None`` when it is a candidate.

    ``neighbours`` may be the whole page; ``detect_figures`` passes only the drawings a
    spatial index found within ``merge_gap_pt`` (same result, no quadratic scan).
    """
    page_area = raw.width * raw.height
    if _is_background(drawing, page_area):
        return "background"
    for table in raw.tables:
        if _contains(_expand(table.bbox, _TABLE_BORDER_TOLERANCE_PT), drawing.bbox):
            return "table_border"
    for box in raw.link_boxes:
        if _contains(_expand(box, _LINK_BOX_TOLERANCE_PT), drawing.bbox):
            return "link_box"
    thin = _is_thin(drawing)
    if thin and in_band(drawing.bbox, raw.height, band_ratio):
        return "band_rule"
    small = _is_small(drawing, body_size)
    if not thin and not small:
        return None
    isolated = not any(
        other is not drawing
        and not _is_thin(other)
        and not _is_small(other, body_size)
        and not _is_background(other, page_area)
        and rect_gap(drawing.bbox, other.bbox) <= options.merge_gap_pt
        for other in neighbours
    )
    if not isolated:
        return None  # arrows / axis ticks next to a cluster are part of the figure
    return "rule" if thin else "bullet"


# ---------------------------------------------------------------------------
# Internal cluster state
# ---------------------------------------------------------------------------


@dataclass
class _Cluster:
    union: BBox
    drawings: int = 0
    images: int = 0
    inner: List[int] = field(default_factory=list)  # kept-line indexes (inner + attached)
    caption: Optional[int] = None  # index into the page's text blocks
    caption_position: Literal["below", "above", "none"] = "none"
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class _TextBlock:
    """Kept lines regrouped by their original ``RawBlock`` (indexes into ``kept_lines``)."""

    indexes: Tuple[int, ...]
    bbox: BBox


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def join(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _group_blocks(raw: RawPage, kept_lines: Sequence[Line]) -> List[_TextBlock]:
    """Regroup the flat kept lines by their source ``RawBlock`` (document order)."""
    owner: Dict[int, int] = {}
    for block_index, block in enumerate(raw.blocks):
        for line in block.lines:
            owner.setdefault(id(line), block_index)
    groups: Dict[int, List[int]] = {}
    order: List[int] = []
    for index, line in enumerate(kept_lines):
        found = owner.get(id(line))
        # A line not taken from ``raw`` (hand-built tests) forms its own block.
        block_index = found if found is not None else -1 - index
        if block_index not in groups:
            groups[block_index] = []
            order.append(block_index)
        groups[block_index].append(index)
    blocks: List[_TextBlock] = []
    for key in order:
        indexes = groups[key]
        boxes = [kept_lines[i].bbox for i in indexes]
        bbox = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        blocks.append(_TextBlock(tuple(indexes), bbox))
    return blocks


def _line_height(kept_lines: Sequence[Line], body_size: float) -> float:
    heights = [ln.height for ln in kept_lines if ln.height > 0]
    if heights:
        return float(statistics.median(heights))
    if body_size > 0:
        return body_size * 1.2
    return _DEFAULT_LINE_HEIGHT


def _body_width(blocks: Sequence[_TextBlock], kept_lines: Sequence[Line], raw: RawPage) -> float:
    widths = [
        b.bbox[2] - b.bbox[0]
        for b in blocks
        if len(b.indexes) >= 2
        or len(" ".join(kept_lines[i].text for i in b.indexes)) >= 60
    ]
    if widths:
        return max(widths)
    return raw.width * 0.6


def _is_band_line(line: Line, raw: RawPage, band: BandInfo, body_size: float) -> bool:
    return in_band(line.bbox, raw.height, band.band_ratio) and is_short_band_candidate(
        line.text, line.size, body_size
    )


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def detect_figures(
    raw: RawPage,
    kept_lines: Sequence[Line],
    band: BandInfo,
    body_size: float,
    options: FigureOptions,
    compounds: FrozenSet[str] = frozenset(),
    notes: Optional[List[str]] = None,
) -> List[FigureRegion]:
    """Figure regions of one page in reading order (design doc 06, §4.1 steps 2-13).

    Step 1 (page exclusion) is the caller's decision so the skip reason can be
    recorded; this function returns ``[]`` for excluded pages as well.

    Text safety (CR-38): only label-like text becomes a figure member. A raster under the
    page text is background, prose inside a candidate stays in the paragraph flow, a
    candidate that is mostly prose is rejected, and figures that would take the page's prose
    out of the flow are dropped (CR-83). Every one of those brakes can also cost a real
    figure, so each one is appended to ``notes`` for the caller's warning list (CR-85/CR-86):
    ``figure_page_dropped:<page>:text_heavy`` and
    ``figure_candidate_rejected:<page>:<background_image|prose>``.
    """
    if not options.enabled or raw.number in options.exclude_pages:
        return []
    page_area = raw.width * raw.height
    if page_area <= 0:
        return []
    line_height = _line_height(kept_lines, body_size)

    # Step 2: decorative filter; step 3: candidates.
    candidates: List[Tuple[BBox, bool]] = []  # (bbox, is_image)
    solid = [
        d
        for d in raw.drawings
        if not _is_thin(d) and not _is_small(d, body_size) and not _is_background(d, page_area)
    ]
    solid_grid = _Grid([d.bbox for d in solid], options.merge_gap_pt)
    for drawing in raw.drawings:
        neighbours: Sequence[RawDrawing] = ()
        if _is_thin(drawing) or _is_small(drawing, body_size):
            # Only thin / small drawings look at their neighbours (CR-60: indexed lookup).
            neighbours = [solid[i] for i in solid_grid.near(drawing.bbox)]
        reason = is_decorative(drawing, raw, neighbours, options, body_size, band.band_ratio)
        if reason is None:
            candidates.append((drawing.bbox, False))
        else:
            log.debug(
                "page %d: drawing skipped as %s",
                raw.number,
                reason,
                extra={"event": "figure_drawing_decorative", "page": raw.number, "reason": reason},
            )
    for image in raw.images:
        if _area(image.bbox) < _MIN_IMAGE_AREA_RATIO * page_area:
            continue
        if _is_background_image(image.bbox, raw, kept_lines, options.inner_text_overlap):
            log.info(
                "figure_candidate_rejected:%d:background_image",
                raw.number,
                extra={
                    "event": "figure_candidate_rejected",
                    "page": raw.number,
                    "reason": "background_image",
                },
            )
            # CR-85: the rule also costs real full-page plates that carry three lines of
            # text, so the rejection is a business warning, not an INFO line only.
            _note(notes, f"figure_candidate_rejected:{raw.number}:background_image")
            continue
        candidates.append((image.bbox, True))
    if not candidates:
        return []

    # Step 4: clustering by separating distance (grid index, CR-60).
    uf = _UnionFind(len(candidates))
    _Grid([bbox for bbox, _ in candidates], options.merge_gap_pt).join_components(uf)
    grouped: Dict[int, _Cluster] = {}
    for index, (bbox, is_image) in enumerate(candidates):
        root = uf.find(index)
        cluster = grouped.get(root)
        if cluster is None:
            cluster = _Cluster(union=bbox)
            grouped[root] = cluster
        else:
            cluster.union = _union(cluster.union, bbox)
        if is_image:
            cluster.images += 1
        else:
            cluster.drawings += 1

    # Step 5: cluster acceptance.
    clusters: List[_Cluster] = []
    for cluster in grouped.values():
        area_ratio = _area(cluster.union) / page_area
        if cluster.images >= 1:
            accepted = area_ratio >= _MIN_IMAGE_AREA_RATIO  # AC US-14/1: raster >= 1 %
        else:
            accepted = (
                cluster.drawings >= options.min_paths and area_ratio >= options.min_area_ratio
            )
        if accepted:
            clusters.append(cluster)
        elif cluster.drawings >= 2:
            reason = "few_paths" if cluster.drawings < options.min_paths else "small_area"
            log.info(
                "figure_candidate_rejected:%d:%s",
                raw.number,
                reason,
                extra={
                    "event": "figure_candidate_rejected",
                    "page": raw.number,
                    "reason": reason,
                    "paths": cluster.drawings,
                    "area_pct": round(area_ratio * 100, 2),
                },
            )
    if not clusters:
        return []

    blocks = _group_blocks(raw, kept_lines)
    body_width = _body_width(blocks, kept_lines, raw)

    # Steps 6-9 per cluster.
    claimed: Set[int] = set()  # kept-line indexes already owned by a cluster
    for cluster in clusters:
        _absorb_inner(
            cluster, blocks, kept_lines, claimed, options.inner_text_overlap, body_size, body_width
        )
    for cluster in clusters:
        _attach_labels(
            cluster,
            blocks,
            kept_lines,
            claimed,
            raw,
            band,
            body_size,
            body_width,
            line_height,
            options,
        )
        _absorb_inner(
            cluster, blocks, kept_lines, claimed, options.inner_text_overlap, body_size, body_width
        )
        _clip_to_band(cluster, _blockers(cluster, raw, band, kept_lines, body_size), band)

    # CR-38 (b): a candidate that is mostly prose is a note box / page frame, not a figure.
    text_safe: List[_Cluster] = []
    for cluster in clusters:
        if _is_prose_cluster(
            cluster, blocks, kept_lines, claimed, options.inner_text_overlap
        ):
            for index in cluster.inner:
                claimed.discard(index)
            log.info(
                "figure_candidate_rejected:%d:prose",
                raw.number,
                extra={
                    "event": "figure_candidate_rejected",
                    "page": raw.number,
                    "reason": "prose",
                    "paths": cluster.drawings,
                },
            )
            # CR-85/CR-86: a diagram with a long note inside it lands here too, so the
            # rejection reaches the caller's warning list instead of the log only.
            _note(notes, f"figure_candidate_rejected:{raw.number}:prose")
            continue
        text_safe.append(cluster)
    clusters = text_safe
    if not clusters:
        return []
    for cluster in clusters:
        _associate_caption(cluster, blocks, kept_lines, claimed, line_height)

    # Step 10: table precedence.
    survivors: List[_Cluster] = []
    for cluster in clusters:
        dropped = False
        for table in raw.tables:
            table_area = _area(table.bbox)
            if table_area > 0 and (
                _intersection_area(cluster.union, table.bbox) / table_area >= _TABLE_OVERLAP_RATIO
            ):
                dropped = True
                break
        if dropped:
            log.info(
                "figure_dropped:table (page %d)",
                raw.number,
                extra={"event": "figure_dropped", "page": raw.number, "reason": "table"},
            )
            for index in cluster.inner:
                claimed.discard(index)
            continue
        survivors.append(cluster)
    clusters = survivors

    # Step 11: reading order and caption-driven sub-figure merge.
    clusters.sort(key=lambda c: (round(c.union[1]), c.union[0]))
    clusters = _merge_subfigures(clusters, blocks, kept_lines, line_height, raw.number)
    _resolve_shared_captions(clusters)
    clusters.sort(key=lambda c: (round(c.union[1]), c.union[0]))

    # CR-38 (c) / CR-83: page-level safety net. Whatever the rules above decided, the figures
    # of one page never take the page's *prose* out of the paragraph flow - while a page that
    # is itself a figure (labels only, no prose to lose) keeps its figures.
    split = _page_text_split(clusters, kept_lines)
    log.debug(
        "figure_page_text:%d (%d of %d characters absorbed, ratio %.3f, prose %d, fill %.3f)",
        raw.number,
        split.absorbed_chars,
        split.page_chars,
        split.absorbed_ratio,
        split.absorbed_prose_chars,
        split.fill,
        extra={
            "event": "figure_page_text",
            "page": raw.number,
            "absorbed_chars": split.absorbed_chars,
            "page_chars": split.page_chars,
            "absorbed_ratio": round(split.absorbed_ratio, 3),
            "absorbed_prose_chars": split.absorbed_prose_chars,
            "flow_chars": split.flow_chars,
            "fill": round(split.fill, 3),
        },
    )
    if split.text_heavy:
        log.warning(
            "figure_page_dropped:%d:text_heavy (%d of %d characters, %d prose, fill %.2f)",
            raw.number,
            split.absorbed_chars,
            split.page_chars,
            split.absorbed_prose_chars,
            split.fill,
            extra={
                "event": "figure_page_dropped",
                "page": raw.number,
                "reason": "text_heavy",
                "absorbed_chars": split.absorbed_chars,
                "page_chars": split.page_chars,
                "absorbed_prose_chars": split.absorbed_prose_chars,
                "flow_chars": split.flow_chars,
                "fill": round(split.fill, 3),
            },
        )
        _note(notes, f"figure_page_dropped:{raw.number}:text_heavy")
        return []

    # Step 13: final regions.
    regions: List[FigureRegion] = []
    for index, cluster in enumerate(clusters, start=1):
        blockers = _blockers(cluster, raw, band, kept_lines, body_size)
        bbox = _final_bbox(cluster, raw, band, blocks, blockers, options)
        if cluster.images and cluster.drawings:
            kind = FigureKind.MIXED
        elif cluster.images:
            kind = FigureKind.RASTER
        else:
            kind = FigureKind.VECTOR
        caption_indexes: Tuple[int, ...] = ()
        if cluster.caption is not None:
            caption_indexes = blocks[cluster.caption].indexes
        labels, label_groups = _labels_in_reading_order(
            cluster, blocks, kept_lines, line_height, compounds
        )
        regions.append(
            FigureRegion(
                page=raw.number,
                index_on_page=index,
                kind=kind,
                bbox=bbox,
                member_drawings=cluster.drawings,
                member_images=cluster.images,
                inner_lines=tuple(sorted(cluster.inner)),
                labels=labels,
                caption_line_indexes=caption_indexes,
                caption_position=cluster.caption_position,
                warnings=tuple(cluster.warnings),
                label_groups=label_groups,
            )
        )
    return regions


def _note(notes: Optional[List[str]], note: str) -> None:
    """Record a page-level decision once (a page can reject several candidates)."""
    if notes is not None and note not in notes:
        notes.append(note)


@dataclass(frozen=True)
class _PageText:
    """How one page's characters are split between its figures and the paragraph flow."""

    absorbed_chars: int  # characters the clusters took out of the flow (labels included)
    page_chars: int  # every kept character of the page
    absorbed_prose_chars: int  # of the absorbed characters, the ones that read as prose
    flow_chars: int  # kept characters no cluster took (captions included)
    fill: float  # absorbed text area / area of the cluster unions, capped at 1

    @property
    def absorbed_ratio(self) -> float:
        return self.absorbed_chars / self.page_chars if self.page_chars else 0.0

    @property
    def text_heavy(self) -> bool:
        """CR-83: the page's text belongs to the paragraph flow, not to the figures.

        Two independent shapes of that failure, both behind the ``_PAGE_ABSORB_MIN_CHARS``
        floor: prose that left the flow (weighed against the prose that stayed, so a page
        whose only text is labels can never trip it), and absorbed text that fills the
        figures themselves (a framed index or contents page).
        """
        if self.absorbed_chars < _PAGE_ABSORB_MIN_CHARS:
            return False
        prose_chars = self.absorbed_prose_chars + self.flow_chars
        prose_lost = (
            self.absorbed_prose_chars >= _PAGE_ABSORB_MIN_CHARS
            and self.absorbed_prose_chars > _PAGE_ABSORB_MAX_RATIO * prose_chars
        )
        return prose_lost or self.fill >= _PAGE_TEXT_FILL_RATIO


def _is_label_line(line: Line) -> bool:
    """Page-level label test for a single absorbed line (CR-83).

    Steps 6-7 judge text per source block, which is what lets a scanned narrow column
    through: every one of its lines is its own short block. The safety net therefore looks
    at each absorbed line on its own - a label is short, a line of prose is not.
    """
    return len(line.text.strip()) <= _PAGE_LABEL_MAX_CHARS


def _page_text_split(clusters: Sequence[_Cluster], kept_lines: Sequence[Line]) -> _PageText:
    """Measure what the figures of the page took from the paragraph flow (CR-83)."""
    absorbed = {index for cluster in clusters for index in cluster.inner}
    page_chars = sum(len(ln.text) for ln in kept_lines)
    absorbed_chars = sum(len(kept_lines[i].text) for i in absorbed)
    prose_chars = sum(
        len(kept_lines[i].text) for i in absorbed if not _is_label_line(kept_lines[i])
    )
    union_area = sum(_area(cluster.union) for cluster in clusters)
    text_area = sum(_area(kept_lines[i].bbox) for i in absorbed)
    fill = min(1.0, text_area / union_area) if union_area > 0 else 0.0
    return _PageText(
        absorbed_chars=absorbed_chars,
        page_chars=page_chars,
        absorbed_prose_chars=prose_chars,
        flow_chars=page_chars - absorbed_chars,
        fill=fill,
    )


def _is_background_image(
    bbox: BBox, raw: RawPage, kept_lines: Sequence[Line], overlap_ratio: float
) -> bool:
    """CR-38 (a): a raster covering most of the page with the page text on top of it is a
    scanned page / paper texture (OCR "sandwich"), never a figure candidate. A text-free
    full-page plate stays a candidate: there is nothing it could swallow."""
    page_box: BBox = (0.0, 0.0, raw.width, raw.height)
    page_area = raw.width * raw.height
    if _intersection_area(bbox, page_box) < _BACKGROUND_IMAGE_AREA_RATIO * page_area:
        return False
    on_top = 0
    for line in kept_lines:
        area = _area(line.bbox)
        if area > 0 and _intersection_area(line.bbox, bbox) / area >= overlap_ratio:
            on_top += 1
            if on_top >= _BACKGROUND_IMAGE_TEXT_LINES:
                return True
    return False


def _inside(
    cluster: _Cluster, block: _TextBlock, kept_lines: Sequence[Line], ratio: float
) -> List[int]:
    """Indexes of the block's lines that lie mostly inside the cluster union."""
    found: List[int] = []
    for index in block.indexes:
        area = _area(kept_lines[index].bbox)
        if area > 0 and _intersection_area(kept_lines[index].bbox, cluster.union) / area >= ratio:
            found.append(index)
    return found


def _is_label_text(lines: Sequence[Line], body_size: float, body_width: float) -> bool:
    """Label test for text inside a candidate: the step-7 ``is_label_like`` rule, or a stack
    of short entries (chart legend) that the PDF stores as one text block."""
    if is_label_like(lines, body_size, body_width):
        return True
    texts = [ln.text.strip() for ln in lines if ln.text.strip()]
    if not texts or len(texts) > _STACK_MAX_LINES:
        return False
    if matches_caption_pattern(texts[0]):
        return False
    return all(
        len(text) <= _STACK_MAX_LINE_CHARS and len(text.split()) <= _STACK_MAX_LINE_WORDS
        for text in texts
    )


def _absorb_inner(
    cluster: _Cluster,
    blocks: Sequence[_TextBlock],
    kept_lines: Sequence[Line],
    claimed: Set[int],
    overlap_ratio: float,
    body_size: float,
    body_width: float,
) -> None:
    """Step 6: label-like text mostly inside the union is a member (FR-33, E-28).

    The lines are judged per source block: a block part that reads as prose (more than two
    lines, long, or wide) is handed back to the paragraph flow untouched (CR-38).
    """
    for block in blocks:
        inside = [i for i in _inside(cluster, block, kept_lines, overlap_ratio) if i not in claimed]
        if not inside:
            continue
        if not _is_label_text([kept_lines[i] for i in inside], body_size, body_width):
            continue
        cluster.inner.extend(inside)
        claimed.update(inside)


def _is_prose_cluster(
    cluster: _Cluster,
    blocks: Sequence[_TextBlock],
    kept_lines: Sequence[Line],
    claimed: Set[int],
    overlap_ratio: float,
) -> bool:
    """True when the text inside the union is mostly prose (shaded note box, framed page)."""
    union_area = _area(cluster.union)
    if union_area <= 0:
        return False
    members = set(cluster.inner)
    prose_area = 0.0
    prose_chars = 0
    prose_blocks = 0  # CR-84: blocks contributing *several* loose lines, not a wrapped label
    label_chars = 0
    for block in blocks:
        inside = _inside(cluster, block, kept_lines, overlap_ratio)
        if not inside:
            continue
        loose = [i for i in inside if i not in members and i not in claimed]
        label_chars += sum(len(kept_lines[i].text) for i in inside if i in members)
        if not loose:
            continue
        prose_chars += sum(len(kept_lines[i].text) for i in loose)
        if len(loose) > 1:
            prose_blocks += 1
        boxes = [kept_lines[i].bbox for i in loose]
        span: BBox = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        prose_area += _intersection_area(span, cluster.union)
    if prose_chars == 0:
        return False
    share = prose_area / union_area
    if share >= _PROSE_AREA_REJECT_RATIO:
        return True
    # CR-84: below the reject ratio the text has to be prose beyond doubt - several times the
    # label characters and at least two multi-line blocks. One annotation box inside a real
    # diagram used to be enough, because labels are short by definition.
    return (
        share >= _PROSE_AREA_MIXED_RATIO
        and prose_chars > _PROSE_MIXED_CHAR_FACTOR * label_chars
        and prose_blocks >= _PROSE_MIXED_MIN_BLOCKS
    )


def _attach_labels(
    cluster: _Cluster,
    blocks: Sequence[_TextBlock],
    kept_lines: Sequence[Line],
    claimed: Set[int],
    raw: RawPage,
    band: BandInfo,
    body_size: float,
    body_width: float,
    line_height: float,
    options: FigureOptions,
) -> None:
    """Step 7: absorb label-like blocks just outside the union (E5 axis titles)."""
    reach = options.label_attach_lines * line_height
    for _ in range(_LABEL_ATTACH_ROUNDS):
        grew = False
        for block in blocks:
            if any(i in claimed for i in block.indexes):
                continue
            lines = [kept_lines[i] for i in block.indexes]
            if any(_is_band_line(ln, raw, band, body_size) for ln in lines):
                continue
            if not is_label_like(lines, body_size, body_width):
                continue
            if rect_gap(block.bbox, cluster.union) > reach:
                continue
            cluster.union = _union(cluster.union, block.bbox)
            cluster.inner.extend(block.indexes)
            claimed.update(block.indexes)
            grew = True
        if not grew:
            break


def _blockers(
    cluster: _Cluster, raw: RawPage, band: BandInfo, kept_lines: Sequence[Line], body_size: float
) -> List[BBox]:
    """Header/footer text the region must never contain: removed band lines plus leaked
    short band lines the v1 repetition rule missed (A-13)."""
    boxes = list(band.removed_line_boxes)
    boxes.extend(
        ln.bbox
        for index, ln in enumerate(kept_lines)
        if index not in cluster.inner and _is_band_line(ln, raw, band, body_size)
    )
    return boxes


def _cut_around(bbox: BBox, blockers: Sequence[BBox], margin: float) -> BBox:
    """Shrink ``bbox`` vertically so it intersects none of ``blockers``."""
    x0, y0, x1, y1 = bbox
    centre = (y0 + y1) / 2.0
    for box in blockers:
        if _intersection_area(box, (x0, y0, x1, y1)) <= 0:
            continue
        if (box[1] + box[3]) / 2.0 < centre:
            y0 = max(y0, box[3] + margin)
        else:
            y1 = min(y1, box[1] - margin)
    return (x0, y0, x1, y1) if y1 > y0 else bbox


def _clip_to_band(cluster: _Cluster, blockers: Sequence[BBox], band: BandInfo) -> None:
    """Step 8: keep the union inside the body band and away from header/footer lines."""
    x0, y0, x1, y1 = cluster.union
    y0 = max(y0, band.header_bottom + _CLIP_MARGIN_PT)
    y1 = min(y1, band.footer_top - _CLIP_MARGIN_PT)
    if y1 > y0:
        cluster.union = _cut_around((x0, y0, x1, y1), blockers, 1.0)


def _associate_caption(
    cluster: _Cluster,
    blocks: Sequence[_TextBlock],
    kept_lines: Sequence[Line],
    claimed: Set[int],
    line_height: float,
) -> None:
    """Step 9: nearest caption-pattern block below the union, else above (FR-34, E-30)."""
    reach = _CAPTION_GAP_LINES * line_height
    best_below: Optional[Tuple[float, int]] = None
    best_above: Optional[Tuple[float, int]] = None
    for index, block in enumerate(blocks):
        if any(i in claimed for i in block.indexes):
            continue
        first = kept_lines[block.indexes[0]].text
        if not matches_caption_pattern(first):
            continue
        gap_below = block.bbox[1] - cluster.union[3]
        gap_above = cluster.union[1] - block.bbox[3]
        if -1.0 <= gap_below <= reach:
            if best_below is None or gap_below < best_below[0]:
                best_below = (gap_below, index)
        elif -1.0 <= gap_above <= reach:
            if best_above is None or gap_above < best_above[0]:
                best_above = (gap_above, index)
    if best_below is not None:
        cluster.caption, cluster.caption_position = best_below[1], "below"
    elif best_above is not None:
        cluster.caption, cluster.caption_position = best_above[1], "above"
    else:
        return
    # The caption is never a member: cut the union 2 pt before it.
    box = blocks[cluster.caption].bbox
    x0, y0, x1, y1 = cluster.union
    if cluster.caption_position == "below" and y1 > box[1] - _CLIP_MARGIN_PT:
        y1 = box[1] - _CLIP_MARGIN_PT
    elif cluster.caption_position == "above" and y0 < box[3] + _CLIP_MARGIN_PT:
        y0 = box[3] + _CLIP_MARGIN_PT
    if y1 > y0:
        cluster.union = (x0, y0, x1, y1)


def _prose_between(a: _Cluster, b: _Cluster, blocks: Sequence[_TextBlock]) -> bool:
    members = set(a.inner) | set(b.inner)
    span = _union(a.union, b.union)
    for block in blocks:
        if any(i in members for i in block.indexes):
            continue
        if _intersection_area(block.bbox, span) <= 0:
            continue
        if _intersection_area(block.bbox, a.union) > 0:
            continue
        if _intersection_area(block.bbox, b.union) > 0:
            continue
        return True
    return False


def _merge_subfigures(
    clusters: List[_Cluster],
    blocks: Sequence[_TextBlock],
    kept_lines: Sequence[Line],
    line_height: float,
    page: int,
) -> List[_Cluster]:
    """Step 11: adjacent clusters sharing an "(a) … (b)" caption become one figure (E-25)."""
    merged = True
    while merged and len(clusters) > 1:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                if rect_gap(a.union, b.union) > _SUBFIGURE_GAP_LINES * line_height:
                    continue
                caption = b.caption if b.caption is not None else a.caption
                if caption is None:
                    continue
                if a.caption is not None and b.caption is not None and a.caption != b.caption:
                    continue
                if _prose_between(a, b, blocks):
                    continue
                text = " ".join(kept_lines[k].text for k in blocks[caption].indexes)
                if not all(marker in text for marker in _SUBFIGURE_MARKERS):
                    log.info(
                        "figure_adjacency_ambiguous (page %d)",
                        page,
                        extra={"event": "figure_adjacency_ambiguous", "page": page},
                    )
                    continue
                a.union = _union(a.union, b.union)
                a.drawings += b.drawings
                a.images += b.images
                a.inner.extend(b.inner)
                if b.caption is not None:
                    a.caption_position = b.caption_position
                a.caption = caption
                a.warnings.extend(b.warnings)
                a.warnings.append("merged_by_caption")
                del clusters[j]
                merged = True
                break
            if merged:
                break
    return clusters


def _resolve_shared_captions(clusters: List[_Cluster]) -> None:
    """Two separate figures pointing at one caption block: the nearer one keeps it."""
    by_caption: Dict[int, List[_Cluster]] = {}
    for cluster in clusters:
        if cluster.caption is not None:
            by_caption.setdefault(cluster.caption, []).append(cluster)
    for sharers in by_caption.values():
        if len(sharers) < 2:
            continue
        sharers.sort(key=lambda c: (c.caption_position != "below", -c.union[3]))
        for loser in sharers[1:]:
            loser.caption = None
            loser.caption_position = "none"
            loser.warnings.append("caption_shared")


def _final_bbox(
    cluster: _Cluster,
    raw: RawPage,
    band: BandInfo,
    blocks: Sequence[_TextBlock],
    blockers: Sequence[BBox],
    options: FigureOptions,
) -> BBox:
    """Step 13: union + border, clipped to the page rect, the band limits and the caption."""
    x0, y0, x1, y1 = _expand(cluster.union, options.border_pt)
    x0 = max(0.0, x0)
    y0 = max(0.0, y0, band.header_bottom)
    x1 = min(raw.width, x1)
    y1 = min(raw.height, y1, band.footer_top)
    x0, y0, x1, y1 = _cut_around((x0, y0, x1, y1), blockers, 0.0)
    if cluster.caption is not None:
        box = blocks[cluster.caption].bbox
        if cluster.caption_position == "below":
            y1 = min(y1, max(cluster.union[3], box[1] - 1.0))
        else:
            y0 = max(y0, min(cluster.union[1], box[3] + 1.0))
    return (round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3))


# ---------------------------------------------------------------------------
# Labels and legend (§4.3)
# ---------------------------------------------------------------------------


def _label_groups(indexes: Sequence[int], kept_lines: Sequence[Line]) -> List[List[int]]:
    """Consecutive lines of one block that overlap horizontally form one label (E6)."""
    groups: List[List[int]] = []
    for index in indexes:
        line = kept_lines[index]
        if groups:
            prev = kept_lines[groups[-1][-1]]
            x_overlap = min(prev.bbox[2], line.bbox[2]) - max(prev.bbox[0], line.bbox[0])
            gap = line.bbox[1] - prev.bbox[3]
            if x_overlap > 0 and gap <= 0.5 * max(prev.height, line.height, 1.0):
                groups[-1].append(index)
                continue
        groups.append([index])
    return groups


def _labels_in_reading_order(
    cluster: _Cluster,
    blocks: Sequence[_TextBlock],
    kept_lines: Sequence[Line],
    line_height: float,
    compounds: FrozenSet[str],
) -> Tuple[Tuple[str, ...], Tuple[Tuple[int, ...], ...]]:
    """``(labels, label_groups)``: distinct label texts in reading order and, aligned with
    them, the kept-line indexes each label was joined from (first occurrence wins)."""
    members = set(cluster.inner)
    labelled: List[Tuple[float, float, str, Tuple[int, ...]]] = []
    for block in blocks:
        owned = [i for i in block.indexes if i in members]
        if not owned:
            continue
        for group in _label_groups(owned, kept_lines):
            lines = [kept_lines[i] for i in group]
            text, _ = join_lines([ln.text for ln in lines], set(compounds))
            text = _WS_RE.sub(" ", text).strip()
            if not text:
                continue
            y0 = min(ln.bbox[1] for ln in lines)
            x0 = min(ln.bbox[0] for ln in lines)
            labelled.append((round(y0 / max(line_height, 1.0)), x0, text, tuple(group)))
    labelled.sort(key=lambda item: (item[0], item[1]))
    seen: Set[str] = set()
    ordered: List[str] = []
    groups: List[Tuple[int, ...]] = []
    for _, _, text, indexes in labelled:
        if text in seen:
            continue
        seen.add(text)
        ordered.append(text)
        groups.append(indexes)
    return tuple(ordered), tuple(groups)


def legend_labels(region: FigureRegion, limit: int) -> Tuple[Tuple[str, ...], int]:
    """Legend labels of a region: ticks dropped, de-duplicated, truncated to ``limit``.

    Returns ``(labels, more)`` where ``more`` is the number of labels cut by the limit.
    """
    kept: List[str] = []
    seen: Set[str] = set()
    for raw in region.labels:
        text = _WS_RE.sub(" ", raw).strip()
        if len(text) <= 1 or _NUMERIC_LABEL_RE.match(text):
            continue
        if text in seen:
            continue
        seen.add(text)
        kept.append(text)
    limit = max(0, limit)
    more = max(0, len(kept) - limit)
    return tuple(kept[:limit]), more
