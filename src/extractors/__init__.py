"""book_translator.extractors: PDF -> ``ExtractedDocument`` (design doc 02, sections 2.2 and 6)."""

from __future__ import annotations

from .base import (
    BaseExtractor,
    ExtractOptions,
    FigureOptions,
    FigureSink,
    ProgressCallback,
    figure_options_from_settings,
    get_extractor,
    list_extractors,
    validate_pdf_path,
)
from .langdetect import check_english, detect_language
from .marker_extractor import MarkerExtractor
from .normalizer import ensure_bt_markdown, normalize_markdown
from .pymupdf_extractor import PyMuPDFExtractor

__all__ = [
    "BaseExtractor",
    "ExtractOptions",
    "FigureOptions",
    "FigureSink",
    "MarkerExtractor",
    "ProgressCallback",
    "PyMuPDFExtractor",
    "check_english",
    "detect_language",
    "ensure_bt_markdown",
    "figure_options_from_settings",
    "get_extractor",
    "list_extractors",
    "normalize_markdown",
    "validate_pdf_path",
]
