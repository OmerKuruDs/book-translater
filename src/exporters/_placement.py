"""Shared measure / redact / insert core of the overlay PDF and the translated figure PNG
(design doc 06, 4.5 steps 1-5; v1.1 review: CR-52, CR-54 and the shared per-page scale).

Placement is a three-step pass over the rectangles of one page (or one figure):

1. **Measure** (:func:`plan_placements`): every translation is laid out on a scratch page
   with the exact ``insert_htmlbox`` call that will later write it. The result is the
   unit's *natural* scale, or a shortened text with an ellipsis when not even the floor
   scale fits (E-38). A rectangle that cannot take any text is reported as unplaceable
   *before* anything is redacted, so the caller leaves the source text alone (CR-52).
2. **Group**: units of one style (the caller's group key: page, kind, font size, family)
   share one font factor, so paragraphs of equal source size come out at an equal size.
   ``bound = max(review_threshold, median - SHARED_SPREAD)``; the shared scale is the
   smallest natural scale at or above ``bound``; units below it are outliers and keep
   their own scale (one bad box never drags a page down). A unit that the shared scale
   would push more than ``max_drop`` below its own natural scale leaves the group as well,
   so text that fits comfortably is never shrunk to a crowded neighbour's size (CR-95).
   Grouping then runs again over everything that was left out, so those units share a size
   with each other instead of each landing on its own - one page ends up with a few clear
   sizes, not a spread of nearly identical ones. ``max_drop`` of 1.0 turns the cap off for
   callers that want one size for the whole set (figure labels). Before the font is shrunk
   the line height may be tightened, but only down to ``LINE_HEIGHT_MIN`` - never below the
   source rhythm's band (CR-96).
3. **Place** (:func:`redact`, :func:`place`): the source text under the rectangles is removed
   with a fill-less redaction (images and vector drawings stay, pre-existing redaction
   annotations are parked and restored untouched) and each plan is written at
   ``font_size x factor`` with the floor scale as safety net.

Nothing here touches the file system; callers own the document.
"""

from __future__ import annotations

import html as html_lib
import math
import re
import statistics
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Hashable, List, Optional, Sequence, Tuple, Union

from ..domain.overlay import BBox, OverlayAlignment, OverlayFamily
from ..pdfkit import api as pdfkit
from .fonts import FontSpec

ELLIPSIS = " …"
"""Suffix appended to a truncated block (E-38)."""

LINE_HEIGHT_MIN = 1.15
"""Tightest line height the tool ever writes (R15: no MuPDF default slack)."""

LINE_HEIGHT_MAX = 1.40
"""Loosest line height derived from the source: beyond this the block is airy enough."""

LINE_HEIGHT = LINE_HEIGHT_MIN
"""Line height of an inserted block whose source rhythm cannot be measured (CR-96)."""

SHARED_SPREAD = 0.10
"""A unit more than this below the group median is an outlier and keeps its own scale.

Narrowed from 0.15 alongside the CR-95 cap: at 0.15 the two most crowded paragraphs of the
reference page stayed *inside* the group, pulled the shared scale down to where the cap then
ejected the six comfortable units, and the page came out in five sizes. At 0.10 those two are
outliers from the start, the six share one size, and the cap never has to fire."""

SHARED_MAX_DROP = 0.10
"""Per-unit ceiling on the shared scale (CR-95): a unit the group scale would shrink by
more than this fraction of its own natural scale leaves the group and keeps that scale."""

LABEL_MAX_DROP = 1.0
"""``max_drop`` for a group of labels: the per-unit cap is off.

The cap exists so a page of prose does not shrink paragraphs that fit down to a crowded
neighbour's size. Labels are the opposite case: a diagram is read as one object, and
labels at seven slightly different sizes look broken where one size does not. Label boxes
also vary far more in size than paragraphs do, so the cap ejected most of them."""

SHARED_SCALE_NOTE = 0.9
"""A shared font factor below this is worth an informational page-level review record."""

SHARED_MARGIN = 0.99
"""Safety factor on the shared scale: font scaling and layout scaling differ slightly."""

_REFINE_ROUNDS = 4
_MIN_TRUNCATED_CHARS = 1

_NON_ASCII_LETTER = re.compile(r"[^\W\d_]")
SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
"""Footnote markers travel through translation as Unicode superscript digits."""
_SUPERSCRIPT_RUN = re.compile(f"[{SUPERSCRIPT_DIGITS}]+")
_FROM_SUPERSCRIPT = str.maketrans(SUPERSCRIPT_DIGITS, "0123456789")
_INDENT_MIN_PT = 1.0
_FAMILY_CSS = {"serif": "serif", "sans": "sans-serif", "mono": "monospace"}
_SCRATCH_ORIGIN = 5.0


@dataclass(frozen=True)
class PlacementSpec:
    """What to write into one rectangle and how it must look."""

    rect: BBox
    text: str
    font_size: float
    bold: bool
    italic: bool
    family: OverlayFamily
    color: str
    alignment: OverlayAlignment
    prefix_style: Optional[Tuple[str, bool, bool]] = None
    first_line_indent_pt: float = 0.0
    """First-line indent of the source paragraph against the other lines: positive for an
    indented paragraph (CSS ``text-indent``), negative for a hanging list item (marker at
    the left edge, wrapped lines further right: ``padding-left`` + negative ``text-indent``).
    Not persisted: derived from the line boxes at render time."""
    rotate: int = 0
    """``/Rotate`` of the target page. ``rect`` is then in unrotated page space and the text
    is turned so that it reads upright (CR-53)."""
    line_height: float = LINE_HEIGHT
    """Line height (em) the block starts out with: the source rhythm as measured by
    :func:`source_line_height`. Not persisted, derived at render time like the indent."""


@dataclass(frozen=True)
class PlacementPlan:
    """Measured decision for one rectangle (step 1-2); nothing has been written yet."""

    spec: PlacementSpec
    text: Optional[str]
    """The text to write (a prefix + ellipsis when ``truncated``); ``None`` when the
    rectangle cannot take any text - the caller must not redact it (CR-52)."""
    truncated: bool
    natural_scale: float
    """The unit's own scale at the source font size (-1.0: does not fit at the floor)."""
    font_factor: float = 1.0
    """Font size multiplier: 1.0, or the group's shared factor."""
    line_height: float = LINE_HEIGHT
    shared: bool = False

    @property
    def placeable(self) -> bool:
        return self.text is not None


@dataclass(frozen=True)
class Placement:
    """Outcome of :func:`place` / :func:`insert_block`."""

    placed: bool
    scale: float
    """Effective scale against the source font size (``font_factor x insert scale``)."""
    spare_height: float
    truncated: bool
    text: str
    """The text actually written (a prefix + ellipsis when ``truncated``)."""
    font_size: float = 0.0
    """Final font size in points (``source size x scale``); 0.0 when nothing was placed."""
    shared_scale: Optional[float] = None
    """The group's font factor when the unit was placed with a shared scale."""


def normalise(text: str) -> str:
    return " ".join(text.split())


def family_css(family: str, font: FontSpec) -> str:
    """CSS ``font-family`` for a block: the user font (``--pdf-font``) replaces serif and
    sans; monospace text keeps the generic family (its glyph widths matter)."""
    if not font.is_builtin and family != "mono":
        return f"'{font.family_css}'"  # single quotes: the rule sits in a style="..." attribute
    return _FAMILY_CSS.get(family, "serif")


def font_archive(font: FontSpec) -> Optional[Any]:
    """``pymupdf.Archive`` resolving the ``@font-face`` url of a user font, else ``None``."""
    if font.archive_dir is None:
        return None
    return pdfkit.new_archive(str(font.archive_dir))


def _style(bold: bool, italic: bool) -> str:
    weight = "bold" if bold else "normal"
    style = "italic" if italic else "normal"
    return f"font-weight:{weight};font-style:{style}"


def first_line_indent(rect: BBox, line_boxes: Sequence[BBox]) -> float:
    """Indent (pt) of the first source line against the other lines of a multi-line block.

    Positive: the first line starts right of the rect's left edge while every other line
    starts at it (+- 1 pt) - an indented paragraph. Negative: the first line starts at the
    left edge and every other line shares one edge further right - a hanging list item
    (CR-41). ``0.0`` for single lines, centred text and anything irregular."""
    if len(line_boxes) < 2:
        return 0.0
    width = rect[2] - rect[0]
    first = line_boxes[0][0] - rect[0]
    rest = [box[0] - rect[0] for box in line_boxes[1:]]
    if max(rest) - min(rest) > _INDENT_MIN_PT:
        return 0.0
    if abs(rest[0]) <= _INDENT_MIN_PT:
        if first < _INDENT_MIN_PT or first > 0.5 * width:
            return 0.0
        return round(first, 2)
    if abs(first) <= _INDENT_MIN_PT and _INDENT_MIN_PT <= rest[0] <= 0.5 * width:
        return -round(rest[0], 2)
    return 0.0


def source_line_height(line_boxes: Sequence[BBox], font_size: float) -> float:
    """Line height (em) of the source block, measured from its line boxes (CR-76, CR-96).

    The pitch of two consecutive lines is the distance of their top edges, which is the
    baseline distance for lines set in one size; the median pitch over the block divided
    by the source font size is the block's own rhythm. The result is clipped to
    ``[LINE_HEIGHT_MIN, LINE_HEIGHT_MAX]``. Single lines, an unknown font size and
    irregular geometry (lines side by side) fall back to ``LINE_HEIGHT``."""
    if font_size <= 0.0 or len(line_boxes) < 2:
        return LINE_HEIGHT
    tops = sorted(box[1] for box in line_boxes)
    pitches = [b - a for a, b in zip(tops, tops[1:], strict=False) if b - a > 0.0]
    if not pitches:
        return LINE_HEIGHT
    ratio = statistics.median(pitches) / font_size
    return min(LINE_HEIGHT_MAX, max(LINE_HEIGHT_MIN, round(ratio, 3)))


def _escape(text: str) -> str:
    """HTML-escape ``text``; runs of Unicode superscript digits become ``<sup>n</sup>``
    (plain digits: every font has them and they match the surrounding face)."""
    escaped = html_lib.escape(text)
    return _SUPERSCRIPT_RUN.sub(
        lambda m: f"<sup>{m.group(0).translate(_FROM_SUPERSCRIPT)}</sup>", escaped
    )


def build_html(
    spec: PlacementSpec,
    font: FontSpec,
    text: Optional[str] = None,
    *,
    font_factor: float = 1.0,
    line_height: float = LINE_HEIGHT,
) -> str:
    """The ``<div>`` handed to ``insert_htmlbox`` (design doc 06, 4.5 step 4)."""
    body = spec.text if text is None else text
    inner = _escape(body)
    prefix = spec.prefix_style
    if prefix is not None and prefix[0] and body.startswith(prefix[0]):
        head, tail = body[: len(prefix[0])], body[len(prefix[0]) :]
        inner = (
            f'<span style="{_style(prefix[1], prefix[2])}">{_escape(head)}</span>'
            f"{_escape(tail)}"
        )
    shift = spec.first_line_indent_pt * font_factor
    padding = "padding:0;"
    indent = ""
    if shift > 0:
        indent = f"text-indent:{shift:g}pt;"
    elif shift < 0:
        padding = f"padding:0 0 0 {-shift:g}pt;"
        indent = f"text-indent:{shift:g}pt;"
    return (
        f'<div style="margin:0;{padding}line-height:{line_height:g};{indent}'
        f"font-family:{family_css(spec.family, font)};"
        f"font-size:{spec.font_size * font_factor:g}pt;"
        f"{_style(spec.bold, spec.italic)};text-align:{spec.alignment.value};"
        f'color:{spec.color}">{inner}</div>'
    )


def redact(doc: Any, page: Any, rects: Sequence[BBox]) -> Tuple[Any, int]:
    """Remove the text under every rect in one ``apply_redactions`` pass (fill-less).

    Redaction annotations that were on the page before are never applied and never
    re-created (E-50, CR-54): they are parked as ``/Square`` for the duration of the pass
    and turned back, so object, rectangle, fill colour and overlay text are untouched.
    Returns ``(page, preserved)``; the page object is a reloaded one when annotations were
    parked - use it from here on."""
    if not rects:
        return page, 0
    parked = pdfkit.page_redact_annot_xrefs(page)
    if parked:
        page = pdfkit.page_set_annot_subtypes(doc, page, parked, "Square")
    for rect in rects:
        pdfkit.page_add_redact_annot(page, pdfkit.rect(*rect), fill=False)
    pdfkit.page_apply_redactions(page)
    if parked:
        page = pdfkit.page_set_annot_subtypes(doc, page, parked, "Redact")
    return page, len(parked)


def clip_text(page: Any, rect: BBox) -> str:
    return pdfkit.page_plain_text(page, clip=pdfkit.rect(*rect))


def _insert(
    page: Any,
    spec: PlacementSpec,
    font: FontSpec,
    text: str,
    floor_scale: float,
    archive: Optional[Any],
    font_factor: float = 1.0,
    line_height: float = LINE_HEIGHT,
) -> Tuple[float, float]:
    return pdfkit.page_insert_htmlbox(
        page,
        pdfkit.rect(*spec.rect),
        build_html(spec, font, text, font_factor=font_factor, line_height=line_height),
        css=font.face_css,
        scale_low=floor_scale,
        archive=archive,
        rotate=spec.rotate,
    )


def _scratch_spec(spec: PlacementSpec) -> PlacementSpec:
    """``spec`` moved to the origin of an upright scratch page (same box as the reader sees
    it: width and height swap for a page rotated by 90 / 270 degrees)."""
    width, height = spec.rect[2] - spec.rect[0], spec.rect[3] - spec.rect[1]
    if spec.rotate % 180 == 90:
        width, height = height, width
    origin = _SCRATCH_ORIGIN
    return replace(spec, rect=(origin, origin, origin + width, origin + height), rotate=0)


def _trial(
    doc: Any,
    spec: PlacementSpec,
    font: FontSpec,
    text: str,
    floor_scale: float,
    archive: Optional[Any],
    font_factor: float = 1.0,
    line_height: float = LINE_HEIGHT,
) -> Tuple[float, float]:
    """``(spare, scale)`` of the exact insert on a throw-away page of ``doc``."""
    scratch = _scratch_spec(spec)
    page = pdfkit.doc_new_page(
        doc, max(scratch.rect[2] + 10.0, 100.0), max(scratch.rect[3] + 10.0, 100.0)
    )
    try:
        return _insert(page, scratch, font, text, floor_scale, archive, font_factor, line_height)
    finally:
        pdfkit.doc_delete_page(doc, pdfkit.doc_page_count(doc) - 1)


def measure_scale(
    scratch_doc: Any,
    spec: PlacementSpec,
    font: FontSpec,
    floor_scale: float,
    archive: Optional[Any] = None,
    *,
    font_factor: float = 1.0,
    line_height: float = LINE_HEIGHT,
) -> float:
    """Scale in ``[floor_scale, 1]`` at which ``spec.text`` fits its rectangle, measured
    with the real ``insert_htmlbox`` call on a scratch page of ``scratch_doc``; ``-1.0``
    when the text does not fit even at the floor. ``Story.fit_scale`` answers differently
    and is deliberately not used."""
    spare, scale = _trial(
        scratch_doc, spec, font, spec.text, floor_scale, archive, font_factor, line_height
    )
    return scale if spare >= 0 else -1.0


def _fits(
    doc: Any, spec: PlacementSpec, font: FontSpec, text: str, floor_scale: float,
    archive: Optional[Any], line_height: float,
) -> bool:
    spare, _scale = _trial(doc, spec, font, text, floor_scale, archive, 1.0, line_height)
    return spare >= 0


def ellipsis_text(
    spec: PlacementSpec,
    font: FontSpec,
    floor_scale: float,
    archive: Optional[Any],
    *,
    scratch_doc: Optional[Any] = None,
    line_height: float = LINE_HEIGHT,
) -> Optional[str]:
    """Longest prefix of ``spec.text`` + ellipsis that fits at ``floor_scale``.

    Binary search over the word count; when not even the first word fits, over the
    characters of that word, so a single long word becomes ``"Donau…"`` and never a bare
    ellipsis (CR-52). ``None`` when no character fits next to the ellipsis."""
    words = spec.text.split()
    if not words:
        return None
    scratch = scratch_doc if scratch_doc is not None else pdfkit.new_document()
    try:
        def fits(candidate: str) -> bool:
            return _fits(scratch, spec, font, candidate, floor_scale, archive, line_height)

        low, high = 0, len(words) - 1  # the full text is known not to fit
        while low < high:
            mid = (low + high + 1) // 2
            if fits(" ".join(words[:mid]) + ELLIPSIS):
                low = mid
            else:
                high = mid - 1
        if low > 0:
            return " ".join(words[:low]) + ELLIPSIS
        mark = ELLIPSIS.strip()
        word = words[0]
        low, high = 0, (len(word) - 1 if len(words) == 1 else len(word))
        while low < high:
            mid = (low + high + 1) // 2
            if fits(word[:mid] + mark):
                low = mid
            else:
                high = mid - 1
        if low >= _MIN_TRUNCATED_CHARS:
            return word[:low] + mark
        return None
    finally:
        if scratch_doc is None:
            pdfkit.close_document(scratch)


def plan_placements(
    specs: Sequence[PlacementSpec],
    font: FontSpec,
    floor_scale: float,
    archive: Optional[Any] = None,
    *,
    group_keys: Optional[Sequence[Hashable]] = None,
    review_threshold: float = 0.0,
    uniform: bool = False,
    max_drop: Union[float, Callable[[Hashable], float]] = SHARED_MAX_DROP,
) -> List[PlacementPlan]:
    """Measure every spec and decide text, font factor and line height (module docstring).

    ``uniform=False`` reproduces the one-rectangle-at-a-time behaviour: own scale, the
    spec's own line height. With ``uniform`` the specs with equal ``group_keys`` entries
    share a font factor and a block may be tightened to ``LINE_HEIGHT_MIN`` before its
    font is shrunk; ``group_keys`` defaults to ``(round(font_size, 1), family)``.
    ``max_drop`` is the per-unit cap on that shared factor, either one value or a function
    of the group key; :data:`LABEL_MAX_DROP` disables it, which is what a caller wants when
    a whole set must come out at one size (figure labels)."""
    if not specs:
        return []
    keys: Sequence[Hashable] = (
        group_keys
        if group_keys is not None
        else [(round(spec.font_size, 1), spec.family) for spec in specs]
    )
    scratch = pdfkit.new_document()
    try:
        plans: List[PlacementPlan] = []
        for spec in specs:
            line_height = spec.line_height
            natural = measure_scale(
                scratch, spec, font, floor_scale, archive, line_height=line_height
            )
            if uniform and natural < 1.0 and line_height > LINE_HEIGHT_MIN:
                # CR-96: the source rhythm may be tightened, but never below the band floor
                tight = measure_scale(
                    scratch, spec, font, floor_scale, archive, line_height=LINE_HEIGHT_MIN
                )
                if tight > natural:
                    natural, line_height = tight, LINE_HEIGHT_MIN
            if natural >= 0:
                plans.append(PlacementPlan(spec, spec.text, False, natural,
                                           line_height=line_height))
                continue
            shortened = ellipsis_text(
                spec, font, floor_scale, archive, scratch_doc=scratch, line_height=line_height
            )
            plans.append(PlacementPlan(spec, shortened, True, -1.0, line_height=line_height))
        if uniform:
            _share_scales(
                scratch, plans, keys, font, floor_scale, archive, review_threshold, max_drop
            )
        return plans
    finally:
        pdfkit.close_document(scratch)


def _share_scales(
    scratch: Any,
    plans: List[PlacementPlan],
    keys: Sequence[Hashable],
    font: FontSpec,
    floor_scale: float,
    archive: Optional[Any],
    review_threshold: float,
    max_drop: Union[float, Callable[[Hashable], float]] = SHARED_MAX_DROP,
) -> None:
    groups: Dict[Hashable, List[int]] = {}
    for index, plan in enumerate(plans):
        if plan.placeable and not plan.truncated:
            groups.setdefault(keys[index], []).append(index)
    for key, members in groups.items():
        drop = max_drop(key) if callable(max_drop) else max_drop
        # A style group is shared in passes: the units the bound or the cap leaves out are
        # not sent off to their own scales one by one - they are offered the same treatment
        # among themselves. Without this the cap traded one over-shrunk size per page for a
        # handful of nearly identical ones (10.33 / 10.45 / 10.50 pt on the reference page).
        pending = list(members)
        while len(pending) >= 2:
            assigned, leftover = _share_one(
                scratch, plans, pending, font, floor_scale, archive, review_threshold, drop
            )
            if not assigned and len(leftover) >= len(pending):
                break  # no progress possible: every remaining unit keeps its own scale
            pending = leftover


def _share_one(
    scratch: Any,
    plans: List[PlacementPlan],
    members: Sequence[int],
    font: FontSpec,
    floor_scale: float,
    archive: Optional[Any],
    review_threshold: float,
    max_drop: float,
) -> Tuple[List[int], List[int]]:
    """Form at most one shared group out of ``members``.

    Returns ``(units given the group scale, units left for the next pass)``. An empty
    first element means no group formed out of this set; the caller stops when the second
    element stops shrinking."""
    naturals = [plans[i].natural_scale for i in members]
    bound = max(review_threshold, statistics.median(naturals) - SHARED_SPREAD)
    eligible = [i for i in members if plans[i].natural_scale >= bound]
    rest = [i for i in members if plans[i].natural_scale < bound]
    if len(eligible) < 2:
        return [], rest
    shared = min(plans[i].natural_scale for i in eligible)
    if shared >= 1.0:
        return [], rest  # everything here fits at the source size: nothing to even out
    # CR-95: a unit the shared scale would shrink by more than ``max_drop`` of its own
    # scale is left out; the group scale only applies to units close to it. ``max_drop``
    # of 1.0 disables the cap (figure labels: one diagram, one label size).
    ceiling = shared / (1.0 - max_drop) if max_drop < 1.0 else math.inf
    capped = [i for i in eligible if plans[i].natural_scale <= ceiling]
    ejected = [i for i in eligible if plans[i].natural_scale > ceiling]
    if len(capped) < 2:
        return [], rest + ejected
    factor = shared * SHARED_MARGIN
    if factor < review_threshold <= shared:
        factor = review_threshold
    for _ in range(_REFINE_ROUNDS):
        worst = 1.0
        for i in capped:
            measured = measure_scale(
                scratch, plans[i].spec, font, floor_scale, archive,
                font_factor=factor, line_height=plans[i].line_height,
            )
            if measured >= 0:
                worst = min(worst, measured)
        if worst >= 1.0:
            break
        factor = max(floor_scale, factor * worst * SHARED_MARGIN)
    # the refine rounds may have pushed the factor past the cap for a member
    final = [i for i in capped if factor >= plans[i].natural_scale * (1.0 - max_drop)]
    dropped = [i for i in capped if factor < plans[i].natural_scale * (1.0 - max_drop)]
    if len(final) < 2:
        return [], rest + ejected + dropped
    for i in final:
        plans[i] = replace(plans[i], font_factor=factor, shared=True)
    return final, rest + ejected + dropped


def low_shared_factors(plans: Sequence[PlacementPlan]) -> List[Tuple[float, int]]:
    """``(font factor, unit count)`` of every shared group below ``SHARED_SCALE_NOTE``,
    smallest factor first (CR-95: the caller records this per page, informational).

    Empty when nothing was shared or every shared group stayed close to the source size."""
    counts: Dict[float, int] = {}
    for plan in plans:
        if not plan.shared or plan.font_factor >= SHARED_SCALE_NOTE:
            continue
        factor = round(plan.font_factor, 4)
        counts[factor] = counts.get(factor, 0) + 1
    return sorted(counts.items())


def place(
    page: Any,
    plan: PlacementPlan,
    font: FontSpec,
    floor_scale: float,
    archive: Optional[Any] = None,
) -> Placement:
    """Write one plan into its (already redacted) rectangle. Never enlarges."""
    spec = plan.spec
    if plan.text is None:
        return Placement(placed=False, scale=0.0, spare_height=-1.0, truncated=True, text="")
    attempts = [(plan.font_factor, plan.line_height)]
    if plan.font_factor != 1.0:
        attempts.append((1.0, plan.line_height))  # scratch and real page disagree: own scale
    for factor, line_height in attempts:
        low = min(1.0, floor_scale / factor) if factor > 0 else floor_scale
        spare, scale = _insert(page, spec, font, plan.text, low, archive, factor, line_height)
        if spare >= 0:
            effective = factor * scale
            return Placement(
                placed=True,
                scale=effective,
                spare_height=spare,
                truncated=plan.truncated,
                text=plan.text,
                font_size=spec.font_size * effective,
                shared_scale=factor if plan.shared and factor == plan.font_factor else None,
            )
    return Placement(placed=False, scale=0.0, spare_height=-1.0, truncated=True, text="")


def insert_block(
    page: Any, spec: PlacementSpec, font: FontSpec, floor_scale: float,
    archive: Optional[Any] = None,
) -> Placement:
    """Measure and write one rectangle on its own: full text when it fits at a scale in
    ``[floor_scale, 1]``, otherwise the ellipsis fallback (E-38). The caller has redacted
    the rectangle already; batch callers use :func:`plan_placements` + :func:`place`."""
    plan = plan_placements([spec], font, floor_scale, archive)[0]
    return place(page, plan, font, floor_scale, archive)


def missing_glyphs(page: Any, rect: BBox, text: str) -> str:
    """Non-ASCII letters of ``text`` absent from the extracted text of ``rect`` after
    insertion (design doc 06, 4.5 step 5); ``""`` when every letter came back."""
    extracted = clip_text(page, rect)
    wanted: List[str] = []
    for char in _NON_ASCII_LETTER.findall(text):
        if char in SUPERSCRIPT_DIGITS:  # rendered as <sup> with plain digits
            continue
        if ord(char) > 127 and char not in wanted and char not in extracted:
            wanted.append(char)
    return "".join(wanted)


def intersects(a: BBox, b: BBox) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
