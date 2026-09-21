"""Figure region -> PNG bytes at a DPI with size back-off (design doc 06, §4.2).

The pymupdf page object is passed in as ``Any`` and every pymupdf call goes
through ``pdfkit.api``. ``render_region`` never raises: exceptions become
``Err(EXTRACTION_FAILED)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Tuple

from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err
from ..pdfkit.api import page_pixmap_png, pixmap_png_bytes, pixmap_size, rect
from .base import FigureOptions
from .cleanup import BBox

__all__ = ["RenderedFigure", "dpi_ladder", "figure_file_name", "render_region"]

_BACKOFF_FACTOR = 0.75
SIZE_LIMIT_WARNING = "size_limit_exceeded"


@dataclass(frozen=True)
class RenderedFigure:
    png: bytes
    dpi: int
    width_px: int
    height_px: int
    dpi_reduced: bool
    warnings: Tuple[str, ...] = ()


def figure_file_name(page: int, index_on_page: int) -> str:
    """Deterministic, page-based file name: ``p002-f01.png`` (E-35)."""
    return f"p{page:03d}-f{index_on_page:02d}.png"


def dpi_ladder(dpi: int, floor: int) -> List[int]:
    """DPI values tried in order: ``d <- max(floor, round(d * 0.75))`` until the floor."""
    floor = max(1, min(floor, dpi))
    ladder = [dpi]
    current = dpi
    while current > floor:
        current = max(floor, int(round(current * _BACKOFF_FACTOR)))
        ladder.append(current)
    return ladder


def render_region(page: Any, bbox: BBox, options: FigureOptions) -> Result[RenderedFigure]:
    """Render ``bbox`` of ``page`` to PNG; step the DPI down while the file is too large."""
    clip = rect(*bbox)
    ladder = dpi_ladder(options.dpi, options.dpi_floor)
    try:
        png = b""
        width = height = 0
        used = ladder[0]
        for used in ladder:
            pixmap = page_pixmap_png(page, used, clip=clip)
            png = pixmap_png_bytes(pixmap)
            width, height = pixmap_size(pixmap)
            if len(png) <= options.max_bytes:
                break
    except Exception as exc:  # noqa: BLE001 - boundary: never raise across layers
        return err(
            ErrorCode.EXTRACTION_FAILED,
            f"figure rendering failed on page {getattr(page, 'number', '?')}: "
            f"{type(exc).__name__}",
            ErrorScope.JOB_FATAL,
            cause=repr(exc),
        )
    warnings: Tuple[str, ...] = ()
    if len(png) > options.max_bytes:
        warnings = (SIZE_LIMIT_WARNING,)
    return Ok(
        RenderedFigure(
            png=png,
            dpi=used,
            width_px=width,
            height_px=height,
            dpi_reduced=used != options.dpi,
            warnings=warnings,
        )
    )
