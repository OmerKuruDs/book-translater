"""Translated figure PNG: a page region rendered after its label boxes were replaced
in place (user decision: figure labels translated with the page's own properties).

Works on an in-memory copy of the source document (the source file is never
modified) with the same measure / redact / ``insert_htmlbox`` core as the overlay PDF
(:mod:`._placement`): labels are measured first, labels of one style share a scale, a
label box that cannot take even a shortened text keeps its source text, the bold running
number of a label ("**2.** Image formation") is reproduced (E-46 / Q22, CR-97), and
redaction annotations that were already in the file are never applied. ``page`` is
1-based like :class:`OverlayBlock.page`.

:func:`render_translated_figures` reads and opens the source once for a whole batch
(CR-61) and renders each job on its own one-page copy of the page it needs, so jobs on
the same page stay independent (CR-88); the single-figure functions are thin wrappers
around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from ..domain.overlay import BBox, LabelReplacement
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from ..pdfkit import api as pdfkit
from . import _placement
from .fonts import FontSpec

FIGURE_FLOOR_SCALE = 0.5
"""Hard shrink floor of a label (same as the overlay default)."""

FIGURE_SHARED_BOUND = 0.65
"""Lower bound of the shared label scale (the overlay's default review threshold)."""

MIN_DPI, MAX_DPI = 72, 600

__all__ = [
    "FigureJob",
    "FigureRenderStats",
    "LabelReplacement",
    "render_translated_figure",
    "render_translated_figure_with_stats",
    "render_translated_figures",
]


@dataclass(frozen=True)
class FigureJob:
    """One figure of a batch: the region of ``page`` (1-based) and its label boxes."""

    page: int
    region: BBox
    replacements: Sequence[LabelReplacement]
    dpi: int


@dataclass(frozen=True)
class FigureRenderStats:
    """Placement outcome per label (index-aligned with the replacements)."""

    scales: Tuple[float, ...]
    truncated: Tuple[bool, ...]
    """The label was cut with an ellipsis, or (see ``kept_source``) not replaced at all."""
    kept_source: Tuple[bool, ...] = ()
    """The box takes no text at all: the source label was left untouched (never blank)."""

    @property
    def truncated_count(self) -> int:
        return sum(1 for flag in self.truncated if flag)


FigureOutcome = Result[Tuple[bytes, FigureRenderStats]]


def _spec(replacement: LabelReplacement, rotate: int) -> _placement.PlacementSpec:
    return _placement.PlacementSpec(
        rect=replacement.bbox,
        text=replacement.text,
        font_size=replacement.font_size,
        bold=replacement.bold,
        italic=replacement.italic,
        family=replacement.family,
        color=replacement.color,
        alignment=replacement.alignment,
        prefix_style=replacement.prefix_style,
        rotate=rotate,
    )


def _open(source_pdf: Path) -> Result[Any]:
    try:
        data = source_pdf.read_bytes()
    except OSError as exc:
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"cannot read {source_pdf.name}: {exc.__class__.__name__}",
            ErrorScope.USER,
            context={"path": str(source_pdf)},
            cause=repr(exc)[:300],
        )
    try:
        return Ok(pdfkit.open_document_bytes(data))
    except Exception as exc:  # noqa: BLE001 - pymupdf raises its own hierarchy
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"cannot open {source_pdf.name}: {exc.__class__.__name__}",
            ErrorScope.USER,
            context={"path": str(source_pdf)},
            cause=repr(exc)[:300],
        )


def _render_job(
    doc: Any, name: str, job: FigureJob, font: FontSpec, archive: Optional[Any], uniform: bool
) -> FigureOutcome:
    if not MIN_DPI <= job.dpi <= MAX_DPI:
        return err(
            ErrorCode.EXPORT_FAILED,
            f"figure DPI {job.dpi} outside {MIN_DPI}-{MAX_DPI}",
            ErrorScope.USER,
            context={"dpi": str(job.dpi)},
        )
    count = pdfkit.doc_page_count(doc)
    if not 1 <= job.page <= count:
        return err(
            ErrorCode.EXPORT_FAILED,
            f"page {job.page} outside 1-{count} of {name}",
            ErrorScope.USER,
            context={"page": str(job.page)},
        )
    # CR-88: redaction and ``insert_htmlbox`` mutate the page, so every job works on its
    # own one-page copy. Two figures on the same page stay independent and a job that
    # fails mid-way cannot leave the page emptied for the ones behind it.
    work: Any = None
    try:
        work = pdfkit.doc_page_copy(doc, job.page - 1)
        page = pdfkit.doc_page(work, 0)
        rotate = pdfkit.page_rotation(page)
        plans = _placement.plan_placements(
            [_spec(item, rotate) for item in job.replacements],
            font,
            FIGURE_FLOOR_SCALE,
            archive,
            review_threshold=FIGURE_SHARED_BOUND,
            uniform=uniform,
            max_drop=_placement.LABEL_MAX_DROP,  # one diagram, one label size
        )
        page, _preserved = _placement.redact(
            work, page, [plan.spec.rect for plan in plans if plan.placeable]
        )
        scales: List[float] = []
        truncated: List[bool] = []
        kept: List[bool] = []
        for plan in plans:
            placement = _placement.place(page, plan, font, FIGURE_FLOOR_SCALE, archive)
            scales.append(placement.scale)
            truncated.append(placement.truncated)
            kept.append(not plan.placeable)
        pixmap = pdfkit.page_pixmap_png(page, job.dpi, clip=pdfkit.rect(*job.region))
        png = pdfkit.pixmap_png_bytes(pixmap)
    except Exception as exc:  # noqa: BLE001 - pymupdf raises its own hierarchy
        return err(
            ErrorCode.EXPORT_FAILED,
            f"figure overlay failed on page {job.page}: {exc.__class__.__name__}",
            ErrorScope.JOB_FATAL,
            context={"page": str(job.page)},
            cause=repr(exc)[:300],
        )
    finally:
        if work is not None:
            pdfkit.close_document(work)
    return Ok((png, FigureRenderStats(tuple(scales), tuple(truncated), tuple(kept))))


def render_translated_figures(
    source_pdf: Path,
    jobs: Sequence[FigureJob],
    font: FontSpec,
    *,
    uniform_scale: bool = True,
) -> Result[List[FigureOutcome]]:
    """Render every job against one in-memory copy of ``source_pdf`` (read and opened
    once). The outer ``Err`` means the file itself is unusable; otherwise one outcome per
    job, index-aligned, so a failing figure does not stop the others. Each outcome carries
    :class:`FigureRenderStats` - callers warn about ``truncated`` labels (CR-52)."""
    if not jobs:
        return Ok([])
    opened = _open(source_pdf)
    if isinstance(opened, Err):
        return opened
    doc = opened.value
    try:
        archive = _placement.font_archive(font)
        return Ok([
            _render_job(doc, source_pdf.name, job, font, archive, uniform_scale) for job in jobs
        ])
    except Exception as exc:  # noqa: BLE001 - font archive / pymupdf
        return err(
            ErrorCode.EXPORT_FAILED,
            f"figure overlay failed on {source_pdf.name}: {exc.__class__.__name__}",
            ErrorScope.JOB_FATAL,
            cause=repr(exc)[:300],
        )
    finally:
        pdfkit.close_document(doc)


def render_translated_figure_with_stats(
    source_pdf: Path,
    page: int,
    region: BBox,
    replacements: Sequence[LabelReplacement],
    dpi: int,
    font: FontSpec,
    *,
    uniform_scale: bool = True,
) -> FigureOutcome:
    """:func:`render_translated_figure` plus the per-label scale / truncation report."""
    if not MIN_DPI <= dpi <= MAX_DPI:  # before the file is touched, as before
        return err(
            ErrorCode.EXPORT_FAILED,
            f"figure DPI {dpi} outside {MIN_DPI}-{MAX_DPI}",
            ErrorScope.USER,
            context={"dpi": str(dpi)},
        )
    outcomes = render_translated_figures(
        source_pdf, [FigureJob(page, region, replacements, dpi)], font,
        uniform_scale=uniform_scale,
    )
    if isinstance(outcomes, Err):
        return outcomes
    return outcomes.value[0]


def render_translated_figure(
    source_pdf: Path,
    page: int,
    region: BBox,
    replacements: Sequence[LabelReplacement],
    dpi: int,
    font: FontSpec,
    *,
    uniform_scale: bool = True,
) -> Result[bytes]:
    """PNG bytes of ``region`` on ``page`` (1-based) of ``source_pdf`` after every
    label box was replaced by its translation, rendered at ``dpi``. The file on disk
    is never modified (the work happens on an in-memory copy)."""
    result = render_translated_figure_with_stats(
        source_pdf, page, region, replacements, dpi, font, uniform_scale=uniform_scale
    )
    if isinstance(result, Ok):
        return Ok(result.value[0])
    return result
