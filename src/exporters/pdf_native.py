"""Native reflowed PDF engine on ``pymupdf.Story`` + ``DocumentWriter`` (design doc 06, 3.5).

Needs nothing outside the core install (FR-50). The HTML fragment produced by
:func:`exporters.base.markdown_to_html` (the EPUB markup) is laid out page by page
into the page rectangle minus the margins; heading positions reported by
``Story.element_positions`` become the outline, a centred 9 pt page number is
stamped on every page after the first, footnote/heading links are added best
effort, fonts are subset and the file is saved with ``garbage=4, deflate=True``
(D11, E3).

Page design (user decision, F2/F3 v1.1): the page rectangle, the body font size and
the margins come from the *source* (``ExportOptions.page_rect_pt``, ``body_font_pt``,
``margins_pt``) unless the user explicitly chose ``--pdf-page-size``/``--pdf-margin``.
Only when nothing at all is known the engine falls back to Letter / 10.5 pt and
reports ``pdf_defaults_assumed`` once. Headings scale with the body size (1.6 / 1.3
/ 1.15 em in ``book.css``).

Spike results (PyMuPDF 1.28.2, recorded for R17 / E-56):

* ``white-space: pre-wrap`` is honoured (spaces kept, lines wrap at spaces) but
  ``word-wrap: break-word`` is not: a run without whitespace wider than the
  column is *clipped*. Runs inside ``<pre>`` are therefore soft-broken here at
  ``min(90, column_width / (0.85 * body_pt * 0.61))`` characters (53 on A5 with
  18/15/20/15 mm margins at 12 pt) and reported once as ``code_lines_wrapped:N``
  (R17, E-56).
* Tables are laid out inside the column and cell text wraps; nothing is scaled.
  A table whose box still exceeds the column (unbreakable cell content) is
  reported as ``table_overflow:N`` (the design's ``table_scaled`` cannot be
  produced: MuPDF exposes no table scaling).
* ``<img>`` without size attributes is auto-fitted to the column width; explicit
  ``width``/``height`` are still emitted here so the 80 % page-height rule holds
  (E-54). The natural size is taken from the PNG pixel size and its stored DPI
  (``Pixmap.xres``; 96 when absent) -- ``figures.json`` is not read (layering).
* ``@font-face { src: url("name.ttf") }`` resolves through ``pymupdf.Archive``.
* Text extraction keeps ligatures ("fi" as one glyph): readers must expand them
  (``pdfkit.page_plain_text_expanded``) before comparing paragraphs.
"""

from __future__ import annotations

import html as html_lib
import io
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .. import __version__
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from ..pdfkit import api as pdfkit
from .base import ExportOptions, resolve_figure
from .fonts import FontSpec, font_css

MM_TO_PT = 72.0 / 25.4
FOOTER_FONT_SIZE = 9.0
FOOTER_FONT = "helv"  # base-14 Helvetica: digits only, never embedded (keeps NFR-24)
MAX_IMAGE_HEIGHT_RATIO = 0.8
DEFAULT_IMAGE_DPI = 96
MAX_PAGES = 20_000
CODE_WRAP_MAX_CHARS = 90
CODE_FONT_RATIO = 0.85  # ``pre { font-size: 0.85em }`` of the body size (book.css)
CODE_CHAR_ADVANCE = 0.61  # Nimbus Mono PS advance is 0.6 em; a little slack
SOURCE_PAGE_SIZE = "source"
"""``ExportOptions.page_size`` value meaning "use the source page rectangle"."""
FALLBACK_PAGE_SIZE = "Letter"  # last resort only, reported as ``pdf_defaults_assumed``
FALLBACK_BODY_PT = 10.5  # last resort only, reported as ``pdf_defaults_assumed``
FALLBACK_MARGIN_RATIO = 0.10  # of the page width / height, when no margin is known
DEFAULTS_ASSUMED_WARNING = "pdf_defaults_assumed"
PAGE_DESIGN_INVALID_WARNING = "pdf_page_design_invalid"
"""``pdf_page_design_invalid:<field>``: a source-derived value (``source_profile.json``) is
out of range and was replaced by the fallback (CR-65)."""
MIN_PAGE_PT, MAX_PAGE_PT = 72.0, 14400.0  # 1 inch .. 200 inches (the PDF limit)
MIN_BODY_PT, MAX_BODY_PT = 4.0, 72.0
MIN_COLUMN_PT = 36.0
_EMPTY_PAGE_LIMIT = 3
_TABLE_ID_PREFIX = "bt-table-"

PAGE_CSS = "html, body { margin: 0; padding: 0; }\n"
"""Reset appended after the stylesheet: the page margins come from the options."""

CSS_ALLOWED_PREFIXES: Tuple[str, ...] = (
    "font-", "margin", "padding", "border", "page-break-", "list-style", "background",
)
CSS_ALLOWED: frozenset[str] = frozenset({
    "color", "text-align", "text-indent", "line-height", "white-space", "display",
    "width", "height", "vertical-align", "word-wrap", "overflow-wrap", "text-decoration",
    "src",  # inside @font-face
})
"""MuPDF Story CSS subset (design doc 06, 3.5 allowlist plus the properties the spike
confirmed: ``background``, ``word-wrap``/``overflow-wrap``, ``text-decoration``)."""
CSS_ALLOWED_AT_RULES: frozenset[str] = frozenset({"font-face"})

_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_CSS_AT_RULE = re.compile(r"@([A-Za-z-]+)")
_CSS_BLOCK = re.compile(r"\{([^{}]*)\}")
_CSS_PROPERTY = re.compile(r"^\s*(-?[A-Za-z][A-Za-z0-9-]*)\s*:")
_IMG = re.compile(r"<img\b([^>]*?)\s*/?>", re.IGNORECASE)
_ATTR = re.compile(r'([A-Za-z-]+)\s*=\s*"([^"]*)"')
_TABLE_OPEN = re.compile(r"<table\b([^>]*)>", re.IGNORECASE)
_ID_ATTR = re.compile(r"\bid\s*=", re.IGNORECASE)
_PRE_BLOCK = re.compile(r"(<pre\b[^>]*>)(.*?)(</pre>)", re.IGNORECASE | re.DOTALL)
_PRE_TOKEN = re.compile(r"(<[^>]+>|&[#A-Za-z0-9]{1,10};|\s|.)", re.DOTALL)


@dataclass(frozen=True)
class NativePdfResult:
    pdf: bytes
    pages: int
    warnings: Tuple[str, ...]


@dataclass(frozen=True)
class PageGeometry:
    """Media box and text column of every page, in points."""

    mediabox: Tuple[float, float, float, float]
    column: Tuple[float, float, float, float]

    @property
    def column_width(self) -> float:
        return self.column[2] - self.column[0]

    @property
    def column_height(self) -> float:
        return self.column[3] - self.column[1]


def _paper_mediabox(page_size: str) -> Result[Tuple[float, float, float, float]]:
    paper = pdfkit.paper_rect(page_size)
    if paper is None:
        return err(
            ErrorCode.EXPORT_FAILED,
            f"unknown PDF page size {page_size!r}; use a pymupdf paper name such as "
            "A4, A5, Letter or Legal (append -l for landscape)",
            ErrorScope.USER,
            context={"page_size": page_size},
        )
    return Ok(pdfkit.rect_tuple(paper))


def geometry_from(
    mediabox: Tuple[float, float, float, float], margins_pt: Sequence[float], label: str
) -> Result[PageGeometry]:
    """``mediabox`` minus ``top right bottom left`` margins (pt); USER error when the
    text column would be narrower or shorter than :data:`MIN_COLUMN_PT`."""
    if len(margins_pt) != 4:
        return err(ErrorCode.EXPORT_FAILED, "margins need four values", ErrorScope.USER)
    x0, y0, x1, y1 = mediabox
    top, right, bottom, left = (float(v) for v in margins_pt)
    column = (x0 + left, y0 + top, x1 - right, y1 - bottom)
    if column[2] - column[0] < MIN_COLUMN_PT or column[3] - column[1] < MIN_COLUMN_PT:
        return err(
            ErrorCode.EXPORT_FAILED,
            f"margins leave no room for text on a {label} page",
            ErrorScope.USER,
            context={"page_size": label, "margins_pt": ",".join(f"{v:g}" for v in margins_pt)},
        )
    return Ok(PageGeometry(mediabox=(x0, y0, x1, y1), column=column))


def page_geometry(page_size: str, margins_mm: Sequence[float]) -> Result[PageGeometry]:
    """``paper_rect(page_size)`` minus ``top right bottom left`` margins (mm)."""
    mediabox = _paper_mediabox(page_size)
    if isinstance(mediabox, Err):
        return mediabox
    return geometry_from(mediabox.value, [float(v) * MM_TO_PT for v in margins_mm], page_size)


@dataclass(frozen=True)
class PageDesign:
    """Resolved page rectangle, margins and body size, plus where they came from."""

    geometry: PageGeometry
    body_pt: float
    margins_pt: Tuple[float, float, float, float]
    assumed: Tuple[str, ...]
    """Which values had to be assumed (``"page_size"``, ``"margins"``, ``"body_font"``);
    empty when every value came from the user or the source."""
    invalid: Tuple[str, ...] = ()
    """Source-derived fields that were out of range and replaced by the fallback."""

    @property
    def warnings(self) -> List[str]:
        """``pdf_defaults_assumed`` (once) and one ``pdf_page_design_invalid:<field>`` each."""
        found = [DEFAULTS_ASSUMED_WARNING] if self.assumed else []
        found.extend(f"{PAGE_DESIGN_INVALID_WARNING}:{name}" for name in self.invalid)
        return found

    @property
    def label(self) -> str:
        x0, y0, x1, y1 = self.geometry.mediabox
        return f"{x1 - x0:g}x{y1 - y0:g}pt"


def uses_source_page(options: ExportOptions) -> bool:
    """``True`` when ``page_size`` asks for the source page rectangle."""
    name = options.page_size.strip().lower()
    return name == SOURCE_PAGE_SIZE or not name


def _finite(values: Sequence[float]) -> bool:
    return all(math.isfinite(float(v)) for v in values)


def _valid_page(rect: Sequence[float]) -> bool:
    return (
        len(rect) == 2
        and _finite(rect)
        and all(MIN_PAGE_PT <= float(v) <= MAX_PAGE_PT for v in rect)
    )


def _valid_margins(
    margins: Sequence[float], mediabox: Tuple[float, float, float, float]
) -> bool:
    """Every margin >= 0 and smaller than half the page in its direction."""
    if len(margins) != 4 or not _finite(margins):
        return False
    top, right, bottom, left = (float(v) for v in margins)
    half_w = (mediabox[2] - mediabox[0]) / 2.0
    half_h = (mediabox[3] - mediabox[1]) / 2.0
    return 0 <= top < half_h and 0 <= bottom < half_h and 0 <= left < half_w and 0 <= right < half_w


def resolve_page_design(options: ExportOptions) -> Result[PageDesign]:
    """Page rectangle / margins / body size in this precedence: explicit user choice
    (``page_size`` other than ``"source"``, ``margins_mm``), then the source-derived
    ``page_rect_pt`` / ``margins_pt`` / ``body_font_pt``, and only as a last resort
    Letter / 10 % margins / 10.5 pt, recorded in ``assumed``.

    A wrong *user* value is a USER error (unknown paper name, margins that leave no room).
    Source-derived values come from a file the user may edit (``source_profile.json``):
    they are range-checked (page 72-14400 pt, margins >= 0 and below half the page, body
    4-72 pt) and an out-of-range value falls back like a missing one, listed in
    ``invalid`` (CR-65)."""
    assumed: List[str] = []
    invalid: List[str] = []
    label = options.page_size
    if uses_source_page(options):
        if options.page_rect_pt is not None and not _valid_page(options.page_rect_pt):
            invalid.append("page_size")
        if options.page_rect_pt is not None and _valid_page(options.page_rect_pt):
            width, height = options.page_rect_pt
            mediabox: Tuple[float, float, float, float] = (0.0, 0.0, float(width), float(height))
            label = f"{width:g}x{height:g}pt"
        else:
            fallback = _paper_mediabox(FALLBACK_PAGE_SIZE)
            if isinstance(fallback, Err):
                return fallback
            mediabox = fallback.value
            label = FALLBACK_PAGE_SIZE
            assumed.append("page_size")
    else:
        paper = _paper_mediabox(options.page_size)
        if isinstance(paper, Err):
            return paper
        mediabox = paper.value
    page_w, page_h = mediabox[2] - mediabox[0], mediabox[3] - mediabox[1]
    proportional = (
        page_h * FALLBACK_MARGIN_RATIO,
        page_w * FALLBACK_MARGIN_RATIO,
        page_h * FALLBACK_MARGIN_RATIO,
        page_w * FALLBACK_MARGIN_RATIO,
    )
    resolved: Optional[PageGeometry] = None
    margins: Tuple[float, ...] = proportional
    if options.margins_pt is not None:  # measured on the source (or edited in the profile)
        if _valid_margins(options.margins_pt, mediabox):
            measured = tuple(float(v) for v in options.margins_pt)
            attempt = geometry_from(mediabox, measured, label)
            if isinstance(attempt, Ok):
                resolved, margins = attempt.value, measured
        if resolved is None:
            invalid.append("margins")
    elif options.margins_mm is not None:  # the user's explicit --pdf-margin
        margins = tuple(float(v) * MM_TO_PT for v in options.margins_mm)
        explicit = geometry_from(mediabox, margins, label)
        if isinstance(explicit, Err):
            return explicit
        resolved = explicit.value
    if resolved is None:
        assumed.append("margins")
        fallback_geometry = geometry_from(mediabox, proportional, label)
        if isinstance(fallback_geometry, Err):
            return fallback_geometry
        resolved = fallback_geometry.value
    body_pt = FALLBACK_BODY_PT
    if options.body_font_pt is None or options.body_font_pt <= 0:
        assumed.append("body_font")
    elif not (
        math.isfinite(options.body_font_pt) and MIN_BODY_PT <= options.body_font_pt <= MAX_BODY_PT
    ):
        invalid.append("body_font")
        assumed.append("body_font")
    else:
        body_pt = float(options.body_font_pt)
    return Ok(PageDesign(
        geometry=resolved,
        body_pt=body_pt,
        margins_pt=(margins[0], margins[1], margins[2], margins[3]),
        assumed=tuple(assumed),
        invalid=tuple(invalid),
    ))


def body_css(body_pt: float) -> str:
    """Body font size rule; headings scale from it through ``book.css`` (em units)."""
    return f"body {{ font-size: {body_pt:g}pt; }}\n"


def scan_unsupported_css(css: str) -> List[str]:
    """Property names and at-rules outside the MuPDF subset, in first-seen order (FR-52)."""
    text = _CSS_STRING.sub('""', _CSS_COMMENT.sub("", css))
    found: List[str] = []

    def add(name: str) -> None:
        if name not in found:
            found.append(name)

    for match in _CSS_AT_RULE.finditer(text):
        name = match.group(1).lower()
        if name not in CSS_ALLOWED_AT_RULES:
            add(f"@{name}")
    for block in _CSS_BLOCK.finditer(text):
        for declaration in block.group(1).split(";"):
            prop = _CSS_PROPERTY.match(declaration)
            if prop is None:
                continue
            name = prop.group(1).lower()
            if name in CSS_ALLOWED or name.startswith(CSS_ALLOWED_PREFIXES):
                continue
            add(name)
    return found


def css_warning(css: str) -> Optional[str]:
    """The single ``css_unsupported:prop,prop`` warning for user CSS, or ``None``."""
    names = scan_unsupported_css(css)
    return f"css_unsupported:{','.join(names)}" if names else None


def _image_size_pt(path: Path) -> Optional[Tuple[float, float]]:
    try:
        pixmap = pdfkit.pixmap_from_file(str(path))
        width_px, height_px = pdfkit.pixmap_size(pixmap)
        xres, yres = pdfkit.pixmap_resolution(pixmap)
    except Exception:  # noqa: BLE001 - unreadable image: keep the tag unsized
        return None
    if width_px <= 0 or height_px <= 0:
        return None
    dpi_x = xres if xres > 0 else DEFAULT_IMAGE_DPI
    dpi_y = yres if yres > 0 else dpi_x
    return width_px * 72.0 / dpi_x, height_px * 72.0 / dpi_y


def fit_image(
    natural: Tuple[float, float], max_width: float, max_height: float
) -> Tuple[float, float]:
    """Scale ``natural`` (pt) down to fit ``max_width`` x ``max_height``, aspect kept (E-54)."""
    width, height = natural
    scale = min(1.0, max_width / width, max_height / height)
    return round(width * scale, 2), round(height * scale, 2)


def size_images(html: str, base_dir: Path, geometry: PageGeometry) -> Tuple[str, List[str]]:
    """Give every ``<img>`` explicit ``width``/``height`` (pt) within the column and
    80 % of the column height; unreadable files get ``image_unsized:src``."""
    warnings: List[str] = []
    max_width = geometry.column_width
    max_height = geometry.column_height * MAX_IMAGE_HEIGHT_RATIO

    def replace(match: "re.Match[str]") -> str:
        attrs = {key.lower(): value for key, value in _ATTR.findall(match.group(1))}
        src = html_lib.unescape(attrs.get("src", ""))
        path = resolve_figure(src, base_dir)
        natural = _image_size_pt(path) if path is not None else None
        if natural is None:
            if src:
                warnings.append(f"image_unsized:{src}")
            return match.group(0)
        width, height = fit_image(natural, max_width, max_height)
        kept = " ".join(
            f'{key}="{html_lib.escape(value, quote=True)}"'
            for key, value in attrs.items()
            if key not in {"width", "height"}
        )
        return f'<img {kept} width="{width:g}" height="{height:g}"/>'

    return _IMG.sub(replace, html), warnings


def code_wrap_chars(column_width: float, body_pt: float) -> int:
    """Characters of the code font (``0.85 x body_pt``) that fit the column, capped at
    :data:`CODE_WRAP_MAX_CHARS`."""
    per_line = int(column_width / (body_pt * CODE_FONT_RATIO * CODE_CHAR_ADVANCE))
    return max(8, min(CODE_WRAP_MAX_CHARS, per_line))


def wrap_code_runs(html: str, max_chars: int) -> Tuple[str, int]:
    """Insert a line break into every whitespace-free run longer than ``max_chars``
    inside ``<pre>`` blocks; tags and entities count as one character and are never
    split. Returns the HTML and the number of breaks inserted."""
    breaks = 0

    def wrap_block(match: "re.Match[str]") -> str:
        nonlocal breaks
        out: List[str] = []
        run = 0
        for token in _PRE_TOKEN.findall(match.group(2)):
            if token.startswith("<"):
                out.append(token)
                continue
            if token.isspace():
                run = 0
                out.append(token)
                continue
            if run >= max_chars:
                out.append("\n")
                breaks += 1
                run = 0
            out.append(token)
            run += 1
        return f"{match.group(1)}{''.join(out)}{match.group(3)}"

    return _PRE_BLOCK.sub(wrap_block, html), breaks


def tag_tables(html: str) -> str:
    """``<table>`` without ``id`` -> ``id="bt-table-N"`` so the layout reports its box."""
    counter = 0

    def replace(match: "re.Match[str]") -> str:
        nonlocal counter
        counter += 1
        attrs = match.group(1)
        if _ID_ATTR.search(attrs):
            return match.group(0)
        return f'<table id="{_TABLE_ID_PREFIX}{counter}"{attrs}>'

    return _TABLE_OPEN.sub(replace, html)


def build_toc(
    headings: Sequence[Tuple[int, str, int]], split_level: int
) -> List[List[Any]]:
    """``(html_level, title, page_1based)`` -> ``set_toc`` rows with outline levels 1/2.

    Headings at or above ``split_level`` are chapters (level 1), the next level
    becomes level 2; deeper headings are left out. A level-2 row before any
    chapter is promoted so the hierarchy stays valid for ``set_toc``.
    """
    rows: List[List[Any]] = []
    for level, title, page in headings:
        clean = " ".join(title.split())
        if not clean:
            continue
        if level <= split_level:
            outline_level = 1
        elif level == split_level + 1:
            outline_level = 2
        else:
            continue
        if outline_level == 2 and not rows:
            outline_level = 1
        rows.append([outline_level, clean, page])
    return rows


class _PositionRecorder:
    """Collects ``ElementPosition`` objects per page for links, outline and table checks."""

    def __init__(self, column_width: float) -> None:
        self.positions: List[Any] = []
        self.headings: List[Tuple[int, str, int]] = []
        self.table_overflow: List[str] = []
        self._column_width = column_width

    def __call__(self, position: Any) -> None:
        self.positions.append(position)
        if not (int(getattr(position, "open_close", 0)) & 1):
            return
        page = int(getattr(position, "page_num", 1))
        heading = int(getattr(position, "heading", 0) or 0)
        text = getattr(position, "text", None)
        if heading > 0 and text:
            self.headings.append((heading, str(text), page))
        element_id = getattr(position, "id", None)
        if isinstance(element_id, str) and element_id.startswith(_TABLE_ID_PREFIX):
            x0, _y0, x1, _y1 = pdfkit.rect_tuple(position.rect)
            if x1 - x0 > self._column_width + 0.5:
                number = element_id[len(_TABLE_ID_PREFIX):]
                if number not in self.table_overflow:
                    self.table_overflow.append(number)


def _render_failed(stage: str, exc: BaseException) -> Err:
    return err(
        ErrorCode.EXPORT_FAILED,
        f"native PDF rendering failed while {stage}: {exc.__class__.__name__}",
        ErrorScope.JOB_FATAL,
        cause=repr(exc)[:300],
    )


def _add_footers(doc: Any, geometry: PageGeometry, bottom_pt: float) -> bool:
    """Centred page number on every page after the first; ``False`` when one did not fit."""
    box_height = FOOTER_FONT_SIZE * 2.4  # insert_textbox needs ~1.7 x size; keep slack
    all_fit = True
    for index in range(1, pdfkit.doc_page_count(doc)):
        page = pdfkit.doc_page(doc, index)
        _width, y1 = pdfkit.page_size(page)  # generated pages: origin (0, 0), no rotation
        top = y1 - bottom_pt * 0.6 if bottom_pt >= box_height * 1.5 else y1 - box_height
        top = max(top, geometry.column[3])
        clip = pdfkit.rect(geometry.column[0], top, geometry.column[2], top + box_height)
        unused = pdfkit.page_insert_centered_text(
            page, clip, str(index + 1), FOOTER_FONT_SIZE, FOOTER_FONT
        )
        if unused < 0:
            all_fit = False
    return all_fit


def _layout(
    story_html: str,
    full_css: str,
    directories: Sequence[str],
    geometry: PageGeometry,
    recorder: _PositionRecorder,
    page_size: str,
) -> Result[Tuple[bytes, int]]:
    """Run the ``place``/``draw`` loop; returns the raw writer output and the page count."""
    buffer = io.BytesIO()
    pages = 0

    def record(position: Any) -> None:  # pymupdf inspects ``__code__``: a plain function
        recorder(position)

    try:
        archive = pdfkit.new_archive(*directories)
        story = pdfkit.new_story(story_html, full_css, archive)
        mediabox = pdfkit.rect(*geometry.mediabox)
        where = pdfkit.rect(*geometry.column)
        writer = pdfkit.new_document_writer(buffer)
        more = True
        empty_pages = 0
        while more:
            if pages >= MAX_PAGES:
                return err(
                    ErrorCode.EXPORT_FAILED,
                    f"native PDF exceeded {MAX_PAGES} pages",
                    ErrorScope.JOB_FATAL,
                )
            device = pdfkit.writer_begin_page(writer, mediabox)
            more, filled = pdfkit.story_place(story, where)
            pdfkit.story_element_positions(story, record, {"page_num": pages + 1})
            pdfkit.story_draw(story, device)
            pdfkit.writer_end_page(writer)
            pages += 1
            fx0, fy0, fx1, fy1 = pdfkit.rect_tuple(filled)
            empty_pages = empty_pages + 1 if (fx1 <= fx0 or fy1 <= fy0) else 0
            if more and empty_pages >= _EMPTY_PAGE_LIMIT:
                return err(
                    ErrorCode.EXPORT_FAILED,
                    "native PDF layout made no progress: an element does not fit the "
                    "text column (reduce the margins or choose a larger page size)",
                    ErrorScope.USER,
                    context={"page_size": page_size},
                )
        pdfkit.writer_close(writer)
    except Exception as exc:  # noqa: BLE001 - pymupdf raises its own hierarchy
        return _render_failed("laying out pages", exc)
    return Ok((buffer.getvalue(), pages))


def render_reflowed_pdf(
    html_body: str,
    css: str,
    options: ExportOptions,
    font: FontSpec,
    outline: Sequence[Tuple[int, str]],
    base_dir: Path,
    metadata: Mapping[str, str],
) -> Result[NativePdfResult]:
    """HTML fragment + stylesheet -> finished PDF bytes (design doc 06, 3.5).

    ``outline`` (``(level, title)`` from the Markdown) is the fallback when the
    layout reports no headings; ``metadata`` keys ``title`` and ``author`` are
    written, the language is ``options.language`` and the producer is
    ``book-translator <version>``. Page rectangle, margins and body size follow
    :func:`resolve_page_design` (source-derived unless the user chose otherwise;
    ``pdf_defaults_assumed`` when neither is known).
    """
    design_result = resolve_page_design(options)
    if isinstance(design_result, Err):
        return design_result
    design = design_result.value
    geometry = design.geometry
    warnings: List[str] = list(design.warnings)

    sized_html, image_warnings = size_images(html_body, base_dir, geometry)
    warnings.extend(image_warnings)
    wrapped_html, code_breaks = wrap_code_runs(
        sized_html, code_wrap_chars(geometry.column_width, design.body_pt)
    )
    if code_breaks:
        warnings.append(f"code_lines_wrapped:{code_breaks}")
    story_html = f"<body>{tag_tables(wrapped_html)}</body>"
    full_css = (
        f"{css}\n{PAGE_CSS}{body_css(design.body_pt)}{font_css(font, options.body_family)}"
    )
    directories = [str(base_dir)]
    if font.archive_dir is not None:
        directories.append(str(font.archive_dir))

    recorder = _PositionRecorder(geometry.column_width)
    laid_out = _layout(story_html, full_css, directories, geometry, recorder, design.label)
    if isinstance(laid_out, Err):
        return laid_out
    raw, pages = laid_out.value

    try:
        doc = pdfkit.open_document_bytes(raw)
    except Exception as exc:  # noqa: BLE001
        return _render_failed("re-opening the layout output", exc)
    try:
        try:
            pdfkit.story_add_pdf_links(doc, recorder.positions)
        except Exception:  # noqa: BLE001 - best effort (design doc 06, 3.5)
            warnings.append("pdf_links_skipped")
        if not _add_footers(doc, geometry, design.margins_pt[2]):
            warnings.append("footer_skipped")
        headings = recorder.headings or [(level, title, 1) for level, title in outline]
        toc = build_toc(headings, options.chapter_split_level)
        if toc:
            pdfkit.doc_set_toc(doc, toc)
        for number in recorder.table_overflow:
            warnings.append(f"table_overflow:{number}")
        info: Dict[str, str] = {
            "title": metadata.get("title", ""),
            "author": metadata.get("author", ""),
            "creator": "book-translator",
            "producer": f"book-translator {__version__}",
        }
        pdfkit.doc_set_metadata(doc, info)
        pdfkit.doc_set_language(doc, options.language)
        pdfkit.doc_subset_fonts(doc)
        data = pdfkit.doc_to_bytes(doc, garbage=4, deflate=True)
    except Exception as exc:  # noqa: BLE001
        return _render_failed("finishing the document", exc)
    finally:
        pdfkit.close_document(doc)
    return Ok(NativePdfResult(pdf=data, pages=pages, warnings=tuple(warnings)))
