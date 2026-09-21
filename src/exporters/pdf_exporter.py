"""PDF exporter facade: native engine (default) or weasyprint (design doc 06, 3.5, D10).

``options.pdf_engine == "native"`` renders through :mod:`.pdf_native` (pymupdf
``Story``; always available, FR-50/51). ``"weasyprint"`` keeps the v1 path: it
needs the ``[pdf]`` extra with its native Pango/Cairo libraries and, when it is
not importable, answers ``EXPORTER_UNAVAILABLE`` (optional exporter semantics:
partial export, exit 2). Fonts for weasyprint are not bundled (v1 deviation
from doc 02 8.4): ``options.font_path`` is declared via ``@font-face`` when
given, otherwise the CSS font stack ``"DejaVu Serif", "Noto Serif", serif``.
"""

from __future__ import annotations

import dataclasses
import html
import importlib
import importlib.util
import re
from pathlib import Path
from typing import ClassVar, Dict, List, Optional, Tuple

from ..domain.models import AssembledDocument
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from ..pdfkit import api as pdfkit
from .base import (
    BaseExporter,
    ExportArtifact,
    ExportOptions,
    atomic_write_bytes,
    load_stylesheet,
    markdown_to_html,
    register,
)
from .fonts import resolve_font
from .pdf_native import (
    DEFAULTS_ASSUMED_WARNING,
    FALLBACK_BODY_PT,
    FALLBACK_PAGE_SIZE,
    css_warning,
    render_reflowed_pdf,
    resolve_page_design,
    uses_source_page,
)

NATIVE_ENGINE = "native"
WEASYPRINT_ENGINE = "weasyprint"
PDF_ENGINES: Tuple[str, ...] = (NATIVE_ENGINE, WEASYPRINT_ENGINE)

PAGE_CSS = """
@page {{
  size: {size};
  margin: {margin};
  @bottom-center {{
    content: counter(page);
    font-size: 9pt;
    color: #555;
  }}
}}
body {{ font-size: {body}pt; }}
"""
"""weasyprint page rule template; filled by :func:`_weasyprint_page_css` from the
resolved page design (source-derived unless the user chose otherwise)."""

_ATX = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_INLINE_MARKUP = re.compile(r"[*_`]+")


def _font_css(font_path: Optional[Path], body_family: Optional[str] = None) -> str:
    if font_path is None:
        if (body_family or "").lower() == "sans":  # sans-serif source (source_profile.json)
            return 'html { font-family: "DejaVu Sans", "Noto Sans", sans-serif; }\n'
        return 'html { font-family: "DejaVu Serif", "Noto Serif", serif; }\n'
    url = font_path.resolve().as_uri()
    return (
        f'@font-face {{ font-family: "BookFont"; src: url("{url}"); }}\n'
        'html, h1, h2, h3, h4, h5, h6, pre, code { font-family: "BookFont", serif; }\n'
    )


def _weasyprint_page_css(options: ExportOptions) -> Tuple[str, List[str]]:
    """``@page`` + body size for weasyprint from the same design rules as the native
    engine; ``(css, warnings)`` with ``pdf_defaults_assumed`` when values were assumed.

    ``PdfExporter.export`` validates the page design first (a bad ``--pdf-page-size`` /
    ``--pdf-margin`` is a USER error, never a silent default); the ``Err`` branch below
    only serves direct callers of :func:`build_html_document`."""
    design = resolve_page_design(options)
    if isinstance(design, Err):
        css = PAGE_CSS.format(
            size=FALLBACK_PAGE_SIZE, margin="10%", body=f"{FALLBACK_BODY_PT:g}",
        )
        return css, [DEFAULTS_ASSUMED_WARNING]
    value = design.value
    x0, y0, x1, y1 = value.geometry.mediabox
    size = f"{x1 - x0:g}pt {y1 - y0:g}pt" if uses_source_page(options) else options.page_size
    top, right, bottom, left = value.margins_pt
    css = PAGE_CSS.format(
        size=size, margin=f"{top:g}pt {right:g}pt {bottom:g}pt {left:g}pt",
        body=f"{value.body_pt:g}",
    )
    return css, value.warnings


def build_html_document(
    document: AssembledDocument, options: ExportOptions, *, base_dir: Optional[Path] = None
) -> Tuple[str, List[str]]:
    """Full standalone HTML page used as weasyprint input, plus ``figure_missing:N`` warnings.

    ``<img src="images/...">`` is resolved by weasyprint through ``base_url`` (the
    target directory), so the same relative path as in the Markdown is emitted.
    """
    title = options.title or document.metadata.get("title") or "translated_book"
    page_css, warnings = _weasyprint_page_css(options)
    css = (
        load_stylesheet(options.css_path)
        + "\n"
        + page_css
        + _font_css(options.font_path, options.body_family)
    )
    render = markdown_to_html(document.markdown, base_dir=base_dir)
    page = (
        f'<!DOCTYPE html>\n<html lang="{html.escape(options.language)}">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        f"<title>{html.escape(title)}</title>\n<style>\n{css}\n</style>\n</head>\n"
        f"<body>\n{render.html}\n</body>\n</html>\n"
    )
    return page, warnings + list(render.warnings)


def markdown_outline(markdown_text: str) -> List[Tuple[int, str]]:
    """``(level, title)`` of every ATX heading outside code fences (outline fallback)."""
    outline: List[Tuple[int, str]] = []
    in_fence = False
    for line in markdown_text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _ATX.match(line)
        if match:
            outline.append((len(match.group(1)), _INLINE_MARKUP.sub("", match.group(2)).strip()))
    return outline


def weasyprint_available() -> bool:
    if importlib.util.find_spec("weasyprint") is None:
        return False
    try:
        importlib.import_module("weasyprint")
    except (ImportError, OSError):
        return False
    return True


@register
class PdfExporter(BaseExporter):
    name: ClassVar[str] = "pdf"
    file_suffix: ClassVar[str] = ".pdf"
    optional: ClassVar[bool] = True

    @classmethod
    def is_available(cls) -> bool:
        """Always: the native engine ships with the core install (D10)."""
        return True

    @classmethod
    def engines(cls) -> Dict[str, bool]:
        return {NATIVE_ENGINE: True, WEASYPRINT_ENGINE: weasyprint_available()}

    def export(
        self, document: AssembledDocument, target_dir: Path, options: ExportOptions
    ) -> Result[ExportArtifact]:
        engine = options.pdf_engine.lower()
        if engine in PDF_ENGINES:
            # CR-63: an unknown --pdf-page-size or impossible --pdf-margin is the user's
            # mistake (USER scope -> exit 1), for either engine and before any rendering.
            design = resolve_page_design(options)
            if isinstance(design, Err):
                return design
        if engine == WEASYPRINT_ENGINE:
            rendered = self._render_weasyprint(document, target_dir, options)
        elif engine == NATIVE_ENGINE:
            rendered = self._render_native(document, target_dir, options)
        else:
            return err(
                ErrorCode.EXPORTER_UNAVAILABLE,
                f"unknown PDF engine {options.pdf_engine!r} (available: {', '.join(PDF_ENGINES)})",
                ErrorScope.USER,
                context={"exporter": self.name, "engine": options.pdf_engine},
            )
        if isinstance(rendered, Err):
            return rendered
        data, warnings = rendered.value
        path = self.output_path(target_dir)
        written = atomic_write_bytes(path, data)
        if isinstance(written, Err):
            return written
        smoke = self._smoke_open(path)
        if isinstance(smoke, Err):
            return smoke
        return Ok(
            ExportArtifact(
                path=path, format=self.name, bytes_written=written.value, warnings=warnings
            )
        )

    # ------------------------------------------------------------------ #
    # engines
    # ------------------------------------------------------------------ #

    def _render_native(
        self, document: AssembledDocument, target_dir: Path, options: ExportOptions
    ) -> Result[Tuple[bytes, List[str]]]:
        if not document.markdown.strip():
            return err(
                ErrorCode.EXPORT_FAILED,
                "empty document; nothing to export (run `translate` first)",
                ErrorScope.JOB_FATAL,
            )
        font = resolve_font(options.font_path)
        if isinstance(font, Err):
            return font
        warnings: List[str] = []
        if options.css_path is not None:
            try:
                user_css = options.css_path.read_text(encoding="utf-8")
            except OSError:
                user_css = ""
            unsupported = css_warning(user_css)
            if unsupported:
                warnings.append(unsupported)
        css = load_stylesheet(options.css_path)
        render = markdown_to_html(document.markdown, base_dir=target_dir)
        warnings.extend(render.warnings)
        metadata: Dict[str, str] = {
            "title": options.title or document.metadata.get("title") or "translated_book",
            "author": options.author or document.metadata.get("author") or "",
        }
        result = render_reflowed_pdf(
            render.html,
            css,
            options,
            font.value,
            markdown_outline(document.markdown),
            target_dir,
            metadata,
        )
        if isinstance(result, Err):
            return result
        warnings.extend(result.value.warnings)
        return Ok((result.value.pdf, warnings))

    def _render_weasyprint(
        self, document: AssembledDocument, target_dir: Path, options: ExportOptions
    ) -> Result[Tuple[bytes, List[str]]]:
        if not weasyprint_available():
            return err(
                ErrorCode.EXPORTER_UNAVAILABLE,
                "PDF engine 'weasyprint' is unavailable: install the [pdf] extra and its "
                "native Pango/Cairo libraries, or use --pdf-engine native",
                ErrorScope.USER,
                context={"exporter": self.name, "engine": WEASYPRINT_ENGINE},
            )
        warnings: List[str] = []
        if options.font_path is not None and not options.font_path.is_file():
            warnings.append(f"pdf_font_missing:{options.font_path.name}")
            options = dataclasses.replace(options, font_path=None)
        page, figure_warnings = build_html_document(document, options, base_dir=target_dir)
        warnings.extend(figure_warnings)
        try:
            weasyprint = importlib.import_module("weasyprint")
            data = weasyprint.HTML(string=page, base_url=str(target_dir)).write_pdf()
        except Exception as exc:  # noqa: BLE001 - renderer boundary
            return err(
                ErrorCode.EXPORT_FAILED,
                f"PDF rendering failed: {exc.__class__.__name__}",
                ErrorScope.JOB_FATAL,
                cause=repr(exc),
            )
        if not isinstance(data, (bytes, bytearray)):
            return err(
                ErrorCode.EXPORT_FAILED, "PDF renderer returned no bytes", ErrorScope.JOB_FATAL
            )
        return Ok((bytes(data), warnings))

    @staticmethod
    def _smoke_open(path: Path) -> Result[None]:
        """Re-open the written file and require at least one page."""
        try:
            doc = pdfkit.open_document(str(path))
        except Exception as exc:  # noqa: BLE001
            return err(
                ErrorCode.EXPORT_FAILED,
                f"written PDF cannot be re-opened: {exc.__class__.__name__}",
                ErrorScope.JOB_FATAL,
                cause=repr(exc)[:300],
                context={"path": str(path)},
            )
        try:
            pages = pdfkit.doc_page_count(doc)
        finally:
            pdfkit.close_document(doc)
        if pages <= 0:
            return err(
                ErrorCode.EXPORT_FAILED, "written PDF has no pages", ErrorScope.JOB_FATAL,
                context={"path": str(path)},
            )
        return Ok(None)
