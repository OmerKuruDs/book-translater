"""``figures.json`` schema and I/O (design doc 06, §3.2, D4).

The inventory is derived extraction data like ``source_book.md``: written by
the orchestrator after a successful extraction, read back for ``status
--json``, stale-image cleanup and the overlay extractor. Version 1.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..domain.models import (
    ExtractedDocument,
    FigureKind,
    FigureRecord,
    FigureTotals,
    LabelBox,
)
from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err
from .base import FigureOptions

__all__ = [
    "FIGURES_FILE_NAME",
    "FIGURES_FILE_VERSION",
    "FIGURE_FILE_PATTERN",
    "FigureEntry",
    "FigureTotalsModel",
    "LabelBoxEntry",
    "FiguresFile",
    "build_figures_file",
    "compute_totals",
    "figures_file_from_document",
    "inventory_signature",
    "load_figures_file",
    "options_to_jsonable",
    "save_figures_file",
]

FIGURES_FILE_NAME = "figures.json"
FIGURES_FILE_VERSION = 1
FIGURE_FILE_PATTERN = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*/p\d{3,}-f\d{2,}\.png$")
"""``FigureEntry.file``: ``<images dir>/pNNN-fKK.png`` as written by the extractor."""


class LabelBoxEntry(BaseModel):
    """``figures.json`` form of :class:`LabelBox` (Addendum A)."""

    model_config = ConfigDict(extra="ignore")

    text: str
    bbox: Tuple[float, float, float, float]
    font_size: float
    bold: bool = False
    italic: bool = False
    family: Literal["serif", "sans", "mono"] = "serif"
    color: str = "#000000"
    alignment: Literal["left", "center", "right", "justify"] = "left"
    prefix_style: Optional[Tuple[str, bool, bool]] = None
    """(prefix_text, bold, italic) of a bold running number (E-46 / Q22, CR-97); absent
    in an inventory written before this field existed."""

    def to_box(self) -> LabelBox:
        return LabelBox(
            text=self.text,
            bbox=self.bbox,
            font_size=self.font_size,
            bold=self.bold,
            italic=self.italic,
            family=self.family,
            color=self.color,
            alignment=self.alignment,
            prefix_style=self.prefix_style,
        )

    @classmethod
    def from_box(cls, box: LabelBox) -> "LabelBoxEntry":
        return cls(
            text=box.text,
            bbox=box.bbox,
            font_size=box.font_size,
            bold=box.bold,
            italic=box.italic,
            family=box.family,
            color=box.color,
            alignment=box.alignment,
            prefix_style=box.prefix_style,
        )


class FigureEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    image_id: int
    page: int
    index_on_page: int
    kind: FigureKind
    region: Tuple[float, float, float, float]
    file: str
    dpi: int
    width_px: int
    height_px: int
    bytes: int
    dpi_reduced: bool = False
    label_count: int = 0
    labels: List[str] = Field(default_factory=list)
    labels_more: int = 0
    caption_present: bool = False
    warnings: List[str] = Field(default_factory=list)
    label_boxes: List[LabelBoxEntry] = Field(default_factory=list)  # Addendum A

    @field_validator("file")
    @classmethod
    def _file_is_a_tool_written_png(cls, value: str) -> str:
        """``file`` is a write target of the export stage (the translated PNG): only the
        names the extractor produces are accepted - ``<dir>/pNNN-fKK.png``, forward
        slashes, no drive, no ``..`` (CR-59)."""
        if not FIGURE_FILE_PATTERN.match(value):
            raise ValueError(
                f"figure file {value!r} is not of the form images/pNNN-fKK.png"
            )
        return value

    def to_record(self) -> FigureRecord:
        return FigureRecord(
            image_id=self.image_id,
            page=self.page,
            index_on_page=self.index_on_page,
            kind=self.kind,
            region=self.region,
            file=self.file,
            dpi=self.dpi,
            width_px=self.width_px,
            height_px=self.height_px,
            bytes=self.bytes,
            dpi_reduced=self.dpi_reduced,
            label_count=self.label_count,
            labels=tuple(self.labels),
            labels_more=self.labels_more,
            caption_present=self.caption_present,
            warnings=tuple(self.warnings),
            label_boxes=tuple(entry.to_box() for entry in self.label_boxes),
        )

    @classmethod
    def from_record(cls, record: FigureRecord) -> "FigureEntry":
        return cls(
            image_id=record.image_id,
            page=record.page,
            index_on_page=record.index_on_page,
            kind=record.kind,
            region=record.region,
            file=record.file,
            dpi=record.dpi,
            width_px=record.width_px,
            height_px=record.height_px,
            bytes=record.bytes,
            dpi_reduced=record.dpi_reduced,
            label_count=record.label_count,
            labels=list(record.labels),
            labels_more=record.labels_more,
            caption_present=record.caption_present,
            warnings=list(record.warnings),
            label_boxes=[LabelBoxEntry.from_box(box) for box in record.label_boxes],
        )


class FigureTotalsModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    count: int = 0
    raster: int = 0
    vector: int = 0
    mixed: int = 0
    total_bytes: int = 0
    largest_file: Optional[str] = None
    largest_bytes: int = 0
    pages_with_figures: int = 0
    pages_skipped: List[Tuple[int, str]] = Field(default_factory=list)
    dpi_reduced: int = 0

    def to_totals(self) -> FigureTotals:
        return FigureTotals(
            count=self.count,
            raster=self.raster,
            vector=self.vector,
            mixed=self.mixed,
            total_bytes=self.total_bytes,
            largest_file=self.largest_file,
            largest_bytes=self.largest_bytes,
            pages_with_figures=self.pages_with_figures,
            pages_skipped=tuple((int(p), str(r)) for p, r in self.pages_skipped),
            dpi_reduced=self.dpi_reduced,
        )

    @classmethod
    def from_totals(cls, totals: FigureTotals) -> "FigureTotalsModel":
        return cls(
            count=totals.count,
            raster=totals.raster,
            vector=totals.vector,
            mixed=totals.mixed,
            total_bytes=totals.total_bytes,
            largest_file=totals.largest_file,
            largest_bytes=totals.largest_bytes,
            pages_with_figures=totals.pages_with_figures,
            pages_skipped=list(totals.pages_skipped),
            dpi_reduced=totals.dpi_reduced,
        )


class FiguresFile(BaseModel):
    """The ``figures.json`` document."""

    model_config = ConfigDict(extra="ignore")

    version: int = FIGURES_FILE_VERSION
    generated_at: str
    extractor: str
    dpi_requested: int
    options: Dict[str, Any] = Field(default_factory=dict)
    figures: List[FigureEntry] = Field(default_factory=list)
    totals: FigureTotalsModel = Field(default_factory=FigureTotalsModel)

    def records(self) -> Tuple[FigureRecord, ...]:
        return tuple(entry.to_record() for entry in self.figures)

    def file_names(self) -> List[str]:
        """Bare PNG file names (``p002-f01.png``) referenced by the inventory."""
        return [entry.file.rsplit("/", 1)[-1] for entry in self.figures]


def compute_totals(
    figures: Sequence[FigureRecord], pages_skipped: Sequence[Tuple[int, str]] = ()
) -> FigureTotals:
    largest: Optional[FigureRecord] = None
    for record in figures:
        if largest is None or record.bytes > largest.bytes:
            largest = record
    return FigureTotals(
        count=len(figures),
        raster=sum(1 for f in figures if f.kind is FigureKind.RASTER),
        vector=sum(1 for f in figures if f.kind is FigureKind.VECTOR),
        mixed=sum(1 for f in figures if f.kind is FigureKind.MIXED),
        total_bytes=sum(f.bytes for f in figures),
        largest_file=largest.file.rsplit("/", 1)[-1] if largest is not None else None,
        largest_bytes=largest.bytes if largest is not None else 0,
        pages_with_figures=len({f.page for f in figures}),
        pages_skipped=tuple((int(p), str(r)) for p, r in pages_skipped),
        dpi_reduced=sum(1 for f in figures if f.dpi_reduced),
    )


def options_to_jsonable(options: FigureOptions) -> Dict[str, Any]:
    return {
        "dpi": options.dpi,
        "dpi_floor": options.dpi_floor,
        "max_bytes": options.max_bytes,
        "min_area_ratio": options.min_area_ratio,
        "min_paths": options.min_paths,
        "merge_gap_pt": options.merge_gap_pt,
        "border_pt": options.border_pt,
        "inner_text_overlap": options.inner_text_overlap,
        "label_attach_lines": options.label_attach_lines,
        "legend": options.legend,
        "legend_limit": options.legend_limit,
        "exclude_pages": sorted(options.exclude_pages),
        "images_dir_name": options.images_dir_name,
    }


def build_figures_file(
    figures: Sequence[FigureRecord],
    pages_skipped: Sequence[Tuple[int, str]],
    *,
    extractor: str,
    options: FigureOptions,
    generated_at: Optional[datetime] = None,
) -> FiguresFile:
    stamp = generated_at or datetime.now(timezone.utc)
    return FiguresFile(
        version=FIGURES_FILE_VERSION,
        generated_at=stamp.isoformat(),
        extractor=extractor,
        dpi_requested=options.dpi,
        options=options_to_jsonable(options),
        figures=[FigureEntry.from_record(f) for f in figures],
        totals=FigureTotalsModel.from_totals(compute_totals(figures, pages_skipped)),
    )


def figures_file_from_document(
    document: ExtractedDocument,
    options: FigureOptions,
    *,
    extractor: Optional[str] = None,
    generated_at: Optional[datetime] = None,
) -> FiguresFile:
    return build_figures_file(
        document.figures,
        document.figure_pages_skipped,
        extractor=extractor or document.extractor_name,
        options=options,
        generated_at=generated_at,
    )


def inventory_signature(data: FiguresFile) -> str:
    """Canonical JSON of the inventory without ``generated_at`` (determinism comparisons)."""
    payload = data.model_dump(mode="json")
    payload.pop("generated_at", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def load_figures_file(path: Path) -> Result[FiguresFile]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"cannot read {path.name}: {exc.__class__.__name__}",
            ErrorScope.USER,
            cause=repr(exc),
        )
    try:
        data = FiguresFile.model_validate_json(raw)
    except ValidationError as exc:
        return err(
            ErrorCode.STATE_CORRUPT,
            f"{path.name} is not a valid figure inventory: "
            f"{'; '.join(str(e['msg']) for e in exc.errors()[:3])}",
            ErrorScope.USER,
        )
    except ValueError as exc:
        return err(
            ErrorCode.STATE_CORRUPT,
            f"{path.name} is not valid JSON: {type(exc).__name__}",
            ErrorScope.USER,
            cause=repr(exc)[:200],
        )
    if data.version != FIGURES_FILE_VERSION:
        return err(
            ErrorCode.STATE_CORRUPT,
            f"{path.name} has unsupported version {data.version} "
            f"(expected {FIGURES_FILE_VERSION})",
            ErrorScope.USER,
        )
    return Ok(data)


def save_figures_file(path: Path, data: FiguresFile) -> Result[int]:
    """Atomic UTF-8 write (temp file + ``os.replace``); returns bytes written."""
    payload = json.dumps(data.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    encoded = payload.encode("utf-8")
    tmp_name: Optional[str] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        return err(
            ErrorCode.EXPORT_FAILED,
            f"cannot write {path.name}: {exc.__class__.__name__}: {exc.strerror or exc}",
            ErrorScope.JOB_FATAL,
            context={"path": str(path)},
            cause=repr(exc),
        )
    finally:
        if tmp_name is not None:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
    return Ok(len(encoded))
