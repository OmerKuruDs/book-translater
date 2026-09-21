"""Overlay-pass domain value types (design doc 06, section 3.6.1; DB design doc 07, section 3.7).

Frozen dataclasses and enums only; standard library imports only. ``BBox`` is the
plain 4-tuple alias of the extractors (doc 07, V6) restated here so that the database
layer never imports ``extractors``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Optional, Tuple

from .models import ChunkStatus

BBox = Tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points, origin top-left

UnitStatus = ChunkStatus  # the overlay unit runs through the v1 chunk state machine


class OverlayBlockKind(str, Enum):
    """Kind of an overlay text block (matches the ``overlay_blocks.kind`` CHECK)."""

    BODY = "body"
    HEADING = "heading"
    CAPTION = "caption"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    FIGURE_LABEL = "figure_label"
    TABLE_CELL = "table_cell"
    FOOTNOTE = "footnote"


class OverlayAlignment(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


OverlayFamily = Literal["serif", "sans", "mono"]

OVERLAY_KEEP_REASONS: Tuple[str, ...] = (
    "page_number",
    "header_footer",
    "figure_text",
    "rotated",
    "no_letters",
    "math",
    "no_bbox",
    "widget_overlap",
)
OVERLAY_PAGE_SKIP_REASONS: Tuple[str, ...] = ("no_text_layer",)


def format_block_id(page: int, index_on_page: int) -> str:
    """The stable block id string ``"p{page:03d}-b{index:03d}"`` (doc 07, B3 / DQ2).

    Not stored: it is a function of two persisted columns and is derived by the
    repository and the extractor alike.
    """
    return f"p{page:03d}-b{index_on_page:03d}"


@dataclass(frozen=True)
class OverlayStyle:
    font_size: float  # dominant span size (pt)
    bold: bool  # dominant style (Q22)
    italic: bool
    family: OverlayFamily
    color: str  # "#rrggbb" of the dominant span
    # (prefix_text, bold, italic) when <= 2 spans show a clear prefix pattern (E-46)
    prefix_style: Optional[Tuple[str, bool, bool]] = None


@dataclass(frozen=True)
class OverlayBlock:
    """One overlay translation unit: write-once extraction facts plus runtime state."""

    unit_id: int  # dense 1..n per job (== order), like chunk_id
    block_id: str  # format_block_id(page, index_on_page)
    page: int
    index_on_page: int
    kind: OverlayBlockKind
    bbox: BBox  # union of line boxes (redaction/placement rect)
    line_boxes: Tuple[BBox, ...]
    style: OverlayStyle
    alignment: OverlayAlignment
    source_text: str  # lines joined with hyphen rejoin (E-43)
    content_hash: str
    char_count: int
    translate: bool  # False -> completed with the source text, never sent, never redacted
    keep_reason: Optional[str]  # one of OVERLAY_KEEP_REASONS when translate is False
    fragment: bool  # E-42
    over_image: bool  # E-40 (informational)
    # runtime state (persisted, DB only): the same fields as Chunk
    status: ChunkStatus = ChunkStatus.PENDING
    retry_count: int = 0
    next_attempt_at: Optional[datetime] = None
    last_error: Optional[str] = None
    translated_text: Optional[str] = None
    review_flag: bool = False
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OverlayPage:
    page: int
    width: float
    height: float
    rotation: int
    has_text_layer: bool
    block_count: int
    translatable_count: int
    skip_reason: Optional[str]  # "no_text_layer" | None
    body_size: float


@dataclass(frozen=True)
class OverlayExtraction:
    pages: Tuple[OverlayPage, ...]
    blocks: Tuple[OverlayBlock, ...]
    overlay_sha256: str  # sha256 over (block_id, bbox, source_text, translate) -> pass binding
    warnings: Tuple[str, ...]


@dataclass(frozen=True)
class LabelReplacement:
    """One figure label to paint back into a figure PNG (design doc 06, Addendum A).

    Shared contract between the pipeline (which builds the list from the legend pairs and
    the ``label_boxes`` of ``figures.json``) and ``exporters.figure_overlay``
    ``render_translated_figure``. ``bbox`` is in page points (origin top-left), like every
    other box in this module; ``text`` is the translated label.
    """

    bbox: BBox
    text: str
    font_size: float
    bold: bool
    italic: bool
    family: OverlayFamily
    color: str  # "#rrggbb"
    alignment: OverlayAlignment
    # (prefix_text, bold, italic) of the source label, like OverlayStyle.prefix_style: the
    # bold running number of "2. Image formation" survives into the figure PNG (CR-97)
    prefix_style: Optional[Tuple[str, bool, bool]] = None


OVERLAY_JOB_STATUSES: Tuple[str, ...] = (
    "NONE",
    "OVERLAY_EXTRACTED",
    "OVERLAY_TRANSLATING",
    "OVERLAY_PAUSED",
    "OVERLAY_TRANSLATED",
    "OVERLAY_EXPORTED",
)
