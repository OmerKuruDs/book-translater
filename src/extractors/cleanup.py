"""Pure cleanup heuristics for PDF text (design doc 02, section 6.1).

Everything here works on simple frozen dataclasses and strings so it can be
unit-tested without a PDF. No I/O, no third-party imports.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..domain.models import SourceProfile
from ..domain.overlay import OverlayAlignment, OverlayFamily, OverlayStyle

BBox = Tuple[float, float, float, float]  # x0, y0, x1, y1 (PDF points, origin top-left)

# ---------------------------------------------------------------------------
# Raw geometry types (filled by the PyMuPDF adapter, consumed by the heuristics)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    text: str
    size: float
    font: str
    bold: bool
    superscript: bool
    mono: bool
    bbox: BBox
    italic: bool = False  # v1.1 F2 (Q22): span flag / font name
    color: str = "#000000"  # v1.1 F2: "#rrggbb" of the span


@dataclass(frozen=True)
class Line:
    spans: Tuple[Span, ...]
    bbox: BBox
    dir: Tuple[float, float] = (1.0, 0.0)  # writing direction; rotated text has dir != (1, 0) (C11)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans).strip()

    @property
    def size(self) -> float:
        """Character-weighted dominant font size of the line."""
        return dominant_size([(s.size, len(s.text.strip())) for s in self.spans])

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def bold(self) -> bool:
        weighted = sum(len(s.text.strip()) for s in self.spans if s.bold)
        total = sum(len(s.text.strip()) for s in self.spans)
        return total > 0 and weighted * 2 > total

    @property
    def mono(self) -> bool:
        return bool(self.spans) and all(s.mono for s in self.spans if s.text.strip())


@dataclass(frozen=True)
class RawBlock:
    page: int  # 1-based page number
    bbox: BBox
    lines: Tuple[Line, ...]

    @property
    def text(self) -> str:
        return " ".join(ln.text for ln in self.lines if ln.text)


@dataclass(frozen=True)
class RawImage:
    page: int
    bbox: BBox


@dataclass(frozen=True)
class RawTable:
    page: int
    bbox: BBox
    rows: Tuple[Tuple[str, ...], ...]


@dataclass(frozen=True)
class RawDrawing:
    """One vector path of ``page.get_drawings()`` (design doc 06, §3.2)."""

    bbox: BBox
    kind: str  # "f" (fill) | "s" (stroke) | "fs" (both)
    item_count: int  # path segments
    fill: Optional[Tuple[float, float, float]]
    stroke: Optional[Tuple[float, float, float]]
    width: Optional[float]

    @property
    def area(self) -> float:
        return max(0.0, self.bbox[2] - self.bbox[0]) * max(0.0, self.bbox[3] - self.bbox[1])


@dataclass(frozen=True)
class RawPage:
    number: int
    width: float
    height: float
    blocks: Tuple[RawBlock, ...]
    images: Tuple[RawImage, ...]
    tables: Tuple[RawTable, ...]
    drawings: Tuple[RawDrawing, ...] = ()  # only read when figures are enabled (v1.1)
    link_boxes: Tuple[BBox, ...] = ()  # link annotation rectangles

    @property
    def char_count(self) -> int:
        return sum(len(ln.text.replace(" ", "")) for b in self.blocks for ln in b.lines)


# ---------------------------------------------------------------------------
# Font size helpers (steps 6 and 7)
# ---------------------------------------------------------------------------


def round_half(size: float) -> float:
    """Round a font size to the nearest 0.5 pt (tier bucketing)."""
    return round(size * 2.0) / 2.0


def dominant_size(weighted: Iterable[Tuple[float, int]]) -> float:
    """Mode of ``(size, char_count)`` pairs weighted by character count; 0.0 if empty."""
    counter: Counter[float] = Counter()
    for size, chars in weighted:
        if chars > 0:
            counter[round_half(size)] += chars
    if not counter:
        return 0.0
    # Deterministic tie-break: larger count first, then larger size.
    best = max(counter.items(), key=lambda kv: (kv[1], kv[0]))
    return best[0]


def body_font_size(spans: Iterable[Tuple[float, int]]) -> float:
    """Body font size = character-weighted mode of span sizes (step 6)."""
    return dominant_size(spans)


def assign_heading_tiers(sizes: Iterable[float]) -> Dict[float, int]:
    """Bucket distinct heading sizes (rounded to 0.5 pt) into at most three tiers.

    The largest size becomes level 1; every size beyond the third distinct one is
    folded into level 3.
    """
    distinct = sorted({round_half(s) for s in sizes}, reverse=True)
    return {size: min(index + 1, 3) for index, size in enumerate(distinct)}


_CHAPTER_RE = re.compile(r"^(?:chapter|part|book)\s+(?:\d+|[ivxlcdm]+)\b", re.IGNORECASE)
_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")


def numbered_heading_level(text: str) -> Optional[int]:
    """Heading level implied by a numbering pattern, or ``None``.

    ``Chapter 3`` -> 1, ``3 Title`` -> 1, ``3.2 Title`` -> 2, ``3.2.1 Title`` -> 3.
    """
    if _CHAPTER_RE.match(text.strip()):
        return 1
    match = _NUMBERED_RE.match(text.strip())
    if match is None:
        return None
    depth = match.group(1).count(".") + 1
    return min(depth, 3)


_TERMINAL_HEADING_PUNCT = ".,;:"


def is_heading_candidate(
    line_texts: Sequence[str],
    size: float,
    bold: bool,
    body_size: float,
    heading_size_ratio: float,
    followed_by_paragraph: bool = False,
) -> bool:
    """Step 7 candidate rule: short block, no terminal period, larger (or bold) font."""
    texts = [t.strip() for t in line_texts if t.strip()]
    if not texts or len(texts) > 2:
        return False
    joined = " ".join(texts)
    if len(joined) > 120 or joined[-1] in _TERMINAL_HEADING_PUNCT:
        return False
    if not any(ch.isalpha() for ch in joined):
        return False
    if body_size <= 0:
        return False
    if size >= body_size * heading_size_ratio:
        return True
    return bold and size >= body_size and followed_by_paragraph


def is_title_case(text: str) -> bool:
    """True when every word of four or more letters starts with a capital letter."""
    words = [w for w in re.findall(r"[^\W\d_]+", text) if len(w) >= 4]
    return bool(words) and all(w[0].isupper() for w in words)


# ---------------------------------------------------------------------------
# Running header/footer and page-number detection (steps 4 and 5)
# ---------------------------------------------------------------------------

_DIGIT_RUN_RE = re.compile(r"\d+")
_PUNCT_RE = re.compile(r"[^\w\s#]", re.UNICODE)
_WS_RE = re.compile(r"\s+")

PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:page\s+)?(\d{1,4}|[ivxlcdm]{1,7})(?:\s*(?:/|of)\s*\d{1,4})?\s*$",
    re.IGNORECASE,
)


def normalize_pattern(text: str) -> str:
    """Lowercase, mask digit runs with ``#``, strip punctuation, collapse whitespace."""
    lowered = _DIGIT_RUN_RE.sub("#", text.lower())
    stripped = _PUNCT_RE.sub("", lowered)
    return _WS_RE.sub(" ", stripped).strip()


def is_page_number(text: str) -> bool:
    """A bare page number: ``47``, ``page 47``, ``xii``, ``3 / 120``, ``3 of 120``."""
    return PAGE_NUMBER_RE.match(text) is not None


def in_band(bbox: BBox, page_height: float, band_ratio: float) -> bool:
    """True when the bbox lies fully inside the top or bottom ``band_ratio`` of the page."""
    if page_height <= 0:
        return False
    band = page_height * band_ratio
    return bbox[3] <= band or bbox[1] >= page_height - band


def running_pattern_threshold(page_count: int, page_ratio: float) -> int:
    """Minimum number of pages a pattern must occur on: ``max(3, ceil(ratio * pages))``."""
    return max(3, math.ceil(page_ratio * page_count))


def detect_running_patterns(
    band_texts_per_page: Sequence[Sequence[str]],
    page_ratio: float,
    min_pages: Optional[int] = None,
) -> Set[str]:
    """Normalized band-line patterns that repeat on enough pages.

    ``band_texts_per_page[i]`` holds the raw texts of the band lines of page ``i``.
    A pattern counts once per page. Pure page numbers are ignored here (step 5
    handles them) and empty patterns never match.
    """
    threshold = (
        min_pages
        if min_pages is not None
        else running_pattern_threshold(len(band_texts_per_page), page_ratio)
    )
    counts: Counter[str] = Counter()
    for texts in band_texts_per_page:
        seen: Set[str] = set()
        for text in texts:
            if is_page_number(text):
                continue
            pattern = normalize_pattern(text)
            if pattern and pattern != "#":
                seen.add(pattern)
        counts.update(seen)
    return {pattern for pattern, count in counts.items() if count >= threshold}


def is_short_band_candidate(text: str, size: float, body_size: float) -> bool:
    """Secondary rule for short chapters: band + short line + no terminal punctuation.

    Only lines at or below body size qualify, so headings at the top of a page
    are never touched.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 60:
        return False
    if body_size > 0 and size > body_size:
        return False
    return not ends_with_terminal(stripped)


def is_running_line(text: str, patterns: Set[str]) -> bool:
    return normalize_pattern(text) in patterns


# ---------------------------------------------------------------------------
# Hyphenation and paragraph joining (steps 8, 9, 10)
# ---------------------------------------------------------------------------

_COMPOUND_RE = re.compile(r"(?<![\w-])([^\W\d_]+)-([^\W\d_]+)(?![\w-])")
_LAST_WORD_RE = re.compile(r"([^\W\d_]+)-$")
_FIRST_WORD_RE = re.compile(r"^([^\W\d_]+)")
_TERMINAL_CHARS = ".!?:”\"’)…"
SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
"""Unicode superscript digits: how overlay units carry footnote markers through translation."""
_TO_SUPERSCRIPT = str.maketrans("0123456789", SUPERSCRIPT_DIGITS)
MARKER_SIZE_RATIO = 0.8  # a digit span this much smaller than its line is a marker


def to_superscript(digits: str) -> str:
    """``"10"`` -> superscript ``"10"`` (non-digits pass through)."""
    return digits.translate(_TO_SUPERSCRIPT)


def collect_compounds(texts: Iterable[str]) -> Set[str]:
    """Hyphenated words that appear mid-line (``well-known``) -> true compounds (lowercased)."""
    compounds: Set[str] = set()
    for text in texts:
        stripped = text.rstrip()
        for match in _COMPOUND_RE.finditer(stripped):
            if match.end() < len(stripped):
                compounds.add(match.group(0).lower())
    return compounds


def ends_with_terminal(text: str) -> bool:
    # a trailing footnote marker ("dependencies.⁹") does not hide the sentence end
    stripped = text.rstrip().rstrip(SUPERSCRIPT_DIGITS).rstrip()
    return bool(stripped) and stripped[-1] in _TERMINAL_CHARS


def join_hyphenated(
    left: str, right: str, compounds: Set[str]
) -> Tuple[str, Optional[str]]:
    """Join two consecutive lines, resolving a trailing hyphen (step 9, E-02).

    Returns the joined text and an optional warning for ambiguous cases.
    """
    left_s = left.rstrip()
    right_s = right.lstrip()
    if not left_s:
        return right_s, None
    if not right_s:
        return left_s, None
    if not left_s.endswith("-"):
        return f"{left_s} {right_s}", None
    left_match = _LAST_WORD_RE.search(left_s)
    right_match = _FIRST_WORD_RE.match(right_s)
    if left_match is None or right_match is None:
        # "--", digits or punctuation around the hyphen: not a word break.
        return f"{left_s} {right_s}", None
    left_word = left_match.group(1)
    right_word = right_match.group(1)
    if not right_word[0].islower():
        return f"{left_s}{right_s}", f"ambiguous hyphen kept: {left_word}-{right_word}"
    if f"{left_word}-{right_word}".lower() in compounds:
        return f"{left_s}{right_s}", None
    if len(left_word) == 1:
        return f"{left_s}{right_s}", f"ambiguous hyphen kept: {left_word}-{right_word}"
    return f"{left_s[:-1]}{right_s}", None


def join_lines(lines: Sequence[str], compounds: Set[str]) -> Tuple[str, List[str]]:
    """Join the lines of one paragraph into a single line (steps 8 and 9)."""
    warnings: List[str] = []
    text = ""
    for line in lines:
        cleaned = _WS_RE.sub(" ", line).strip()
        if not cleaned:
            continue
        if not text:
            text = cleaned
            continue
        text, warning = join_hyphenated(text, cleaned, compounds)
        if warning:
            warnings.append(warning)
    return text, warnings


def should_merge_cross_page(prev_text: str, next_text: str) -> bool:
    """Step 10: paragraph continues on the next page (no terminal punctuation / hyphen)."""
    prev_s = prev_text.rstrip()
    next_s = next_text.lstrip()
    if not prev_s or not next_s:
        return False
    if prev_s.endswith("-"):
        return next_s[0].isalpha()
    if ends_with_terminal(prev_s):
        return False
    return next_s[0].islower()


def same_paragraph(prev: Line, nxt: Line, block_x0: float) -> bool:
    """Step 8: line ``nxt`` continues the paragraph started by ``prev``."""
    line_height = max(prev.height, nxt.height, 1.0)
    baseline_gap = nxt.bbox[1] - prev.bbox[1]
    if baseline_gap >= 1.5 * line_height:
        return False
    if parse_list_marker(nxt.text) is not None:
        return False
    indent = nxt.bbox[0] - block_x0
    size = max(nxt.size, 1.0)
    prev_at_margin = abs(prev.bbox[0] - block_x0) < size * 0.5
    return not (prev_at_margin and indent > size * 1.5)


# ---------------------------------------------------------------------------
# Lists, code, captions (steps 11, 12, 15)
# ---------------------------------------------------------------------------

# Bullet glyphs as they come out of PDFs: "•" is often stored as "·" (WinAnsi fonts), plus
# the usual squares, circles and arrows of word processors (CR-41).
_BULLET_RE = re.compile(r"^([•·▪■□◦‣○●►▸➢✓–—\-\*])\s+(\S.*)$")
_ORDERED_RE = re.compile(r"^(\d{1,3})[.)]\s+(\S.*)$")
_LETTERED_RE = re.compile(r"^([a-z])[.)]\s+(\S.*)$")


def parse_list_marker(text: str) -> Optional[Tuple[str, str]]:
    """Return ``(normalized_marker, content)`` for a list item line, else ``None``.

    Bullets (``•``, ``–``, ``-``, ``*``) and lettered items normalize to ``-``;
    numbered items keep their number as ``N.``.
    """
    stripped = text.strip()
    bullet = _BULLET_RE.match(stripped)
    if bullet:
        return "-", bullet.group(2).strip()
    ordered = _ORDERED_RE.match(stripped)
    if ordered:
        return f"{ordered.group(1)}.", ordered.group(2).strip()
    lettered = _LETTERED_RE.match(stripped)
    if lettered:
        return "-", lettered.group(2).strip()
    return None


def list_level(x0: float, base_x0: float, size: float) -> int:
    """Nesting level from the horizontal offset of the marker (step 11)."""
    step = max(size, 4.0)
    return max(0, min(3, int((x0 - base_x0) / step + 0.5)))


_MONO_FONT_RE = re.compile(r"mono|courier|consolas|code", re.IGNORECASE)


def is_monospace_font(font_name: str) -> bool:
    return _MONO_FONT_RE.search(font_name) is not None


_CAPTION_RE = re.compile(r"^(?:figure|fig|table|illustration)\b", re.IGNORECASE)


def looks_like_caption(text: str) -> bool:
    return _CAPTION_RE.match(text.strip()) is not None


_CAPTION_PATTERN = re.compile(r"^(?:Figure|Fig\.|Table|Illustration|Plate|Listing)\s*[\dA-Z]")


def matches_caption_pattern(text: str) -> bool:
    return _CAPTION_PATTERN.match(text.strip()) is not None


_LABEL_MAX_LINES = 2
_LABEL_MAX_CHARS = 80
_LABEL_MAX_WIDTH_RATIO = 0.5


def is_label_like(lines: Sequence[Line], body_size: float, body_width: float) -> bool:
    """Short text block that can be an axis title or node label just outside a drawing.

    Rules of design doc 06, §4.1 step 7: at most two lines, at most 80 characters,
    block width at most half the body text width, not a caption start. Prose blocks
    (multi-line or wide) are never label-like even when adjacent to a figure.
    """
    texts = [ln.text for ln in lines if ln.text]
    if not texts or len(texts) > _LABEL_MAX_LINES:
        return False
    joined = " ".join(texts)
    if len(joined) > _LABEL_MAX_CHARS or matches_caption_pattern(joined):
        return False
    x0 = min(ln.bbox[0] for ln in lines)
    x1 = max(ln.bbox[2] for ln in lines)
    if body_width > 0 and (x1 - x0) > _LABEL_MAX_WIDTH_RATIO * body_width:
        return False
    return True


_FOOTNOTE_DEF_RE = re.compile(r"^\s*(\d{1,3}|[*†‡])[.):]?\s+(\S.*)$")


def parse_footnote_definition(text: str) -> Optional[Tuple[str, str]]:
    """``"1 Some note."`` -> ``("1", "Some note.")`` (step 14), else ``None``."""
    match = _FOOTNOTE_DEF_RE.match(text)
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def is_footnote_marker(text: str) -> bool:
    """Superscript span text that can act as a footnote reference."""
    stripped = text.strip()
    return bool(stripped) and (stripped.isdigit() or stripped in "*†‡")


# ---------------------------------------------------------------------------
# v1.1 F2: overlay style, alignment and line grouping (design doc 06, §4.4; Addendum A)
# ---------------------------------------------------------------------------

_SANS_FONT_RE = re.compile(
    r"sans|arial|helvetica|nimbus ?sans|verdana|calibri|segoe|tahoma", re.IGNORECASE
)
_ITALIC_FONT_RE = re.compile(r"italic|oblique", re.IGNORECASE)
_LETTER_RE = re.compile(r"[^\W\d_]")
_PREFIX_MAX_CHARS = 6
_PREFIX_TERMINALS = ".):"
_EDGE_TOLERANCE_PT = 1.0
_RIGHT_EDGE_TOLERANCE_PT = 1.5  # justified lines end within this of the block's right edge
_CENTER_TOLERANCE_PT = 2.0
_JUSTIFY_MIN_LINES = 3  # two full lines + the (possibly short) last one
_FIRST_INDENT_MAX_HEIGHTS = 3.0  # a first-line indent is at most ~3 line heights wide
_PARA_INDENT_MIN_EM = 0.8
_PARA_INDENT_MAX_EM = 4.0
_PARA_SHORT_GAP_EM = 2.0


def font_family(font_name: str) -> OverlayFamily:
    """``serif`` / ``sans`` / ``mono`` from a PDF font name (design doc 06, §4.4 step 6)."""
    if is_monospace_font(font_name):
        return "mono"
    if _SANS_FONT_RE.search(font_name):
        return "sans"
    return "serif"


def is_italic_font(font_name: str) -> bool:
    return _ITALIC_FONT_RE.search(font_name) is not None


def color_hex(value: int) -> str:
    """pymupdf span colour (``0xRRGGBB`` int) -> ``"#rrggbb"``."""
    return f"#{max(0, int(value)) & 0xFFFFFF:06x}"


def has_letters(text: str) -> bool:
    """True when ``text`` holds at least one letter; ``"1.3"``, ``"19"``, ``"©"`` do not."""
    return _LETTER_RE.search(text) is not None


def _style_runs(spans: Sequence[Span]) -> List[Tuple[str, bool, bool]]:
    """Consecutive spans with equal (bold, italic) merged into ``(text, bold, italic)`` runs."""
    runs: List[Tuple[str, bool, bool]] = []
    for span in spans:
        if not span.text:
            continue
        italic = span.italic or is_italic_font(span.font)
        if runs and runs[-1][1] == span.bold and runs[-1][2] == italic:
            runs[-1] = (runs[-1][0] + span.text, span.bold, italic)
        else:
            runs.append((span.text, span.bold, italic))
    return runs


def detect_prefix_style(lines: Sequence[Line]) -> Optional[Tuple[str, bool, bool]]:
    """E-46 / Q22: ``(prefix_text, bold, italic)`` for a "**13.** 3D reconstruction" pattern.

    Exactly two style runs over the whole block where the first run is at most six
    characters (after stripping) and ends with ``.``, ``)`` or ``:``; otherwise ``None``.
    """
    spans = [sp for ln in lines for sp in ln.spans]
    runs = _style_runs(spans)
    if len(runs) != 2:
        return None
    prefix = runs[0][0].strip()
    if not prefix or len(prefix) > _PREFIX_MAX_CHARS or prefix[-1] not in _PREFIX_TERMINALS:
        return None
    return (prefix, runs[0][1], runs[0][2])


def detect_style(lines: Sequence[Line]) -> OverlayStyle:
    """Dominant span style by character count (design doc 06, §4.4 step 6)."""
    weights: Counter[Tuple[float, bool, bool, OverlayFamily, str]] = Counter()
    for ln in lines:
        for span in ln.spans:
            chars = len(span.text.strip())
            if chars <= 0:
                continue
            key = (
                round_half(span.size),
                span.bold,
                span.italic or is_italic_font(span.font),
                font_family(span.font),
                span.color,
            )
            weights[key] += chars
    if not weights:
        return OverlayStyle(
            font_size=0.0, bold=False, italic=False, family="serif", color="#000000"
        )
    (size, bold, italic, family, color), _ = max(
        weights.items(), key=lambda kv: (kv[1], kv[0][0], not kv[0][1])
    )
    return OverlayStyle(
        font_size=size,
        bold=bold,
        italic=italic,
        family=family,
        color=color,
        prefix_style=detect_prefix_style(lines),
    )


def first_line_indent(line_boxes: Sequence[BBox]) -> float:
    """First-line indent (pt) of a block: the first line starts right of the other lines,
    which share one left edge (+- 1 pt); ``0.0`` when the block shows no such pattern."""
    boxes = [b for b in line_boxes if b[2] > b[0]]
    if len(boxes) < 2:
        return 0.0
    rest = [b[0] for b in boxes[1:]]
    if max(rest) - min(rest) > _EDGE_TOLERANCE_PT:
        return 0.0
    indent = boxes[0][0] - min(rest)
    height = max(boxes[0][3] - boxes[0][1], 1.0)
    if indent <= _EDGE_TOLERANCE_PT or indent > _FIRST_INDENT_MAX_HEIGHTS * height:
        return 0.0
    return indent


def detect_alignment(
    line_boxes: Sequence[BBox], container: Optional[BBox] = None
) -> OverlayAlignment:
    """Alignment of a block from its line boxes (design doc 06, §4.4 step 7).

    Three or more lines: left edges within 1 pt (a first-line indent is tolerated)
    **and** right edges within 1.5 pt (all but the last line, which may be short)
    -> JUSTIFY; left only -> LEFT; every line centred
    (|left margin - right margin| <= 2 pt) inside the block extent -> CENTER; right edges
    only -> RIGHT. A single line is CENTER when centred (+- 2 pt) inside ``container``
    (the smallest drawing rect around it), else LEFT.
    """
    boxes = [b for b in line_boxes if b[2] > b[0]]
    if not boxes:
        return OverlayAlignment.LEFT
    if len(boxes) == 1:
        box = boxes[0]
        if container is not None and container[2] > container[0]:
            left_margin = box[0] - container[0]
            right_margin = container[2] - box[2]
            if abs(left_margin - right_margin) <= _CENTER_TOLERANCE_PT and left_margin > 0.5:
                return OverlayAlignment.CENTER
        return OverlayAlignment.LEFT
    lefts = [b[0] for b in boxes]
    rights = [b[2] for b in boxes]
    indented = first_line_indent(boxes) > 0.0
    body_lefts = lefts[1:] if indented else lefts
    left_aligned = max(body_lefts) - min(body_lefts) <= _EDGE_TOLERANCE_PT
    if left_aligned and len(boxes) >= _JUSTIFY_MIN_LINES:
        full = rights[:-1]
        if (
            max(full) - min(full) <= _RIGHT_EDGE_TOLERANCE_PT
            and rights[-1] <= max(full) + _RIGHT_EDGE_TOLERANCE_PT
        ):
            return OverlayAlignment.JUSTIFY
    if left_aligned:
        return OverlayAlignment.LEFT
    extent_x0, extent_x1 = min(lefts), max(rights)
    centred = all(
        abs((b[0] - extent_x0) - (extent_x1 - b[2])) <= _CENTER_TOLERANCE_PT for b in boxes
    )
    if centred:
        return OverlayAlignment.CENTER
    if max(rights) - min(rights) <= _EDGE_TOLERANCE_PT:
        return OverlayAlignment.RIGHT
    return OverlayAlignment.LEFT


def _line_size(line: Line) -> float:
    return line.size if line.size > 0 else max(1.0, line.height / 1.2)


def lines_join(
    prev: Line,
    line: Line,
    group_bbox: BBox,
    group_size: float,
    *,
    line_gap_ratio: float,
    min_h_overlap: float,
) -> bool:
    """§4.4 step 3: does ``line`` continue the open group ending with ``prev``?"""
    if line.dir != prev.dir:
        return False
    height_l = max(line.height, 0.1)
    height_p = max(prev.height, 0.1)
    gap = line.bbox[1] - prev.bbox[3]
    if gap > line_gap_ratio * min(height_l, height_p):
        return False
    if line.bbox[1] < prev.bbox[1] + 0.5 * height_p:  # same baseline row: never a continuation
        return False
    size = _line_size(line)
    if abs(size - group_size) > 1.0:
        return False
    overlap = min(line.bbox[2], group_bbox[2]) - max(line.bbox[0], group_bbox[0])
    width = min(line.bbox[2] - line.bbox[0], group_bbox[2] - group_bbox[0])
    if width > 0 and overlap / width >= min_h_overlap:
        return True
    return abs(line.bbox[0] - group_bbox[0]) <= size


def union_bbox(a: BBox, b: BBox) -> BBox:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def split_paragraphs(group: Sequence[Line]) -> List[List[Line]]:
    """Paragraphs of one vertically contiguous line group (no extra gap between them).

    A line opens a new paragraph when

    * it is indented against the group's left edge by 0.8-4 em, is not merely a centred
      line (left indent == right gap), is not the continuation of a hanging list item,
      and the line before it closes a paragraph (ends short of the right edge by >= 2 em
      or ends with terminal punctuation); or
    * it starts at the left edge with a capital/digit while the previous line ends with
      terminal punctuation >= 2 em short of the right edge, in a group whose lines mostly
      reach the right edge (block paragraphs of justified text; ragged text is left alone); or
    * it starts with a list marker (``parse_list_marker``: bullet, ``1.``, ``a)``) after a
      closed line or inside a list (CR-41): every list item is its own unit, the wrapped
      (hanging) lines of an item stay with it.
    """
    if len(group) < 2:
        return [list(group)]
    left = min(ln.bbox[0] for ln in group)
    right = max(ln.bbox[2] for ln in group)
    full_lines = sum(1 for ln in group if right - ln.bbox[2] <= _RIGHT_EDGE_TOLERANCE_PT)
    mostly_full = full_lines * 2 >= len(group)
    paragraphs: List[List[Line]] = [[group[0]]]
    for prev, line in zip(group, group[1:], strict=False):
        em = max(_line_size(line), 1.0)
        indent = line.bbox[0] - left
        right_gap = right - line.bbox[2]
        prev_short = right - prev.bbox[2] >= _PARA_SHORT_GAP_EM * em
        prev_closed = prev_short or ends_with_terminal(prev.text)
        starts_new = False
        current = paragraphs[-1]
        in_item = parse_list_marker(current[0].text) is not None
        if parse_list_marker(line.text) is not None and (prev_closed or in_item):
            starts_new = True
        elif _PARA_INDENT_MIN_EM * em <= indent <= _PARA_INDENT_MAX_EM * em:
            centred = abs(indent - right_gap) <= _CENTER_TOLERANCE_PT
            # wrapped lines of a list item hang under the item text, right of the marker
            hanging = in_item and (
                len(current) == 1 or abs(line.bbox[0] - current[1].bbox[0]) <= _EDGE_TOLERANCE_PT
            )
            starts_new = prev_closed and not centred and not hanging
        elif indent <= _EDGE_TOLERANCE_PT and mostly_full:
            first = line.text[:1]
            starts_new = (
                prev_short
                and ends_with_terminal(prev.text)
                and prev.bbox[0] - left <= _PARA_INDENT_MAX_EM * em
                and (first.isupper() or first.isdigit())
            )
        if starts_new:
            paragraphs.append([line])
        else:
            paragraphs[-1].append(line)
    return paragraphs


def group_lines(
    lines: Sequence[Line],
    *,
    line_gap_ratio: float = 0.5,
    min_h_overlap: float = 0.30,
    paragraphs: bool = False,
) -> List[List[Line]]:
    """Greedy vertical grouping of lines into overlay units (design doc 06, §4.4 steps 2-3).

    Lines are sorted by ``(y0, x0)``; a line joins the most recent open group whose last
    line it succeeds vertically while overlapping horizontally (or sharing the left edge
    within one em), with the same size (+- 1 pt) and direction. Everything else starts a
    new group; groups keep the order in which they were opened. With ``paragraphs`` every
    group is further split at paragraph starts (:func:`split_paragraphs`).
    """
    ordered = sorted(lines, key=lambda ln: (round(ln.bbox[1], 3), round(ln.bbox[0], 3)))
    groups: List[List[Line]] = []
    boxes: List[BBox] = []
    sizes: List[float] = []
    for line in ordered:
        # Interleaved columns (E6: the boxes of a diagram) put lines of other groups between
        # two lines of one label, so every open group is a candidate; the most recent one
        # that satisfies the succession rule wins (deterministic: sorted input, fixed scan).
        joined = False
        for index in range(len(groups) - 1, -1, -1):
            if lines_join(
                groups[index][-1],
                line,
                boxes[index],
                sizes[index],
                line_gap_ratio=line_gap_ratio,
                min_h_overlap=min_h_overlap,
            ):
                groups[index].append(line)
                boxes[index] = union_bbox(boxes[index], line.bbox)
                joined = True
                break
        if joined:
            continue
        groups.append([line])
        boxes.append(line.bbox)
        sizes.append(_line_size(line))
    if paragraphs:
        return [part for group in groups for part in split_paragraphs(group)]
    return groups


SIDE_BY_SIDE_X_RATIO = 0.25
"""Horizontal overlap (as a fraction of the *narrower* rect) below which two rects that
share rows are read as genuine side-by-side text - two columns, a table row, the two
halves of a text row broken by inline math - and are pulled apart sideways instead of
losing rows to each other.

Measured against the narrower rect, never against the page: a word-wide rect inside a
paragraph-wide one overlaps it over its whole width and is never "beside" it, while two
columns whose glyph boxes touch by a point or two keep their rects. A quarter of the
narrower rect is far more than the sub-point contact of touching columns and far less
than the share any rect that really sits *over* another one covers."""


def _clear_of(rect: BBox, blocker: BBox) -> BBox:
    """``rect`` cut back to the larger of the two parts that stay off ``blocker``.

    The part above (``rect.y0 .. blocker.y0``) or the part below (``blocker.y1 ..
    rect.y1``), whichever is taller - the side ``blocker`` does *not* sit on. Both parts
    may come out empty (``blocker`` covers ``rect``), which the caller checks."""
    above = blocker[1] - rect[1]
    below = rect[3] - blocker[3]
    if above >= below:
        return (rect[0], rect[1], rect[2], max(rect[1], blocker[1]))
    return (rect[0], min(rect[3], blocker[3]), rect[2], rect[3])


def _clear_of_x(rect: BBox, blocker: BBox) -> BBox:
    """``rect`` cut back sideways so that it stops at the near edge of ``blocker``."""
    if rect[0] <= blocker[0]:
        return (rect[0], rect[1], min(rect[2], blocker[0]), rect[3])
    return (max(rect[0], blocker[2]), rect[1], rect[2], rect[3])


def _contains_vertically(outer: BBox, inner: BBox) -> bool:
    return outer[1] <= inner[1] and outer[3] >= inner[3]


def _covers(outer: BBox, inner: BBox) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _overlaps(a: BBox, b: BBox) -> Tuple[float, float]:
    """``(x_overlap, y_overlap)`` of two rects; either is <= 0 when they are disjoint."""
    return (min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1]))


def _redaction_rects(
    rects: Sequence[BBox], placement: Sequence[BBox], painted: Sequence[bool]
) -> List[BBox]:
    """The rect each painted unit is *redacted* over: its own, minus every kept unit.

    A painted unit's placement rect is also pulled off its painted neighbours, which is
    what keeps two translations from printing on top of each other - but the source glyphs
    under the strip that was cut away belong to nobody then, and stay on the page next to
    the translation. Redaction therefore starts from the unit's *original* rect and only
    gives way to units that keep their source text (a math band, a page number, a unit that
    lost its rect to an overlap): those glyphs must survive. Two painted units may end up
    with overlapping redaction rects, which is harmless - redaction is one pass over the
    page before anything is written back.

    A cut is refused when it would eat into the unit's own placement rect (only reachable
    when that rect already overlapped a kept one): the unit then redacts exactly what it
    paints, the behaviour before this split existed. Kept units are never redacted; their
    entry is the placement rect so that a caller cannot widen it by accident.
    """
    red: List[BBox] = list(rects)
    for i, _ in enumerate(red):
        if not painted[i]:
            red[i] = placement[i]
            continue
        for j, blocker in enumerate(rects):
            if j == i or painted[j]:
                continue
            x_overlap, y_overlap = _overlaps(red[i], blocker)
            narrower = min(red[i][2] - red[i][0], blocker[2] - blocker[0])
            if x_overlap <= 0 or y_overlap <= 0 or narrower <= 0:
                continue
            if x_overlap < SIDE_BY_SIDE_X_RATIO * narrower:
                cut = _clear_of_x(red[i], blocker)
            else:
                cut = _clear_of(red[i], blocker)
            red[i] = cut if _covers(cut, placement[i]) else placement[i]
    return red


def split_overlapping_rects(
    rects: Sequence[BBox],
    *,
    painted: Optional[Sequence[bool]] = None,
    min_heights: Optional[Sequence[float]] = None,
) -> Tuple[List[BBox], List[BBox], List[int]]:
    """§4.4 step 4: no painted rect may overlap another rect of the page.

    A painted rect is redacted and re-drawn, so two painted rects that share a row print
    two texts on top of each other, and a painted rect over a *kept* one (a math band, a
    page number, the dot leaders of a ToC line) redacts glyphs that nobody re-draws.
    ``painted`` says which rects are painted (default: all of them); a pair of kept rects
    is never touched, because nothing is drawn for either of them.

    Pairs that share rows only by a sliver of width (``SIDE_BY_SIDE_X_RATIO``) are real
    side-by-side text - two halves of a row broken by inline math, two columns, a cell and
    its neighbour - and are separated *horizontally* instead, at the middle of that sliver:
    they belong beside each other and neither may lose a row. Every other pair is separated
    vertically:

    * a kept rect never moves - the painted one is cut back to the taller of its two
      parts that stay off it (:func:`_clear_of`);
    * of two painted rects, one containing the other vertically is cut back the same way,
      so the container ends where the contained one begins (a paragraph that flows around
      a display equation loses the rows the equation occupies);
    * two painted rects that only cross keep the historical behaviour: the overlap band is
      split at its middle, the upper rect ends there and the lower one starts there.

    A cut that would leave a rect less than its ``min_heights`` entry (one line at the
    smallest font the exporter will set for it) is not made: the *smaller* rect of the pair
    is reported as unpaintable instead and keeps its source text. Rects only ever shrink,
    so one pass over the pairs is enough: a pair made disjoint stays disjoint.

    Returns ``(placement_rects, redaction_rects, unpaintable)``, all in input order. The
    placement rect is the one a translation is written into (the rects described above);
    the redaction rect is the one the source glyphs are removed from and gives way to kept
    units only (:func:`_redaction_rects`). ``unpaintable`` holds the sorted indexes of the
    rects that could not be pulled off a neighbour.
    """
    out: List[BBox] = list(rects)
    paints: List[bool] = [True] * len(out) if painted is None else list(painted)
    floors: List[float] = [0.0] * len(out) if min_heights is None else list(min_heights)
    dropped: Set[int] = set()

    def drop_smaller(i: int, j: int) -> None:
        area_i = (out[i][2] - out[i][0]) * (out[i][3] - out[i][1])
        area_j = (out[j][2] - out[j][0]) * (out[j][3] - out[j][1])
        # the kept rect of a mixed pair cannot be dropped - it is not painted to begin with
        candidates = [k for k in (i, j) if paints[k]]
        index = min(candidates, key=lambda k: (area_i if k == i else area_j, k))
        paints[index] = False
        dropped.add(index)

    for i in range(len(out)):
        for j in range(i + 1, len(out)):
            if not (paints[i] or paints[j]):
                continue
            a, b = out[i], out[j]
            x_overlap = min(a[2], b[2]) - max(a[0], b[0])
            narrower = min(a[2] - a[0], b[2] - b[0])
            if x_overlap <= 0 or narrower <= 0:
                continue
            if min(a[3], b[3]) - max(a[1], b[1]) <= 0:
                continue
            if x_overlap < SIDE_BY_SIDE_X_RATIO * narrower:  # beside each other: trim in x
                if paints[i] != paints[j]:  # the kept rect keeps its full width, too
                    moving, blocker = (i, j) if paints[i] else (j, i)
                    out[moving] = _clear_of_x(out[moving], out[blocker])
                    continue
                middle_x = max(a[0], b[0]) + x_overlap / 2.0
                left, right = (i, j) if a[0] <= b[0] else (j, i)
                out[left] = (out[left][0], out[left][1], middle_x, out[left][3])
                out[right] = (middle_x, out[right][1], out[right][2], out[right][3])
                continue
            if paints[i] != paints[j]:  # one kept: only the painted rect gives way
                moving, blocker = (i, j) if paints[i] else (j, i)
                cut = _clear_of(out[moving], out[blocker])
                if cut[3] - cut[1] >= floors[moving]:
                    out[moving] = cut
                else:
                    drop_smaller(i, j)
                continue
            if _contains_vertically(a, b) or _contains_vertically(b, a):
                outer, inner = (i, j) if _contains_vertically(a, b) else (j, i)
                cut = _clear_of(out[outer], out[inner])
                if cut[3] - cut[1] >= floors[outer]:
                    out[outer] = cut
                else:
                    drop_smaller(i, j)
                continue
            top, bottom, ti, bi = (a, b, i, j) if a[1] <= b[1] else (b, a, j, i)
            middle = bottom[1] + (top[3] - bottom[1]) / 2.0
            if middle - top[1] < floors[ti] or bottom[3] - middle < floors[bi]:
                drop_smaller(i, j)
                continue
            out[ti] = (top[0], top[1], top[2], middle)
            out[bi] = (bottom[0], middle, bottom[2], bottom[3])
    return out, _redaction_rects(rects, out, paints), sorted(dropped)


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 2)


def compute_source_profile(pages: Sequence[RawPage], body_size: float) -> SourceProfile:
    """Page size, body font, margins and serif-ness of the source (Addendum A).

    Page size = the most common ``(width, height)``; margins = median distance of each
    text envelope to the page edges (pages with text only); ``serif`` = the
    character-weighted dominant family of the body-sized spans is serif.
    """
    sizes: Counter[Tuple[float, float]] = Counter(
        (round(p.width, 2), round(p.height, 2)) for p in pages
    )
    width, height = 0.0, 0.0
    if sizes:
        (width, height), _ = max(sizes.items(), key=lambda kv: (kv[1], kv[0]))
    tops: List[float] = []
    rights: List[float] = []
    bottoms: List[float] = []
    lefts: List[float] = []
    families: Counter[str] = Counter()
    measured = 0
    for page in pages:
        boxes = [ln.bbox for b in page.blocks for ln in b.lines if ln.text]
        if not boxes:
            continue
        measured += 1
        tops.append(max(0.0, min(b[1] for b in boxes)))
        rights.append(max(0.0, page.width - max(b[2] for b in boxes)))
        bottoms.append(max(0.0, page.height - max(b[3] for b in boxes)))
        lefts.append(max(0.0, min(b[0] for b in boxes)))
        for block in page.blocks:
            for ln in block.lines:
                for span in ln.spans:
                    if body_size <= 0 or abs(round_half(span.size) - body_size) <= 0.5:
                        families[font_family(span.font)] += len(span.text.strip())
    serif = families.most_common(1)[0][0] == "serif" if families else True
    return SourceProfile(
        page_width_pt=width,
        page_height_pt=height,
        body_font_size_pt=body_size,
        margins_pt=(_median(tops), _median(rights), _median(bottoms), _median(lefts)),
        serif=serif,
        pages_measured=measured,
    )
