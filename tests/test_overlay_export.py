"""Overlay PDF placement engine, review file and translated figure PNG
(design doc 06, 3.6.4 / 4.5 / 8.2; US-18, US-19, US-21; FR-43..FR-48).

Fixture PDFs are built here with pymupdf; overlay blocks are hand-built from the
line boxes of ``page.get_text("dict")`` with a deliberately simple grouping (the
real grouping lives in the extractor, which is not under test here).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import pymupdf
import pytest

from book_translator.domain.models import ChunkStatus
from book_translator.domain.overlay import (
    BBox,
    LabelReplacement,
    OverlayAlignment,
    OverlayBlock,
    OverlayBlockKind,
    OverlayPage,
    OverlayStyle,
    format_block_id,
)
from book_translator.domain.result import Err, ErrorCode, ErrorScope, unwrap
from book_translator.exporters import _placement
from book_translator.exporters import figure_overlay as figure_overlay_module
from book_translator.exporters.figure_overlay import (
    render_translated_figure,
    render_translated_figure_with_stats,
)
from book_translator.exporters.fonts import BUILTIN_FONT, resolve_font
from book_translator.exporters.pdf_overlay_exporter import (
    OVERLAY_PDF_NAME,
    OVERLAY_REVIEW_NAME,
    OverlayArtifact,
    OverlayRenderer,
    OverlayRenderOptions,
    OverlayReviewReason,
)
from book_translator.pdfkit import api as pdfkit

TEST_DOC = Path(__file__).resolve().parent.parent / "docs" / "test_doc.pdf"
FIGURE_REGION: BBox = (106.1, 146.4, 492.6, 590.9)  # design doc 06, E5

PAGE2_TRANSLATIONS = {
    "2D (what?)": "2B (ne?)",
    "2. Image formation": "2. Görüntü oluşumu",
    "3. Image processing": "3. Görüntü işleme",
    "4. Model fitting and optimization": "4. Model uydurma ve optimizasyon",
    "5. Deep learning": "5. Derin öğrenme",
    "6. Recognition": "6. Tanıma",
    "7. Feature detection and matching": "7. Öznitelik tespiti ve eşleme",
    "8. Image alignment and stitching": "8. Görüntü hizalama ve birleştirme",
    "9. Motion estimation": "9. Hareket kestirimi",
    "10. Computational photography": "10. Hesaplamalı fotoğrafçılık",
    "11. Structure from motion and SLAM": "11. Hareketten yapı ve SLAM",
    "12. Depth estimation": "12. Derinlik kestirimi",
    "13. 3D reconstruction": "13. 3B geri çatım",
    "14. Image-based rendering": "14. Görüntü tabanlı görselleştirme",
}
"""Fourteen labels of Figure 1.12 (the PoC translations; ``3D (where?)`` stays)."""


# --------------------------------------------------------------------------- #
# Block building from line boxes (simple grouping, test-local)
# --------------------------------------------------------------------------- #


def _lines(page: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            text = "".join(span["text"] for span in line["spans"])
            if not text.strip():
                continue
            dominant = max(line["spans"], key=lambda span: len(span["text"]))
            found.append({
                "bbox": tuple(float(v) for v in line["bbox"]),
                "text": text,
                "size": float(dominant["size"]),
                "bold": bool(dominant["flags"] & 16) or "Bold" in dominant["font"],
                "italic": bool(dominant["flags"] & 2) or "Italic" in dominant["font"],
                "font": str(dominant["font"]),
                "color": int(dominant["color"]),
            })
    found.sort(key=lambda line: (round(line["bbox"][1]), line["bbox"][0]))
    return found


def group_lines(page: Any) -> List[Dict[str, Any]]:
    """Lines join a group when directly below it (gap <= 0.5 x height, x-overlap >= 30 %)
    or when they continue the same row (a bold caption prefix)."""
    groups: List[Dict[str, Any]] = []
    for line in _lines(page):
        joined = False
        for group in groups:
            prev = group["lines"][-1]
            lb, pb, gb = line["bbox"], prev["bbox"], group["bbox"]
            height = min(lb[3] - lb[1], pb[3] - pb[1])
            gap = lb[1] - pb[3]
            overlap_x = min(lb[2], gb[2]) - max(lb[0], gb[0])
            width = min(lb[2] - lb[0], gb[2] - gb[0])
            overlap_y = min(lb[3], pb[3]) - max(lb[1], pb[1])
            same_size = abs(line["size"] - group["lines"][0]["size"]) <= 1.0
            below = gap <= 0.5 * height and overlap_x / max(width, 1.0) >= 0.3 and same_size
            same_row = (
                overlap_y >= 0.5 * height and 0 <= lb[0] - pb[2] <= 3 * line["size"] and same_size
            )
            if below or same_row:
                group["lines"].append(line)
                group["bbox"] = (
                    min(gb[0], lb[0]), min(gb[1], lb[1]), max(gb[2], lb[2]), max(gb[3], lb[3])
                )
                joined = True
                break
        if not joined:
            groups.append({"lines": [line], "bbox": line["bbox"]})
    return groups


def make_block(
    page_no: int,
    index: int,
    bbox: BBox,
    source: str,
    translated: Optional[str],
    *,
    kind: OverlayBlockKind = OverlayBlockKind.BODY,
    size: float = 11.0,
    bold: bool = False,
    italic: bool = False,
    family: str = "serif",
    color: str = "#000000",
    alignment: OverlayAlignment = OverlayAlignment.LEFT,
    keep_reason: Optional[str] = None,
    status: ChunkStatus = ChunkStatus.COMPLETED,
    prefix_style: Optional[Tuple[str, bool, bool]] = None,
    line_boxes: Sequence[BBox] = (),
    **extra: Any,
) -> OverlayBlock:
    translate = translated is not None and keep_reason is None
    return OverlayBlock(
        unit_id=page_no * 100 + index + 1,
        block_id=format_block_id(page_no, index),
        page=page_no,
        index_on_page=index,
        kind=kind,
        bbox=bbox,
        line_boxes=tuple(line_boxes) or (bbox,),
        style=OverlayStyle(
            font_size=size, bold=bold, italic=italic, family=family,  # type: ignore[arg-type]
            color=color, prefix_style=prefix_style,
        ),
        alignment=alignment,
        source_text=source,
        content_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        char_count=len(source),
        translate=translate,
        keep_reason=keep_reason if not translate else None,
        fragment=bool(extra.get("fragment", False)),
        over_image=bool(extra.get("over_image", False)),
        status=status if translate else ChunkStatus.COMPLETED,
        translated_text=(translated if status is ChunkStatus.COMPLETED else None)
        if translate else source,
        review_flag=bool(extra.get("review_flag", False)),
    )


def blocks_from_page(
    page: Any,
    page_no: int,
    translations: Dict[str, str],
    *,
    kinds: Optional[Dict[str, OverlayBlockKind]] = None,
    alignments: Optional[Dict[str, OverlayAlignment]] = None,
    keep: Optional[Dict[str, str]] = None,
) -> List[OverlayBlock]:
    """One block per line group; translated when its text is in ``translations``,
    otherwise kept with ``keep[text]`` (default ``figure_text``)."""
    blocks: List[OverlayBlock] = []
    for index, group in enumerate(group_lines(page)):
        text = " ".join(" ".join(line["text"].split()) for line in group["lines"])
        dominant = max(group["lines"], key=lambda line: len(line["text"]))
        first = group["lines"][0]
        family = "sans" if any(k in dominant["font"] for k in ("Sans", "Helv")) else "serif"
        prefix = None
        if first["bold"] and not all(line["bold"] for line in group["lines"]):
            head = text.split(" ", 1)[0]
            if head.endswith("."):
                prefix = (head, True, False)
        kind = (kinds or {}).get(text, OverlayBlockKind.BODY)
        alignment = (alignments or {}).get(
            text, OverlayAlignment.JUSTIFY if len(group["lines"]) > 1 else OverlayAlignment.LEFT
        )
        translated = translations.get(text)
        keep_reason = None if translated is not None else (keep or {}).get(text, "figure_text")
        blocks.append(make_block(
            page_no, index, group["bbox"], text, translated, kind=kind,
            size=round(dominant["size"], 2), bold=all(line["bold"] for line in group["lines"]),
            italic=dominant["italic"], family=family, color=f"#{dominant['color']:06x}",
            alignment=alignment, keep_reason=keep_reason, prefix_style=prefix,
            line_boxes=[line["bbox"] for line in group["lines"]],
        ))
    return blocks


def page_infos(doc: Any, skip: Dict[int, str] = {}) -> List[OverlayPage]:  # noqa: B006
    infos = []
    for index in range(doc.page_count):
        width, height = doc[index].rect.width, doc[index].rect.height
        infos.append(OverlayPage(
            page=index + 1, width=width, height=height, rotation=0,
            has_text_layer=(index + 1) not in skip, block_count=0, translatable_count=0,
            skip_reason=skip.get(index + 1), body_size=11.0,
        ))
    return infos


def infos_of(path: Path) -> List[OverlayPage]:
    """``page_infos`` of a file without leaking the document handle (CR-68)."""
    doc = pymupdf.open(str(path))
    try:
        return page_infos(doc)
    finally:
        doc.close()


def by_page(blocks: Sequence[OverlayBlock]) -> Callable[[int], Iterator[OverlayBlock]]:
    return lambda page: iter([b for b in blocks if b.page == page])


def render(
    source: Path,
    blocks: Sequence[OverlayBlock],
    target: Path,
    *,
    pages: Optional[Sequence[OverlayPage]] = None,
    outline_map: Optional[Dict[Tuple[int, str], str]] = None,
    **options: Any,
) -> OverlayArtifact:
    doc = pymupdf.open(str(source))
    infos = list(pages) if pages is not None else page_infos(doc)
    doc.close()
    opts = OverlayRenderOptions(font=options.pop("font", BUILTIN_FONT), **options)
    return unwrap(OverlayRenderer().render(
        source, infos, by_page(blocks), outline_map or {}, target, opts
    ))


# --------------------------------------------------------------------------- #
# Fixture PDFs
# --------------------------------------------------------------------------- #

STYLE_TEXTS = {
    "header": "Running header",
    "page_number": "7",
    "bold": "Bold heading text",
    "italic": "Italic sentence here.",
    "centred": "Centred title",
    "coloured": "Coloured line of prose.",
    "justified": (
        "This justified paragraph has enough words to fill three lines of the box "
        "so that the alignment detection has something to look at in the fixture."
    ),
    "right": "Right aligned",
    "super_word": "Superscript",
    "super_mark": "1",
    "footnote": "1 Footnote text in small type at the bottom of the page.",
}
STYLE_TRANSLATIONS = {
    "Bold heading text": "Kalın başlık metni",
    "Italic sentence here.": "İtalik cümle burada.",
    "Centred title": "Ortalanmış başlık",
    "Coloured line of prose.": "Renkli düz yazı satırı.",
    STYLE_TEXTS["justified"]: (
        "Bu iki yana yaslı paragraf, hizalama tespitinin bakacağı bir şeyler olsun diye "
        "kutunun üç satırını dolduracak kadar sözcük içeriyor; çğışöü."
    ),
    "Right aligned": "Sağa yaslı",
    "Superscript": "Üst simge",
    STYLE_TEXTS["footnote"]: "1 Sayfanın altında küçük puntoyla dipnot metni.",
}
BLUE = (0.2, 0.3, 0.6)


def build_overlay_styles(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=520)
    page.insert_text((40, 30), STYLE_TEXTS["header"], fontsize=9, fontname="hebo")
    page.insert_text((350, 30), STYLE_TEXTS["page_number"], fontsize=9, fontname="hebo")
    page.insert_text((40, 80), STYLE_TEXTS["bold"], fontsize=14, fontname="hebo")
    page.insert_text((40, 110), STYLE_TEXTS["italic"], fontsize=11, fontname="tiit")
    page.insert_textbox(
        pymupdf.Rect(40, 130, 360, 150), STYLE_TEXTS["centred"], fontsize=12,
        fontname="tiro", align=pymupdf.TEXT_ALIGN_CENTER,
    )
    page.insert_text((40, 175), STYLE_TEXTS["coloured"], fontsize=11, fontname="tiro",
                     color=BLUE)
    page.insert_textbox(
        pymupdf.Rect(40, 190, 300, 250), STYLE_TEXTS["justified"], fontsize=11,
        fontname="tiro", align=pymupdf.TEXT_ALIGN_JUSTIFY,
    )
    page.insert_textbox(
        pymupdf.Rect(40, 270, 360, 290), STYLE_TEXTS["right"], fontsize=11, fontname="tiro",
        align=pymupdf.TEXT_ALIGN_RIGHT,
    )
    page.insert_text((40, 330), STYLE_TEXTS["super_word"], fontsize=11, fontname="tiro")
    page.insert_text((104, 325), STYLE_TEXTS["super_mark"], fontsize=6, fontname="tiro")
    page.insert_text((40, 490), STYLE_TEXTS["footnote"], fontsize=7, fontname="tiro")
    page.draw_rect(pymupdf.Rect(30, 60, 370, 300), color=(0, 0, 0), width=0.8)
    page.draw_line(pymupdf.Point(40, 470), pymupdf.Point(200, 470), color=(0, 0, 0))
    doc.save(str(path))
    doc.close()
    return path


def grey_pixmap(width: int = 120, height: int = 80, value: int = 90) -> Any:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pixmap.clear_with(value)
    return pixmap


def build_overlay_no_text(path: Path) -> Path:
    doc = pymupdf.open()
    first = doc.new_page(width=300, height=300)
    first.insert_text((30, 50), "Text page before the scan.", fontsize=11, fontname="tiro")
    scanned = doc.new_page(width=300, height=300)
    scanned.insert_image(pymupdf.Rect(20, 20, 280, 280), pixmap=grey_pixmap(260, 260, 200))
    doc.save(str(path))
    doc.close()
    return path


OVER_IMAGE_TEXT = "Caption inside the photo"
OVER_IMAGE_RECT = pymupdf.Rect(40, 40, 260, 200)


def build_overlay_over_image(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=300)
    page.insert_image(OVER_IMAGE_RECT, pixmap=grey_pixmap(220, 160, 120))
    page.insert_text((50, 120), OVER_IMAGE_TEXT, fontsize=12, fontname="hebo",
                     color=(1, 1, 1))
    page.insert_text((40, 250), "Prose below the image.", fontsize=11, fontname="tiro")
    doc.save(str(path))
    doc.close()
    return path


LINK_RECT = pymupdf.Rect(38, 96, 200, 112)
WIDGET_RECT = pymupdf.Rect(35, 145, 220, 165)
TOC = [[1, "Chapter One", 1], [1, "Chapter Two", 2], [2, "Section", 2]]


def build_overlay_links_toc(path: Path) -> Path:
    doc = pymupdf.open()
    one = doc.new_page(width=300, height=300)
    one.insert_text((40, 50), "Chapter One", fontsize=16, fontname="hebo")
    one.insert_text((40, 108), "See the second chapter", fontsize=11, fontname="tiro")
    two = doc.new_page(width=300, height=300)
    one = doc[0]  # page objects are invalidated by new_page
    one.insert_link({"kind": pymupdf.LINK_GOTO, "from": LINK_RECT, "page": 1,
                     "to": pymupdf.Point(0, 0)})
    two.insert_text((40, 50), "Chapter Two", fontsize=16, fontname="hebo")
    two.insert_text((40, 100), "Section A", fontsize=13, fontname="hebo")
    two.insert_text((40, 160), "Field label text", fontsize=11, fontname="tiro")
    widget = pymupdf.Widget()
    widget.field_name = "answer"
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.rect = WIDGET_RECT
    widget.field_value = "x"
    two.add_widget(widget)
    two.insert_text((40, 220), "Plain body text on page two.", fontsize=11, fontname="tiro")
    doc.set_toc(TOC)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def styles_pdf(tmp_path: Path) -> Path:
    return build_overlay_styles(tmp_path / "overlay_styles.pdf")


@pytest.fixture
def no_text_pdf(tmp_path: Path) -> Path:
    return build_overlay_no_text(tmp_path / "overlay_no_text.pdf")


@pytest.fixture
def over_image_pdf(tmp_path: Path) -> Path:
    return build_overlay_over_image(tmp_path / "overlay_over_image.pdf")


@pytest.fixture
def links_toc_pdf(tmp_path: Path) -> Path:
    return build_overlay_links_toc(tmp_path / "overlay_links_toc.pdf")


def styles_blocks(styles_pdf: Path) -> List[OverlayBlock]:
    doc = pymupdf.open(str(styles_pdf))
    blocks = blocks_from_page(
        doc[0], 1, STYLE_TRANSLATIONS,
        kinds={
            STYLE_TEXTS["header"]: OverlayBlockKind.HEADER,
            STYLE_TEXTS["page_number"]: OverlayBlockKind.PAGE_NUMBER,
            STYLE_TEXTS["bold"]: OverlayBlockKind.HEADING,
            STYLE_TEXTS["footnote"]: OverlayBlockKind.FOOTNOTE,
        },
        alignments={
            STYLE_TEXTS["centred"]: OverlayAlignment.CENTER,
            STYLE_TEXTS["right"]: OverlayAlignment.RIGHT,
        },
        keep={
            STYLE_TEXTS["header"]: "header_footer",
            STYLE_TEXTS["page_number"]: "page_number",
            STYLE_TEXTS["super_mark"]: "no_letters",
        },
    )
    doc.close()
    return blocks


def page2_blocks() -> List[OverlayBlock]:
    doc = pymupdf.open(str(TEST_DOC))
    page = doc[1]
    kinds: Dict[str, OverlayBlockKind] = {}
    keep: Dict[str, str] = {}
    for group in group_lines(page):
        text = " ".join(" ".join(line["text"].split()) for line in group["lines"])
        if group["bbox"][1] < 60:
            kinds[text] = (
                OverlayBlockKind.PAGE_NUMBER if text.isdigit() else OverlayBlockKind.HEADER
            )
            keep[text] = "page_number" if text.isdigit() else "header_footer"
        elif text.startswith("Figure 1.12"):
            kinds[text] = OverlayBlockKind.CAPTION
        else:
            kinds[text] = OverlayBlockKind.FIGURE_LABEL
    alignments = {
        text: OverlayAlignment.CENTER
        for text, kind in kinds.items() if kind is OverlayBlockKind.FIGURE_LABEL
    }
    blocks = blocks_from_page(page, 2, PAGE2_TRANSLATIONS, kinds=kinds, alignments=alignments,
                              keep=keep)
    doc.close()
    return blocks


# --------------------------------------------------------------------------- #
# Harness helpers
# --------------------------------------------------------------------------- #


def masked_samples(path: Path, page_index: int, rects: Sequence[BBox], dpi: int = 150) -> bytes:
    """Page pixels at ``dpi`` with every rect (expanded by 1 pt) painted black."""
    doc = pymupdf.open(str(path))
    pixmap = doc[page_index].get_pixmap(dpi=dpi, alpha=False, annots=False)
    scale = dpi / 72.0
    for x0, y0, x1, y1 in rects:
        box = pymupdf.IRect(
            math.floor((x0 - 1) * scale), math.floor((y0 - 1) * scale),
            math.ceil((x1 + 1) * scale), math.ceil((y1 + 1) * scale),
        ) & pixmap.irect
        pixmap.set_rect(box, (0, 0, 0))
    samples = bytes(pixmap.samples)
    doc.close()
    return samples


def clip_text(path: Path, page_index: int, rect: BBox) -> str:
    doc = pymupdf.open(str(path))
    text = doc[page_index].get_text("text", clip=pymupdf.Rect(*rect))
    doc.close()
    return text


def normalise(text: str) -> str:
    return " ".join(text.split())


def substrings(text: str, length: int = 6) -> List[str]:
    clean = normalise(text)
    return [clean[i:i + length] for i in range(0, max(0, len(clean) - length + 1))]


def spans_in(path: Path, page_index: int, rect: BBox) -> List[Dict[str, Any]]:
    doc = pymupdf.open(str(path))
    found = [
        span
        for block in doc[page_index].get_text("dict", clip=pymupdf.Rect(*rect))["blocks"]
        if block["type"] == 0
        for line in block["lines"]
        for span in line["spans"]
        if span["text"].strip()
    ]
    doc.close()
    return found


def fonts_of(path: Path, page_index: int) -> List[str]:
    doc = pymupdf.open(str(path))
    names = [str(row[3]) for row in doc[page_index].get_fonts(full=False)]
    doc.close()
    return names


def counts(path: Path, page_index: int) -> Tuple[int, int, int]:
    doc = pymupdf.open(str(path))
    page = doc[page_index]
    result = (len(page.get_drawings()), len(page.get_images()), doc.page_count)
    doc.close()
    return result


def placed_rects(blocks: Sequence[OverlayBlock], artifact: OverlayArtifact) -> List[BBox]:
    kept = {
        entry.unit_id for entry in artifact.review
        if entry.reason is OverlayReviewReason.KEPT_ORIGINAL
    }
    return [b.bbox for b in blocks if b.translate and b.unit_id not in kept]


# --------------------------------------------------------------------------- #
# US-18: identity outside the blocks, residual glyphs, styles
# --------------------------------------------------------------------------- #


def test_styles_page_pixels_outside_the_blocks_are_identical(
    styles_pdf: Path, tmp_path: Path
) -> None:
    blocks = styles_blocks(styles_pdf)
    assert len([b for b in blocks if b.translate]) == 8
    out = tmp_path / "out"
    artifact = render(styles_pdf, blocks, out)
    assert artifact.pdf_path == out / OVERLAY_PDF_NAME and artifact.pdf_path.is_file()
    assert artifact.pages == 1 and artifact.could_not_fit == 0 and artifact.pages_skipped == ()
    assert artifact.placed == 8 and artifact.kept_original == 3
    rects = placed_rects(blocks, artifact)
    assert masked_samples(styles_pdf, 0, rects) == masked_samples(artifact.pdf_path, 0, rects)
    assert counts(styles_pdf, 0) == counts(artifact.pdf_path, 0) == (2, 0, 1)


def test_replaced_rects_hold_the_translation_and_no_source_substring(
    styles_pdf: Path, tmp_path: Path
) -> None:
    blocks = styles_blocks(styles_pdf)
    artifact = render(styles_pdf, blocks, tmp_path)
    for block in blocks:
        text = clip_text(artifact.pdf_path, 0, block.bbox)
        if block.translate:
            assert normalise(text) == normalise(block.translated_text or ""), block.source_text
            translated = normalise(block.translated_text or "")
            leaked = [s for s in substrings(block.source_text)
                      if s in normalise(text) and s not in translated]
            assert leaked == [], (block.source_text, text)
        else:  # untouched units keep their source text byte for byte (AC US-21/1)
            assert normalise(text) == normalise(block.source_text), block.source_text
    # the untouched superscript digit and the footnote both survive at their sizes
    footnote = [b for b in blocks if b.kind is OverlayBlockKind.FOOTNOTE][0]
    spans = spans_in(artifact.pdf_path, 0, footnote.bbox)
    assert spans and all(span["size"] <= 7.2 for span in spans), spans


def test_bold_italic_colour_and_alignment_follow_the_source(
    styles_pdf: Path, tmp_path: Path
) -> None:
    blocks = {b.source_text: b for b in styles_blocks(styles_pdf)}
    artifact = render(styles_pdf, list(blocks.values()), tmp_path)
    bold = spans_in(artifact.pdf_path, 0, blocks[STYLE_TEXTS["bold"]].bbox)
    assert bold and all("Bold" in span["font"] for span in bold), bold
    assert bold[0]["size"] == pytest.approx(14.0, abs=14.0 * 0.5) and bold[0]["size"] <= 14.01
    italic = spans_in(artifact.pdf_path, 0, blocks[STYLE_TEXTS["italic"]].bbox)
    assert italic and all("Italic" in span["font"] for span in italic), italic
    coloured = spans_in(artifact.pdf_path, 0, blocks[STYLE_TEXTS["coloured"]].bbox)
    source_colour = blocks[STYLE_TEXTS["coloured"]].style.color
    assert coloured and all(f"#{span['color']:06x}" == source_colour for span in coloured)
    assert source_colour != "#000000"
    centred = blocks[STYLE_TEXTS["centred"]]
    span_box = spans_in(artifact.pdf_path, 0, centred.bbox)[0]["bbox"]
    rect_centre = (centred.bbox[0] + centred.bbox[2]) / 2
    assert abs((span_box[0] + span_box[2]) / 2 - rect_centre) <= 2.0
    right = blocks[STYLE_TEXTS["right"]]
    right_box = spans_in(artifact.pdf_path, 0, right.bbox)[0]["bbox"]
    assert abs(right_box[2] - right.bbox[2]) <= 2.0  # flush with the source right edge
    # never enlarged: every replaced span is at most the source size
    for block in blocks.values():
        if block.translate:
            for span in spans_in(artifact.pdf_path, 0, block.bbox):
                assert span["size"] <= block.style.font_size + 0.01, block.source_text


def test_test_doc_page2_reproduces_the_poc_look(tmp_path: Path) -> None:
    blocks = page2_blocks()
    translated = [b for b in blocks if b.translate]
    assert len(translated) == 14 and {b.kind for b in translated} == {
        OverlayBlockKind.FIGURE_LABEL
    }
    pages = infos_of(TEST_DOC)
    artifact = render(TEST_DOC, blocks, tmp_path, pages=pages)
    assert artifact.pages == 3 and artifact.placed == 14 and artifact.could_not_fit == 0
    assert artifact.shrunk_below_threshold == 0  # R15: labels fit above 0.65 at 1.15 lh
    rects = placed_rects(blocks, artifact)
    for index in range(3):
        page_rects = rects if index == 1 else []
        assert masked_samples(TEST_DOC, index, page_rects) == masked_samples(
            artifact.pdf_path, index, page_rects
        ), index
        assert counts(TEST_DOC, index) == counts(artifact.pdf_path, index)
    assert counts(artifact.pdf_path, 1)[0] == 39
    # the PoC defect: blocks 13/14 hold only their Turkish text
    for label in ("13. 3D reconstruction", "14. Image-based rendering"):
        block = [b for b in blocks if b.source_text == label][0]
        text = normalise(clip_text(artifact.pdf_path, 1, block.bbox))
        assert text == PAGE2_TRANSLATIONS[label]
        assert "reconstruction" not in text and "rendering" not in text
    # R14: no collateral damage on the untouched neighbours (header, "3D (where?)", caption)
    assert not [e for e in artifact.review if e.reason is OverlayReviewReason.COLLATERAL_REDACTION]
    assert not [e for e in artifact.review if e.reason is OverlayReviewReason.RESIDUAL_GLYPHS]


# --------------------------------------------------------------------------- #
# Shrink policy (AC US-18/4, US-19, E-38)
# --------------------------------------------------------------------------- #


def shrink_fixture(tmp_path: Path) -> Tuple[Path, BBox]:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((40, 60), "Short label", fontsize=12, fontname="tiro")
    path = tmp_path / "shrink.pdf"
    doc.save(str(path))
    line = _lines(doc[0])[0]["bbox"]
    doc.close()
    return path, line


def test_scale_at_or_above_threshold_is_placed_silently(tmp_path: Path) -> None:
    path, rect = shrink_fixture(tmp_path)
    block = make_block(1, 0, rect, "Short label", "Kısa etiket", size=12.0)
    artifact = render(path, [block], tmp_path / "a")
    assert artifact.placed == 1 and artifact.shrunk_below_threshold == 0
    assert artifact.review == () and artifact.could_not_fit == 0
    review = json.loads(artifact.review_path.read_text(encoding="utf-8"))
    assert review["entries"] == [] and review["counts"]["placed"] == 1


def test_scale_below_threshold_lands_in_the_review_list(tmp_path: Path) -> None:
    path, rect = shrink_fixture(tmp_path)
    longer = "Biraz daha uzun etiket"  # ~2x the source width: fits only when shrunk
    block = make_block(1, 0, rect, "Short label", longer, size=12.0)
    artifact = render(path, [block], tmp_path / "b", review_threshold=0.9, floor_scale=0.5)
    assert artifact.placed == 1 and artifact.shrunk_below_threshold == 1
    assert artifact.could_not_fit == 0
    entry = artifact.review[0]
    assert entry.reason is OverlayReviewReason.SHRUNK_BELOW_THRESHOLD
    assert entry.scale is not None and 0.5 <= entry.scale < 0.9
    assert entry.page == 1 and entry.block_id == "p001-b000" and entry.unit_id == block.unit_id
    assert entry.source_text == "Short label" and entry.translated_text == longer
    assert normalise(clip_text(artifact.pdf_path, 0, rect)) == longer
    # with the default threshold the same placement is silent (AC US-19/2)
    quiet = render(path, [block], tmp_path / "c", review_threshold=0.5, floor_scale=0.5)
    assert quiet.review == () and quiet.shrunk_below_threshold == 0


def test_impossible_fit_gets_an_ellipsis_and_could_not_fit(tmp_path: Path) -> None:
    path, rect = shrink_fixture(tmp_path)
    huge = " ".join(f"sözcük{i}" for i in range(60))
    block = make_block(1, 0, rect, "Short label", huge, size=12.0)
    artifact = render(path, [block], tmp_path / "d")
    assert artifact.could_not_fit == 1 and artifact.placed == 1
    entry = [e for e in artifact.review if e.reason is OverlayReviewReason.COULD_NOT_FIT][0]
    # the prefix is placed at the floor or the (larger) scale it happens to fit at
    assert entry.note == "ellipsis" and entry.scale is not None and 0.5 <= entry.scale <= 0.65
    assert entry.translated_text is not None and entry.translated_text.endswith("…")
    placed = normalise(clip_text(artifact.pdf_path, 0, rect))
    assert placed.endswith("…") and huge.startswith(placed[:-2].strip())
    assert "Short" not in placed  # never overflowing, never leaving the source behind
    # nothing spilled outside the rectangle
    spill = clip_text(artifact.pdf_path, 0, (0, rect[3] + 0.5, 300, 200))
    assert spill.strip() == ""


def test_floor_above_threshold_is_a_user_error(tmp_path: Path) -> None:
    path, rect = shrink_fixture(tmp_path)
    block = make_block(1, 0, rect, "Short label", "Kısa", size=12.0)
    result = OverlayRenderer().render(
        path, infos_of(path), by_page([block]), {}, tmp_path,
        OverlayRenderOptions(font=BUILTIN_FONT, review_threshold=0.6, floor_scale=0.7),
    )
    assert isinstance(result, Err) and result.error.scope is ErrorScope.USER
    assert not (tmp_path / OVERLAY_PDF_NAME).exists()


# --------------------------------------------------------------------------- #
# Glyph coverage (NFR-25, AC US-18/6)
# --------------------------------------------------------------------------- #


def test_turkish_letters_survive_in_serif_sans_and_mono(tmp_path: Path) -> None:
    probe = "çğıİöşüÇĞİÖŞÜ"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    for index, _family in enumerate(("serif", "sans", "mono")):
        page.insert_text((30, 40 + 40 * index), f"line {index} of latin text", fontsize=12,
                         fontname="tiro")
    path = tmp_path / "glyphs.pdf"
    doc.save(str(path))
    lines = _lines(doc[0])
    doc.close()
    blocks = [
        make_block(1, index, line["bbox"], line["text"], f"{probe} {index}", size=12.0,
                   family=family)
        for index, (line, family) in enumerate(zip(lines, ("serif", "sans", "mono"), strict=True))
    ]
    artifact = render(path, blocks, tmp_path)
    assert artifact.placed == 3
    assert not [e for e in artifact.review if e.reason is OverlayReviewReason.GLYPH_MISSING]
    for block in blocks:
        assert probe in clip_text(artifact.pdf_path, 0, block.bbox)
    families = {name.split("+", 1)[-1].split(" ")[0] for name in fonts_of(artifact.pdf_path, 0)}
    assert {"Charis", "Nimbus"} <= families or {"CharisSIL", "NimbusSans"} <= families, families


def test_font_lacking_a_glyph_is_reported_as_glyph_missing(
    styles_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _placement.pdfkit.page_plain_text

    def without_g(page: Any, clip: Any = None) -> str:  # a font whose ``ğ`` never renders
        return real(page, clip).replace("ğ", "").replace("Ğ", "")

    monkeypatch.setattr(_placement.pdfkit, "page_plain_text", without_g)
    blocks = [b for b in styles_blocks(styles_pdf) if b.source_text == STYLE_TEXTS["bold"]]
    blocks = [dataclasses.replace(blocks[0], translated_text="Dağ başı")]
    artifact = render(styles_pdf, blocks, tmp_path)
    entries = [e for e in artifact.review if e.reason is OverlayReviewReason.GLYPH_MISSING]
    assert len(entries) == 1 and entries[0].note == "missing ğ"
    assert entries[0].block_id == blocks[0].block_id and artifact.placed == 1


def test_user_font_is_used_once_subset_and_validated(tmp_path: Path, styles_pdf: Path) -> None:
    font_file = tmp_path / "tiro.otf"
    font_file.write_bytes(pymupdf.Font("tiro").buffer)  # Nimbus Roman: not an engine default
    spec = unwrap(resolve_font(font_file))
    blocks = styles_blocks(styles_pdf)
    artifact = render(styles_pdf, blocks, tmp_path / "out", font=spec)
    assert artifact.placed == 8
    names = fonts_of(artifact.pdf_path, 0)
    roman = [n for n in names if "NimbusRoman" in n.replace(" ", "") or "Nimbus Roman" in n]
    assert roman and all(n.split("+")[0].isupper() and "+" in n for n in roman), names
    assert not any("Charis" in n for n in names), names
    assert "Kalın başlık metni" in normalise(clip_text(artifact.pdf_path, 0, blocks[2].bbox))


# --------------------------------------------------------------------------- #
# Images, skipped pages, partial units (E-40, E-47, FR-48)
# --------------------------------------------------------------------------- #


def test_text_over_an_image_keeps_the_image_pixels(over_image_pdf: Path, tmp_path: Path) -> None:
    doc = pymupdf.open(str(over_image_pdf))
    blocks = blocks_from_page(
        doc[0], 1, {OVER_IMAGE_TEXT: "Fotoğrafın içindeki altyazı",
                    "Prose below the image.": "Görselin altındaki düz yazı."},
    )
    doc.close()
    caption = [b for b in blocks if b.source_text == OVER_IMAGE_TEXT][0]
    caption = dataclasses.replace(caption, over_image=True)
    blocks = [caption if b.unit_id == caption.unit_id else b for b in blocks]
    artifact = render(over_image_pdf, blocks, tmp_path)
    assert artifact.placed == 2 and counts(artifact.pdf_path, 0)[1] == 1
    out = pymupdf.open(str(artifact.pdf_path))
    pixmap = out[0].get_pixmap(dpi=100, clip=pymupdf.Rect(*caption.bbox), alpha=False)
    mean = sum(pixmap.samples) / len(pixmap.samples)
    assert mean < 200, mean  # the grey photo is still under the caption (not blanked)
    rects = placed_rects(blocks, artifact)
    assert masked_samples(over_image_pdf, 0, rects) == masked_samples(artifact.pdf_path, 0, rects)
    entries = [e for e in artifact.review if e.reason is OverlayReviewReason.OVER_IMAGE]
    assert [e.block_id for e in entries] == [caption.block_id]
    assert normalise(clip_text(artifact.pdf_path, 0, caption.bbox)) == caption.translated_text


def test_page_without_text_layer_is_copied_and_listed(no_text_pdf: Path, tmp_path: Path) -> None:
    doc = pymupdf.open(str(no_text_pdf))
    blocks = blocks_from_page(doc[0], 1, {"Text page before the scan.": "Taramadan önceki sayfa."})
    pages = page_infos(doc, skip={2: "no_text_layer"})
    doc.close()
    artifact = render(no_text_pdf, blocks, tmp_path, pages=pages)
    assert artifact.pages_skipped == (2,) and artifact.pages == 2 and artifact.placed == 1
    assert masked_samples(no_text_pdf, 1, []) == masked_samples(artifact.pdf_path, 1, [])
    review = json.loads(artifact.review_path.read_text(encoding="utf-8"))
    assert review["pages_skipped"] == [2] and review["counts"]["pages_skipped"] == 1


def test_partial_export_keeps_unfinished_units_and_lists_them(
    styles_pdf: Path, tmp_path: Path
) -> None:
    blocks = styles_blocks(styles_pdf)
    failed = dataclasses.replace(
        blocks[2], status=ChunkStatus.FAILED, translated_text=None, last_error="boom"
    )
    pending = dataclasses.replace(blocks[3], status=ChunkStatus.PENDING, translated_text=None)
    blocks = [failed, pending, *blocks[4:]] + blocks[:2]
    artifact = render(styles_pdf, blocks, tmp_path, allow_partial=True)
    assert artifact.placed == 6 and artifact.kept_original == 5
    partial = [e for e in artifact.review if e.note == "partial"]
    assert {e.block_id for e in partial} == {failed.block_id, pending.block_id}
    assert all(e.reason is OverlayReviewReason.KEPT_ORIGINAL for e in partial)
    for block in (failed, pending):
        assert normalise(clip_text(artifact.pdf_path, 0, block.bbox)) == block.source_text
    # expected keep reasons are counted, not listed; the superscript digit is not either
    assert not [e for e in artifact.review if e.note in {"page_number", "header_footer",
                                                          "no_letters"}]


def test_headers_are_replaced_only_with_translate_headers(styles_pdf: Path, tmp_path: Path) -> None:
    blocks = styles_blocks(styles_pdf)
    header = [b for b in blocks if b.kind is OverlayBlockKind.HEADER][0]
    number = [b for b in blocks if b.kind is OverlayBlockKind.PAGE_NUMBER][0]
    translated_header = dataclasses.replace(
        header, translate=True, keep_reason=None, translated_text="Sayfa üstbilgisi"
    )
    translated_number = dataclasses.replace(
        number, translate=True, keep_reason=None, translated_text="8"
    )
    others = [b for b in blocks if b.unit_id not in {header.unit_id, number.unit_id}]
    kept = render(styles_pdf, [translated_header, translated_number, *others], tmp_path / "k")
    assert normalise(clip_text(kept.pdf_path, 0, header.bbox)) == header.source_text
    on = render(styles_pdf, [translated_header, translated_number, *others], tmp_path / "t",
                translate_headers=True)
    assert normalise(clip_text(on.pdf_path, 0, header.bbox)) == "Sayfa üstbilgisi"
    assert normalise(clip_text(on.pdf_path, 0, number.bbox)) == "7"  # page numbers: never


def test_empty_translation_and_review_flag_are_reported(styles_pdf: Path, tmp_path: Path) -> None:
    blocks = styles_blocks(styles_pdf)
    empty = dataclasses.replace(blocks[2], translated_text="   ")
    flagged = dataclasses.replace(blocks[3], review_flag=True)
    artifact = render(styles_pdf, [empty, flagged, *blocks[4:]], tmp_path)
    reasons = {(e.block_id, e.reason) for e in artifact.review}
    assert (empty.block_id, OverlayReviewReason.PROVIDER_EMPTY) in reasons
    assert (flagged.block_id, OverlayReviewReason.PROVIDER_EMPTY) in reasons
    assert normalise(clip_text(artifact.pdf_path, 0, empty.bbox)) == empty.source_text
    assert normalise(clip_text(artifact.pdf_path, 0, flagged.bbox)) == flagged.translated_text
    fragment = dataclasses.replace(blocks[4], fragment=True)
    again = render(styles_pdf, [fragment], tmp_path / "f")
    assert OverlayReviewReason.FRAGMENT in [e.reason for e in again.review]


# --------------------------------------------------------------------------- #
# US-21: links, widgets, outline, metadata; E-50 redaction annotations
# --------------------------------------------------------------------------- #


def links_toc_blocks(path: Path) -> List[OverlayBlock]:
    doc = pymupdf.open(str(path))
    translations = {
        "Chapter One": "Birinci Bölüm", "See the second chapter": "İkinci bölüme bakınız",
        "Chapter Two": "İkinci Bölüm", "Section A": "Kesim A",
        "Field label text": "Alan etiketi metni",
        "Plain body text on page two.": "İkinci sayfada düz gövde metni.",
    }
    kinds = {
        "Chapter One": OverlayBlockKind.HEADING, "Chapter Two": OverlayBlockKind.HEADING,
        "Section A": OverlayBlockKind.HEADING,
    }
    blocks = blocks_from_page(doc[0], 1, translations, kinds=kinds)
    blocks += blocks_from_page(doc[1], 2, translations, kinds=kinds)
    doc.close()
    return blocks


def test_links_survive_widgets_keep_their_unit_and_outline_is_retitled_one_to_one(
    links_toc_pdf: Path, tmp_path: Path
) -> None:
    blocks = links_toc_blocks(links_toc_pdf)
    outline_map = {
        (b.page, b.source_text): b.translated_text or ""
        for b in blocks if b.kind is OverlayBlockKind.HEADING
    }
    artifact = render(links_toc_pdf, blocks, tmp_path, outline_map=outline_map,
                      title="Çeviri", author="Yazar")
    out = pymupdf.open(str(artifact.pdf_path))
    links = out[0].get_links()
    assert len(links) == 1 and links[0]["kind"] == pymupdf.LINK_GOTO and links[0]["page"] == 1
    assert pdfkit.rect_tuple(links[0]["from"]) == pytest.approx(pdfkit.rect_tuple(LINK_RECT))
    assert "links_reinserted:1" in artifact.warnings
    assert normalise(clip_text(artifact.pdf_path, 0, blocks[1].bbox)) == "İkinci bölüme bakınız"
    widgets = list(out[1].widgets())
    assert len(widgets) == 1 and widgets[0].field_name == "answer"
    field = [b for b in blocks if b.source_text == "Field label text"][0]
    entry = [e for e in artifact.review if e.block_id == field.block_id][0]
    assert entry.reason is OverlayReviewReason.KEPT_ORIGINAL and entry.note == "widget_overlap"
    field_text = normalise(clip_text(artifact.pdf_path, 1, field.bbox))
    assert field_text.startswith("Field label text") and "Alan" not in field_text
    assert out.get_toc() == [[1, "Birinci Bölüm", 1], [1, "İkinci Bölüm", 2], [2, "Section", 2]]
    assert "outline_retitled:2" in artifact.warnings
    assert out.language == "tr"
    meta = out.metadata
    assert meta["title"] == "Çeviri" and meta["author"] == "Yazar"
    assert meta["producer"].startswith("book-translator ") and meta["modDate"].startswith("D:")
    out.close()


def test_existing_author_is_preserved_when_not_overridden(
    links_toc_pdf: Path, tmp_path: Path
) -> None:
    doc = pymupdf.open(str(links_toc_pdf))
    doc.set_metadata({"title": "Original", "author": "Someone"})
    doc.save(str(tmp_path / "meta.pdf"))
    doc.close()
    artifact = render(tmp_path / "meta.pdf", links_toc_blocks(tmp_path / "meta.pdf"), tmp_path)
    out = pymupdf.open(str(artifact.pdf_path))
    assert out.metadata["author"] == "Someone" and out.metadata["title"] == "Original"
    out.close()


def test_pre_existing_redaction_annotations_are_kept_and_not_applied(
    styles_pdf: Path, tmp_path: Path
) -> None:
    doc = pymupdf.open(str(styles_pdf))
    blocks = styles_blocks(styles_pdf)
    footnote = [b for b in blocks if b.kind is OverlayBlockKind.FOOTNOTE][0]
    doc[0].add_redact_annot(pymupdf.Rect(*footnote.bbox))  # a user's own, unapplied redaction
    path = tmp_path / "with_redact.pdf"
    doc.save(str(path))
    doc.close()
    untouched = [dataclasses.replace(footnote, translate=False, keep_reason="figure_text",
                                     translated_text=footnote.source_text)]
    others = [b for b in blocks if b.unit_id != footnote.unit_id]
    artifact = render(path, others + untouched, tmp_path)
    assert "redact_annots_preserved:1" in artifact.warnings
    out = pymupdf.open(str(artifact.pdf_path))
    page = out[0]  # keep the page alive while its annotation objects are in use
    annots = [a for a in page.annots() if a.type[0] == pymupdf.PDF_ANNOT_REDACT]
    assert len(annots) == 1
    assert pdfkit.rect_tuple(annots[0].rect) == pytest.approx(footnote.bbox, abs=0.5)
    assert normalise(clip_text(artifact.pdf_path, 0, footnote.bbox)) == footnote.source_text
    out.close()


def test_encrypted_or_no_modify_pdf_is_refused(styles_pdf: Path, tmp_path: Path) -> None:
    doc = pymupdf.open(str(styles_pdf))
    locked = tmp_path / "locked.pdf"
    doc.save(str(locked), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner",
             user_pw="", permissions=pymupdf.PDF_PERM_PRINT)
    doc.close()
    result = OverlayRenderer().render(
        locked, infos_of(locked), by_page(styles_blocks(styles_pdf)), {},
        tmp_path, OverlayRenderOptions(font=BUILTIN_FONT),
    )
    assert isinstance(result, Err) and result.error.code is ErrorCode.OVERLAY_PDF_RESTRICTED
    assert result.error.scope is ErrorScope.USER and not (tmp_path / OVERLAY_PDF_NAME).exists()


# --------------------------------------------------------------------------- #
# NFR-24 size, review file schema (7.2)
# --------------------------------------------------------------------------- #


def test_overlay_of_test_doc_is_small_and_embeds_one_subset_family(tmp_path: Path) -> None:
    blocks = page2_blocks()
    artifact = render(TEST_DOC, blocks, tmp_path, pages=infos_of(TEST_DOC))
    source_size = TEST_DOC.stat().st_size
    assert artifact.bytes_written == artifact.pdf_path.stat().st_size
    assert artifact.bytes_written <= 1.5 * source_size + 2 * 1024 * 1024
    names = fonts_of(artifact.pdf_path, 1)
    new_faces = {n for n in names if "Charis" in n}
    assert new_faces and all("+" in n for n in new_faces), names  # every new face is subset
    families = {n.split("+", 1)[-1].rsplit(" ", 1)[0] for n in new_faces}
    assert families == {"Charis SIL"} and len(new_faces) <= 3, names


def test_review_file_schema_and_sorted_entries(styles_pdf: Path, tmp_path: Path) -> None:
    blocks = styles_blocks(styles_pdf)
    tweaked = [
        dataclasses.replace(blocks[5], fragment=True),
        dataclasses.replace(blocks[4], translated_text="x " * 300),  # could_not_fit
        dataclasses.replace(blocks[3], review_flag=True),
        *[b for b in blocks if b.index_on_page not in {3, 4, 5}],
    ]
    artifact = render(styles_pdf, tweaked, tmp_path)
    assert artifact.review_path == tmp_path / OVERLAY_REVIEW_NAME
    review = json.loads(artifact.review_path.read_text(encoding="utf-8"))
    assert review["version"] == 1 and review["generated_at"].endswith("+00:00")
    assert review["thresholds"] == {"review_threshold": 0.65, "floor_scale": 0.5}
    assert set(review["counts"]) == {
        "placed", "shrunk_below_threshold", "could_not_fit", "kept_original", "pages_skipped",
        "entries",
    }
    assert review["counts"]["could_not_fit"] == 1 == artifact.could_not_fit
    assert review["counts"]["entries"] == len(artifact.review) == len(review["entries"])
    keys = [(e["page"], e["block_id"], e["reason"]) for e in review["entries"]]
    assert keys == sorted(keys) and [e["block_id"] for e in review["entries"]] == [
        "p001-b003", "p001-b004", "p001-b005"
    ]
    assert [e["reason"] for e in review["entries"]] == [
        "provider_empty", "could_not_fit", "fragment"
    ]
    for entry in review["entries"]:
        assert set(entry) == {"page", "block_id", "unit_id", "reason", "scale", "source_text",
                              "translated_text", "note", "shared_scale"}
    assert all(e.reason.value in {"provider_empty", "could_not_fit", "fragment"}
               for e in artifact.review)
    # the review file is written after the PDF: both exist and agree on the counts
    assert artifact.pdf_path.stat().st_mtime <= artifact.review_path.stat().st_mtime + 0.001


# --------------------------------------------------------------------------- #
# Translated figure PNG (figure_overlay)
# --------------------------------------------------------------------------- #


def page2_replacements() -> List[LabelReplacement]:
    return [
        LabelReplacement(
            bbox=b.bbox, text=b.translated_text or "", font_size=b.style.font_size,
            bold=b.style.bold, italic=b.style.italic, family=b.style.family,
            color=b.style.color, alignment=b.alignment,
        )
        for b in page2_blocks()
        if b.translate and b.kind is OverlayBlockKind.FIGURE_LABEL
    ]


def test_render_translated_figure_returns_the_region_with_turkish_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacements = page2_replacements()
    assert len(replacements) == 14
    before = hashlib.sha256(TEST_DOC.read_bytes()).hexdigest()
    seen: Dict[str, Any] = {}
    real_pixmap = figure_overlay_module.pdfkit.page_pixmap_png

    def spy(page: Any, dpi: int, clip: Any = None) -> Any:  # peek at the temp doc's page
        seen["texts"] = [
            normalise(page.get_text("text", clip=pymupdf.Rect(*r.bbox))) for r in replacements
        ]
        seen["page_text"] = normalise(page.get_text("text"))
        return real_pixmap(page, dpi, clip)

    monkeypatch.setattr(figure_overlay_module.pdfkit, "page_pixmap_png", spy)
    for dpi in (72, 200):
        png = unwrap(render_translated_figure(TEST_DOC, 2, FIGURE_REGION, replacements, dpi,
                                              BUILTIN_FONT))
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        pixmap = pymupdf.Pixmap(png)
        expected_w = round((FIGURE_REGION[2] - FIGURE_REGION[0]) * dpi / 72)
        expected_h = round((FIGURE_REGION[3] - FIGURE_REGION[1]) * dpi / 72)
        assert abs(pixmap.width - expected_w) <= 2 and abs(pixmap.height - expected_h) <= 2
    assert seen["texts"] == [r.text for r in replacements]
    assert "Image formation" not in seen["page_text"] and "rendering" not in seen["page_text"]
    assert "3D (where?)" in seen["page_text"]  # the untouched label is still there
    assert "Figure 1.12" in seen["page_text"]  # so is the caption
    assert hashlib.sha256(TEST_DOC.read_bytes()).hexdigest() == before  # source never modified
    _png, stats = unwrap(render_translated_figure_with_stats(
        TEST_DOC, 2, FIGURE_REGION, replacements, 100, BUILTIN_FONT
    ))
    assert len(stats.scales) == 14 and all(0.65 <= s <= 1.0 for s in stats.scales), stats
    assert not any(stats.truncated)


def test_render_translated_figure_rejects_bad_page_and_dpi(tmp_path: Path) -> None:
    bad_page = render_translated_figure(TEST_DOC, 9, FIGURE_REGION, [], 100, BUILTIN_FONT)
    assert isinstance(bad_page, Err) and bad_page.error.scope is ErrorScope.USER
    bad_dpi = render_translated_figure(TEST_DOC, 2, FIGURE_REGION, [], 20, BUILTIN_FONT)
    assert isinstance(bad_dpi, Err) and "DPI" in bad_dpi.error.message
    missing = render_translated_figure(tmp_path / "nope.pdf", 1, FIGURE_REGION, [], 100,
                                       BUILTIN_FONT)
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.INPUT_UNREADABLE


# --------------------------------------------------------------------------- #
# Paragraph split, justification, first-line indent and superscript markers
# --------------------------------------------------------------------------- #

PARAGRAPHS = (
    "The first paragraph of this fixture is long enough to wrap onto several lines of the "
    "column so that the justified right edge can be measured by the alignment detection.",
    "A second paragraph follows without any vertical gap and is told apart only by the "
    "indent of its first line, exactly like the body text of a typeset book; this one "
    "carries a note marker.<sup>9</sup>",
    "The third paragraph closes the fixture and it also wraps over more than two lines so "
    "that every unit of the page comes back as a justified block with a short last line.",
)
PARAGRAPH_TRANSLATIONS = (
    "Bu demirbaşın ilk paragrafı, sütunun birkaç satırına yayılacak kadar uzundur; böylece "
    "iki yana yaslı sağ kenar hizalama tespiti tarafından ölçülebilir.",
    "İkinci paragraf hiçbir dikey boşluk olmadan gelir ve yalnızca ilk satır girintisiyle "
    "ayırt edilir, tıpkı dizilmiş bir kitabın gövde metni gibi; bunda bir not işareti "
    "vardır.\u2079",
    "Üçüncü paragraf demirbaşı kapatır ve o da ikiden fazla satıra yayılır; böylece sayfanın "
    "her birimi kısa son satırlı iki yana yaslı bir blok olarak geri gelir.",
)
PARAGRAPH_RECT: BBox = (100.0, 100.0, 420.0, 330.0)
PARAGRAPH_INDENT = 16.0


def build_overlay_paragraphs(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=520, height=700)
    body = "".join(
        f'<p style="margin:0;text-indent:{PARAGRAPH_INDENT:g}pt">{text}</p>'
        for text in PARAGRAPHS
    )
    html = (
        '<div style="font-family:serif;font-size:10.5pt;line-height:1.2;text-align:justify">'
        f"{body}</div>"
    )
    spare, _scale = page.insert_htmlbox(pymupdf.Rect(*PARAGRAPH_RECT), html)
    assert spare >= 0
    doc.save(str(path))
    doc.close()
    return path


def _line_boxes_in(path: Path, rect: BBox) -> List[BBox]:
    doc = pymupdf.open(str(path))
    boxes: List[BBox] = [
        (line["bbox"][0], line["bbox"][1], line["bbox"][2], line["bbox"][3])
        for block in doc[0].get_text("dict", clip=pymupdf.Rect(*rect))["blocks"]
        if block["type"] == 0
        for line in block["lines"]
        if "".join(span["text"] for span in line["spans"]).strip()
    ]
    doc.close()
    return sorted(boxes, key=lambda b: (round(b[1], 1), b[0]))


def test_three_indented_justified_paragraphs_round_trip(tmp_path: Path) -> None:
    from book_translator.extractors.overlay_extractor import OverlayOptions, extract_overlay

    source = build_overlay_paragraphs(tmp_path / "paragraphs.pdf")
    extraction = unwrap(extract_overlay(source, OverlayOptions(), {}))
    units = [b for b in extraction.blocks if b.kind is OverlayBlockKind.BODY]
    assert len(extraction.blocks) == 3 and len(units) == 3  # one unit per paragraph
    assert all(b.alignment is OverlayAlignment.JUSTIFY for b in units)
    assert all(len(b.line_boxes) >= 3 for b in units)
    source_indents = [b.line_boxes[0][0] - b.bbox[0] for b in units]
    assert all(indent == pytest.approx(PARAGRAPH_INDENT, abs=1.0) for indent in source_indents)
    # superscript marker: a Unicode superscript digit in the source text
    assert units[1].source_text.endswith("marker.\u2079") and "9" not in units[1].source_text
    assert "\u2079" not in units[0].source_text and "\u2079" not in units[2].source_text

    translated = [
        dataclasses.replace(block, status=ChunkStatus.COMPLETED, translated_text=text)
        for block, text in zip(units, PARAGRAPH_TRANSLATIONS, strict=True)
    ]
    artifact = render(source, translated, tmp_path / "out")
    target = artifact.pdf_path
    assert artifact.placed == 3 and artifact.could_not_fit == 0
    assert not any("glyph_missing" in entry.reason for entry in artifact.review)
    for block in translated:
        lines = _line_boxes_in(target, block.bbox)
        assert len(lines) >= 2
        # first-line indent preserved (+- 1 pt); insert_htmlbox insets every line of every
        # block by a constant 1 pt, hence 1.5 pt on the absolute first-line x0
        source_indent = block.line_boxes[0][0] - block.line_boxes[1][0]
        assert lines[0][0] - lines[1][0] == pytest.approx(source_indent, abs=1.0)
        assert lines[0][0] == pytest.approx(block.line_boxes[0][0], abs=1.5)
        # ... and the following lines start at the left edge of the paragraph
        assert all(box[0] == pytest.approx(block.bbox[0], abs=1.5) for box in lines[1:])
        # justified: every line but the last reaches the right edge of the rect
        assert all(block.bbox[2] - box[2] <= 3.0 for box in lines[:-1])
    # the marker comes back as a superscript "9" at the sentence end, not as body text
    marks = [sp for sp in spans_in(target, 0, translated[1].bbox) if sp["text"].strip() == "9"]
    assert len(marks) == 1 and marks[0]["flags"] & 1
    assert marks[0]["size"] < translated[1].style.font_size
    assert "\u2079" not in clip_text(target, 0, translated[1].bbox)


def test_build_html_indent_and_superscript() -> None:
    spec = _placement.PlacementSpec(
        rect=(0.0, 0.0, 200.0, 50.0), text="konumland\u0131rd\u0131m.\u00b9\u2070 Sonra <b>",
        font_size=10.5, bold=False, italic=False, family="serif", color="#000000",
        alignment=OverlayAlignment.JUSTIFY, first_line_indent_pt=15.9,
    )
    html = _placement.build_html(spec, BUILTIN_FONT)
    assert "text-indent:15.9pt;" in html and "text-align:justify" in html
    assert "m.<sup>10</sup> Sonra &lt;b&gt;" in html
    plain = dataclasses.replace(spec, first_line_indent_pt=0.0)
    assert "text-indent" not in _placement.build_html(plain, BUILTIN_FONT)
    # indent from the line boxes: first line only, the rest at the left edge of the rect
    rect: BBox = (100.0, 0.0, 400.0, 40.0)
    assert _placement.first_line_indent(
        rect, [(116.0, 0.0, 400.0, 12.0), (100.0, 13.0, 400.0, 25.0)]
    ) == pytest.approx(16.0)
    assert _placement.first_line_indent(rect, [(116.0, 0.0, 400.0, 12.0)]) == 0.0
    assert _placement.first_line_indent(
        rect, [(150.0, 0.0, 350.0, 12.0), (100.0, 13.0, 400.0, 25.0), (160.0, 26.0, 340.0, 38.0)]
    ) == 0.0  # centred lines are not an indent



# --------------------------------------------------------------------------- #
# v1.1 review: shared per-page scale, CR-52 (measure before redacting), CR-54, CR-67,
# CR-50 (outline), CR-53 (rotated pages), CR-41 (list write-back), CR-61 (figure batch)
# --------------------------------------------------------------------------- #

PARAGRAPH_RECTS: List[BBox] = [(40.0, 40.0 + i * 70.0, 300.0, 100.0 + i * 70.0) for i in range(3)]
SENTENCE = "Bu paragraf kutusuna yerleştirilen çeviri metninin bir cümlesidir. "


def paragraphs_pdf(tmp_path: Path, count: int = 3) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=340, height=60 + count * 70)
    for i in range(count):
        page.insert_textbox(pymupdf.Rect(40, 40 + i * 70, 300, 100 + i * 70),
                            f"Source paragraph {i} " + "with some English words " * 4,
                            fontsize=10, fontname="tiro")
    path = tmp_path / "paragraphs.pdf"
    doc.save(str(path))
    doc.close()
    return path


def paragraph_blocks(texts: Sequence[str], **extra: Any) -> List[OverlayBlock]:
    rects = [(40.0, 40.0 + i * 70.0, 300.0, 100.0 + i * 70.0) for i in range(len(texts))]
    return [
        make_block(1, i, rects[i], f"Source paragraph {i}", text, size=10.0,
                   alignment=OverlayAlignment.JUSTIFY, **extra)
        for i, text in enumerate(texts)
    ]


def span_sizes(path: Path, rect: BBox) -> List[float]:
    return sorted({round(float(s["size"]), 2) for s in spans_in(path, 0, rect)})


CLOSE_TEXTS = [SENTENCE * 4 + SENTENCE[: len(SENTENCE) // 2], SENTENCE * 5, SENTENCE * 6]
"""Three translations whose natural scales lie within the CR-95 cap of each other."""


def test_equal_style_paragraphs_share_one_font_size(tmp_path: Path) -> None:
    """Three paragraphs of one style, translations of different length: one size.

    The lengths are within the CR-95 cap (no unit loses more than 10 % of its own scale),
    so the whole group shares; ``test_shared_scale_cap_*`` covers the other case."""
    path = paragraphs_pdf(tmp_path)
    blocks = paragraph_blocks(CLOSE_TEXTS)
    artifact = render(path, blocks, tmp_path / "uniform")
    assert artifact.placed == 3 and artifact.could_not_fit == 0
    sizes = [span_sizes(artifact.pdf_path, b.bbox) for b in blocks]
    assert sizes[0] == sizes[1] == sizes[2] and len(sizes[0]) == 1
    assert sizes[0][0] < 10.0  # the longest one needed shrinking; all follow
    reported = {round(p.font_size, 3) for p in artifact.placements}
    assert len(reported) == 1 and reported.pop() == pytest.approx(sizes[0][0], abs=0.02)
    assert len({p.shared_scale for p in artifact.placements}) == 1
    assert artifact.placements[0].shared_scale is not None
    # without the shared scale every rectangle is scaled on its own
    own = render(path, blocks, tmp_path / "own", uniform_scale=False)
    own_sizes = [span_sizes(own.pdf_path, b.bbox)[0] for b in blocks]
    assert len(set(own_sizes)) > 1 and all(p.shared_scale is None for p in own.placements)


def test_outlier_keeps_its_own_scale_and_is_reviewed(tmp_path: Path) -> None:
    """One bad box never drags the page down: it keeps its own scale + review entry."""
    path = paragraphs_pdf(tmp_path, 4)
    blocks = paragraph_blocks([SENTENCE * 3, SENTENCE * 3, SENTENCE * 3, SENTENCE * 9])
    artifact = render(path, blocks, tmp_path, review_threshold=0.8, floor_scale=0.5)
    by_unit = {p.unit_id: p for p in artifact.placements}
    normal = [by_unit[b.unit_id] for b in blocks[:3]]
    outlier = by_unit[blocks[3].unit_id]
    assert len({round(p.font_size, 3) for p in normal}) == 1
    assert all(p.scale >= 0.8 for p in normal)
    assert outlier.shared_scale is None and outlier.scale < min(p.scale for p in normal) - 0.1
    assert OverlayReviewReason.SHRUNK_BELOW_THRESHOLD in outlier.reasons
    assert [e.unit_id for e in artifact.review] == [blocks[3].unit_id]
    assert artifact.shrunk_below_threshold == 1


def _plan_of(natural: float) -> _placement.PlacementPlan:
    """A measured plan with a chosen natural scale; the text is short enough that the
    refine rounds of ``_share_scales`` measure 1.0 and leave the factor alone."""
    spec = _placement.PlacementSpec(
        rect=(0.0, 0.0, 300.0, 120.0), text="x", font_size=10.0, bold=False, italic=False,
        family="serif", color="#000000", alignment=OverlayAlignment.LEFT,
    )
    return _placement.PlacementPlan(spec, spec.text, False, natural)


def _share(
    naturals: Sequence[float],
    review_threshold: float = 0.65,
    max_drop: float = _placement.SHARED_MAX_DROP,
) -> List[_placement.PlacementPlan]:
    plans = [_plan_of(value) for value in naturals]
    scratch = pymupdf.open()
    try:
        _placement._share_scales(scratch, plans, ["g"] * len(plans), BUILTIN_FONT, 0.5,
                                 None, review_threshold, max_drop)
    finally:
        scratch.close()
    return plans


def test_shared_scale_cap_keeps_a_comfortable_unit_at_its_own_scale() -> None:
    """CR-95: a unit is never placed more than ``SHARED_MAX_DROP`` below the size it
    reaches on its own. Here the bound keeps all four in the group and the shared scale
    lands on 0.86; the 1.0 unit is more than 10 % above it and leaves, the rest share."""
    plans = _share([1.0, 0.90, 0.88, 0.86])
    assert [p.shared for p in plans] == [False, True, True, True]
    assert plans[0].font_factor == 1.0
    factor = 0.86 * _placement.SHARED_MARGIN
    assert [p.font_factor for p in plans[1:]] == [pytest.approx(factor)] * 3
    # every shared unit stays within the cap of its own scale
    for plan in plans:
        if plan.shared:
            assert plan.font_factor >= plan.natural_scale * (1 - _placement.SHARED_MAX_DROP)


def test_the_narrow_spread_keeps_the_crowded_units_out_of_the_group() -> None:
    """The review's original scenario, under the ``SHARED_SPREAD`` the cap shipped with.

    At 0.15 the median put every unit above the bound, the shared scale fell to the most
    crowded one (0.66) and the cap then ejected the roomy units one by one - the page came
    out in as many sizes as it had paragraphs. At 0.10 the two crowded units are outliers
    from the start: the group forms around 0.80, and only the 1.0 unit is far enough above
    it for the cap to matter. Two sizes instead of five."""
    plans = _share([1.0, 0.85, 0.80, 0.69, 0.66])
    assert [p.shared for p in plans] == [False, True, True, True, True]
    # two clear groups instead of five sizes: the outliers are grouped with each other
    first, second = (x * _placement.SHARED_MARGIN for x in (0.80, 0.66))
    assert [p.font_factor for p in plans[1:3]] == [pytest.approx(first)] * 2
    assert [p.font_factor for p in plans[3:]] == [pytest.approx(second)] * 2
    assert plans[0].font_factor == 1.0  # the capped unit keeps its own scale
    assert len({round(p.font_factor, 4) for p in plans}) == 3


def test_shared_scale_group_that_the_cap_empties_falls_back_to_own_scales() -> None:
    """Nothing close enough left: every unit keeps its natural scale (no group of one)."""
    plans = _share([0.95, 0.70])
    assert [p.shared for p in plans] == [False, False]
    assert [p.font_factor for p in plans] == [1.0, 1.0]


def test_low_shared_scale_is_recorded_at_page_level(tmp_path: Path) -> None:
    """CR-95: a page whose group scale drops below 0.9 gets an informational record that
    is told apart from a unit finding by its empty ``block_id`` / zero ``unit_id``."""
    path = paragraphs_pdf(tmp_path)
    blocks = paragraph_blocks(CLOSE_TEXTS)
    artifact = render(path, blocks, tmp_path / "uniform")
    notes = [e for e in artifact.review if e.reason is OverlayReviewReason.PAGE_SHARED_SCALE]
    assert len(notes) == 1
    note = notes[0]
    assert note.page == 1 and note.block_id == "" and note.unit_id == 0
    assert note.scale is not None and note.scale < _placement.SHARED_SCALE_NOTE
    assert note.scale == note.shared_scale and note.note == f"3 units share a font factor "\
        f"of {note.scale:.2f}"
    assert note.source_text == "" and note.translated_text is None
    review = json.loads(artifact.review_path.read_text(encoding="utf-8"))
    written = [e for e in review["entries"] if e["reason"] == "page_shared_scale"]
    assert len(written) == 1 and written[0]["block_id"] == ""
    assert set(written[0]) == {"page", "block_id", "unit_id", "reason", "scale", "source_text",
                               "translated_text", "note", "shared_scale"}  # schema unchanged
    assert review["counts"]["entries"] == len(review["entries"])
    # no shared scale, no record
    own = render(path, blocks, tmp_path / "own", uniform_scale=False)
    assert not [e for e in own.review if e.reason is OverlayReviewReason.PAGE_SHARED_SCALE]


def test_source_line_height_is_derived_from_the_line_boxes_and_clipped() -> None:
    """CR-96: the block's own rhythm, clipped to 1.15 - 1.40, with 1.15 as fallback."""
    def stack(pitch: float, count: int = 4) -> List[BBox]:
        return [(40.0, 100.0 + i * pitch, 300.0, 111.0 + i * pitch) for i in range(count)]

    assert _placement.source_line_height(stack(12.6), 10.0) == pytest.approx(1.26)
    assert _placement.source_line_height(stack(10.0), 10.0) == _placement.LINE_HEIGHT_MIN
    assert _placement.source_line_height(stack(20.0), 10.0) == _placement.LINE_HEIGHT_MAX
    assert _placement.LINE_HEIGHT == _placement.LINE_HEIGHT_MIN == 1.15
    assert _placement.LINE_HEIGHT_MAX == 1.40
    # not derivable: one line, no font size, lines side by side
    assert _placement.source_line_height(stack(12.6, 1), 10.0) == _placement.LINE_HEIGHT
    assert _placement.source_line_height(stack(12.6), 0.0) == _placement.LINE_HEIGHT
    assert _placement.source_line_height(
        [(0.0, 10.0, 50.0, 22.0), (60.0, 10.0, 110.0, 22.0)], 10.0
    ) == _placement.LINE_HEIGHT


def test_a_block_is_never_tightened_below_the_line_height_band() -> None:
    """CR-96: the source rhythm may be tightened before the font is shrunk, but only down
    to 1.15 - the fix round went to 1.05, further from the source than the default."""
    assert not hasattr(_placement, "LINE_HEIGHT_TIGHT")
    loose = _placement.PlacementSpec(
        rect=(40.0, 40.0, 300.0, 100.0), text=SENTENCE * 6, font_size=10.0, bold=False,
        italic=False, family="serif", color="#000000", alignment=OverlayAlignment.JUSTIFY,
        line_height=1.3,
    )
    (tightened,) = _placement.plan_placements([loose], BUILTIN_FONT, 0.5, uniform=True)
    assert tightened.line_height == _placement.LINE_HEIGHT_MIN  # tightened, not below
    (own,) = _placement.plan_placements([loose], BUILTIN_FONT, 0.5)
    assert own.line_height == 1.3  # per-rectangle mode keeps the source rhythm
    assert own.natural_scale < tightened.natural_scale
    fitting = dataclasses.replace(loose, text="Kısa bir satır")
    (kept,) = _placement.plan_placements([fitting], BUILTIN_FONT, 0.5, uniform=True)
    assert kept.line_height == 1.3 and kept.natural_scale == 1.0
    assert "line-height:1.3" in _placement.build_html(
        fitting, BUILTIN_FONT, line_height=kept.line_height
    )


def test_the_source_rhythm_reaches_the_placement_spec() -> None:
    """CR-96: the renderer derives the line height from the block's line boxes."""
    from book_translator.exporters import pdf_overlay_exporter as overlay_module

    boxes: List[BBox] = [(40.0, 100.0 + i * 12.74, 300.0, 111.0 + i * 12.74) for i in range(5)]
    block = make_block(1, 0, (40.0, 100.0, 300.0, 162.0), "Source", "Çeviri",
                       size=10.0, line_boxes=boxes)
    assert overlay_module._spec(block, "Çeviri").line_height == pytest.approx(1.274)
    single = make_block(1, 1, (40.0, 100.0, 300.0, 112.0), "Source", "Çeviri", size=10.0)
    assert overlay_module._spec(single, "Çeviri").line_height == _placement.LINE_HEIGHT


def test_single_unit_group_is_placed_like_without_the_shared_scale(tmp_path: Path) -> None:
    path = paragraphs_pdf(tmp_path, 1)
    blocks = paragraph_blocks([SENTENCE * 3])  # fits at the source size
    uniform = render(path, blocks, tmp_path / "u")
    own = render(path, blocks, tmp_path / "o", uniform_scale=False)
    assert uniform.placements[0].shared_scale is None
    assert uniform.placements[0].scale == own.placements[0].scale == 1.0
    rect = blocks[0].bbox
    assert spans_in(uniform.pdf_path, 0, rect) == spans_in(own.pdf_path, 0, rect)


def test_uniform_scale_off_equals_a_lone_insert_htmlbox(tmp_path: Path) -> None:
    """``uniform_scale=False`` is the per-rectangle behaviour: the scale MuPDF picks for the
    unit alone, line height 1.15."""
    path = paragraphs_pdf(tmp_path)
    blocks = paragraph_blocks([SENTENCE * 3, SENTENCE * 4, SENTENCE * 5])
    own = render(path, blocks, tmp_path, uniform_scale=False)
    scratch = pymupdf.open()
    for block, placement in zip(blocks, own.placements, strict=True):
        page = scratch.new_page(width=340, height=300)
        spec = _placement.PlacementSpec(
            rect=block.bbox, text=block.translated_text or "", font_size=10.0, bold=False,
            italic=False, family="serif", color="#000000", alignment=OverlayAlignment.JUSTIFY,
        )
        html = _placement.build_html(spec, BUILTIN_FONT)
        assert "line-height:1.15" in html
        _spare, scale = page.insert_htmlbox(pymupdf.Rect(*block.bbox), html, scale_low=0.5)
        assert placement.scale == pytest.approx(scale)
    scratch.close()


def test_unplaceable_unit_keeps_its_source_text(tmp_path: Path) -> None:
    """CR-52: a rectangle that cannot take even one character + ellipsis is not redacted
    (it used to end up blank); it is kept original and still counts as could_not_fit."""
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((40, 60), "ab", fontsize=12, fontname="tiro")
    page.insert_text((40, 120), "Second line stays", fontsize=12, fontname="tiro")
    path = tmp_path / "tiny.pdf"
    doc.save(str(path))
    lines = _lines(doc[0])
    doc.close()
    tiny = (lines[0]["bbox"][0], lines[0]["bbox"][1], lines[0]["bbox"][0] + 3.0,
            lines[0]["bbox"][1] + 4.0)  # 3 x 4 pt: nothing fits at scale 0.5
    blocks = [
        make_block(1, 0, tiny, "ab", "Sığmayan uzun çeviri", size=12.0),
        make_block(1, 1, lines[1]["bbox"], "Second line stays", "İkinci satır", size=12.0),
    ]
    artifact = render(path, blocks, tmp_path / "out")
    assert artifact.placed == 1 and artifact.could_not_fit == 1 and artifact.kept_original == 1
    entry = [e for e in artifact.review if e.unit_id == blocks[0].unit_id][0]
    assert entry.reason is OverlayReviewReason.KEPT_ORIGINAL and entry.note == "unplaceable"
    assert "ab" in clip_text(artifact.pdf_path, 0, lines[0]["bbox"])  # source text untouched
    assert normalise(clip_text(artifact.pdf_path, 0, lines[1]["bbox"])) == "İkinci satır"
    assert artifact.placements[0].placed is False


def test_single_long_word_is_cut_by_characters_never_a_bare_ellipsis() -> None:
    spec = _placement.PlacementSpec(
        rect=(50.0, 100.0, 120.0, 114.0), text="Supercalifragilisticexpialidocious",
        font_size=10.0, bold=False, italic=False, family="serif", color="#000000",
        alignment=OverlayAlignment.LEFT,
    )
    shortened = _placement.ellipsis_text(spec, BUILTIN_FONT, 0.5, None)
    assert shortened is not None and shortened.endswith("…") and len(shortened) > 5
    assert spec.text.startswith(shortened[:-1])
    (plan,) = _placement.plan_placements([spec], BUILTIN_FONT, 0.5)
    assert plan.truncated and plan.text == shortened and plan.natural_scale == -1.0
    # a box that takes no character at all yields no text (the caller keeps the source)
    hopeless = dataclasses.replace(spec, rect=(50.0, 100.0, 52.0, 103.0))
    assert _placement.ellipsis_text(hopeless, BUILTIN_FONT, 0.5, None) is None
    assert not _placement.plan_placements([hopeless], BUILTIN_FONT, 0.5)[0].placeable


def test_measure_scale_matches_the_real_insert() -> None:
    spec = _placement.PlacementSpec(
        rect=(40.0, 40.0, 300.0, 100.0), text=SENTENCE * 5, font_size=10.0, bold=False,
        italic=False, family="serif", color="#000000", alignment=OverlayAlignment.JUSTIFY,
    )
    scratch = pymupdf.open()
    measured = _placement.measure_scale(scratch, spec, BUILTIN_FONT, 0.5)
    assert scratch.page_count == 0  # the scratch page is thrown away
    page = scratch.new_page(width=340, height=300)
    placed = _placement.insert_block(page, spec, BUILTIN_FONT, 0.5)
    assert 0.5 <= measured < 1.0 and placed.scale == pytest.approx(measured)
    assert _placement.measure_scale(scratch, dataclasses.replace(spec, text=SENTENCE * 60),
                                    BUILTIN_FONT, 0.5) == -1.0
    scratch.close()


def test_test_doc_page_one_body_units_are_never_shrunk_past_the_cap(tmp_path: Path) -> None:
    """CR-95 on the reference document. Synthetic Turkish-like text, ~17 % longer than the
    source (EN -> TR grows by 10-20 %).

    Until the cap this test asserted one font size for every BODY unit of page 1: the two
    most crowded paragraphs (natural scale ~0.81) pulled the six comfortable ones (0.91 -
    0.95) down with them, a ~14 % shrink of text that fitted. The user decision reverses
    that: a unit is never placed more than ``SHARED_MAX_DROP`` below the size it reaches
    on its own, and only the units near the group scale share it."""
    from book_translator.extractors.overlay_extractor import OverlayOptions, extract_overlay

    extraction = unwrap(extract_overlay(TEST_DOC, OverlayOptions(), None))

    def translated(block: OverlayBlock) -> str:
        words = block.source_text.split()
        extra = words[: max(1, len(words) // 6)]
        return " ".join(w.replace("th", "ğı").replace("s", "ş", 1) for w in words + extra)

    blocks = [
        dataclasses.replace(b, status=ChunkStatus.COMPLETED,
                            translated_text=translated(b) if b.translate else None)
        for b in extraction.blocks if b.page == 1
    ]
    pages = [p for p in extraction.pages if p.page == 1]
    artifact = render(TEST_DOC, blocks, tmp_path / "uniform", pages=pages)
    own = render(TEST_DOC, blocks, tmp_path / "own", pages=pages, uniform_scale=False)
    own_scale = {p.unit_id: p.scale for p in own.placements}
    body = {b.unit_id: b for b in blocks if b.kind is OverlayBlockKind.BODY and b.translate}
    assert len(body) >= 6
    placements = [p for p in artifact.placements if p.unit_id in body]
    for placement in placements:
        floor = own_scale[placement.unit_id] * (1.0 - _placement.SHARED_MAX_DROP)
        assert placement.scale >= floor - 1e-6, placement
    shared = [p for p in placements if p.shared_scale is not None]
    assert len(shared) >= 2  # the crowded paragraphs still share one size with each other
    # The units the cap leaves out are grouped with each other rather than each landing on
    # its own scale, so the page carries a couple of clear sizes, not a spread of them.
    assert 1 <= len({round(p.font_size, 3) for p in shared}) <= 2
    assert len({round(p.font_size, 3) for p in placements}) <= 3
    assert artifact.could_not_fit == 0 and artifact.shrunk_below_threshold == 0
    # the shared factor is well below 0.9, so the page carries an informational record
    page_notes = [e for e in artifact.review
                  if e.reason is OverlayReviewReason.PAGE_SHARED_SCALE]
    # one record per shared group below the note threshold, all of them on page 1
    assert {e.page for e in page_notes} == {1} and page_notes
    assert {round(e.scale or 0, 4) for e in page_notes} >= {
        round(p.shared_scale or 0, 4) for p in shared if (p.shared_scale or 1) < 0.9
    }


def test_db_error_from_the_block_callback_is_an_err_not_an_exception(tmp_path: Path) -> None:
    """CR-67: ``blocks_by_page`` reads the state DB; a lock error must not escape."""
    path, _rect = shrink_fixture(tmp_path)

    def failing(page: int) -> Iterator[OverlayBlock]:
        raise RuntimeError("database is locked")

    result = OverlayRenderer().render(
        path, infos_of(path), failing, {}, tmp_path, OverlayRenderOptions(font=BUILTIN_FONT)
    )
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.EXPORT_FAILED
    assert result.error.scope is ErrorScope.JOB_FATAL and "page 1" in result.error.message
    assert not (tmp_path / OVERLAY_PDF_NAME).exists()


def test_residual_glyphs_are_reported_when_redaction_leaves_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive RESIDUAL_GLYPHS case: the redaction pass removes nothing."""
    path, rect = shrink_fixture(tmp_path)
    monkeypatch.setattr(_placement.pdfkit, "page_apply_redactions", lambda page: False)
    block = make_block(1, 0, rect, "Short label", "Kısa etiket", size=12.0)
    artifact = render(path, [block], tmp_path / "out")
    reasons = [e.reason for e in artifact.review]
    assert reasons == [OverlayReviewReason.RESIDUAL_GLYPHS]
    assert artifact.placements[0].residual is True


def test_collateral_redaction_is_reported_for_both_units(tmp_path: Path) -> None:
    """Positive COLLATERAL_REDACTION case: a kept unit lies inside a placed unit's rect."""
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((40, 60), "Translated part", fontsize=12, fontname="tiro")
    page.insert_text((150, 60), "KEPT", fontsize=12, fontname="tiro")
    path = tmp_path / "collateral.pdf"
    doc.save(str(path))
    first, second = _lines(doc[0])[:2]
    doc.close()
    wide = (first["bbox"][0], first["bbox"][1], second["bbox"][2], first["bbox"][3])
    placed = make_block(1, 0, wide, "Translated part", "Çevrilen kısım", size=12.0)
    kept = make_block(1, 1, second["bbox"], "KEPT", None, size=12.0, keep_reason="figure_text")
    artifact = render(path, [placed, kept], tmp_path / "out")
    collateral = [e for e in artifact.review
                  if e.reason is OverlayReviewReason.COLLATERAL_REDACTION]
    assert sorted(e.unit_id for e in collateral) == sorted([placed.unit_id, kept.unit_id])
    assert OverlayReviewReason.COLLATERAL_REDACTION in artifact.placements[0].reasons


def test_existing_redaction_annotation_keeps_every_property(tmp_path: Path) -> None:
    """CR-54: same object, fill colour and overlay text; never applied, in both paths."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((50, 60), "KEEPME secret text", fontsize=11)
    page.insert_text((50, 120), "Body text to translate", fontsize=11)
    page.add_redact_annot(pymupdf.Rect(45, 45, 250, 70), text="CENSORED", fill=(0, 0, 0),
                          text_color=(1, 1, 1), fontsize=9)
    path = tmp_path / "redact.pdf"
    doc.save(str(path))
    doc.close()
    source = pymupdf.open(str(path))
    annot = next(source[0].annots())
    before = (annot.xref, source.xref_object(annot.xref, compressed=True))
    body = [ln for ln in _lines(source[0]) if ln["text"].startswith("Body")][0]["bbox"]
    del annot
    source.close()

    block = make_block(1, 0, body, "Body text to translate", "Çevrilecek gövde", size=11.0)
    artifact = render(path, [block], tmp_path / "out")
    assert "redact_annots_preserved:1" in artifact.warnings
    out = pymupdf.open(str(artifact.pdf_path))
    out_page = out[0]
    (kept,) = [a for a in out_page.annots() if a.type[0] == pymupdf.PDF_ANNOT_REDACT]
    obj = out.xref_object(kept.xref, compressed=True)
    assert "/OverlayText(CENSORED)" in obj and "/IC[0 0 0]" in obj and "/Subtype/Redact" in obj
    assert "/OverlayText(CENSORED)" in before[1]
    assert "KEEPME secret text" in out_page.get_text()  # not applied
    assert "Çevrilecek gövde" in out_page.get_text()
    del kept
    out.close()

    # figure path: the shared redact core leaves the annotation and its text alone as well
    copy = pymupdf.open(str(path))
    page_after, preserved = _placement.redact(copy, copy[0], [body])
    assert preserved == 1 and "KEEPME secret text" in page_after.get_text()
    assert "Body text" not in page_after.get_text()
    (still,) = [a for a in page_after.annots() if a.type[0] == pymupdf.PDF_ANNOT_REDACT]
    assert still.xref == before[0]
    del still
    copy.close()


def test_outline_destinations_style_and_uri_bookmarks_survive_retitling(tmp_path: Path) -> None:
    """CR-50: retitling one bookmark used to rebuild the outline (every destination moved
    to the top of its page, bold/colour dropped, the URI bookmark broken)."""
    doc = pymupdf.open()
    for number in range(3):
        doc.new_page().insert_text((72, 100), f"Chapter text {number}", fontsize=11)
    doc.set_toc([
        [1, "Chapter One", 1, {"kind": pymupdf.LINK_GOTO, "page": 0,
                               "to": pymupdf.Point(72, 400), "zoom": 0, "bold": True,
                               "color": (1, 0, 0)}],
        [2, "Section 1.1", 2, {"kind": pymupdf.LINK_GOTO, "page": 1,
                               "to": pymupdf.Point(72, 555), "zoom": 0}],
        [1, "External", 3, {"kind": pymupdf.LINK_URI, "uri": "https://example.org"}],
    ])
    path = tmp_path / "toc.pdf"
    doc.save(str(path))
    before = doc.get_toc(simple=False)
    doc.close()
    line = (72.0, 88.0, 200.0, 104.0)
    block = make_block(1, 0, line, "Chapter text 0", "Bölüm metni 0", size=11.0)
    artifact = render(path, [block], tmp_path / "out",
                      outline_map={(1, "Chapter One"): "Birinci Bölüm"})
    assert "outline_retitled:1" in artifact.warnings
    out = pymupdf.open(str(artifact.pdf_path))
    after = out.get_toc(simple=False)
    out.close()
    assert [row[1] for row in after] == ["Birinci Bölüm", "Section 1.1", "External"]
    for old, new in zip(before, after, strict=True):
        assert (old[0], old[2]) == (new[0], new[2])
        for key in ("kind", "page", "to", "zoom", "bold", "color", "uri"):
            assert old[3].get(key) == new[3].get(key), (old[1], key)


@pytest.mark.parametrize("rotation", [90, 270])
def test_rotated_page_is_translated_upright(rotation: int, tmp_path: Path) -> None:
    """CR-53: extraction stores visible-space rects, the renderer maps them back and turns
    the text by the page's /Rotate."""
    from book_translator.extractors.overlay_extractor import OverlayOptions, extract_overlay

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=400)
    page.set_rotation(rotation)
    for index, y in enumerate((80, 95, 110, 125)):
        page.insert_text(pymupdf.Point(50, y) * page.derotation_matrix,
                         f"Upright body text line {index} of a landscape-stored page, long.",
                         fontsize=10, rotate=rotation)
    path = tmp_path / "rotated.pdf"
    doc.save(str(path))
    doc.close()
    source = pymupdf.open(str(path))
    source_lines = [ln for b in source[0].get_text("dict")["blocks"] for ln in b["lines"]]
    source_dir = tuple(round(v) for v in source_lines[0]["dir"])
    union = pymupdf.Rect()
    for ln in source_lines:
        union |= pymupdf.Rect(ln["bbox"])
    source.close()

    extraction = unwrap(extract_overlay(path, OverlayOptions(), {}))
    blocks = [dataclasses.replace(b, status=ChunkStatus.COMPLETED,
                                  translated_text="ÇEVİRİ " + b.source_text)
              for b in extraction.blocks]
    artifact = render(path, blocks, tmp_path / "out", pages=list(extraction.pages))
    assert artifact.placed == 1 and artifact.kept_original == 0 and artifact.review == ()
    out = pymupdf.open(str(artifact.pdf_path))
    assert out[0].rotation == rotation
    out_lines = [ln for b in out[0].get_text("dict")["blocks"] for ln in b.get("lines", [])]
    text = " ".join("".join(s["text"] for s in ln["spans"]) for ln in out_lines)
    out.close()
    assert "ÇEVİRİ Upright body text line 0" in text
    assert all(tuple(round(v) for v in ln["dir"]) == source_dir for ln in out_lines)
    for ln in out_lines:  # written into the same (unrotated-space) rectangle
        assert union.x0 - 3 <= ln["bbox"][0] and ln["bbox"][2] <= union.x1 + 3
        assert union.y0 - 3 <= ln["bbox"][1] and ln["bbox"][3] <= union.y1 + 3


def test_list_item_keeps_its_marker_and_hanging_indent(tmp_path: Path) -> None:
    """CR-41 write-back: the marker survives and wrapped lines hang under the item text."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((50, 60), "• first line of a list item that wraps", fontsize=10,
                     fontname="tiro")
    page.insert_text((62, 73), "onto a second, hanging line.", fontsize=10, fontname="tiro")
    path = tmp_path / "item.pdf"
    doc.save(str(path))
    first, second = _lines(doc[0])[:2]
    doc.close()
    rect = (first["bbox"][0], first["bbox"][1], 330.0, second["bbox"][3] + 14.0)
    assert _placement.first_line_indent(rect, [first["bbox"], second["bbox"]]) == pytest.approx(
        -12.0, abs=0.6
    )
    block = make_block(
        1, 0, rect, "item", "• listenin ilk maddesi iki satıra yayılacak kadar uzun "
        "tutulmuş bir çeviri metnidir ve devam eder.", size=10.0,
        line_boxes=[first["bbox"], second["bbox"]],
    )
    html = _placement.build_html(
        _placement.PlacementSpec(
            rect=rect, text="x", font_size=10.0, bold=False, italic=False, family="serif",
            color="#000000", alignment=OverlayAlignment.LEFT, first_line_indent_pt=-12.0),
        BUILTIN_FONT,
    )
    assert "padding:0 0 0 12pt;" in html and "text-indent:-12pt;" in html
    artifact = render(path, [block], tmp_path / "out")
    out = pymupdf.open(str(artifact.pdf_path))
    lines = _lines(out[0])
    out.close()
    assert len(lines) >= 2 and lines[0]["text"].lstrip()[0] in "•·"
    assert lines[0]["bbox"][0] == pytest.approx(rect[0], abs=2.0)
    assert all(ln["bbox"][0] == pytest.approx(second["bbox"][0], abs=2.5) for ln in lines[1:])


def test_figure_labels_share_a_scale_and_report_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Labels of one style in a figure come out at one size; a label that had to be cut is
    visible to the caller (CR-52), and a batch opens the source once (CR-61)."""
    from book_translator.exporters.figure_overlay import FigureJob, render_translated_figures

    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.draw_rect(pymupdf.Rect(30, 30, 370, 270), color=(0, 0, 1))
    for index, (x, y) in enumerate([(50, 80), (50, 140), (50, 200)]):
        page.insert_text((x, y), f"Label number {index}", fontsize=12)
    page.insert_text((250, 80), "Ab", fontsize=12)
    path = tmp_path / "figure.pdf"
    doc.save(str(path))
    found = {ln["text"].strip(): ln["bbox"] for ln in _lines(doc[0])}
    boxes = [found[f"Label number {index}"] for index in range(3)] + [found["Ab"]]
    doc.close()

    def label(bbox: BBox, text: str) -> LabelReplacement:
        return LabelReplacement(bbox, text, 12.0, False, False, "sans", "#000000",
                                OverlayAlignment.LEFT)

    labels = [label(boxes[0], "Etiket numarası 0"), label(boxes[1], "Etiket numarası bir"),
              label(boxes[2], "Etiket numarası 2")]
    png, stats = unwrap(render_translated_figure_with_stats(
        path, 1, (30.0, 30.0, 370.0, 270.0), labels, 100, BUILTIN_FONT))
    assert png.startswith(b"\x89PNG") and stats.truncated == (False, False, False)
    assert len({round(scale, 4) for scale in stats.scales}) == 1 and stats.scales[0] < 1.0

    cut = [label(boxes[3], "Buraya sığmayacak kadar uzun bir etiket metni")]
    _png, cut_stats = unwrap(render_translated_figure_with_stats(
        path, 1, (30.0, 30.0, 370.0, 270.0), cut, 100, BUILTIN_FONT))
    assert cut_stats.truncated == (True,) and cut_stats.truncated_count == 1

    opened: List[int] = []
    real_open = pdfkit.open_document_bytes

    def counting(data: bytes, filetype: str = "pdf") -> Any:
        opened.append(len(data))
        return real_open(data, filetype)

    monkeypatch.setattr(figure_overlay_module.pdfkit, "open_document_bytes", counting)
    jobs = [FigureJob(1, (30.0, 30.0, 370.0, 270.0), labels, 100),
            FigureJob(1, (30.0, 30.0, 370.0, 150.0), labels[:1], 100),
            FigureJob(9, (0.0, 0.0, 10.0, 10.0), [], 100)]
    outcomes = unwrap(render_translated_figures(path, jobs, BUILTIN_FONT))
    assert len(opened) == 1 and len(outcomes) == 3
    assert [isinstance(o, Err) for o in outcomes] == [False, False, True]  # page 9 of 1


# --------------------------------------------------------------------------- #
# CR-97: the bold running number of a figure label survives into the .tr.png
# --------------------------------------------------------------------------- #


def test_label_prefix_style_reaches_the_placement_spec() -> None:
    """``LabelReplacement.prefix_style`` is handed to the shared placement core, so the
    figure path builds the same two-run HTML as the overlay PDF (E-46 / Q22)."""
    replacement = LabelReplacement(
        (10.0, 10.0, 200.0, 26.0), "2. Görüntü oluşumu", 13.0, False, False, "serif",
        "#000000", OverlayAlignment.CENTER, prefix_style=("2.", True, False),
    )
    spec = figure_overlay_module._spec(replacement, 0)
    assert spec.prefix_style == ("2.", True, False)
    html = _placement.build_html(spec, BUILTIN_FONT)
    assert '<span style="font-weight:bold;font-style:normal">2.</span>' in html
    assert "Görüntü oluşumu" in html
    plain = dataclasses.replace(replacement, prefix_style=None)
    assert "<span" not in _placement.build_html(
        figure_overlay_module._spec(plain, 0), BUILTIN_FONT
    )


def test_translated_figure_png_keeps_the_bold_running_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-97 end to end: the source label is "**2.** Image formation"; before the fix the
    translated label came out fully regular in the figure PNG."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((40, 80), "2.", fontsize=13, fontname="tibo")
    page.insert_text((58, 80), " Image formation", fontsize=13, fontname="tiro")
    path = tmp_path / "labelled.pdf"
    doc.save(str(path))
    doc.close()
    replacement = LabelReplacement(
        (38.0, 66.0, 260.0, 84.0), "2. Görüntü oluşumu", 13.0, False, False, "serif",
        "#000000", OverlayAlignment.LEFT, prefix_style=("2.", True, False),
    )
    seen: Dict[str, Any] = {}
    real_pixmap = figure_overlay_module.pdfkit.page_pixmap_png

    def spy(page: Any, dpi: int, clip: Any = None) -> Any:
        seen["spans"] = [
            (span["text"], "Bold" in span["font"] or bool(span["flags"] & 16))
            for block in page.get_text("dict", clip=pymupdf.Rect(*replacement.bbox))["blocks"]
            for line in block.get("lines", []) for span in line["spans"]
        ]
        return real_pixmap(page, dpi, clip)

    monkeypatch.setattr(figure_overlay_module.pdfkit, "page_pixmap_png", spy)
    png = unwrap(render_translated_figure(
        path, 1, (20.0, 40.0, 380.0, 120.0), [replacement], 150, BUILTIN_FONT
    ))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    spans = seen["spans"]
    assert "".join(text for text, _bold in spans).strip() == "2. Görüntü oluşumu"
    assert spans[0][0].strip() == "2." and spans[0][1] is True  # the prefix stayed bold
    assert any(not bold for _text, bold in spans[1:])  # the rest did not become bold


def test_prefix_style_survives_the_figures_json_round_trip() -> None:
    """CR-97: the field is what the pipeline reads back, so it has to be in the file."""
    from book_translator.domain.models import FigureKind, FigureRecord, LabelBox
    from book_translator.extractors.figure_inventory import LabelBoxEntry
    from book_translator.pipeline.figure_labels import build_replacements

    box = LabelBox(
        text="2. Image formation", bbox=(10.0, 10.0, 200.0, 26.0), font_size=13.0,
        bold=False, italic=False, family="serif", color="#000000", alignment="center",
        prefix_style=("2.", True, False),
    )
    entry = LabelBoxEntry.from_box(box)
    assert entry.prefix_style == ("2.", True, False)
    assert LabelBoxEntry.model_validate_json(entry.model_dump_json()).to_box() == box
    # an entry written before the field existed still loads
    assert LabelBoxEntry.model_validate(
        {"text": "x", "bbox": (0.0, 0.0, 1.0, 1.0), "font_size": 10.0}
    ).prefix_style is None
    record = FigureRecord(
        image_id=1, page=2, index_on_page=0, kind=FigureKind.VECTOR,
        region=(0.0, 0.0, 300.0, 300.0), file="images/p002-f01.png", dpi=150, width_px=10,
        height_px=10, bytes=1, dpi_reduced=False, label_count=1,
        labels=("2. Image formation",), labels_more=0, caption_present=False, warnings=(),
        label_boxes=(box,),
    )
    (replacement,) = build_replacements(record, {"2. Image formation": "2. Görüntü oluşumu"})
    assert replacement.prefix_style == ("2.", True, False)


def test_figure_labels_share_one_size_because_the_cap_is_off_for_them() -> None:
    """``LABEL_MAX_DROP``: a diagram is read as one object, so its labels share a size.

    The CR-95 cap protects a page of prose from being shrunk to its most crowded
    paragraph. Label boxes vary far more in size than paragraphs, so the same cap ejected
    most of them and the reference page's diagram came out in seven sizes. Labels opt out;
    body text does not."""
    naturals = [1.0, 0.92, 0.84, 0.75, 0.70]
    capped = _share(naturals)
    uncapped = _share(naturals, max_drop=_placement.LABEL_MAX_DROP)
    # the bound still drops a true outlier (0.70); everything else comes out at one size
    assert [p.shared for p in uncapped] == [True, True, True, True, False]
    assert len({round(p.font_factor, 4) for p in uncapped if p.shared}) == 1
    # the same units under the body cap are split into more sizes
    assert len({round(p.font_factor, 4) for p in capped}) > 1
    assert sum(p.shared for p in capped) < sum(p.shared for p in uncapped)
