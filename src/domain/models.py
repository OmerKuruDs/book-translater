"""Domain value types (design doc 02, section 3; DB design deviation D2 applied).

Frozen dataclasses only; no I/O, no third-party imports.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Literal, Optional, Tuple


class BlockKind(str, Enum):
    """Kinds produced by the BT-Markdown block parser (chunker-internal)."""

    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST = "LIST"
    TABLE = "TABLE"
    CODE = "CODE"
    BLOCKQUOTE = "BLOCKQUOTE"
    FOOTNOTE = "FOOTNOTE"
    IMAGE = "IMAGE"
    HR = "HR"
    BLANK = "BLANK"


class ChunkKind(str, Enum):
    """Kind of a persisted chunk (matches the ``chunks.kind`` CHECK)."""

    TEXT = "TEXT"  # packed prose: headings + paragraphs + lists + blockquotes + footnotes
    HEADING = "HEADING"  # heading-only chunk (only possible at end of document)
    LIST = "LIST"
    TABLE = "TABLE"
    CODE = "CODE"
    IMAGE = "IMAGE"
    HR = "HR"
    FOOTNOTE = "FOOTNOTE"


class ChunkStatus(str, Enum):
    """Runtime state of a chunk (design doc 02, section 3.2)."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class GlossaryStrategy(str, Enum):
    """How a provider applies the glossary."""

    NATIVE = "native"
    PROMPT = "prompt"
    POST_REPLACE = "post_replace"
    NONE = "none"


class GlossaryStatus(str, Enum):
    """Approval state of a glossary entry."""

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    AMBIGUOUS = "AMBIGUOUS"


def content_hash_of(source_text: str) -> str:
    """Definition of ``Chunk.content_hash``: sha256 hex digest of the UTF-8 encoded text.

    The chunker must produce hashes with this function; ``ChunkRepository.replace_all``
    verifies them before inserting.
    """
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest()


class FigureKind(str, Enum):
    """What a detected figure region is made of (design doc 06, §3.2)."""

    RASTER = "raster"
    VECTOR = "vector"
    MIXED = "mixed"


Rect = Tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF points, origin top-left


@dataclass(frozen=True)
class LabelBox:
    """Geometry and style of one figure label (design doc 06, Addendum A; ``figures.json``).

    Written by the extractor next to the legend labels so the export step can paint the
    translated label back into the figure PNG (``exporters.figure_overlay``). Style fields
    mirror ``domain.overlay.OverlayStyle`` / ``OverlayAlignment`` values.
    """

    text: str  # the legend label exactly as emitted in the legend block
    bbox: Rect
    font_size: float
    bold: bool
    italic: bool
    family: Literal["serif", "sans", "mono"]
    color: str  # "#rrggbb"
    alignment: Literal["left", "center", "right", "justify"]
    # (prefix_text, bold, italic) of a bold running number like "2." (E-46 / Q22, CR-97);
    # mirrors ``domain.overlay.OverlayStyle.prefix_style`` so the figure PNG keeps it too
    prefix_style: Optional[Tuple[str, bool, bool]] = None


@dataclass(frozen=True)
class SourceProfile:
    """Page properties of the source PDF (``source_profile.json``; Addendum A).

    The reflow PDF export derives its page size, body font size and margins from these
    values unless the user gave ``--pdf-page-size`` / ``--pdf-margin`` explicitly.
    """

    page_width_pt: float
    page_height_pt: float
    body_font_size_pt: float
    margins_pt: Tuple[float, float, float, float]  # top, right, bottom, left
    serif: bool
    pages_measured: int = 0


@dataclass(frozen=True)
class FigureRecord:
    """One ``figures.json`` entry; also carried in ``ExtractedDocument.figures`` (C7)."""

    image_id: int  # the N of "<!-- image:N ... -->"
    page: int
    index_on_page: int
    kind: FigureKind
    region: Rect
    file: str  # "images/p002-f01.png" (POSIX, relative to the output directory)
    dpi: int
    width_px: int
    height_px: int
    bytes: int
    dpi_reduced: bool
    label_count: int
    labels: Tuple[str, ...]  # legend labels actually emitted (after filtering/limit)
    labels_more: int
    caption_present: bool
    warnings: Tuple[str, ...]
    label_boxes: Tuple[LabelBox, ...] = ()  # Addendum A: per-label geometry (in-place translation)


@dataclass(frozen=True)
class FigureTotals:
    """Aggregate of the figure inventory for the summary and ``status --json`` (FR-37)."""

    count: int
    raster: int
    vector: int
    mixed: int
    total_bytes: int
    largest_file: Optional[str]
    largest_bytes: int
    pages_with_figures: int
    pages_skipped: Tuple[Tuple[int, str], ...]
    dpi_reduced: int


@dataclass(frozen=True)
class PageInfo:
    number: int
    char_count: int
    skipped: bool
    had_images: int
    figures: int = 0  # figures detected on the page (C16)


@dataclass(frozen=True)
class ExtractedDocument:
    markdown: str  # BT-Markdown, "\n" newlines, UTF-8, ends with "\n"
    extractor_name: str
    fallback_used: bool
    pages: List[PageInfo]
    detected_language: Optional[str]  # "en", "tr", "de", ... or None
    language_confidence: float  # stopword ratio of the winning language
    removed_headers_footers: List[str]  # normalized patterns, for the log/summary
    footnote_mode: Literal["markdown", "notes_section", "none"]
    image_count: int
    warnings: List[str]
    figures: Tuple[FigureRecord, ...] = ()  # C7
    figure_pages_skipped: Tuple[Tuple[int, str], ...] = ()  # (page, reason)
    source_profile: Optional[SourceProfile] = None  # Addendum A (pymupdf extractor only)


@dataclass(frozen=True)
class Block:
    """Chunker-internal span; not persisted."""

    block_id: int  # 1-based sequential
    kind: BlockKind
    start: int  # char offsets in markdown; blocks tile the document exactly
    end: int
    level: int = 0  # heading level, list nesting
    translatable: bool = True


@dataclass(frozen=True)
class Chunk:
    """One chunk: static chunker output plus persisted runtime state."""

    chunk_id: int  # == order, 1-based
    order: int
    kind: ChunkKind
    translatable: bool  # False -> copied verbatim, no provider call
    source_text: str  # exact substring of source_book.md (includes trailing blank lines)
    content_hash: str  # content_hash_of(source_text)
    char_count: int  # len(source_text)
    parent_block_id: Optional[int]  # set only for sub-splits of an oversize block
    sub_index: Optional[int]  # 0..n-1 within parent; None if not a sub-split
    sub_count: Optional[int]
    heading_path: Tuple[str, ...]  # nearest H1/H2/H3 titles (for logs, EPUB chapter mapping)
    # runtime state (persisted, mutable in DB only):
    status: ChunkStatus = ChunkStatus.PENDING
    retry_count: int = 0
    next_attempt_at: Optional[datetime] = None
    last_error: Optional[str] = None
    translated_text: Optional[str] = None
    review_flag: bool = False  # structure/glossary post-check mismatch (E-17)
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class GlossaryEntry:
    source: str
    target: str
    count: int
    status: GlossaryStatus
    origin: Literal["discovered", "user"]
    note: str = ""


@dataclass(frozen=True)
class EffectiveGlossary:
    # APPROVED entries with a non-empty target only, sorted longest source first.
    entries: Tuple[GlossaryEntry, ...]
    glossary_hash: str


@dataclass(frozen=True)
class TranslationResult:
    """Outcome of translating one chunk; consumed by ``ChunkRepository.complete``."""

    chunk_id: int
    translated_text: str  # full chunk text with markers/whitespace re-applied
    provider: str
    chars_sent: int  # payload chars sent (0 for non-translatable)
    chars_billed: int  # provider-reported when available
    latency_ms: int
    attempts: int
    glossary_strategy: GlossaryStrategy
    warnings: Tuple[str, ...]
    review_flag: bool


@dataclass(frozen=True)
class StatusCounts:
    """Chunk counts per status.

    ``waiting_backoff`` (D2) = PENDING rows whose ``next_attempt_at`` is in the future.
    """

    pending: int
    processing: int
    completed: int
    failed: int
    total: int
    waiting_backoff: int


@dataclass(frozen=True)
class OverlaySummary:
    """Overlay-pass block of ``JobSummary`` / ``summary.json`` (design doc 06, 5.6)."""

    status: str  # jobs.overlay_status
    counts: StatusCounts  # units per status
    pages_completed: int
    pages_total: int
    pages_skipped: int
    translatable_units: int
    kept_units: int
    overlay_sha256: Optional[str]
    failed_unit_ids: List[int]
    review_unit_ids: List[int]
    placed: int = 0
    shrunk_below_threshold: int = 0
    could_not_fit: int = 0
    kept_original: int = 0
    review_path: Optional[str] = None


@dataclass(frozen=True)
class JobSummary:
    """Written to summary.json and printed at the end of a run."""

    job_id: str
    run_id: str
    command: str
    input_path: str
    input_sha256: str
    source_md_sha256: Optional[str]
    provider: Optional[str]
    glossary_strategy: Optional[GlossaryStrategy]
    glossary_entries_applied: int
    extractor: Optional[str]
    fallback_used: bool
    counts: StatusCounts
    failed_chunk_ids: List[int]
    review_chunk_ids: List[int]
    chars_sent_this_run: int
    chars_sent_total: int
    started_at: datetime
    finished_at: datetime
    duration_s: float
    outputs: Dict[str, str]  # {"markdown": path, "epub": path, "pdf": path}
    exit_code: int
    warnings: List[str]
    figures: Optional[FigureTotals] = None  # from figures.json when present (v1.1, FR-37)
    mode: str = "reflow"  # v1.1 F2: "reflow" | "overlay" (the pass this run worked on)
    overlay: Optional[OverlaySummary] = None  # v1.1 F2: overlay pass state (translate/export)


@dataclass(frozen=True)
class AssembledDocument:
    """Output of ``pipeline/assembler.py``; input of every exporter (design doc 02, 8.1)."""

    markdown: str  # translated BT-Markdown (partial markers included when allowed)
    metadata: Dict[str, str]  # title, author, source file name, job_id, provider ...
    failed_chunk_ids: List[int]
    review_chunk_ids: List[int]
    heading_outline: List[Tuple[int, str]]  # (level, title) in document order
    warnings: List[str]  # structural parity mismatches etc.
