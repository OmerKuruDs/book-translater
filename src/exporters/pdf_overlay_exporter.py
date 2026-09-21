"""Overlay PDF: the source pages with every translated block written back into its
own rectangle (design doc 06, 3.6.4 and 4.5; US-18, US-19, US-21).

The page's properties are detected upstream (``OverlayBlock.style``/``alignment``)
and reproduced here: same rectangle, size, weight, style, alignment and colour;
images, drawings, annotations, page count and page sizes are untouched. Text is
only ever shrunk (uniformly, down to ``floor_scale``), never enlarged; what does
not fit even at the floor is cut with an ellipsis and reported as
``could_not_fit`` (E-38). Every judgement call lands in ``overlay_review.json``.

Per page: every unit is measured first and units of one style share a scale
(``uniform_scale``, see :mod:`._placement`); redaction rectangles are collected for
every unit that can take text and applied once (fill-less, images/line art kept),
links that vanished under a redaction are re-inserted, pre-existing redaction
annotations are parked during the pass and stay exactly as they were (E-50, never
applied by the tool), then each block is inserted. A unit that cannot take even a
shortened text keeps its source text (``kept_original`` / ``unplaceable``). After all
pages: outline retitling (4.8), metadata, ``set_language("tr")``,
``subset_fonts()``, ``garbage=4, deflate=True`` save via a temp file, smoke re-open,
and the review file -- written *after* the PDF.

Page numbers in :class:`OverlayPage`/:class:`OverlayBlock` are 1-based.
"""

from __future__ import annotations

import functools
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Hashable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .. import __version__
from ..domain.models import ChunkStatus
from ..domain.overlay import BBox, OverlayBlock, OverlayBlockKind, OverlayPage
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from ..pdfkit import api as pdfkit
from . import _placement
from .base import atomic_write_bytes
from .fonts import FontSpec

OVERLAY_PDF_NAME = "translated_book.overlay.pdf"
OVERLAY_REVIEW_NAME = "overlay_review.json"
REVIEW_FILE_VERSION = 1
DEFAULT_REVIEW_THRESHOLD = 0.65
DEFAULT_FLOOR_SCALE = 0.5
LANGUAGE = "tr"

OverlayProgress = Callable[[int, int], None]
"""``(pages_done, pages_total)`` after every page."""

_EXPECTED_KEEP_REASONS = frozenset({"page_number", "header_footer", "no_letters", "math"})
"""Keep reasons that are counted but not listed (design doc 06, 4.5)."""

UNPLACEABLE_NOTE = "unplaceable"
"""``kept_original`` note of a unit whose rectangle cannot take even a shortened text: the
source text is left in place instead of a blank box; it still counts as ``could_not_fit``."""


def _label_max_drop(key: Hashable) -> float:
    """Per-unit cap on the shared font factor, by style group.

    Figure labels are exempt (:data:`_placement.LABEL_MAX_DROP`): a diagram's labels are
    read as one set, so they share one size even when that shrinks a roomy label. Every
    other kind keeps the CR-95 cap, which is there to stop one crowded paragraph from
    setting the size for a page of prose that fits."""
    kind = key[0] if isinstance(key, tuple) and key else None
    if kind == OverlayBlockKind.FIGURE_LABEL.value:
        return _placement.LABEL_MAX_DROP
    return _placement.SHARED_MAX_DROP


class OverlayReviewReason(str, Enum):
    SHRUNK_BELOW_THRESHOLD = "shrunk_below_threshold"
    COULD_NOT_FIT = "could_not_fit"
    FRAGMENT = "fragment"
    KEPT_ORIGINAL = "kept_original"
    GLYPH_MISSING = "glyph_missing"
    PROVIDER_EMPTY = "provider_empty"
    RESIDUAL_GLYPHS = "residual_glyphs"
    OVER_IMAGE = "over_image"
    COLLATERAL_REDACTION = "collateral_redaction"
    PAGE_SHARED_SCALE = "page_shared_scale"
    """Informational, page level (CR-95): a style group of this page was placed at a
    shared font factor below ``_placement.SHARED_SCALE_NOTE``. Not a unit finding -
    ``block_id`` is empty and ``unit_id`` is 0."""


@dataclass(frozen=True)
class OverlayReviewEntry:
    page: int
    block_id: str
    unit_id: int
    reason: OverlayReviewReason
    scale: Optional[float]
    source_text: str
    translated_text: Optional[str]
    note: str = ""
    shared_scale: Optional[float] = None
    """Font factor of the unit's style group when it was placed with a shared scale."""


@dataclass(frozen=True)
class PlacementResult:
    unit_id: int
    placed: bool
    scale: float
    spare_height: float
    truncated: bool
    residual: bool
    reasons: Tuple[OverlayReviewReason, ...]
    font_size: float = 0.0
    """Final font size in points (0.0 when nothing was placed)."""
    shared_scale: Optional[float] = None


@dataclass(frozen=True)
class OverlayRenderOptions:
    """Rendering knobs. ``font`` has no default, so it comes first (dataclass rule);
    the design lists the thresholds first -- same fields, different order."""

    font: FontSpec
    title: Optional[str] = None
    author: Optional[str] = None
    producer: str = f"book-translator {__version__}"
    translate_headers: bool = False
    allow_partial: bool = False
    review_threshold: float = DEFAULT_REVIEW_THRESHOLD
    floor_scale: float = DEFAULT_FLOOR_SCALE
    uniform_scale: bool = True
    """Units of one style on a page share one font scale (outliers keep their own);
    ``False`` scales every rectangle on its own."""


@dataclass(frozen=True)
class OverlayArtifact:
    pdf_path: Path
    review_path: Path
    bytes_written: int
    pages: int
    placed: int
    shrunk_below_threshold: int
    could_not_fit: int
    kept_original: int
    pages_skipped: Tuple[int, ...]
    review: Tuple[OverlayReviewEntry, ...]
    warnings: Tuple[str, ...]
    placements: Tuple[PlacementResult, ...] = ()
    """Per-unit outcome (scale, final font size, shared scale) in page order."""


@dataclass
class _Counts:
    placed: int = 0
    shrunk_below_threshold: int = 0
    could_not_fit: int = 0
    kept_original: int = 0
    redact_annots_preserved: int = 0
    links_reinserted: int = 0
    outline_retitled: int = 0


def _failed(stage: str, exc: BaseException, **context: str) -> Err:
    return err(
        ErrorCode.EXPORT_FAILED,
        f"overlay PDF rendering failed while {stage}: {exc.__class__.__name__}",
        ErrorScope.JOB_FATAL,
        context=context,
        cause=repr(exc)[:300],
    )


def _entry(
    block: OverlayBlock, reason: OverlayReviewReason, scale: Optional[float] = None,
    note: str = "", translated: Optional[str] = None, shared_scale: Optional[float] = None,
) -> OverlayReviewEntry:
    return OverlayReviewEntry(
        page=block.page,
        block_id=block.block_id,
        unit_id=block.unit_id,
        reason=reason,
        scale=None if scale is None else round(scale, 4),
        source_text=block.source_text,
        translated_text=block.translated_text if translated is None else translated,
        note=note,
        shared_scale=None if shared_scale is None else round(shared_scale, 4),
    )


def _page_entry(page: int, factor: float, units: int) -> OverlayReviewEntry:
    """Page-level informational record of a low shared font factor (CR-95).

    Tells apart from a unit finding by ``block_id == ""`` / ``unit_id == 0``; the schema
    of the entry itself is the one every other record uses."""
    return OverlayReviewEntry(
        page=page,
        block_id="",
        unit_id=0,
        reason=OverlayReviewReason.PAGE_SHARED_SCALE,
        scale=round(factor, 4),
        source_text="",
        translated_text=None,
        note=f"{units} units share a font factor of {factor:.2f}",
        shared_scale=round(factor, 4),
    )


def _report(
    entries: List[OverlayReviewEntry],
    reasons: List[OverlayReviewReason],
    block: OverlayBlock,
    placement: _placement.Placement,
    reason: OverlayReviewReason,
    note: str = "",
    translated: Optional[str] = None,
) -> None:
    """One review entry for a placed (or attempted) unit, mirrored into ``reasons``."""
    reasons.append(reason)
    entries.append(_entry(block, reason, placement.scale, note=note, translated=translated,
                          shared_scale=placement.shared_scale))


def _spec(
    block: OverlayBlock, text: str, rect: Optional[BBox] = None, rotate: int = 0
) -> _placement.PlacementSpec:
    """``rect`` is the block's rectangle in unrotated page space (``None``: the page is
    upright and ``block.bbox`` is used as stored)."""
    style = block.style
    return _placement.PlacementSpec(
        rect=block.bbox if rect is None else rect,
        text=text,
        font_size=style.font_size,
        bold=style.bold,
        italic=style.italic,
        family=style.family,
        color=style.color,
        alignment=block.alignment,
        prefix_style=style.prefix_style,
        first_line_indent_pt=_placement.first_line_indent(block.bbox, block.line_boxes),
        rotate=rotate,
        line_height=_placement.source_line_height(block.line_boxes, style.font_size),
    )


def _pdf_date(moment: datetime) -> str:
    return moment.strftime("D:%Y%m%d%H%M%SZ")


class OverlayRenderer:
    """Build ``translated_book.overlay.pdf`` + ``overlay_review.json`` from the source
    PDF and the translated units (design doc 06, 3.6.4)."""

    def render(
        self,
        source_pdf: Path,
        pages: Sequence[OverlayPage],
        blocks_by_page: Callable[[int], Iterator[OverlayBlock]],
        outline_map: Mapping[Tuple[int, str], str],
        target_dir: Path,
        options: OverlayRenderOptions,
        progress: Optional[OverlayProgress] = None,
    ) -> Result[OverlayArtifact]:
        if not 0.0 < options.floor_scale <= options.review_threshold <= 1.0:
            return err(
                ErrorCode.EXPORT_FAILED,
                "overlay scales must satisfy 0 < floor-scale <= min-scale <= 1 "
                f"(got floor {options.floor_scale}, min {options.review_threshold})",
                ErrorScope.USER,
            )
        try:
            doc = pdfkit.open_document(str(source_pdf))
        except Exception as exc:  # noqa: BLE001 - pymupdf raises its own hierarchy
            return err(
                ErrorCode.INPUT_UNREADABLE,
                f"cannot open {source_pdf.name}: {exc.__class__.__name__}",
                ErrorScope.USER,
                context={"path": str(source_pdf)},
                cause=repr(exc)[:300],
            )
        try:
            return self._render_open(doc, source_pdf, pages, blocks_by_page, outline_map,
                                     target_dir, options, progress)
        finally:
            pdfkit.close_document(doc)

    # ------------------------------------------------------------------ #

    def _render_open(
        self,
        doc: Any,
        source_pdf: Path,
        pages: Sequence[OverlayPage],
        blocks_by_page: Callable[[int], Iterator[OverlayBlock]],
        outline_map: Mapping[Tuple[int, str], str],
        target_dir: Path,
        options: OverlayRenderOptions,
        progress: Optional[OverlayProgress],
    ) -> Result[OverlayArtifact]:
        if pdfkit.doc_needs_password(doc) or not pdfkit.doc_modifiable(doc):
            return err(
                ErrorCode.OVERLAY_PDF_RESTRICTED,
                f"{source_pdf.name} is encrypted or forbids modification; the overlay "
                "PDF cannot be written",
                ErrorScope.USER,
                context={"path": str(source_pdf)},
            )
        source_pages = pdfkit.doc_page_count(doc)
        archive = _placement.font_archive(options.font)
        counts = _Counts()
        entries: List[OverlayReviewEntry] = []
        warnings: List[str] = []
        skipped: List[int] = []
        placements: List[PlacementResult] = []
        ordered = sorted(pages, key=lambda info: info.page)
        for done, info in enumerate(ordered, start=1):
            if not 1 <= info.page <= source_pages:
                warnings.append(f"overlay_page_out_of_range:{info.page}")
            elif info.skip_reason:
                skipped.append(info.page)
            else:
                try:
                    # the callback reads the state DB: a lock / I/O error must not cross
                    # the layer boundary as an exception (CR-67)
                    blocks = list(blocks_by_page(info.page))
                    page_result = self._build_page(doc, info, blocks, options, archive,
                                                   counts, entries)
                except Exception as exc:  # noqa: BLE001 - pymupdf / DB driver hierarchies
                    return _failed(f"building page {info.page}", exc, page=str(info.page))
                if isinstance(page_result, Err):
                    return page_result
                placements.extend(page_result.value)
            if progress is not None:
                progress(done, len(ordered))

        try:
            counts.outline_retitled = _retitle_outline(doc, outline_map)
            _write_metadata(doc, options)
            try:
                pdfkit.doc_subset_fonts(doc)
            except Exception:  # noqa: BLE001 - size only; the file is still valid
                warnings.append("subset_fonts_failed")
            data = pdfkit.doc_to_bytes(doc, garbage=4, deflate=True)
        except Exception as exc:  # noqa: BLE001
            return _failed("finishing the document", exc)

        pdf_path = target_dir / OVERLAY_PDF_NAME
        written = atomic_write_bytes(pdf_path, data)
        if isinstance(written, Err):
            return written
        smoke = _smoke_open(pdf_path, source_pages)
        if isinstance(smoke, Err):
            return smoke

        if counts.redact_annots_preserved:
            warnings.append(f"redact_annots_preserved:{counts.redact_annots_preserved}")
        if counts.links_reinserted:
            warnings.append(f"links_reinserted:{counts.links_reinserted}")
        if counts.outline_retitled:
            warnings.append(f"outline_retitled:{counts.outline_retitled}")
        entries.sort(key=lambda e: (e.page, e.block_id, e.reason.value))
        review_path = target_dir / OVERLAY_REVIEW_NAME
        review = write_review_file(review_path, tuple(entries), options, counts, skipped)
        if isinstance(review, Err):
            return review
        return Ok(
            OverlayArtifact(
                pdf_path=pdf_path,
                review_path=review_path,
                bytes_written=written.value,
                pages=source_pages,
                placed=counts.placed,
                shrunk_below_threshold=counts.shrunk_below_threshold,
                could_not_fit=counts.could_not_fit,
                kept_original=counts.kept_original,
                pages_skipped=tuple(skipped),
                review=tuple(entries),
                warnings=tuple(warnings),
                placements=tuple(placements),
            )
        )

    # ------------------------------------------------------------------ #

    def _build_page(
        self,
        doc: Any,
        info: OverlayPage,
        blocks: Sequence[OverlayBlock],
        options: OverlayRenderOptions,
        archive: Optional[Any],
        counts: _Counts,
        entries: List[OverlayReviewEntry],
    ) -> Result[List[PlacementResult]]:
        """Measure -> group -> redact -> place for one page (module docstring of
        :mod:`._placement`). Nothing is removed from the page before every unit is known
        to take at least a shortened text (CR-52)."""
        page = pdfkit.doc_page(doc, info.page - 1)
        rotation = pdfkit.page_rotation(page)
        derotation = pdfkit.page_derotation_matrix(page) if rotation else None

        def unrotate(rect: BBox) -> BBox:
            # stored rects are in the orientation the reader sees; MuPDF works unrotated
            if derotation is None:
                return rect
            return pdfkit.transform_rect(rect, derotation)

        def pdf_rect(block: OverlayBlock) -> BBox:
            return unrotate(block.bbox)

        def redact_rect(block: OverlayBlock) -> BBox:
            """What the block's source glyphs are cleared from.

            Wider than the placement rect whenever a *painted* neighbour cut that one back:
            the strip taken away still holds this block's English glyphs and nobody paints
            over it, so redaction keeps the original extent there. It gives way to *kept*
            units (a math band, a page number) exactly like the placement rect does -
            :func:`cleanup.split_overlapping_rects` builds both."""
            return unrotate(block.redact_bbox or block.bbox)

        widgets = pdfkit.page_widget_rects(page)
        candidates: List[OverlayBlock] = []
        untouched: List[OverlayBlock] = []
        for block in blocks:
            keep = self._classify(block, options, widgets)
            if keep is None:
                candidates.append(block)
                continue
            counts.kept_original += 1
            reason, note = keep
            untouched.append(block)
            if reason is OverlayReviewReason.KEPT_ORIGINAL and note in _EXPECTED_KEEP_REASONS:
                continue
            entries.append(_entry(block, reason, note=note))
        if not candidates:
            return Ok([])

        # 0: measure every unit first, then even out the scale per style group
        specs = [
            _spec(block, block.translated_text or "", pdf_rect(block), rotation)
            for block in candidates
        ]
        keys = [
            (block.kind.value, round(block.style.font_size, 1), block.style.family)
            for block in candidates
        ]
        plans = _placement.plan_placements(
            specs,
            options.font,
            options.floor_scale,
            archive,
            group_keys=keys,
            review_threshold=options.review_threshold,
            uniform=options.uniform_scale,
            max_drop=_label_max_drop,
        )
        for factor, units in _placement.low_shared_factors(plans):
            entries.append(_page_entry(info.page, factor, units))
        results: List[PlacementResult] = []
        to_place: List[Tuple[OverlayBlock, _placement.PlacementPlan]] = []
        for block, plan in zip(candidates, plans, strict=True):
            if plan.placeable:
                to_place.append((block, plan))
                continue
            # not even a shortened text fits: the source text stays (never a blank box)
            counts.kept_original += 1
            counts.could_not_fit += 1
            untouched.append(block)
            entries.append(_entry(block, OverlayReviewReason.KEPT_ORIGINAL, note=UNPLACEABLE_NOTE))
            results.append(PlacementResult(
                unit_id=block.unit_id, placed=False, scale=0.0, spare_height=-1.0,
                truncated=True, residual=False,
                reasons=(OverlayReviewReason.KEPT_ORIGINAL,),
            ))
        if not to_place:
            return Ok(results)

        # 1-2: redaction, applied once; links and pre-existing redactions preserved
        links_before = pdfkit.page_links(page)
        before_texts = {
            block.unit_id: _placement.normalise(_placement.clip_text(page, pdf_rect(block)))
            for block in untouched
        }
        page, preserved = _placement.redact(doc, page, [redact_rect(b) for b, _ in to_place])
        counts.redact_annots_preserved += preserved
        reinserted = _reinsert_links(page, links_before)
        counts.links_reinserted += reinserted
        if reinserted:
            page = pdfkit.doc_reload_page(doc, page)

        # 3: residual + collateral checks
        residual: Dict[int, bool] = {}
        for block, plan in to_place:
            residual[block.unit_id] = bool(_placement.clip_text(page, plan.spec.rect).strip())
        collateral: Dict[int, bool] = {}
        for block in untouched:
            after = _placement.normalise(_placement.clip_text(page, pdf_rect(block)))
            if after != before_texts[block.unit_id]:
                entries.append(_entry(block, OverlayReviewReason.COLLATERAL_REDACTION,
                                      note="neighbour redaction changed this unit"))
                for placed, _ in to_place:
                    if _placement.intersects(redact_rect(placed), pdf_rect(block)):
                        collateral[placed.unit_id] = True

        # 4-5: insertion, shrink policy, glyph check
        for block, plan in to_place:
            placement = _placement.place(page, plan, options.font, options.floor_scale, archive)
            reasons: List[OverlayReviewReason] = []
            report = functools.partial(_report, entries, reasons, block, placement)
            if placement.placed:
                counts.placed += 1
            if placement.truncated:
                counts.could_not_fit += 1
                report(
                    OverlayReviewReason.COULD_NOT_FIT,
                    "ellipsis" if placement.placed else "not placed",
                    placement.text or block.translated_text,
                )
            elif placement.scale < options.review_threshold:
                counts.shrunk_below_threshold += 1
                report(OverlayReviewReason.SHRUNK_BELOW_THRESHOLD)
            if placement.placed:
                missing = _placement.missing_glyphs(page, plan.spec.rect, placement.text)
                if missing:
                    report(OverlayReviewReason.GLYPH_MISSING, f"missing {missing}")
            if residual[block.unit_id]:
                report(OverlayReviewReason.RESIDUAL_GLYPHS)
            if collateral.get(block.unit_id):
                report(OverlayReviewReason.COLLATERAL_REDACTION)
            if block.fragment:
                report(OverlayReviewReason.FRAGMENT)
            if block.over_image:
                report(OverlayReviewReason.OVER_IMAGE)
            if block.review_flag:
                report(OverlayReviewReason.PROVIDER_EMPTY, "review_flag")
            results.append(PlacementResult(
                unit_id=block.unit_id,
                placed=placement.placed,
                scale=placement.scale,
                spare_height=placement.spare_height,
                truncated=placement.truncated,
                residual=residual[block.unit_id],
                reasons=tuple(reasons),
                font_size=placement.font_size,
                shared_scale=placement.shared_scale,
            ))
        return Ok(results)

    @staticmethod
    def _classify(
        block: OverlayBlock, options: OverlayRenderOptions, widgets: Sequence[BBox]
    ) -> Optional[Tuple[OverlayReviewReason, str]]:
        """``None`` when the unit is placed; otherwise the review reason and note of
        the kept original."""
        if not block.translate:
            return OverlayReviewReason.KEPT_ORIGINAL, block.keep_reason or "not_translatable"
        if block.kind is OverlayBlockKind.PAGE_NUMBER:  # FR-46: never replaced
            return OverlayReviewReason.KEPT_ORIGINAL, "page_number"
        if (
            block.kind in (OverlayBlockKind.HEADER, OverlayBlockKind.FOOTER)
            and not options.translate_headers
        ):
            return OverlayReviewReason.KEPT_ORIGINAL, "header_footer"
        if block.status is not ChunkStatus.COMPLETED:
            return OverlayReviewReason.KEPT_ORIGINAL, "partial"
        if not (block.translated_text or "").strip():
            return OverlayReviewReason.PROVIDER_EMPTY, "empty translation"
        for widget in widgets:
            if _placement.intersects(block.bbox, widget):
                return OverlayReviewReason.KEPT_ORIGINAL, "widget_overlap"
        return None


def _reinsert_links(page: Any, before: Sequence[Any]) -> int:
    """Re-insert every link of ``before`` that the redaction removed (R16)."""
    remaining = {pdfkit.link_signature(link) for link in pdfkit.page_links(page)}
    count = 0
    for link in before:
        if pdfkit.link_signature(link) in remaining:
            continue
        try:
            pdfkit.page_insert_link(page, link)
            count += 1
        except Exception:  # noqa: BLE001 - best effort per link
            continue
    return count


def _retitle_outline(doc: Any, outline_map: Mapping[Tuple[int, str], str]) -> int:
    """Replace ToC titles that match a translated heading 1:1 (design doc 06, 4.8).

    Entries are retitled in place (``set_toc_item``): destination point, zoom, colour,
    bold/italic and URI bookmarks stay exactly as they were (CR-50); rebuilding the
    outline with ``set_toc`` would move every destination to the top of its page."""
    toc = pdfkit.doc_toc(doc)
    if not toc or not outline_map:
        return 0
    lookup = {
        (page, _placement.normalise(title)): translated
        for (page, title), translated in outline_map.items()
        if translated and translated.strip()
    }
    changed = 0
    for index, row in enumerate(toc):
        title, page = str(row[1]), int(row[2])
        translated = lookup.get((page, _placement.normalise(title)))
        if translated is not None and translated != title:
            pdfkit.doc_set_toc_title(doc, index, translated)
            changed += 1
    return changed


def _write_metadata(doc: Any, options: OverlayRenderOptions) -> None:
    existing = pdfkit.doc_metadata(doc)
    info: Dict[str, str] = {
        "title": options.title or str(existing.get("title") or ""),
        "author": options.author or str(existing.get("author") or ""),
        "producer": options.producer,
        "modDate": _pdf_date(datetime.now(timezone.utc)),
    }
    for key in ("subject", "keywords", "creator", "creationDate"):
        value = existing.get(key)
        if value:
            info[key] = str(value)
    pdfkit.doc_set_metadata(doc, info)
    pdfkit.doc_set_language(doc, LANGUAGE)


def _smoke_open(path: Path, expected_pages: int) -> Result[None]:
    try:
        doc = pdfkit.open_document(str(path))
    except Exception as exc:  # noqa: BLE001
        return err(
            ErrorCode.EXPORT_FAILED,
            f"written overlay PDF cannot be re-opened: {exc.__class__.__name__}",
            ErrorScope.JOB_FATAL,
            cause=repr(exc)[:300],
            context={"path": str(path)},
        )
    try:
        pages = pdfkit.doc_page_count(doc)
    finally:
        pdfkit.close_document(doc)
    if pages != expected_pages:
        return err(
            ErrorCode.EXPORT_FAILED,
            f"overlay PDF has {pages} pages, source has {expected_pages}",
            ErrorScope.JOB_FATAL,
            context={"path": str(path)},
        )
    return Ok(None)


def write_review_file(
    path: Path,
    entries: Sequence[OverlayReviewEntry],
    options: OverlayRenderOptions,
    counts: _Counts,
    pages_skipped: Sequence[int],
) -> Result[int]:
    """``overlay_review.json`` (design doc 06, 7.2), written atomically."""
    payload = {
        "version": REVIEW_FILE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "thresholds": {
            "review_threshold": options.review_threshold,
            "floor_scale": options.floor_scale,
        },
        "counts": {
            "placed": counts.placed,
            "shrunk_below_threshold": counts.shrunk_below_threshold,
            "could_not_fit": counts.could_not_fit,
            "kept_original": counts.kept_original,
            "pages_skipped": len(pages_skipped),
            "entries": len(entries),
        },
        "pages_skipped": list(pages_skipped),
        "entries": [
            {**asdict(entry), "reason": entry.reason.value} for entry in entries
        ],
    }
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return atomic_write_bytes(path, data)
