"""Extractor interface and registry (design doc 02, section 2.2)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ClassVar, Dict, FrozenSet, List, Optional, Type

from ..config import Settings, parse_page_ranges
from ..domain.models import ExtractedDocument, FigureRecord
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err

FIGURE_DPI_MIN = 72
FIGURE_DPI_MAX = 600


@dataclass(frozen=True)
class FigureOptions:
    """Figure detection / rendering settings (design doc 06, §3.2; defaults of D12/D15)."""

    enabled: bool = True
    dpi: int = 200  # 72..600
    dpi_floor: int = 96
    max_bytes: int = 8 * 1024 * 1024
    min_area_ratio: float = 0.02  # region area / page area
    min_paths: int = 5
    merge_gap_pt: float = 12.0
    border_pt: float = 4.0
    inner_text_overlap: float = 0.80  # FR-33
    label_attach_lines: float = 1.5  # x line height, §4.1 step 7
    legend: bool = True
    legend_limit: int = 40
    exclude_pages: FrozenSet[int] = frozenset()
    images_dir_name: str = "images"


def figure_options_from_settings(settings: Settings) -> Result[FigureOptions]:
    """Build ``FigureOptions`` from the validated settings (env / CLI overrides applied)."""
    pages = parse_page_ranges(settings.figure_exclude_pages)
    if isinstance(pages, Err):
        return pages
    return Ok(
        FigureOptions(
            enabled=settings.figures,
            dpi=settings.figure_dpi,
            max_bytes=int(settings.figure_max_mb * 1024 * 1024),
            min_area_ratio=settings.figure_min_area_pct / 100.0,
            min_paths=settings.figure_min_paths,
            merge_gap_pt=settings.figure_merge_gap_pt,
            border_pt=settings.figure_border_pt,
            legend=True,  # Addendum A: the legend block is the translation vehicle
            legend_limit=settings.figure_legend_limit,
            exclude_pages=pages.value,
        )
    )


@dataclass(frozen=True)
class ExtractOptions:
    header_footer_page_ratio: float = 0.30  # repeated on >= 30% of pages (min 3)
    band_ratio: float = 0.12  # top/bottom 12% of page height is the candidate band
    min_chars_per_page: int = 50  # median below -> NO_TEXT_LAYER
    heading_size_ratio: float = 1.15  # >= body_size * ratio -> heading candidate
    footnote_size_ratio: float = 0.85
    keep_images_as_placeholders: bool = True
    figures: FigureOptions = FigureOptions()  # C6 (v1.1)


ProgressCallback = Callable[[int, int], None]  # (done, total) pages

FigureSink = Callable[[FigureRecord, bytes], Result[Path]]
"""Orchestrator-owned writer: stores the PNG under ``<output>/images/<file>`` atomically and
returns the path written; an ``Err`` (``FIGURE_SINK_FAILED``) aborts the extraction."""


class BaseExtractor(ABC):
    name: ClassVar[str]  # "pymupdf" | "marker"
    supports_ocr: ClassVar[bool]
    supports_footnotes: ClassVar[bool]

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Import probe; never raises."""

    @abstractmethod
    def extract(
        self,
        pdf_path: Path,
        options: ExtractOptions,
        progress: Optional[ProgressCallback] = None,
        figure_sink: Optional[FigureSink] = None,
    ) -> Result[ExtractedDocument]: ...


def validate_pdf_path(pdf_path: Path) -> Result[None]:
    """Cheap input checks shared by all extractors (6.1 step 1, before opening)."""
    try:
        if not pdf_path.exists() or not pdf_path.is_file():
            return err(
                ErrorCode.INPUT_NOT_FOUND, f"input file not found: {pdf_path}", ErrorScope.USER
            )
        if pdf_path.suffix.lower() != ".pdf":
            return err(
                ErrorCode.INPUT_NOT_PDF, f"input is not a .pdf file: {pdf_path}", ErrorScope.USER
            )
        with pdf_path.open("rb") as fh:
            head = fh.read(1024)
    except OSError as exc:
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"input cannot be read: {pdf_path}",
            ErrorScope.USER,
            cause=repr(exc),
        )
    if b"%PDF" not in head:
        return err(
            ErrorCode.INPUT_NOT_PDF, f"input has no %PDF signature: {pdf_path}", ErrorScope.USER
        )
    return Ok(None)


def _registry() -> Dict[str, Type[BaseExtractor]]:
    # Imported lazily to avoid an import cycle (the extractors import this module).
    from .marker_extractor import MarkerExtractor
    from .pymupdf_extractor import PyMuPDFExtractor

    return {
        PyMuPDFExtractor.name: PyMuPDFExtractor,
        MarkerExtractor.name: MarkerExtractor,
    }


def list_extractors() -> List[str]:
    """Registered extractor names in registry order."""
    return list(_registry().keys())


def get_extractor(name: str) -> Result[BaseExtractor]:
    """Resolve an extractor by name.

    Unknown name -> ``Err(PROVIDER_CONFIG, USER)``; a known extractor whose optional
    dependency is missing -> ``Err(EXTRACTOR_UNAVAILABLE, USER)``.
    """
    registry = _registry()
    key = name.strip().lower()
    cls = registry.get(key)
    if cls is None:
        return err(
            ErrorCode.PROVIDER_CONFIG,
            f"unknown extractor '{name}' (available: {', '.join(registry)})",
            ErrorScope.USER,
        )
    if not cls.is_available():
        return err(
            ErrorCode.EXTRACTOR_UNAVAILABLE,
            f"extractor '{key}' is not available; install the [{key}] extra",
            ErrorScope.USER,
            context={"extractor": key},
        )
    return Ok(cls())
