"""Opt-in extractor: marker-pdf -> normalized BT-Markdown (design doc 02, 6.2).

``marker-pdf`` is an optional extra; every access goes through ``importlib`` so
this module imports cleanly when marker is absent. Fallback to PyMuPDF is the
orchestrator's job: any marker failure is ``Err(EXTRACTION_FAILED, JOB_FATAL)``.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import Any, ClassVar, List, Literal, Optional

from ..domain.bt_syntax import is_image_line
from ..domain.models import ExtractedDocument, PageInfo
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from ..pdfkit.api import close_document, open_document, page_image_count, page_plain_text
from .base import (
    BaseExtractor,
    ExtractOptions,
    FigureSink,
    ProgressCallback,
    validate_pdf_path,
)
from .langdetect import detect_language
from .normalizer import normalize_markdown

log = logging.getLogger("book_translator.extractors.marker")

_MARKER_MODULES = ("marker.converters.pdf", "marker.models", "marker.output")
FIGURES_UNSUPPORTED_WARNING = "figures_unsupported:marker"


def _find_spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


class MarkerExtractor(BaseExtractor):
    name: ClassVar[str] = "marker"
    supports_ocr: ClassVar[bool] = True
    supports_footnotes: ClassVar[bool] = False

    @classmethod
    def is_available(cls) -> bool:
        """True when the ``marker`` package and the modules we use are importable."""
        return _find_spec("marker") and all(_find_spec(m) for m in _MARKER_MODULES)

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
        if options.figures.enabled:
            # D16: marker keeps the v1 bare tokens; vector-figure detection is PyMuPDF-only.
            log.warning(
                "figure preservation is not supported by the marker extractor; "
                "bare image tokens are emitted",
                extra={"event": "figures_unsupported"},
            )
        if not self.is_available():
            return err(
                ErrorCode.EXTRACTOR_UNAVAILABLE,
                "marker-pdf is not installed; install the [marker] extra",
                ErrorScope.USER,
            )
        try:
            raw_markdown = _run_marker(pdf_path)
        except Exception as exc:  # noqa: BLE001 - boundary: never raise across layers
            return err(
                ErrorCode.EXTRACTION_FAILED,
                f"marker extraction failed on {pdf_path.name}: {type(exc).__name__}",
                ErrorScope.JOB_FATAL,
                cause=repr(exc),
            )
        markdown, warnings = normalize_markdown(raw_markdown)
        if options.figures.enabled:
            warnings.append(FIGURES_UNSUPPORTED_WARNING)
        pages = _page_infos(pdf_path, warnings)
        if progress is not None:
            progress(len(pages), len(pages))
        language, confidence = detect_language(markdown)
        image_count = sum(1 for line in markdown.split("\n") if is_image_line(line))
        footnote_mode: Literal["markdown", "notes_section", "none"] = (
            "markdown" if "[^" in markdown else "none"
        )
        return Ok(
            ExtractedDocument(
                markdown=markdown,
                extractor_name=self.name,
                fallback_used=False,
                pages=pages,
                detected_language=language,
                language_confidence=confidence,
                removed_headers_footers=[],
                footnote_mode=footnote_mode,
                image_count=image_count,
                warnings=warnings,
            )
        )


def _run_marker(pdf_path: Path) -> str:
    """Run marker's ``PdfConverter`` and return its Markdown text (may raise)."""
    converters: Any = importlib.import_module("marker.converters.pdf")
    models: Any = importlib.import_module("marker.models")
    output: Any = importlib.import_module("marker.output")
    converter = converters.PdfConverter(artifact_dict=models.create_model_dict())
    rendered = converter(str(pdf_path))
    text, _, _ = output.text_from_rendered(rendered)
    return str(text)


def _page_infos(pdf_path: Path, warnings: List[str]) -> List[PageInfo]:
    """Per-page character counts via PyMuPDF (marker exposes no page geometry)."""
    try:
        doc = open_document(str(pdf_path))
        try:
            infos: List[PageInfo] = []
            for index in range(int(doc.page_count)):
                page = doc[index]
                text = page_plain_text(page)
                images = page_image_count(page)
                chars = len("".join(text.split()))
                infos.append(PageInfo(index + 1, chars, chars == 0, images))
            return infos
        finally:
            close_document(doc)
    except Exception as exc:  # noqa: BLE001 - metadata only, never fatal
        warnings.append(f"page statistics unavailable ({type(exc).__name__})")
        return []
