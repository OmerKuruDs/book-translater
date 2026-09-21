"""Assembler and exporters (design doc 02, sections 8 and 11)."""

from __future__ import annotations

import base64
import dataclasses
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import ebooklib  # type: ignore[import-untyped]
import pymupdf
import pytest
from ebooklib import epub  # type: ignore[import-untyped]

from book_translator.domain.models import AssembledDocument, Chunk, ChunkKind, ChunkStatus
from book_translator.domain.result import Err, ErrorCode, ErrorScope, unwrap
from book_translator.exporters import pdf_exporter as pdf_exporter_module
from book_translator.exporters.base import (
    ExportOptions,
    atomic_write_bytes,
    get_exporter,
    list_exporters,
    markdown_to_html,
)
from book_translator.exporters.epub_exporter import FRONT_MATTER_TITLE, EpubExporter
from book_translator.exporters.markdown_exporter import MarkdownExporter
from book_translator.exporters.pdf_exporter import PdfExporter, build_html_document
from book_translator.pipeline.assembler import (
    assemble,
    escape_label,
    escape_ordinals,
    scan_structure,
)
from book_translator.pipeline.chunker import chunk
from tests.conftest import make_chunk

TURKISH = "çğıİöşü"
SOURCE_DOC = (
    "# Chapter One\n\n"
    "First paragraph with a footnote[^1].\n\n"
    "## Section A\n\n"
    "- alpha\n- beta\n\n"
    "<!-- image:1 -->\n\n"
    "```python\nprint('x')\n```\n\n"
    "# Chapter Two\n\n"
    "## Section B\n\n"
    "### Deep\n\n"
    "Second chapter text.\n\n"
    "[^1]: Footnote text.\n"
)


def completed(chunk: Chunk, translated: Optional[str] = None) -> Chunk:
    return dataclasses.replace(
        chunk,
        status=ChunkStatus.COMPLETED,
        translated_text=chunk.source_text if translated is None else translated,
    )


DEFAULT_ERROR = "provider_transient: connection reset after 5 attempts"


def failed(chunk: Chunk, error: str = DEFAULT_ERROR) -> Chunk:
    return dataclasses.replace(chunk, status=ChunkStatus.FAILED, last_error=error)


def identity_chunks(texts: Sequence[str]) -> List[Chunk]:
    return [completed(make_chunk(i, text)) for i, text in enumerate(texts, start=1)]


def split_source(markdown: str) -> List[str]:
    """Split at blank lines into chunk texts whose concatenation is the input."""
    parts = re.split(r"(?<=\n\n)", markdown)
    return [p for p in parts if p]


def assembled(markdown: str = SOURCE_DOC, **metadata: str) -> AssembledDocument:
    meta = {"job_id": "job-1", "source_file": "book.pdf", **metadata}
    return unwrap(
        assemble(identity_chunks(split_source(markdown)), allow_partial=False, metadata=meta)
    )


def epub_xhtml(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as zf:
        return {
            name.split("/")[-1]: zf.read(name).decode("utf-8")
            for name in zf.namelist()
            if name.endswith(".xhtml")
        }


def flatten_toc(items: Any) -> List[str]:
    titles: List[str] = []
    for item in items:
        if isinstance(item, tuple):
            titles.append(item[0].title)
            titles.extend(flatten_toc(item[1]))
        else:
            titles.append(item.title)
    return titles


# ---------------------------------------------------------------------------
# assembler


def test_assembler_preserves_heading_hierarchy() -> None:
    doc = assembled()
    assert doc.markdown == SOURCE_DOC
    assert doc.heading_outline == [
        (1, "Chapter One"), (2, "Section A"), (1, "Chapter Two"), (2, "Section B"), (3, "Deep")
    ]
    assert doc.heading_outline == list(scan_structure(SOURCE_DOC).headings)
    assert doc.warnings == []
    assert doc.failed_chunk_ids == [] and doc.review_chunk_ids == []
    assert doc.metadata == {"job_id": "job-1", "source_file": "book.pdf"}


def test_assembler_source_markdown_check_and_parity_warning() -> None:
    chunks = identity_chunks(split_source(SOURCE_DOC))
    doc = unwrap(assemble(chunks, allow_partial=False, metadata={}, source_markdown=SOURCE_DOC))
    assert doc.warnings == []
    # a hand-edited chunk drops its heading marker and a list item
    edited = list(chunks)
    edited[0] = completed(edited[0], "Chapter One\n\n")
    edited[3] = completed(edited[3], "- alpha\n\n")
    doc = unwrap(assemble(edited, allow_partial=False, metadata={}, source_markdown=SOURCE_DOC))
    assert doc.review_chunk_ids == [1, 4]
    assert any(w.startswith("structure_mismatch:headings:chunk 1:") for w in doc.warnings)
    assert any(w.startswith("structure_mismatch:list_items:chunk 4:") for w in doc.warnings)
    assert any(w.startswith("structure_mismatch:headings:document:") for w in doc.warnings)
    assert doc.heading_outline[0] == (2, "Section A")


def test_assembler_partial_markers_when_allowed() -> None:
    chunks = identity_chunks(split_source(SOURCE_DOC))
    chunks[1] = failed(chunks[1])
    doc = unwrap(assemble(chunks, allow_partial=True, metadata={}))
    assert doc.failed_chunk_ids == [2]
    expected = (
        "> **[UNTRANSLATED — chunk 2 — provider_transient: connection reset after 5 attempts]**\n\n"
        "First paragraph with a footnote[^1].\n\n"
        "> **[END UNTRANSLATED — chunk 2]**\n\n"
    )
    assert expected in doc.markdown
    assert doc.markdown.startswith("# Chapter One\n\n" + expected + "## Section A")
    assert doc.heading_outline == assembled().heading_outline
    assert not any("chunk 2" in w for w in doc.warnings)


def test_assembler_refuses_partial_without_flag() -> None:
    chunks = identity_chunks(split_source(SOURCE_DOC))
    chunks[1] = failed(chunks[1])
    result = assemble(chunks, allow_partial=False, metadata={})
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.EXPORT_REFUSED_PARTIAL
    assert result.error.scope is ErrorScope.USER
    assert result.error.context["chunk_id"] == 2
    assert "allow-partial" in result.error.message


@pytest.mark.parametrize("status", [ChunkStatus.PENDING, ChunkStatus.PROCESSING])
def test_assembler_refuses_untranslated_chunks(status: ChunkStatus) -> None:
    chunks = identity_chunks(split_source(SOURCE_DOC))
    chunks[2] = dataclasses.replace(chunks[2], status=status, translated_text=None)
    for allow in (False, True):
        result = assemble(chunks, allow_partial=allow, metadata={})
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.EXPORT_REFUSED_PARTIAL
        assert "translate" in result.error.message
        assert result.error.context["status"] == status.value


def test_assembler_non_translatable_copied_verbatim_and_streams() -> None:
    texts = ["# H\n\n", "```\nraw\n```\n\n", "<!-- image:2 -->\n\n", "tail\n"]
    seen: List[int] = []

    def stream() -> Iterator[Chunk]:
        for i, text in enumerate(texts, start=1):
            seen.append(i)
            translatable = i not in (2, 3)
            chunk = make_chunk(i, text, translatable=translatable, kind=ChunkKind.CODE)
            yield completed(chunk, "# H\n\n" if i == 1 else "tail\n") if translatable else chunk

    doc = unwrap(assemble(stream(), allow_partial=False, metadata={}))
    assert doc.markdown == "".join(texts)
    assert seen == [1, 2, 3, 4]
    assert doc.warnings == []


def test_assembler_concatenates_subchunks_before_parity() -> None:
    parent = [
        make_chunk(1, "- one\n- two\n", parent_block_id=7, sub_index=0, sub_count=2),
        make_chunk(2, "- three\n\n", parent_block_id=7, sub_index=1, sub_count=2),
    ]
    # items move across the sub-chunk boundary: per-chunk counts differ, the group matches
    chunks = [completed(parent[0], "- bir\n"), completed(parent[1], "- iki\n- üç\n\n")]
    doc = unwrap(assemble(chunks, allow_partial=False, metadata={}))
    assert doc.warnings == [] and doc.review_chunk_ids == []
    broken = [completed(parent[0], "- bir\n"), completed(parent[1], "- iki üç\n\n")]
    doc = unwrap(assemble(broken, allow_partial=False, metadata={}))
    assert doc.review_chunk_ids == [1, 2]
    assert any(w.startswith("structure_mismatch:list_items:chunks 1-2:") for w in doc.warnings)


LIVE_SOURCE = (
    "Chapter 3 covers image processing, which is needed in almost all computer vision "
    "applications. Chapter 3 also presents applications such as seamless image blending.\n\n"
)
LIVE_TRANSLATION = (
    "3. Bölüm, neredeyse tüm bilgisayar görme uygulamalarında gerekli olan görüntü işleme "
    "konusunu ele almaktadır. 3. Bölümde ayrıca kesintisiz görüntü birleştirme gibi "
    "uygulamalar da ele alınmaktadır.\n\n"
)


def test_assembler_escapes_turkish_ordinal_at_paragraph_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """CR-34 (a): the live pair 'Chapter 3 covers …' -> '3. Bölüm, …' stays a paragraph."""
    chunks = [
        completed(make_chunk(1, "Intro paragraph.\n\n"), "Giriş paragrafı.\n\n"),
        completed(make_chunk(2, LIVE_SOURCE), LIVE_TRANSLATION),
        completed(make_chunk(3, "Chapter 4 begins with data fitting.\n\n"),
                  "4. Bölüm, veri uyumu ile başlar.\n\n"),
    ]
    with caplog.at_level("DEBUG", logger="book_translator.pipeline.assembler"):
        doc = unwrap(assemble(chunks, allow_partial=False, metadata={}))
    assert doc.warnings == [] and doc.review_chunk_ids == []
    assert doc.markdown.startswith("Giriş paragrafı.\n\n3\\. Bölüm, neredeyse")
    assert "3\\. Bölümde ayrıca" not in doc.markdown  # mid-paragraph occurrence untouched
    assert "\n\n4\\. Bölüm, veri" in doc.markdown
    escaped = [r for r in caplog.records if getattr(r, "event", None) == "ordinal_escaped"]
    assert [r.chunk_id for r in escaped] == [2, 3]  # type: ignore[attr-defined]
    html = markdown_to_html(doc.markdown).html
    assert "<p>3. Bölüm, neredeyse" in html and "<p>4. Bölüm, veri" in html
    assert "<ol" not in html
    # the Markdown exporter writes the escaped form verbatim
    unwrap(MarkdownExporter(render_placeholders=False).export(doc, tmp_path, ExportOptions()))
    text = (tmp_path / "translated_book.md").read_text(encoding="utf-8")
    assert "3\\. Bölüm, neredeyse" in text


def test_assembler_still_flags_dropped_ordered_items() -> None:
    """CR-34 (b): a real 3-item ordered list translated with 2 items is a mismatch."""
    source = "Steps:\n\n1. first\n2. second\n3. third\n\n"
    chunk = make_chunk(1, source)
    doc = unwrap(assemble(
        [completed(chunk, "Adımlar:\n\n1. birinci\n2. ikinci\n\n")],
        allow_partial=False, metadata={},
    ))
    assert doc.review_chunk_ids == [1]
    assert any(
        w.startswith("structure_mismatch:list_items:chunk 1:source=3 translated=2")
        for w in doc.warnings
    )


def test_assembler_keeps_translated_ordered_lists_intact() -> None:
    """CR-34 (c): source ordered list translated as 1. 2. 3. -> no warning, no escaping."""
    source = "Steps:\n\n1. first\n2. second\n3. third\n\n"
    translated = "Adımlar:\n\n1. birinci\n2. ikinci\n3. üçüncü\n\n"
    doc = unwrap(assemble(
        [completed(make_chunk(1, source), translated)], allow_partial=False, metadata={}
    ))
    assert doc.warnings == [] and doc.review_chunk_ids == []
    assert doc.markdown == translated
    assert "\\." not in doc.markdown
    assert '<ol>' in markdown_to_html(doc.markdown).html


def test_assembler_leaves_mid_paragraph_ordinals_alone() -> None:
    """CR-34 (d): '3. Bölümde' after a prose line (not '1.') cannot start a list."""
    source = "This book is long.\nChapter 3 covers filtering.\n\n"
    translated = "Bu kitap uzun.\n3. Bölümde filtreleme anlatılır.\n\n"
    doc = unwrap(assemble(
        [completed(make_chunk(1, source), translated)], allow_partial=False, metadata={}
    ))
    assert doc.warnings == []
    assert doc.markdown == translated
    assert "<p>Bu kitap uzun.\n3. Bölümde" in markdown_to_html(doc.markdown).html


@pytest.mark.parametrize(
    ("text", "expected", "count"),
    [
        ("3. Bölüm, x\n\n", "3\\. Bölüm, x\n\n", 1),
        ("Intro.\n\n3) Bölüm\n", "Intro.\n\n3\\) Bölüm\n", 1),
        ("Intro.\n1. Bölüm interrupts\n", "Intro.\n1\\. Bölüm interrupts\n", 1),
        ("Intro.\n3. Bölüm no\n", "Intro.\n3. Bölüm no\n", 0),
        ("    3. indented code\n", "    3. indented code\n", 0),
        ("```\n3. in fence\n```\n", "```\n3. in fence\n```\n", 0),
        ("4.2 bölümü\n", "4.2 bölümü\n", 0),
        ("- a\n2. b\n", "- a\n2\\. b\n", 1),
    ],
)
def test_escape_ordinals_follows_commonmark_start_rule(
    text: str, expected: str, count: int
) -> None:
    assert escape_ordinals(text) == (expected, count)


def test_scan_structure_splits_bullet_and_ordered_items() -> None:
    text = "- a\n- b\n\n1. x\n   more x\n2. y\n\nprose\n3. not an item\n"
    structure = scan_structure(text)
    assert (structure.bullet_items, structure.ordered_items) == (2, 2)
    assert structure.list_items == 4


def test_assembler_carries_review_flags_and_rejects_disorder() -> None:
    chunks = identity_chunks(["a\n\n", "b\n\n"])
    chunks[1] = dataclasses.replace(chunks[1], review_flag=True)
    doc = unwrap(assemble(chunks, allow_partial=False, metadata={}))
    assert doc.review_chunk_ids == [2]
    result = assemble(reversed(chunks), allow_partial=False, metadata={})
    assert isinstance(result, Err) and result.error.code is ErrorCode.INTERNAL


# ---------------------------------------------------------------------------
# markdown exporter


def test_markdown_exporter_identity_without_placeholders(tmp_path: Path) -> None:
    doc = assembled(SOURCE_DOC.replace("Chapter One", f"Bölüm {TURKISH}"))
    exporter = MarkdownExporter(render_placeholders=False)
    artifact = unwrap(exporter.export(doc, tmp_path, ExportOptions()))
    assert artifact.path == tmp_path / "translated_book.md"
    assert artifact.format == "markdown"
    raw = artifact.path.read_bytes()
    assert raw == doc.markdown.encode("utf-8")
    assert artifact.bytes_written == len(raw)
    assert b"\r\n" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    assert TURKISH.encode("utf-8") in raw


def test_markdown_exporter_localizes_placeholders(tmp_path: Path) -> None:
    doc = assembled()
    exporter = unwrap(get_exporter("markdown"))
    assert isinstance(exporter, MarkdownExporter) and exporter.render_placeholders
    unwrap(exporter.export(doc, tmp_path, ExportOptions()))
    text = (tmp_path / "translated_book.md").read_text(encoding="utf-8")
    assert "<!-- image:1 -->" not in text
    assert "*[Şekil 1 — görsel dahil edilmedi]*" in text
    assert text.replace("*[Şekil 1 — görsel dahil edilmedi]*", "<!-- image:1 -->") == SOURCE_DOC


# ---------------------------------------------------------------------------
# epub exporter


@pytest.fixture
def epub_path(tmp_path: Path) -> Path:
    doc = assembled(SOURCE_DOC.replace("Second chapter text.", f"İkinci bölüm {TURKISH}."))
    artifact = unwrap(EpubExporter().export(doc, tmp_path, ExportOptions(author="Yazar")))
    assert artifact.format == "epub" and artifact.warnings == []
    return artifact.path


def test_epub_opens_and_declares_turkish(epub_path: Path) -> None:
    book = epub.read_epub(str(epub_path))
    assert book.get_metadata("DC", "language") == [("tr", {})]
    assert book.get_metadata("DC", "title")[0][0] == "Chapter One"
    assert book.get_metadata("DC", "creator")[0][0] == "Yazar"
    assert book.get_metadata("DC", "source")[0][0] == "book.pdf"
    identifier = book.get_metadata("DC", "identifier")[0][0]
    assert identifier.startswith("urn:uuid:") and len(identifier) == len("urn:uuid:") + 36
    for name, content in epub_xhtml(epub_path).items():
        if name != "nav.xhtml":
            assert 'lang="tr"' in content and 'xml:lang="tr"' in content


def test_epub_chapters_and_toc_follow_headings(epub_path: Path) -> None:
    files = epub_xhtml(epub_path)
    chapters = sorted(name for name in files if name.startswith("ch"))
    assert chapters == ["ch000.xhtml", "ch001.xhtml"]  # two H1, no front matter
    assert "<h1" in files["ch000.xhtml"] and "Chapter One" in files["ch000.xhtml"]
    assert "Chapter Two" in files["ch001.xhtml"]
    assert 'class="figure-placeholder"' in files["ch000.xhtml"]
    assert "Şekil 1 — görsel dahil edilmedi" in files["ch000.xhtml"]
    assert "fn:1" in files["ch000.xhtml"] and "Footnote text." in files["ch000.xhtml"]
    assert "Footnote text." not in files["ch001.xhtml"]
    book = epub.read_epub(str(epub_path))
    assert flatten_toc(book.toc) == ["Chapter One", "Section A", "Chapter Two", "Section B"]
    assert [item[0].href for item in book.toc] == ["ch000.xhtml", "ch001.xhtml"]
    assert book.toc[0][1][0].href == "ch000.xhtml#bt-000-2"
    with zipfile.ZipFile(epub_path) as zf:
        assert "EPUB/toc.ncx" in zf.namelist() and "EPUB/nav.xhtml" in zf.namelist()
        opf = zf.read("EPUB/content.opf").decode("utf-8")
        assert opf.index('idref="nav"') < opf.index('idref="ch000"') < opf.index('idref="ch001"')
        assert "EPUB/style/book.css" in zf.namelist()
        assert ".figure-placeholder" in zf.read("EPUB/style/book.css").decode("utf-8")


def test_epub_front_matter_and_h2_fallback(tmp_path: Path) -> None:
    markdown = (
        "Preface text before any heading.\n\n"
        "# Only Title\n\n"
        "## Part A\n\nText a.\n\n"
        "## Part B\n\nText b.\n\n"
        "### Sub B\n\nText.\n"
    )
    doc = assembled(markdown)
    artifact = unwrap(EpubExporter().export(doc, tmp_path, ExportOptions()))
    assert artifact.warnings == ["chapter_split_fallback:h2"]
    files = epub_xhtml(artifact.path)
    chapters = sorted(name for name in files if name.startswith("ch"))
    assert chapters == ["ch000.xhtml", "ch001.xhtml", "ch002.xhtml"]
    assert FRONT_MATTER_TITLE in files["ch000.xhtml"] and "Preface text" in files["ch000.xhtml"]
    book = epub.read_epub(str(artifact.path))
    assert flatten_toc(book.toc) == [FRONT_MATTER_TITLE, "Only Title", "Part A", "Part B", "Sub B"]


def test_epub_turkish_round_trip(epub_path: Path) -> None:
    files = epub_xhtml(epub_path)
    assert f"İkinci bölüm {TURKISH}." in files["ch001.xhtml"]
    with zipfile.ZipFile(epub_path) as zf:
        assert TURKISH.encode("utf-8") in zf.read("EPUB/ch001.xhtml")


def test_epub_identifier_is_stable_per_job(tmp_path: Path) -> None:
    doc = assembled()
    first = unwrap(EpubExporter().export(doc, tmp_path / "a", ExportOptions()))
    second = unwrap(EpubExporter().export(doc, tmp_path / "b", ExportOptions()))
    ids = [
        epub.read_epub(str(p)).get_metadata("DC", "identifier")[0][0]
        for p in (first.path, second.path)
    ]
    assert ids[0] == ids[1]
    other_doc = assembled(job_id="job-2")
    other = unwrap(EpubExporter().export(other_doc, tmp_path / "c", ExportOptions()))
    assert epub.read_epub(str(other.path)).get_metadata("DC", "identifier")[0][0] != ids[0]


def test_markdown_to_html_wraps_untranslated_blocks() -> None:
    chunks = identity_chunks(split_source(SOURCE_DOC))
    chunks[1] = failed(chunks[1])
    doc = unwrap(assemble(chunks, allow_partial=True, metadata={}))
    html = markdown_to_html(doc.markdown).html
    assert html.count('<div class="untranslated">') == 1
    assert "[UNTRANSLATED — chunk 2" in html and "</div>" in html
    assert "<table" not in html and "<h1>Chapter One</h1>" in html


# ---------------------------------------------------------------------------
# pdf exporter, registry, atomic writes


def test_pdf_exporter_is_optional_and_always_available(tmp_path: Path) -> None:
    """v1.1: the native engine ships with the core install (design doc 06, D10)."""
    assert PdfExporter.optional is True and PdfExporter.is_available() is True
    artifact = unwrap(PdfExporter().export(assembled(), tmp_path, ExportOptions()))
    assert artifact.path.read_bytes().startswith(b"%PDF")
    assert artifact.bytes_written == artifact.path.stat().st_size
    assert artifact.warnings == ["pdf_defaults_assumed"]  # no source design was passed
    sourced = unwrap(PdfExporter().export(
        assembled(), tmp_path, ExportOptions(
            page_rect_pt=(595.0, 842.0), body_font_pt=10.0, margins_pt=(50.0, 45.0, 55.0, 45.0)
        )
    ))
    assert sourced.warnings == []


def test_pdf_turkish_round_trip(tmp_path: Path) -> None:
    """Native engine: Turkish text survives; a figure placeholder and code render too."""
    doc = assembled(SOURCE_DOC.replace("Second chapter text.", f"İkinci bölüm {TURKISH}."))
    artifact = unwrap(PdfExporter().export(doc, tmp_path, ExportOptions()))
    assert artifact.path.stat().st_size > 0
    with pymupdf.open(str(artifact.path)) as pdf:
        text = "\n".join(page.get_text() for page in pdf)
        toc = [(level, title) for level, title, _page in pdf.get_toc()]
    assert f"İkinci bölüm {TURKISH}." in text
    assert "Şekil 1 — görsel dahil edilmedi" in text and "print('x')" in text
    assert toc == [(1, "Chapter One"), (2, "Section A"), (1, "Chapter Two"), (2, "Section B")]


def test_pdf_weasyprint_engine_requires_weasyprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_exporter_module, "weasyprint_available", lambda: False)
    result = PdfExporter().export(assembled(), tmp_path, ExportOptions(pdf_engine="weasyprint"))
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.EXPORTER_UNAVAILABLE
    assert result.error.scope is ErrorScope.USER
    assert not (tmp_path / "translated_book.pdf").exists()
    assert PdfExporter.engines() == {"native": True, "weasyprint": False}


def test_registry() -> None:
    assert set(list_exporters()) == {"markdown", "epub", "pdf"}
    for name in list_exporters():
        exporter = unwrap(get_exporter(name))
        assert exporter.name == name
        assert exporter.output_path(Path("out")).name == f"translated_book{exporter.file_suffix}"
    unknown = get_exporter("docx")
    assert isinstance(unknown, Err) and unknown.error.code is ErrorCode.EXPORTER_UNAVAILABLE


def test_atomic_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "translated_book.md"
    target.write_bytes(b"previous")

    def boom(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    result = atomic_write_bytes(target, b"new content")
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.EXPORT_FAILED
    assert target.read_bytes() == b"previous"
    assert [p.name for p in tmp_path.iterdir()] == ["translated_book.md"]

    doc = assembled()
    for exporter in (MarkdownExporter(), EpubExporter()):
        result = exporter.export(doc, tmp_path / "fresh", ExportOptions())
        assert isinstance(result, Err) and result.error.code is ErrorCode.EXPORT_FAILED
        assert not (tmp_path / "fresh" / f"translated_book{exporter.file_suffix}").exists()
        assert list((tmp_path / "fresh").iterdir()) == []


def test_atomic_write_success_and_fsync_path(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "translated_book.md"
    payload = TURKISH.encode("utf-8")
    assert unwrap(atomic_write_bytes(target, payload)) == len(payload)
    assert target.read_text(encoding="utf-8") == TURKISH
    assert [p.name for p in target.parent.iterdir()] == ["translated_book.md"]
    # a second write replaces the file in place and leaves no temp file behind
    assert unwrap(atomic_write_bytes(target, b"ikinci")) == len(b"ikinci")
    assert target.read_bytes() == b"ikinci"
    assert [p.name for p in target.parent.iterdir()] == ["translated_book.md"]


# ---------------------------------------------------------------------------
# fix round (docs/04_code_review.md)


def test_raw_html_characters_in_prose_are_escaped(tmp_path: Path) -> None:
    """CR-05: `<`, `>`, `&` in book text stay text; code, quotes and markers keep working."""
    doc = assembled(
        "# Chapter One\n\n"
        "Use the <div> element; AT&T pays if a < b > c.\n\n"
        "Inline `a < b` code and an &amp; entity.\n\n"
        "> quoted <b>bold</b>\n\n"
        "```html\n<script>alert(1)</script>\n```\n\n"
        "<!-- image:1 -->\n\n"
        "# Chapter Two\n\nSecond.\n"
    )
    html = markdown_to_html(doc.markdown).html
    assert "&lt;div&gt;" in html and "AT&amp;T" in html and "a &lt; b &gt; c" in html
    assert "<div>" not in html and "<b>" not in html and "<script>" not in html
    assert "&amp;amp;" not in html and "&amp; entity" in html
    assert "<code>a &lt; b</code>" in html
    assert "<blockquote>" in html and "&lt;b&gt;bold&lt;/b&gt;" in html
    assert "&lt;script&gt;" in html
    assert 'class="figure-placeholder"' in html
    assert html.count("<h1>") == 2

    artifact = unwrap(EpubExporter().export(doc, tmp_path, ExportOptions()))
    assert epub.read_epub(str(artifact.path)) is not None
    joined = "\n".join(epub_xhtml(artifact.path).values())
    assert "&lt;div&gt;" in joined and "AT&amp;T" in joined and "<div>" not in joined


def test_untranslated_marker_survives_prose_escaping() -> None:
    chunks = identity_chunks(split_source(SOURCE_DOC))
    chunks[1] = failed(chunks[1], "provider_bad_request: tag <x> rejected & dropped")
    doc = unwrap(assemble(chunks, allow_partial=True, metadata={}))
    html = markdown_to_html(doc.markdown).html
    assert html.count('<div class="untranslated">') == 1 and "</div>" in html
    assert "&lt;x&gt;" in html and "<x>" not in html


def test_epub_exporter_refuses_empty_document(tmp_path: Path) -> None:
    """CR-02: an empty document is an error, not a 0-byte or crashing EPUB."""
    doc = AssembledDocument(
        markdown="", metadata={}, failed_chunk_ids=[], review_chunk_ids=[],
        heading_outline=[], warnings=[],
    )
    result = EpubExporter().export(doc, tmp_path, ExportOptions())
    assert isinstance(result, Err) and result.error.code is ErrorCode.EXPORT_FAILED
    assert not (tmp_path / "translated_book.epub").exists()


def test_assembler_refuses_empty_chunk_stream() -> None:
    """CR-02: no chunks -> "run translate first"."""
    result = assemble([], allow_partial=True, metadata={})
    assert isinstance(result, Err) and result.error.code is ErrorCode.EXPORT_REFUSED_PARTIAL
    assert "translate" in result.error.message


# ---------------------------------------------------------------------------
# v1.1 figures (design doc 06, 3.3 / C10): legend pairing, image refs, EPUB packaging

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
FIGURE_SOURCE = (
    "# Chapter One\n\n"
    "Intro.\n\n"
    '<!-- image:1 src="images/p001-f01.png" -->\n\n'
    "Figure 1: Flow.\n\n"
    '<!-- legend:1 more="2" -->\n- Start\n- 2. Step\n- Stop\n\n'
    '<!-- image:2 src="images/p002-f01.png" -->\n\n'
    "<!-- image:3 -->\n\n"
    "<!-- legend:7 -->\n\n"
    "Tail.\n"
)
LEGEND_CHUNK_SOURCE = (
    'Figure 1: Flow.\n\n<!-- legend:1 more="2" -->\n- Start\n- 2. Step\n- Stop\n\n'
)
LEGEND_CHUNK_TRANSLATED = (
    'Şekil 1: Akış.\n\n<!-- legend:1 more="2" -->\n- Başla\n- 2. Adım\n- Dur\n\n'
)
PAIRED_LEGEND = (
    'Şekil 1: Akış.\n\n<!-- legend:1 more="2" -->\n'
    "- Start — Başla\n- 2\\. Step — 2\\. Adım\n- Stop — Dur\n\n*… ve 2 etiket daha*\n\n"
)


def translated_chunks(source: str, translations: Dict[str, str]) -> List[Chunk]:
    """Real chunker output, translated through ``translations`` (identity otherwise)."""
    out: List[Chunk] = []
    for c in unwrap(chunk(source, min_chars=10, max_chars=1000)).chunks:
        out.append(completed(c, translations.get(c.source_text)) if c.translatable else c)
    return out


def paired_doc() -> AssembledDocument:
    chunks = translated_chunks(FIGURE_SOURCE, {LEGEND_CHUNK_SOURCE: LEGEND_CHUNK_TRANSLATED})
    meta = {"job_id": "job-f", "source_file": "book.pdf"}
    return unwrap(assemble(chunks, allow_partial=False, metadata=meta))


def write_png(target_dir: Path, relative: str) -> Path:
    path = target_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TINY_PNG)
    return path


def test_assembler_pairs_legend_labels_escapes_ordinals_and_notes_truncation() -> None:
    chunks = translated_chunks(FIGURE_SOURCE, {LEGEND_CHUNK_SOURCE: LEGEND_CHUNK_TRANSLATED})
    doc = unwrap(
        assemble(chunks, allow_partial=False, metadata={}, source_markdown=FIGURE_SOURCE)
    )
    assert doc.warnings == [] and doc.review_chunk_ids == []
    assert PAIRED_LEGEND in doc.markdown
    assert doc.markdown.startswith(
        '# Chapter One\n\nIntro.\n\n<!-- image:1 src="images/p001-f01.png" -->\n\n'
    )
    assert "<!-- legend:7 -->\n\n" in doc.markdown  # orphan marker copied verbatim
    assert doc.markdown.endswith("<!-- image:3 -->\n\n<!-- legend:7 -->\n\nTail.\n")
    html = markdown_to_html(doc.markdown).html
    assert "<li>Start — Başla</li>" in html and "<li>2. Step — 2. Adım</li>" in html
    assert "<ol" not in html


def test_assembler_legend_without_truncation_has_no_note() -> None:
    src = "Cap.\n\n<!-- legend:4 -->\n- a\n- b\n\n"
    tr = "Kap.\n\n<!-- legend:4 -->\n- x\n- y\n\n"
    doc = unwrap(assemble([completed(make_chunk(1, src), tr)], allow_partial=False, metadata={}))
    assert doc.markdown == "Kap.\n\n<!-- legend:4 -->\n- a — x\n- b — y\n\n"
    assert doc.warnings == [] and doc.review_chunk_ids == []


def test_assembler_legend_count_mismatch_keeps_translation_and_flags_review() -> None:
    src = "Cap.\n\n<!-- legend:4 -->\n- a\n- b\n\n"
    merged = "Kap.\n\n<!-- legend:4 -->\n- x y\n\n"
    doc = unwrap(
        assemble([completed(make_chunk(1, src), merged)], allow_partial=False, metadata={})
    )
    assert doc.markdown == merged
    assert doc.review_chunk_ids == [1]
    assert "legend_mismatch:4" in doc.warnings
    dropped_marker = "Kap.\n\n- x\n- y\n\n"
    doc = unwrap(
        assemble([completed(make_chunk(1, src), dropped_marker)], allow_partial=False, metadata={})
    )
    assert doc.markdown == dropped_marker and "legend_mismatch:4" in doc.warnings


def test_assembler_pair_legends_false_is_identity() -> None:
    chunks = translated_chunks(FIGURE_SOURCE, {})
    doc = unwrap(assemble(
        chunks, allow_partial=False, metadata={}, source_markdown=FIGURE_SOURCE,
        pair_legends=False,
    ))
    assert doc.markdown == FIGURE_SOURCE and doc.warnings == []
    paired = unwrap(
        assemble(chunks, allow_partial=False, metadata={}, source_markdown=FIGURE_SOURCE)
    )
    assert "- Start — Start\n- 2\\. Step — 2\\. Step\n- Stop — Stop\n" in paired.markdown


def test_assembler_pairs_legend_across_sub_chunks_and_ignores_fenced_markers() -> None:
    parent = [
        make_chunk(1, "<!-- legend:2 -->\n- a\n", parent_block_id=5, sub_index=0, sub_count=2),
        make_chunk(2, "- b\n\n", parent_block_id=5, sub_index=1, sub_count=2),
    ]
    chunks = [completed(parent[0], "<!-- legend:2 -->\n- x\n"), completed(parent[1], "- y\n\n")]
    doc = unwrap(assemble(chunks, allow_partial=False, metadata={}))
    assert doc.markdown == "<!-- legend:2 -->\n- a — x\n- b — y\n\n" and doc.warnings == []
    fenced = "```\n<!-- legend:3 -->\n- raw\n```\n\n"
    code = make_chunk(1, fenced, translatable=False, kind=ChunkKind.CODE)
    doc = unwrap(assemble([code], allow_partial=False, metadata={}))
    assert doc.markdown == fenced and doc.warnings == []


def test_assembler_failed_legend_chunk_is_marked_not_paired() -> None:
    chunks = translated_chunks(FIGURE_SOURCE, {LEGEND_CHUNK_SOURCE: LEGEND_CHUNK_TRANSLATED})
    legend_index = next(i for i, c in enumerate(chunks) if c.source_text == LEGEND_CHUNK_SOURCE)
    chunks[legend_index] = failed(chunks[legend_index])
    doc = unwrap(assemble(chunks, allow_partial=True, metadata={}))
    assert doc.failed_chunk_ids == [chunks[legend_index].chunk_id]
    assert LEGEND_CHUNK_SOURCE in doc.markdown  # source legend inside the marker block
    assert "- Start — " not in doc.markdown and "etiket daha" not in doc.markdown
    assert not any(w.startswith("legend_mismatch") for w in doc.warnings)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("2. Step", "2\\. Step"),
        ("3) Go", "3\\) Go"),
        ("7.", "7\\."),
        ("Step 2.", "Step 2."),
        ("4.2 rate", "4.2 rate"),
        ("Input", "Input"),
    ],
)
def test_escape_label_only_touches_a_leading_list_marker(label: str, expected: str) -> None:
    assert escape_label(label) == expected


def test_markdown_exporter_renders_image_refs_and_drops_legend_markers(tmp_path: Path) -> None:
    write_png(tmp_path, "images/p001-f01.png")
    artifact = unwrap(MarkdownExporter().export(paired_doc(), tmp_path, ExportOptions()))
    text = artifact.path.read_text(encoding="utf-8")
    assert "![Şekil 1](images/p001-f01.png)\n\nŞekil 1: Akış.\n\n" in text
    assert "*[Şekil 2 — görsel dahil edilmedi]*" in text  # src given, file missing
    assert "*[Şekil 3 — görsel dahil edilmedi]*" in text  # v1 token
    assert "<!--" not in text
    assert "- Start — Başla\n- 2\\. Step — 2\\. Adım\n- Stop — Dur\n\n*… ve 2 etiket daha*" in text
    assert artifact.warnings == ["figure_missing:2"]


def test_markdown_exporter_identity_keeps_markers(tmp_path: Path) -> None:
    doc = paired_doc()
    artifact = unwrap(
        MarkdownExporter(render_placeholders=False).export(doc, tmp_path, ExportOptions())
    )
    assert artifact.path.read_text(encoding="utf-8") == doc.markdown
    assert artifact.warnings == []


def test_markdown_to_html_renders_figure_legend_and_placeholders(tmp_path: Path) -> None:
    write_png(tmp_path, "images/p001-f01.png")
    render = markdown_to_html(paired_doc().markdown, base_dir=tmp_path)
    html = render.html
    assert (
        '<figure class="figure"><img src="images/p001-f01.png" alt="Şekil 1"/></figure>' in html
    )
    assert html.count('class="figure-placeholder"') == 2
    assert "Şekil 2 — görsel dahil edilmedi" in html and "Şekil 3 — görsel dahil edilmedi" in html
    assert render.warnings == ("figure_missing:2",)
    assert html.count('<div class="figure-legend">') == 1
    legend = html[html.index('<div class="figure-legend">') :]
    legend = legend[: legend.index("</div>")]
    assert "<li>Start — Başla</li>" in legend and "<li>2. Step — 2. Adım</li>" in legend
    assert "<em>… ve 2 etiket daha</em>" in legend
    assert "<ol" not in html and "<!--" not in html and "legend:7" not in html
    assert "<p>Tail.</p>" in html
    without = markdown_to_html(paired_doc().markdown)
    assert without.warnings == ("figure_missing:1", "figure_missing:2")
    assert "<figure" not in without.html


def test_markdown_to_html_ignores_files_outside_the_output_directory(tmp_path: Path) -> None:
    (tmp_path / "outside.png").write_bytes(TINY_PNG)
    out = tmp_path / "out"
    out.mkdir()
    render = markdown_to_html('<!-- image:1 src="../outside.png" -->\n', base_dir=out)
    assert "<figure" not in render.html and render.warnings == ("figure_missing:1",)
    fenced = markdown_to_html('```\n<!-- image:1 src="x.png" -->\n<!-- legend:1 -->\n- a\n```\n')
    assert fenced.warnings == () and "figure-legend" not in fenced.html
    assert "&lt;!-- image:1" in fenced.html


def test_epub_packages_figures_and_references_them(tmp_path: Path) -> None:
    write_png(tmp_path, "images/p001-f01.png")
    artifact = unwrap(EpubExporter().export(paired_doc(), tmp_path, ExportOptions()))
    assert artifact.warnings == ["figure_missing:2"]
    with zipfile.ZipFile(artifact.path) as zf:
        assert "EPUB/images/p001-f01.png" in zf.namelist()
        assert zf.read("EPUB/images/p001-f01.png") == TINY_PNG
        opf = zf.read("EPUB/content.opf").decode("utf-8")
        assert 'href="images/p001-f01.png"' in opf and 'media-type="image/png"' in opf
        assert 'id="img1"' in opf and "p002-f01" not in opf
        css = zf.read("EPUB/style/book.css").decode("utf-8")
        assert "figure.figure" in css and ".figure-legend" in css
    chapter = epub_xhtml(artifact.path)["ch000.xhtml"]
    assert '<img src="images/p001-f01.png" alt="Şekil 1"/>' in chapter
    assert 'class="figure-legend"' in chapter and "Start — Başla" in chapter
    assert "Şekil 2 — görsel dahil edilmedi" in chapter and "Şekil 3 — görsel" in chapter
    book = epub.read_epub(str(artifact.path))
    images = [item for item in book.get_items() if item.get_type() == ebooklib.ITEM_IMAGE]
    assert [item.file_name for item in images] == ["images/p001-f01.png"]


def test_epub_without_any_figure_file_falls_back_to_placeholders(tmp_path: Path) -> None:
    artifact = unwrap(EpubExporter().export(paired_doc(), tmp_path, ExportOptions()))
    assert artifact.warnings == ["figure_missing:1", "figure_missing:2"]
    with zipfile.ZipFile(artifact.path) as zf:
        assert not any(name.startswith("EPUB/images/") for name in zf.namelist())
    chapter = epub_xhtml(artifact.path)["ch000.xhtml"]
    assert "<img" not in chapter and chapter.count('class="figure-placeholder"') == 3


def test_pdf_html_document_references_figures_relative_to_the_target_dir(
    tmp_path: Path,
) -> None:
    write_png(tmp_path, "images/p001-f01.png")
    options = ExportOptions(
        page_rect_pt=(595.0, 842.0), body_font_pt=10.0, margins_pt=(50.0, 45.0, 55.0, 45.0)
    )
    page, warnings = build_html_document(paired_doc(), options, base_dir=tmp_path)
    assert '<img src="images/p001-f01.png" alt="Şekil 1"/>' in page
    assert "size: 595pt 842pt;" in page and "body { font-size: 10pt; }" in page
    assert warnings == ["figure_missing:2"]
    _page, assumed = build_html_document(paired_doc(), ExportOptions(), base_dir=tmp_path)
    assert assumed == ["pdf_defaults_assumed", "figure_missing:2"]
    assert "figure.figure" in page and ".figure-legend" in page


# ---------------------------------------------------------------------------
# CR-59: figure paths never leave the output directory (Windows forms included)
# ---------------------------------------------------------------------------


def test_resolve_figure_rejects_backslash_drive_and_parent_paths(tmp_path: Path) -> None:
    from book_translator.exporters.base import resolve_figure

    out = tmp_path / "out"
    write_png(out, "images/p001-f01.png")
    secret = write_png(tmp_path, "outside/secret.png")
    assert resolve_figure("images/p001-f01.png", out) == out / "images" / "p001-f01.png"
    for src in (
        "../outside/secret.png",
        "..\\outside\\secret.png",
        "images\\..\\..\\outside\\secret.png",
        "images/../../outside/secret.png",
        str(secret),  # absolute, with a drive letter on Windows
        secret.as_posix(),
        "C:outside/secret.png",
        "/outside/secret.png",
        "images\\p001-f01.png",  # tool-written paths use "/" only
        "",
    ):
        assert resolve_figure(src, out) is None, src
    assert resolve_figure("images/p001-f01.png", None) is None
    assert resolve_figure("images/missing.png", out) is None


def test_epub_never_embeds_a_file_from_outside_the_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    write_png(tmp_path, "outside/secret.png")
    markdown = '# Bölüm\n\nMetin.\n\n<!-- image:1 src="..\\outside\\secret.png" -->\n'
    artifact = unwrap(EpubExporter().export(assembled(markdown), out, ExportOptions()))
    assert "figure_missing:1" in artifact.warnings
    with zipfile.ZipFile(artifact.path) as archive:
        assert not [name for name in archive.namelist() if name.endswith(".png")]


def test_figure_inventory_file_names_are_validated_on_load(tmp_path: Path) -> None:
    import json

    from book_translator.extractors.figure_inventory import FigureEntry, load_figures_file

    entry = {
        "image_id": 1, "page": 2, "index_on_page": 1, "kind": "vector",
        "region": [10.0, 10.0, 100.0, 100.0], "file": "images/p002-f01.png", "dpi": 200,
        "width_px": 100, "height_px": 100, "bytes": 10,
    }
    assert FigureEntry.model_validate(entry).file == "images/p002-f01.png"
    assert FigureEntry.model_validate({**entry, "file": "images/p1000-f100.png"}).page == 2
    for bad in (
        "..\\evil.png", "../images/p002-f01.png", "C:\\Users\\x\\p002-f01.png",
        "/etc/p002-f01.png", "images/../p002-f01.png", "images\\p002-f01.png",
        "images/my-cover.png", "images/p002-f01.jpg", "p002-f01.png",
    ):
        with pytest.raises(ValueError):
            FigureEntry.model_validate({**entry, "file": bad})
    # a tampered figures.json is refused as a whole (Result boundary, no exception)
    good = unwrap_figures_payload(entry)
    path = tmp_path / "figures.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    assert not isinstance(load_figures_file(path), Err)
    good["figures"][0]["file"] = "..\\x.png"
    path.write_text(json.dumps(good), encoding="utf-8")
    loaded = load_figures_file(path)
    assert isinstance(loaded, Err) and "pNNN-fKK" in loaded.error.message


def unwrap_figures_payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    """A minimal valid ``figures.json`` document around one entry."""
    from book_translator.extractors.base import FigureOptions
    from book_translator.extractors.figure_inventory import FigureEntry, build_figures_file

    record = FigureEntry.model_validate(entry).to_record()
    built = build_figures_file([record], (), extractor="pymupdf", options=FigureOptions())
    payload: Dict[str, Any] = built.model_dump(mode="json")
    return payload
