"""Native reflowed PDF engine (design doc 06, 3.5 / 8.2 "Native PDF"; US-22, US-23).

Text is extracted with ligatures expanded (MuPDF renders ``fi`` as one glyph) and
whitespace-normalised before comparing paragraphs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pymupdf
import pytest

from book_translator.config import parse_pdf_margin
from book_translator.domain.result import Err, ErrorCode, ErrorScope
from book_translator.exporters import fonts as fonts_module
from book_translator.exporters import pdf_exporter as pdf_exporter_module
from book_translator.exporters.base import ExportOptions
from book_translator.exporters.fonts import (
    BUILTIN_FONT,
    TURKISH_PROBE,
    FontSpec,
    font_css,
    resolve_font,
    validate_turkish_coverage,
)
from book_translator.exporters.pdf_exporter import PdfExporter
from book_translator.exporters.pdf_native import (
    DEFAULTS_ASSUMED_WARNING,
    FALLBACK_BODY_PT,
    MM_TO_PT,
    build_toc,
    code_wrap_chars,
    fit_image,
    page_geometry,
    resolve_page_design,
    scan_unsupported_css,
    size_images,
    wrap_code_runs,
)
from book_translator.pdfkit import api as pdfkit
from book_translator.pipeline.orchestrator import EXIT_PARTIAL, EXIT_SUCCESS, exit_code_for
from tests.test_exporters import TURKISH, assembled, unwrap
from tests.test_orchestrator import build, make_workspace, paragraphs, seed

PARAGRAPHS = [
    "Birinci paragraf: çağdaş Türkçe metin ile başlıyoruz, ğ ve ş harfleri dahil.",
    "İkinci paragraf uzun bir cümle içeriyor ve satır sonunda kırılması gerekiyor "
    "çünkü sayfa genişliği sınırlı; bu yüzden birkaç satıra yayılır.",
    "Üçüncü paragraf kısa.",
    "Fourth paragraph mixes English with figures such as 3.14 and words like fine.",
]
FIXTURE_DOC = (
    f"# Bölüm Bir {TURKISH}\n\n"
    + "\n\n".join(PARAGRAPHS[:2])
    + "\n\n## Kesim A\n\n"
    + PARAGRAPHS[2]
    + "\n\n```python\nprint('x')  # " + TURKISH + "\n```\n\n"
    + "# Bölüm İki\n\n## Kesim B\n\n### Derin\n\n"
    + PARAGRAPHS[3]
    + "\n"
)
LONG_DOC = "# Uzun\n\n" + "\n\n".join(f"Paragraf {i}: " + "kelime " * 60 for i in range(1, 41))


def normalise(text: str) -> str:
    return " ".join(text.split())


def open_pdf(path: Path) -> Any:
    return pymupdf.open(str(path))


def all_text(doc: Any) -> str:
    return "\n".join(pdfkit.page_plain_text_expanded(page) for page in doc)


EXPLICIT_DESIGN: Dict[str, Any] = {
    "page_size": "A5", "body_font_pt": 12.0, "margins_mm": (18.0, 15.0, 20.0, 15.0),
}
"""An explicit user choice (``--pdf-page-size A5 --pdf-margin "18mm 15mm 20mm 15mm"`` +
12 pt body) so the geometry assertions below stay exact; the source-derived path is
tested separately. There is no built-in margin any more (CR-66)."""


def export(markdown: str, target: Path, **options: Any) -> Tuple[Any, Any]:
    chosen = {**EXPLICIT_DESIGN, **options}
    artifact = unwrap(PdfExporter().export(assembled(markdown), target, ExportOptions(**chosen)))
    return artifact, open_pdf(artifact.path)


def font_names(doc: Any) -> List[str]:
    """Embedded font names (the footer's base-14 Helvetica is never embedded)."""
    names: List[str] = []
    for page in doc:
        for row in pdfkit.page_fonts(page):
            if row[3] not in names and row[1] != "n/a":
                names.append(str(row[3]))
    return names


def builtin_font_file(tmp_path: Path, name: str = "tiro") -> Path:
    """A real font file: pymupdf's built-in Nimbus Roman (``tiro``) written to disk."""
    path = tmp_path / f"{name}.otf"
    path.write_bytes(pymupdf.Font(name).buffer)
    return path


def write_png(path: Path, width: int, height: int, dpi: int = 96) -> Path:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pixmap.clear_with(90)
    pixmap.set_dpi(dpi, dpi)
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(path))
    return path


# --------------------------------------------------------------------------- #
# Rendering: text, fonts, geometry, footer, outline
# --------------------------------------------------------------------------- #


def test_every_paragraph_appears_in_the_pdf_text(tmp_path: Path) -> None:
    artifact, doc = export(FIXTURE_DOC, tmp_path)
    assert artifact.path.name == "translated_book.pdf" and artifact.warnings == []
    text = normalise(all_text(doc))
    for paragraph in PARAGRAPHS:
        assert normalise(paragraph) in text, paragraph
    assert f"Bölüm Bir {TURKISH}" in text and "print('x')" in text


def test_turkish_round_trip_in_serif_sans_and_mono(tmp_path: Path) -> None:
    probe = "çğıİöşüÇĞİÖŞÜ"
    markdown = (
        f"# Başlık {probe}\n\nGövde {probe}.\n\n```text\nkod {probe}\n```\n\n"
        f"Second paragraph so the document is not a single line."
    )
    _artifact, doc = export(markdown, tmp_path)
    text = all_text(doc)
    assert text.count(probe) == 3  # heading (sans), body (serif), code (mono)
    names = font_names(doc)
    families = {name.split("+", 1)[-1] for name in names}
    assert any("Charis SIL" in family for family in families), names
    assert any("Nimbus Sans" in family for family in families), names
    assert any("Nimbus Mono" in family for family in families), names
    assert all(re.match(r"^[A-Z]{6}\+", name) for name in names), names  # every font subset


def test_a4_with_20mm_margins_places_the_page_and_first_text(tmp_path: Path) -> None:
    markdown = "Başlangıç paragrafı sayfanın sol üst köşesinde başlar.\n\nİkinci paragraf.\n"
    _artifact, doc = export(markdown, tmp_path, page_size="A4", margins_mm=(20, 20, 20, 20))
    page = doc[0]
    x0, y0, x1, y1 = pdfkit.rect_tuple(page.rect)
    assert (x0, y0) == (0.0, 0.0)
    a4 = pdfkit.rect_tuple(pymupdf.paper_rect("A4"))  # 595 x 842 pt
    assert (x1, y1) == (a4[2], a4[3])
    margin = 20 * MM_TO_PT  # 56.69 pt
    first = pdfkit.page_words(page)[0]
    assert first[4] == "Başlangıç"
    assert abs(float(first[0]) - margin) <= 1.0
    # y0 of an extracted word is the glyph box top (font ascender), which sits ~2 pt
    # above the CSS line box that starts exactly at the margin: ±2 pt (8.2 note).
    assert abs(float(first[1]) - margin) <= 2.0
    assert pdfkit.doc_language(doc) == "tr"
    meta = pdfkit.doc_metadata(doc)
    assert meta["producer"].startswith("book-translator ") and meta["title"] == "translated_book"


def test_footer_page_number_absent_on_page_1_and_centred_afterwards(tmp_path: Path) -> None:
    _artifact, doc = export(LONG_DOC, tmp_path)
    assert doc.page_count >= 3
    geometry = unwrap(page_geometry("A5", (18, 15, 20, 15)))
    column_bottom = geometry.column[3]
    first_words = pdfkit.page_words(doc[0])
    assert not [w for w in first_words if float(w[1]) >= column_bottom]  # nothing in the margin
    for index in range(1, doc.page_count):
        page = doc[index]
        footer = [w for w in pdfkit.page_words(page) if float(w[1]) >= column_bottom]
        assert [w[4] for w in footer] == [str(index + 1)], (index, footer)
        centre = (float(footer[0][0]) + float(footer[0][2])) / 2
        page_centre = pdfkit.rect_tuple(page.rect)[2] / 2
        assert abs(centre - page_centre) <= 1.0
        assert float(footer[0][3]) <= pdfkit.rect_tuple(page.rect)[3]


def test_outline_has_one_entry_per_h1_and_h2(tmp_path: Path) -> None:
    _artifact, doc = export(FIXTURE_DOC, tmp_path)
    toc = [(level, title) for level, title, _page in doc.get_toc()]
    assert toc == [
        (1, f"Bölüm Bir {TURKISH}"), (2, "Kesim A"), (1, "Bölüm İki"), (2, "Kesim B")
    ]
    assert all(page >= 1 for _l, _t, page in doc.get_toc())


def test_outline_follows_chapter_split_level_2(tmp_path: Path) -> None:
    _artifact, doc = export(FIXTURE_DOC, tmp_path, chapter_split_level=2)
    assert [(level, title) for level, title, _ in doc.get_toc()] == [
        (1, f"Bölüm Bir {TURKISH}"), (1, "Kesim A"), (1, "Bölüm İki"), (1, "Kesim B"),
        (2, "Derin"),
    ]


def test_build_toc_promotes_an_orphan_level_2_and_skips_deeper_levels() -> None:
    rows = build_toc([(2, "Orphan", 1), (1, " Chapter  One ", 2), (2, "Sec", 2), (3, "x", 3)], 1)
    assert rows == [[1, "Orphan", 1], [1, "Chapter One", 2], [2, "Sec", 2]]
    assert build_toc([(1, "  ", 1)], 1) == []


def test_footnote_links_are_added_best_effort(tmp_path: Path) -> None:
    markdown = "# One\n\nText with a note[^1].\n\nMore text.\n\n[^1]: The note.\n"
    artifact, doc = export(markdown, tmp_path)
    assert "pdf_links_skipped" not in artifact.warnings
    assert sum(len(page.get_links()) for page in doc) >= 1


# --------------------------------------------------------------------------- #
# CSS allowlist (FR-52, E-53)
# --------------------------------------------------------------------------- #


def test_scan_unsupported_css_reports_properties_and_at_rules_once() -> None:
    css = """
    /* comment with hyphens: auto; inside */
    @page { size: A4; margin: 1cm; }
    @font-face { font-family: "X"; src: url("x.ttf"); }
    p { color: red; hyphens: auto; -webkit-hyphens: auto; max-width: 100%;
        font-family: serif; margin-top: 1em; page-break-after: avoid; content: "a: b"; }
    h1 { hyphens: manual; background: #eee; word-wrap: break-word; }
    """
    assert scan_unsupported_css(css) == ["@page", "size", "hyphens", "-webkit-hyphens",
                                         "max-width", "content"]
    assert scan_unsupported_css("p { color: red; }") == []


def test_unsupported_user_css_gives_exactly_one_warning_and_still_exports(
    tmp_path: Path,
) -> None:
    css = tmp_path / "custom.css"
    css.write_text(
        "@page { size: A4; }\np { hyphens: auto; max-width: 90%; color: #222; }\n",
        encoding="utf-8",
    )
    artifact, doc = export(FIXTURE_DOC, tmp_path, css_path=css)
    unsupported = [w for w in artifact.warnings if w.startswith("css_unsupported:")]
    assert unsupported == ["css_unsupported:@page,size,hyphens,max-width"]
    assert doc.page_count >= 1 and normalise(PARAGRAPHS[0]) in normalise(all_text(doc))


# --------------------------------------------------------------------------- #
# Images (E-54) and geometry
# --------------------------------------------------------------------------- #


def test_fit_image_scales_down_only_and_keeps_the_aspect() -> None:
    assert fit_image((100.0, 50.0), 300.0, 300.0) == (100.0, 50.0)
    assert fit_image((1000.0, 250.0), 300.0, 300.0) == (300.0, 75.0)
    assert fit_image((100.0, 1000.0), 300.0, 400.0) == (40.0, 400.0)


def test_image_wider_than_the_column_is_scaled_to_the_column_width(tmp_path: Path) -> None:
    write_png(tmp_path / "images" / "p001-f01.png", 2000, 500)  # 1500 x 375 pt at 96 dpi
    markdown = '# Fig\n\nBefore.\n\n<!-- image:1 src="images/p001-f01.png" -->\n\nAfter.\n'
    artifact, doc = export(markdown, tmp_path)
    assert artifact.warnings == []
    geometry = unwrap(page_geometry("A5", (18, 15, 20, 15)))
    boxes = [box for page in doc for box in pdfkit.page_image_boxes(page)]
    assert len(boxes) == 1
    x0, y0, x1, y1 = boxes[0]
    assert abs((x1 - x0) - geometry.column_width) <= 1.0
    assert abs((y1 - y0) / (x1 - x0) - 0.25) <= 0.01  # aspect 2000:500 kept
    assert x0 >= geometry.column[0] - 0.5 and x1 <= geometry.column[2] + 0.5


def test_tall_image_is_capped_at_80_percent_of_the_column_height(tmp_path: Path) -> None:
    write_png(tmp_path / "images" / "p001-f01.png", 500, 4000)
    markdown = 'Intro.\n\n<!-- image:1 src="images/p001-f01.png" -->\n\nAfter.\n'
    _artifact, doc = export(markdown, tmp_path)
    geometry = unwrap(page_geometry("A5", (18, 15, 20, 15)))
    boxes = [box for page in doc for box in pdfkit.page_image_boxes(page)]
    assert len(boxes) == 1
    x0, y0, x1, y1 = boxes[0]
    assert abs((y1 - y0) - geometry.column_height * 0.8) <= 1.0
    assert abs((x1 - x0) / (y1 - y0) - 0.125) <= 0.01


def test_size_images_uses_stored_dpi_and_warns_for_unreadable_files(tmp_path: Path) -> None:
    write_png(tmp_path / "images" / "a.png", 400, 200, dpi=200)  # 144 x 72 pt: fits as is
    (tmp_path / "images" / "bad.png").write_bytes(b"not a png")
    geometry = unwrap(page_geometry("A4", (20, 20, 20, 20)))
    html = '<img src="images/a.png" alt="x"/><img src="images/bad.png"/><img src="missing.png"/>'
    sized, warnings = size_images(html, tmp_path, geometry)
    assert '<img src="images/a.png" alt="x" width="144" height="72"/>' in sized
    assert warnings == ["image_unsized:images/bad.png", "image_unsized:missing.png"]


def test_page_geometry_rejects_unknown_paper_and_oversized_margins() -> None:
    geometry = unwrap(page_geometry("A4", (20, 20, 20, 20)))
    assert geometry.mediabox[2:] == pytest.approx((595.0, 842.0), abs=1.0)
    assert geometry.column[0] == pytest.approx(20 * MM_TO_PT)
    landscape = unwrap(page_geometry("a5-l", (10, 10, 10, 10)))
    assert landscape.mediabox[2] > landscape.mediabox[3]
    unknown = page_geometry("Tabloid-XL", (10, 10, 10, 10))
    assert isinstance(unknown, Err) and unknown.error.scope is ErrorScope.USER
    assert unknown.error.code is ErrorCode.EXPORT_FAILED and "Tabloid-XL" in unknown.error.message
    huge = page_geometry("A5", (100, 100, 100, 100))
    assert isinstance(huge, Err) and "no room" in huge.error.message


def test_unknown_page_size_is_a_user_error_from_the_exporter(tmp_path: Path) -> None:
    result = PdfExporter().export(
        assembled(FIXTURE_DOC), tmp_path, ExportOptions(page_size="Z9", body_font_pt=12.0)
    )
    assert isinstance(result, Err) and result.error.scope is ErrorScope.USER
    assert not (tmp_path / "translated_book.pdf").exists()


def test_long_code_lines_are_soft_wrapped_and_tables_render(tmp_path: Path) -> None:
    code = "x" * 200
    markdown = (
        f"# Code\n\n```text\n{code}\nshort\n```\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\nTail.\n"
    )
    artifact, doc = export(markdown, tmp_path)
    lines = [line for line in all_text(doc).splitlines() if line.startswith("xxxx")]
    assert len(lines) == 4 and all(len(line) <= 53 for line in lines), lines  # A5 column
    assert "".join(lines) == code  # nothing clipped
    assert "code_lines_wrapped:3" in artifact.warnings
    text = normalise(all_text(doc))
    assert "A B 1 2" in text and "Tail." in text
    assert not [w for w in artifact.warnings if w.startswith("table_overflow")]


def test_wrap_code_runs_keeps_entities_and_tags_whole() -> None:
    code = '<pre><code class="x">&lt;' + "a" * 10 + " <b>bb</b>cccccccccccc</code></pre>"
    html = code + "<p>" + "d" * 30 + "</p>"
    wrapped, breaks = wrap_code_runs(html, 5)
    assert breaks == 4
    expected = '<pre><code class="x">&lt;aaaa\naaaaa\na <b>bb</b>ccc\nccccc\ncccc</code></pre>'
    assert wrapped.startswith(expected)
    assert wrapped.endswith("<p>" + "d" * 30 + "</p>")  # prose untouched
    assert code_wrap_chars(335.0, 12.0) == 53 and code_wrap_chars(2000.0, 12.0) == 90
    assert code_wrap_chars(335.0, 10.5) == 61  # a smaller source body size fits more


# --------------------------------------------------------------------------- #
# Fonts (FR-53, E-55, NFR-24, NFR-25)
# --------------------------------------------------------------------------- #


def test_resolve_font_without_a_file_is_the_builtin_spec() -> None:
    assert unwrap(resolve_font(None)) == BUILTIN_FONT and BUILTIN_FONT.is_builtin
    assert TURKISH_PROBE == "çğıİöşüÇĞİÖŞÜ"


def test_font_file_format_and_existence_are_validated(tmp_path: Path) -> None:
    missing = validate_turkish_coverage(tmp_path / "nope.ttf")
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.FONT_UNSUPPORTED
    assert missing.error.scope is ErrorScope.USER and exit_code_for(missing.error) == 1
    text = tmp_path / "font.txt"
    text.write_text("hello", encoding="utf-8")
    wrong = validate_turkish_coverage(text)
    assert isinstance(wrong, Err) and "unsupported format" in wrong.error.message
    garbage = tmp_path / "garbage.ttf"
    garbage.write_bytes(b"\x00\x01garbage")
    broken = validate_turkish_coverage(garbage)
    assert isinstance(broken, Err) and broken.error.code is ErrorCode.FONT_UNSUPPORTED
    assert "not a usable font" in broken.error.message


def test_font_lacking_a_turkish_glyph_is_rejected_before_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    font = builtin_font_file(tmp_path)
    real_has_glyph = fonts_module.pdfkit.font_has_glyph
    monkeypatch.setattr(
        fonts_module.pdfkit, "font_has_glyph",
        lambda f, char: False if char in "ğĞ" else real_has_glyph(f, char),
    )
    rejected = resolve_font(font)
    assert isinstance(rejected, Err) and rejected.error.code is ErrorCode.FONT_UNSUPPORTED
    assert "ğĞ" in rejected.error.message and rejected.error.context["missing_glyphs"] == "ğĞ"
    assert "omit --pdf-font" in rejected.error.message

    def never_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("render_reflowed_pdf must not run with a rejected font")

    monkeypatch.setattr(pdf_exporter_module, "render_reflowed_pdf", never_called)
    result = PdfExporter().export(
        assembled(FIXTURE_DOC), tmp_path, ExportOptions(font_path=font)
    )
    assert isinstance(result, Err) and result.error.code is ErrorCode.FONT_UNSUPPORTED
    assert not (tmp_path / "translated_book.pdf").exists()


def test_user_font_is_embedded_once_subset_and_the_file_stays_small(tmp_path: Path) -> None:
    font = builtin_font_file(tmp_path)  # Nimbus Roman: not one of the engine's defaults
    spec = unwrap(resolve_font(font))
    assert isinstance(spec, FontSpec) and not spec.is_builtin
    assert spec.family_css == "BookFont" and spec.archive_dir == font.resolve().parent
    assert 'src: url("tiro.otf")' in spec.face_css and "Nimbus Roman" in spec.family_name
    artifact, doc = export(LONG_DOC + "\n\n" + FIXTURE_DOC, tmp_path, font_path=font)
    names = font_names(doc)
    roman = [name for name in names if "Nimbus Roman" in name]
    assert len(roman) == 1 and re.match(r"^[A-Z]{6}\+", roman[0]), names
    assert not any("Charis" in name for name in names), names  # body font replaced
    assert artifact.bytes_written <= 2 * 1024 * 1024
    assert TURKISH in all_text(doc)


def test_round_trip_fixture_is_under_two_megabytes(tmp_path: Path) -> None:
    write_png(tmp_path / "images" / "p001-f01.png", 1200, 900, dpi=200)
    markdown = (
        LONG_DOC + '\n\n<!-- image:1 src="images/p001-f01.png" -->\n\n' + FIXTURE_DOC
    )
    artifact, doc = export(markdown, tmp_path)
    assert artifact.bytes_written <= 2 * 1024 * 1024 and doc.page_count > 3
    assert all(re.match(r"^[A-Z]{6}\+", name) for name in font_names(doc))


# --------------------------------------------------------------------------- #
# Facade / engines / orchestrator
# --------------------------------------------------------------------------- #


def test_pdf_exporter_is_always_available_and_reports_engines() -> None:
    assert PdfExporter.is_available() is True and PdfExporter.optional is True
    engines = PdfExporter.engines()
    assert engines["native"] is True and set(engines) == {"native", "weasyprint"}
    assert engines["weasyprint"] == pdf_exporter_module.weasyprint_available()


def test_weasyprint_engine_without_weasyprint_is_exporter_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_exporter_module, "weasyprint_available", lambda: False)
    result = PdfExporter().export(
        assembled(FIXTURE_DOC), tmp_path, ExportOptions(pdf_engine="weasyprint")
    )
    assert isinstance(result, Err) and result.error.code is ErrorCode.EXPORTER_UNAVAILABLE
    assert result.error.scope is ErrorScope.USER and "--pdf-engine native" in result.error.message
    assert not (tmp_path / "translated_book.pdf").exists()
    unknown = PdfExporter().export(assembled(FIXTURE_DOC), tmp_path, ExportOptions(pdf_engine="x"))
    assert isinstance(unknown, Err) and unknown.error.code is ErrorCode.EXPORTER_UNAVAILABLE


def test_orchestrator_weasyprint_missing_gives_exit_2_and_keeps_md_and_epub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_exporter_module, "weasyprint_available", lambda: False)
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1, 2, 3])
    orch, _ = build(ws, __import__("tests.conftest", fromlist=["FakeTranslator"]).FakeTranslator())
    outcome = unwrap(orch.export(
        formats=["md", "epub", "pdf"], options=ExportOptions(pdf_engine="weasyprint")
    ))
    assert outcome.exit_code == EXIT_PARTIAL
    assert set(outcome.outputs) == {"markdown", "epub"}
    assert any(w.startswith("export_failed:pdf:") for w in outcome.warnings)
    assert (ws.output / "translated_book.md").exists()
    assert (ws.output / "translated_book.epub").exists()
    assert not (ws.output / "translated_book.pdf").exists()


def test_orchestrator_native_pdf_is_produced_by_default(tmp_path: Path) -> None:
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1, 2, 3])
    from tests.conftest import FakeTranslator

    orch, _ = build(ws, FakeTranslator())
    outcome = unwrap(orch.export(formats=["md", "pdf"]))
    assert outcome.exit_code == EXIT_SUCCESS and set(outcome.outputs) == {"markdown", "pdf"}
    doc = open_pdf(ws.output / "translated_book.pdf")
    assert doc.page_count >= 1 and "OLD:1" in all_text(doc)


def test_orchestrator_validates_the_font_before_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import FakeTranslator

    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1, 2, 3])
    font = builtin_font_file(tmp_path)
    monkeypatch.setattr(fonts_module.pdfkit, "font_has_glyph", lambda f, char: char != "ğ")
    orch, _ = build(ws, FakeTranslator())
    refused = orch.export(formats=["md", "pdf"], options=ExportOptions(font_path=font))
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.FONT_UNSUPPORTED
    assert exit_code_for(refused.error) == 1
    assert not (ws.output / "translated_book.md").exists()  # nothing assembled or written

    # Without a PDF format the font is irrelevant and not checked (FR-53 scope).
    orch2, _ = build(ws, FakeTranslator(), run_id="run000000002")
    fine = unwrap(orch2.export(formats=["md"], options=ExportOptions(font_path=font)))
    assert fine.exit_code == EXIT_SUCCESS


# --------------------------------------------------------------------------- #
# --pdf-margin parsing table (design doc 06, 7.1)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("18mm 15mm 20mm 15mm", (18.0, 15.0, 20.0, 15.0)),
        ("10mm", (10.0, 10.0, 10.0, 10.0)),
        ("12mm 8mm", (12.0, 8.0, 12.0, 8.0)),
        ("72pt", (25.4, 25.4, 25.4, 25.4)),
        ("10 20", (10.0, 20.0, 10.0, 20.0)),  # bare numbers are millimetres
        ("  0mm,0mm,0mm,0mm ", (0.0, 0.0, 0.0, 0.0)),
        ("1.5MM 2.25PT", (1.5, 2.25 * 25.4 / 72.0, 1.5, 2.25 * 25.4 / 72.0)),
    ],
)
def test_parse_pdf_margin_accepts_css_style_values(
    text: str, expected: Tuple[float, float, float, float]
) -> None:
    assert unwrap(parse_pdf_margin(text)) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text", ["", "1mm 2mm 3mm", "a b c d", "-5mm", "10cm", "1mm 2mm 3mm 4mm 5mm"]
)
def test_parse_pdf_margin_rejects_bad_input(text: str) -> None:
    result = parse_pdf_margin(text)
    assert isinstance(result, Err) and result.error.scope is ErrorScope.USER
    assert "--pdf-margin" in result.error.message


def test_export_options_defaults_follow_the_source() -> None:
    """User decision (v1.1): no A5 / 12 pt defaults -- the page design is the source's."""
    options = ExportOptions()
    assert options.pdf_engine == "native" and options.page_size == "source"
    assert options.margins_mm is None  # CR-66: no silent 18/15/20/15 mm default
    assert options.page_rect_pt is None and options.body_font_pt is None
    assert options.margins_pt is None and options.body_family is None
    # nothing known -> proportional margins (10 % of the page), recorded as assumed
    design = unwrap(resolve_page_design(options))
    assert "margins" in design.assumed
    width, height = design.geometry.mediabox[2], design.geometry.mediabox[3]
    assert design.margins_pt == pytest.approx(
        (height * 0.1, width * 0.1, height * 0.1, width * 0.1)
    )
    assert design.warnings == [DEFAULTS_ASSUMED_WARNING]


# --------------------------------------------------------------------------- #
# Source-derived page design (user decision: detect -> translate -> write back)
# --------------------------------------------------------------------------- #

SOURCE_RECT = (595.276, 790.866)  # docs/test_doc.pdf page size
SOURCE_MARGINS = (44.0, 58.0, 108.0, 57.0)  # top, right, bottom, left (pt)


def test_source_page_rect_body_font_and_margins_are_honoured(tmp_path: Path) -> None:
    markdown = "# Başlık\n\nGövde metni sayfanın kaynak kenar boşluğunda başlar.\n\nİkinci.\n"
    artifact, doc = export(
        markdown, tmp_path, page_size="source", page_rect_pt=SOURCE_RECT,
        body_font_pt=10.6, margins_pt=SOURCE_MARGINS,
    )
    assert DEFAULTS_ASSUMED_WARNING not in artifact.warnings and artifact.warnings == []
    page = doc[0]
    x0, y0, x1, y1 = pdfkit.rect_tuple(page.rect)
    assert (x1, y1) == pytest.approx(SOURCE_RECT, abs=0.01)
    words = pdfkit.page_words(page)
    body = [w for w in words if w[4] == "Gövde"][0]
    assert abs(float(body[0]) - SOURCE_MARGINS[3]) <= 1.0  # left margin from the source
    # body spans at the source size (10.6 pt), the H1 at 1.6 x that (book.css)
    sizes = {
        span["text"].strip(): float(span["size"])
        for block in pdfkit.page_text_dict(page)["blocks"] if block["type"] == 0
        for line in block["lines"] for span in line["spans"]
    }
    body_size = next(size for text, size in sizes.items() if text.startswith("Gövde"))
    assert body_size == pytest.approx(10.6, abs=0.15)
    assert sizes["Başlık"] == pytest.approx(10.6 * 1.6, abs=0.3)
    design = unwrap(resolve_page_design(ExportOptions(
        page_rect_pt=SOURCE_RECT, body_font_pt=10.6, margins_pt=SOURCE_MARGINS
    )))
    assert design.assumed == () and design.body_pt == 10.6
    assert design.geometry.column == pytest.approx(
        (57.0, 44.0, SOURCE_RECT[0] - 58.0, SOURCE_RECT[1] - 108.0)
    )


def test_explicit_page_size_and_margins_win_over_the_source_values() -> None:
    design = unwrap(resolve_page_design(ExportOptions(
        page_size="A4", margins_mm=(20, 20, 20, 20),
        page_rect_pt=SOURCE_RECT, body_font_pt=10.6, margins_pt=None,
    )))
    assert design.geometry.mediabox[2:] == pytest.approx((595.0, 842.0), abs=1.0)
    assert design.geometry.column[0] == pytest.approx(20 * MM_TO_PT)
    assert design.body_pt == 10.6 and design.assumed == ()
    # explicit --pdf-margin (margins_pt) beats margins_mm; explicit paper beats page_rect_pt
    design2 = unwrap(resolve_page_design(ExportOptions(
        page_size="Letter", margins_pt=(10.0, 10.0, 10.0, 10.0), page_rect_pt=SOURCE_RECT,
        body_font_pt=9.0,
    )))
    assert design2.geometry.mediabox[2:] == pytest.approx((612.0, 792.0), abs=1.0)
    assert design2.geometry.column[:2] == (10.0, 10.0) and design2.body_pt == 9.0


def test_nothing_given_falls_back_to_letter_and_warns_once(tmp_path: Path) -> None:
    design = unwrap(resolve_page_design(ExportOptions()))
    assert design.assumed == ("page_size", "margins", "body_font")
    assert design.body_pt == FALLBACK_BODY_PT
    assert design.geometry.mediabox[2:] == pytest.approx((612.0, 792.0), abs=1.0)
    artifact = unwrap(PdfExporter().export(assembled(FIXTURE_DOC), tmp_path, ExportOptions()))
    assert artifact.warnings.count(DEFAULTS_ASSUMED_WARNING) == 1
    doc = open_pdf(artifact.path)
    assert pdfkit.rect_tuple(doc[0].rect)[2:] == pytest.approx((612.0, 792.0), abs=1.0)
    # only the body size missing -> still one warning; nothing missing -> none
    half = unwrap(resolve_page_design(ExportOptions(
        page_rect_pt=SOURCE_RECT, margins_pt=SOURCE_MARGINS
    )))
    assert half.assumed == ("body_font",)
    no_margins = unwrap(resolve_page_design(ExportOptions(
        page_rect_pt=SOURCE_RECT, body_font_pt=11
    )))
    assert no_margins.assumed == ("margins",)  # CR-66: never a silent built-in margin
    full = unwrap(resolve_page_design(ExportOptions(
        page_rect_pt=SOURCE_RECT, body_font_pt=11, margins_pt=SOURCE_MARGINS
    )))
    assert full.assumed == () and full.warnings == []
    bad = resolve_page_design(ExportOptions(
        page_rect_pt=(100.0, 100.0), body_font_pt=11, margins_mm=(18.0, 15.0, 20.0, 15.0)
    ))
    assert isinstance(bad, Err) and "no room" in bad.error.message
    assert bad.error.scope is ErrorScope.USER


# --------------------------------------------------------------------------- #
# v1.1 review: CR-63 (USER errors), CR-65 (profile ranges), CR-66 (serif, margins)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("engine", ["native", "weasyprint"])
def test_bad_page_design_is_a_user_error_for_either_engine(engine: str, tmp_path: Path) -> None:
    """CR-63: the orchestrator maps USER to exit 1; nothing is written. The check runs
    before the engine is touched, so it does not depend on weasyprint being installed."""
    for options in (
        ExportOptions(pdf_engine=engine, page_size="Foo"),
        ExportOptions(pdf_engine=engine, page_size="A5", margins_mm=(200.0, 200.0, 200.0, 200.0)),
    ):
        result = PdfExporter().export(assembled(FIXTURE_DOC), tmp_path, options)
        assert isinstance(result, Err) and result.error.scope is ErrorScope.USER
        assert result.error.code is ErrorCode.EXPORT_FAILED
    assert "Foo" in unwrap_err(
        PdfExporter().export(assembled(FIXTURE_DOC), tmp_path,
                             ExportOptions(pdf_engine=engine, page_size="Foo"))
    )
    assert not (tmp_path / "translated_book.pdf").exists()


def unwrap_err(result: Any) -> str:
    assert isinstance(result, Err)
    return result.error.message


@pytest.mark.parametrize(
    ("overrides", "invalid", "assumed"),
    [
        ({"margins_pt": (-50.0, 58.0, 108.0, 57.0)}, ("margins",), ("margins",)),
        ({"margins_pt": (44.0, 400.0, 108.0, 57.0)}, ("margins",), ("margins",)),  # > half page
        ({"margins_pt": (44.0, 58.0, float("nan"), 57.0)}, ("margins",), ("margins",)),
        ({"page_rect_pt": (1e9, 1e9)}, ("page_size",), ("page_size",)),
        ({"page_rect_pt": (10.0, 10.0)}, ("page_size",), ("page_size",)),
        ({"body_font_pt": 500.0}, ("body_font",), ("body_font",)),
        ({"body_font_pt": 1.0}, ("body_font",), ("body_font",)),
    ],
)
def test_out_of_range_source_profile_values_fall_back_with_a_warning(
    overrides: Dict[str, Any], invalid: Tuple[str, ...], assumed: Tuple[str, ...]
) -> None:
    """CR-65: ``source_profile.json`` is user-editable; text must never leave the page."""
    base: Dict[str, Any] = {
        "page_rect_pt": SOURCE_RECT, "body_font_pt": 10.6, "margins_pt": SOURCE_MARGINS,
    }
    design = unwrap(resolve_page_design(ExportOptions(**{**base, **overrides})))
    assert design.invalid == invalid and design.assumed == assumed
    assert design.warnings == [
        DEFAULTS_ASSUMED_WARNING, *[f"pdf_page_design_invalid:{name}" for name in invalid]
    ]
    x0, y0, x1, y1 = design.geometry.mediabox
    cx0, cy0, cx1, cy1 = design.geometry.column
    assert x0 <= cx0 < cx1 <= x1 and y0 <= cy0 < cy1 <= y1  # the column is inside the page
    assert 72.0 <= x1 - x0 <= 14400.0 and 4.0 <= design.body_pt <= 72.0


def test_invalid_profile_values_reach_the_artifact_warnings(tmp_path: Path) -> None:
    options = ExportOptions(page_rect_pt=SOURCE_RECT, body_font_pt=10.6,
                            margins_pt=(-50.0, 58.0, 108.0, 57.0))
    artifact = unwrap(PdfExporter().export(assembled(FIXTURE_DOC), tmp_path, options))
    assert "pdf_page_design_invalid:margins" in artifact.warnings
    assert artifact.warnings.count(DEFAULTS_ASSUMED_WARNING) == 1
    doc = open_pdf(artifact.path)
    width = doc[0].rect.width
    for page in doc:
        for block in pdfkit.page_text_dict(page)["blocks"]:
            assert 0.0 <= block["bbox"][0] and block["bbox"][2] <= width  # nothing off the page
    doc.close()


def test_sans_source_gets_a_sans_body(tmp_path: Path) -> None:
    """CR-66 (a): ``source_profile.serif = false`` -> ``ExportOptions.body_family="sans"``."""
    assert "html, body { font-family: serif; }" in font_css(BUILTIN_FONT)
    assert "html, body { font-family: sans-serif; }" in font_css(BUILTIN_FONT, "sans")
    assert "html, body { font-family: serif; }" in font_css(BUILTIN_FONT, "serif")
    markdown = "# Başlık\n\nGövde metni burada duruyor ve yeterince uzun.\n"

    def body_font(family: Any) -> str:
        target = tmp_path / str(family)
        target.mkdir()
        _artifact, doc = export(markdown, target, body_family=family)
        spans = [
            span for block in pdfkit.page_text_dict(doc[0])["blocks"] if block["type"] == 0
            for line in block["lines"] for span in line["spans"]
            if span["text"].startswith("Gövde")
        ]
        doc.close()
        return str(spans[0]["font"])

    serif, sans = body_font(None), body_font("sans")
    assert serif != sans and "Sans" in sans and "Sans" not in serif
