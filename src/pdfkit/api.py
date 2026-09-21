"""Thin typed wrapper around the (untyped) pymupdf API (design doc 06, D17).

The only module allowed to import ``pymupdf`` directly; keeps ``# type: ignore``
confined here. Callers (extractors and exporters) receive ``Any`` objects and
must classify the exceptions pymupdf raises. No internal imports — this is a
leaf layer (see ``tests/test_layering.py``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymupdf as _pymupdf

Matrix = Tuple[float, float, float, float, float, float]
"""``(a, b, c, d, e, f)`` of a pymupdf ``Matrix``: ``x' = ax + cy + e``, ``y' = bx + dy + f``."""


def open_document(path: str) -> Any:
    """Open a PDF; raises whatever pymupdf raises (callers classify)."""
    return _pymupdf.open(path)  # type: ignore[no-untyped-call]


def open_document_bytes(data: bytes, filetype: str = "pdf") -> Any:
    return _pymupdf.open(stream=data, filetype=filetype)  # type: ignore[no-untyped-call]


def new_document() -> Any:
    return _pymupdf.open()  # type: ignore[no-untyped-call]


def close_document(doc: Any) -> None:
    doc.close()


def page_text_dict(page: Any) -> Any:
    return page.get_text("dict")


def page_plain_text(page: Any, clip: Optional[Any] = None) -> str:
    if clip is None:
        return str(page.get_text("text"))
    return str(page.get_text("text", clip=clip))


def page_image_count(page: Any) -> int:
    images: List[Any] = list(page.get_images(full=False))
    return len(images)


def page_drawings(page: Any) -> List[Any]:
    """Vector paths of the page (``page.get_drawings()``), each a dict with ``rect``."""
    return list(page.get_drawings())


def page_links(page: Any) -> List[Any]:
    return list(page.get_links())


def page_pixmap_png(page: Any, dpi: int, clip: Optional[Any] = None) -> Any:
    """Render (a clip of) the page and return the pymupdf ``Pixmap``."""
    return page.get_pixmap(dpi=dpi, clip=clip, alpha=False, annots=False)


def pixmap_png_bytes(pixmap: Any) -> bytes:
    """Encode a ``Pixmap`` as PNG bytes."""
    data: bytes = pixmap.tobytes("png")
    return data


def pixmap_size(pixmap: Any) -> Tuple[int, int]:
    return int(pixmap.width), int(pixmap.height)


def rect(x0: float, y0: float, x1: float, y1: float) -> Any:
    return _pymupdf.Rect(x0, y0, x1, y1)  # type: ignore[no-untyped-call]


def rect_tuple(value: Any) -> Tuple[float, float, float, float]:
    """``Rect``/``IRect``/4-sequence -> plain floats ``(x0, y0, x1, y1)``."""
    if hasattr(value, "x0"):
        return (float(value.x0), float(value.y0), float(value.x1), float(value.y1))
    x0, y0, x1, y1 = (float(v) for v in value)
    return (x0, y0, x1, y1)


def doc_toc(doc: Any) -> List[Any]:
    """Simple outline rows ``[level, title, page]`` in document order."""
    return list(doc.get_toc())


def doc_set_toc_title(doc: Any, index: int, title: str) -> None:
    """Retitle outline entry ``index`` (position in :func:`doc_toc`) in place (CR-50).

    Only the ``/Title`` of the outline object is rewritten, so the destination point, zoom,
    colour, bold/italic flags and URI targets stay as they are (``set_toc`` rebuilds every
    entry; ``set_toc_item(title=...)`` clears the style flags)."""
    xref = int(doc.get_outline_xrefs()[index])
    doc.xref_set_key(xref, "Title", _pymupdf.get_pdf_str(title))


def silence_layout_hint() -> None:
    """Stop pymupdf printing its layout-package advertisement on ``find_tables``."""
    silencer = getattr(_pymupdf, "no_recommend_layout", None)
    if callable(silencer):
        silencer()


def version() -> str:
    return str(getattr(_pymupdf, "__version__", "unknown"))


def _const(name: str, default: int) -> int:
    """A pymupdf integer constant (``PDF_PERM_MODIFY`` ...); the stubs do not declare them."""
    return int(getattr(_pymupdf, name, default))


# --------------------------------------------------------------------------- #
# v1.1 F3: Story / DocumentWriter (design doc 06, 3.5) and font probing (FR-53)
# --------------------------------------------------------------------------- #


def paper_rect(name: str) -> Optional[Any]:
    """``pymupdf.paper_rect`` for a paper name (``A4``, ``letter``, ``a5-l`` ...);
    ``None`` when the name is unknown (pymupdf answers with a negative rect)."""
    value = _pymupdf.paper_rect(name)
    if value.width <= 0 or value.height <= 0:
        return None
    return value


def new_archive(*directories: str) -> Any:
    """``pymupdf.Archive`` over the given directories (relative ``src``/``url`` lookups)."""
    archive = _pymupdf.Archive()  # type: ignore[no-untyped-call]
    for directory in directories:
        archive.add(directory)  # type: ignore[no-untyped-call]
    return archive


def new_story(html: str, user_css: str, archive: Any) -> Any:
    return _pymupdf.Story(html=html, user_css=user_css, archive=archive)  # type: ignore[no-untyped-call]


def story_place(story: Any, where: Any) -> Tuple[bool, Any]:
    """Lay out the next portion of the story into ``where``: ``(more, filled_rect)``."""
    more, filled = story.place(where)
    return bool(more), filled


def story_element_positions(
    story: Any, callback: Any, args: Optional[Dict[str, Any]] = None
) -> None:
    """Report the elements laid out by the last ``place()`` (ids, hrefs, headings)."""
    story.element_positions(callback, args or {})


def story_draw(story: Any, device: Any) -> None:
    story.draw(device)


def story_add_pdf_links(document: Any, positions: List[Any]) -> Any:
    """``Story.add_pdf_links``: internal ``#id`` links from recorded element positions.
    Raises when an ``href`` has no destination (callers treat it as best effort)."""
    return _pymupdf.Story.add_pdf_links(document, positions)  # type: ignore[no-untyped-call]


def new_document_writer(buffer: Any) -> Any:
    return _pymupdf.DocumentWriter(buffer)  # type: ignore[no-untyped-call]


def writer_begin_page(writer: Any, mediabox: Any) -> Any:
    return writer.begin_page(mediabox)


def writer_end_page(writer: Any) -> None:
    writer.end_page()


def writer_close(writer: Any) -> None:
    writer.close()


def page_insert_centered_text(
    page: Any, clip: Any, text: str, fontsize: float, fontname: str = "helv"
) -> float:
    """``page.insert_textbox`` centred inside ``clip``; returns the unused height
    (negative when the text did not fit)."""
    result = page.insert_textbox(
        clip, text, fontsize=fontsize, fontname=fontname, align=_pymupdf.TEXT_ALIGN_CENTER
    )
    return float(result)


def page_fonts(page: Any) -> List[Tuple[Any, ...]]:
    return [tuple(item) for item in page.get_fonts(full=False)]


def page_plain_text_expanded(page: Any) -> str:
    """Plain text with ligatures expanded (``ﬁ`` -> ``fi``); pymupdf keeps them by default."""
    flags = int(_pymupdf.TEXT_PRESERVE_WHITESPACE)
    return str(page.get_text("text", flags=flags))


def page_words(page: Any) -> List[Tuple[Any, ...]]:
    """``page.get_text("words")``: ``(x0, y0, x1, y1, word, block, line, word_no)``."""
    return [tuple(item) for item in page.get_text("words")]


def page_image_boxes(page: Any) -> List[Tuple[float, float, float, float]]:
    """Bounding boxes of the images placed on the page."""
    return [rect_tuple(info["bbox"]) for info in page.get_image_info()]


def doc_set_toc(doc: Any, toc: List[List[Any]]) -> None:
    doc.set_toc(toc)


def doc_set_metadata(doc: Any, metadata: Dict[str, Any]) -> None:
    doc.set_metadata(metadata)


def doc_set_language(doc: Any, language: str) -> None:
    doc.set_language(language)


def doc_metadata(doc: Any) -> Dict[str, Any]:
    return dict(doc.metadata or {})


def doc_language(doc: Any) -> Optional[str]:
    value = doc.language
    return str(value) if value else None


def doc_subset_fonts(doc: Any) -> None:
    doc.subset_fonts()


def doc_to_bytes(doc: Any, *, garbage: int = 4, deflate: bool = True) -> bytes:
    data: bytes = doc.tobytes(garbage=garbage, deflate=deflate)
    return data


def doc_page_count(doc: Any) -> int:
    return int(doc.page_count)


def pixmap_from_file(path: str) -> Any:
    """Decode an image file (PNG/JPEG/...) into a ``Pixmap``; raises on unreadable input."""
    return _pymupdf.Pixmap(path)  # type: ignore[no-untyped-call]


def pixmap_resolution(pixmap: Any) -> Tuple[int, int]:
    """``(xres, yres)`` in DPI as stored in the image (0 when the file carries none)."""
    return int(getattr(pixmap, "xres", 0) or 0), int(getattr(pixmap, "yres", 0) or 0)


def font_from_file(path: str) -> Any:
    """``pymupdf.Font(fontfile=...)``; raises for a file that is not a usable font."""
    return _pymupdf.Font(fontfile=path)  # type: ignore[no-untyped-call]


def font_has_glyph(font: Any, char: str) -> bool:
    return int(font.has_glyph(ord(char))) != 0


def font_name(font: Any) -> str:
    return str(font.name)


# --------------------------------------------------------------------------- #
# v1.1 F2: overlay extraction (design doc 06, 3.6.1 / 4.4; E-49)
# --------------------------------------------------------------------------- #


def doc_needs_password(doc: Any) -> bool:
    return bool(doc.needs_pass)


def doc_modifiable(doc: Any) -> bool:
    """The one E-49 test of extraction and rendering: ``False`` for a document that is
    encrypted (even when it opens without a password) or whose permission bits forbid
    modification. A password-protected document is reported by :func:`doc_needs_password`."""
    if bool(doc.is_encrypted):
        return False
    return bool(int(doc.permissions) & _const("PDF_PERM_MODIFY", 8))


def page_rotation(page: Any) -> int:
    """``/Rotate`` of the page (0, 90, 180, 270)."""
    return int(page.rotation) % 360


def page_size(page: Any) -> Tuple[float, float]:
    """``(width, height)`` of the page as displayed (``page.rect``: ``/Rotate`` applied)."""
    r = page.rect
    return float(r.width), float(r.height)


def _matrix(value: Any) -> Matrix:
    return (
        float(value.a), float(value.b), float(value.c),
        float(value.d), float(value.e), float(value.f),
    )


def page_rotation_matrix(page: Any) -> Matrix:
    """Unrotated page space (where MuPDF reports text and takes insertions) -> the
    orientation the reader sees. Identity for ``/Rotate 0``."""
    return _matrix(page.rotation_matrix)


def page_derotation_matrix(page: Any) -> Matrix:
    """Inverse of :func:`page_rotation_matrix` (visible orientation -> unrotated space)."""
    return _matrix(page.derotation_matrix)


def transform_rect(
    bbox: Tuple[float, float, float, float], matrix: Matrix
) -> Tuple[float, float, float, float]:
    """Axis-aligned box of ``bbox`` under ``matrix`` (exact for multiples of 90 degrees)."""
    a, b, c, d, e, f = matrix
    xs: List[float] = []
    ys: List[float] = []
    for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[3])):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d * y + f)
    return (min(xs), min(ys), max(xs), max(ys))


TableCells = Tuple[Tuple[float, float, float, float], int, int, List[Any]]


def page_table_cells(page: Any) -> List[TableCells]:
    """``page.find_tables()`` -> ``[(table_bbox, row_count, col_count, [cell_bbox | None])]``.

    Cells are the ``Table.cells`` entries (``None`` for a missing cell). Raises whatever
    pymupdf raises so callers can treat table detection as best effort.
    """
    finder = getattr(page, "find_tables", None)
    if finder is None:
        return []
    found = finder()
    tables: List[TableCells] = []
    for table in found.tables:
        cells = [None if cell is None else rect_tuple(cell) for cell in table.cells]
        tables.append(
            (rect_tuple(table.bbox), int(table.row_count), int(table.col_count), cells)
        )
    return tables


# --------------------------------------------------------------------------- #
# v1.1 F2: overlay placement (design doc 06, 3.6.4 / 4.5)
# --------------------------------------------------------------------------- #


def doc_page(doc: Any, index: int) -> Any:
    """Page object at ``index`` (0-based)."""
    return doc[index]


def doc_page_copy(doc: Any, index: int) -> Any:
    """A fresh one-page document holding a copy of page ``index`` (0-based).

    Redaction and ``insert_htmlbox`` mutate the page they run on, so two jobs that render
    different regions of the *same* page are not independent while they share one document
    (CR-88): the second one rasterizes what the first one wrote, and a job that fails after
    its redaction leaves the page emptied for everyone behind it. A copy per job keeps the
    source document untouched; page size, ``/Rotate``, annotations and links come with it.
    The caller closes the copy (:func:`close_document`)."""
    copy = _pymupdf.open()  # type: ignore[no-untyped-call]
    copy.insert_pdf(doc, from_page=index, to_page=index)  # type: ignore[no-untyped-call]
    return copy


def doc_reload_page(doc: Any, page: Any) -> Any:
    """Re-create the page object so newly inserted links/annotations are visible."""
    return doc.reload_page(page)


def doc_new_page(doc: Any, width: float, height: float) -> Any:
    return doc.new_page(width=width, height=height)


def page_add_redact_annot(page: Any, rect: Any, *, fill: bool = False) -> None:
    """Mark ``rect`` for redaction; ``fill=False`` paints nothing over the area (E-40)."""
    page.add_redact_annot(rect, fill=fill)


def page_apply_redactions(page: Any) -> bool:
    """Remove the text under every redaction annotation of the page, keeping images and
    vector drawings (``images=NONE, graphics=LINE_ART_NONE, text=REMOVE``)."""
    return bool(
        page.apply_redactions(
            images=_const("PDF_REDACT_IMAGE_NONE", 0),
            graphics=_const("PDF_REDACT_LINE_ART_NONE", 0),
            text=_const("PDF_REDACT_TEXT_REMOVE", 0),
        )
    )


def page_insert_htmlbox(
    page: Any,
    rect: Any,
    html: str,
    *,
    css: Optional[str] = None,
    scale_low: float = 0.0,
    archive: Optional[Any] = None,
    rotate: int = 0,
) -> Tuple[float, float]:
    """``Page.insert_htmlbox`` -> ``(spare_height, scale)``; ``spare_height == -1`` when
    the text does not fit even at ``scale_low`` (nothing is written in that case).
    ``rect`` is in unrotated page space; ``rotate`` (the page's ``/Rotate``) turns the text
    so that it reads upright on a rotated page."""
    spare, scale = page.insert_htmlbox(
        rect, html, css=css or None, scale_low=scale_low, archive=archive, rotate=rotate
    )
    return float(spare), float(scale)


def page_insert_link(page: Any, link: Dict[str, Any]) -> None:
    """``Page.insert_link`` with a ``get_links`` dict (its ``xref``/``id`` are dropped)."""
    payload = {key: value for key, value in link.items() if key not in {"xref", "id"}}
    page.insert_link(payload)


def link_signature(link: Dict[str, Any]) -> Tuple[Any, ...]:
    """Hashable identity of a link: kind, rounded ``from`` rect, and its target."""
    x0, y0, x1, y1 = rect_tuple(link["from"])
    target: Any = link.get("uri") or link.get("file") or link.get("page")
    return (int(link.get("kind", 0)), round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1),
            target)


def page_widget_rects(page: Any) -> List[Tuple[float, float, float, float]]:
    """Rectangles of the form-field widgets on the page."""
    return [rect_tuple(widget.rect) for widget in page.widgets()]


def page_redact_annot_xrefs(page: Any) -> List[int]:
    """xrefs of the redaction annotations on the page (pre-existing ones, E-50)."""
    redact = _const("PDF_ANNOT_REDACT", 12)
    return [int(annot.xref) for annot in page.annots() if int(annot.type[0]) == redact]


def page_set_annot_subtypes(doc: Any, page: Any, xrefs: Sequence[int], subtype: str) -> Any:
    """Rewrite ``/Subtype`` of the given annotation objects and return the reloaded page.

    Used to park pre-existing redaction annotations as ``/Square`` while the tool applies
    its own redactions (``apply_redactions`` executes *every* ``/Redact`` of the page) and
    to turn them back afterwards: same object, same xref, every property kept (CR-54)."""
    for xref in xrefs:
        doc.xref_set_key(xref, "Subtype", f"/{subtype}")
    return doc.reload_page(page)


def doc_delete_page(doc: Any, index: int) -> None:
    doc.delete_page(index)
