"""Extractor tests: fixture PDFs built with PyMuPDF (design doc 02, section 11) plus
direct unit tests of the pure heuristics (``cleanup.py``) and the normalizer."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import pymupdf
import pytest

from book_translator.domain.models import ExtractedDocument
from book_translator.domain.result import Err, ErrorCode, ErrorScope, Ok, err, unwrap
from book_translator.extractors import (
    ExtractOptions,
    FigureOptions,
    MarkerExtractor,
    PyMuPDFExtractor,
    check_english,
    cleanup,
    detect_language,
    ensure_bt_markdown,
    get_extractor,
    list_extractors,
    normalize_markdown,
)

PAGE_W, PAGE_H = 400.0, 600.0
BODY = 10.0
LINE_STEP = 14.0

PROSE = [
    "The old house stood at the end of the lane, and nobody had lived in it for years.",
    "Every evening the wind came down from the hills and rattled the loose shutters.",
    "The children of the village told each other stories about the people who once",
    "lived there, and about the lights that some of them claimed to have seen inside.",
    "None of the grown-ups believed a word of it, of course, but they never went near.",
]

GERMAN = [
    "Das alte Haus stand am Ende der Gasse, und seit Jahren hatte niemand darin gewohnt.",
    "Jeden Abend kam der Wind von den Hügeln herab und rüttelte an den losen Läden.",
    "Die Kinder des Dorfes erzählten sich Geschichten über die Menschen, die dort",
    "einmal gelebt hatten, und über die Lichter, die einige von ihnen gesehen haben wollten.",
    "Keiner der Erwachsenen glaubte ein Wort davon, aber sie gingen nie in die Nähe.",
]


# ---------------------------------------------------------------------------
# Fixture PDF builders
# ---------------------------------------------------------------------------


def _png_bytes() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), False)
    pix.clear_with(180)
    data: bytes = pix.tobytes("png")
    return data


def _lines(
    page: pymupdf.Page,
    y: float,
    texts: Sequence[str],
    *,
    size: float = BODY,
    font: str = "helv",
    x: float = 40.0,
    step: Optional[float] = None,
) -> float:
    """Insert one text line per entry; returns the y position after the last line."""
    gap = step if step is not None else size * 1.4
    for text in texts:
        page.insert_text((x, y), text, fontsize=size, fontname=font)
        y += gap
    return y


def _prose(page: pymupdf.Page, y: float = 120.0, lines: Sequence[str] = PROSE) -> float:
    return _lines(page, y, lines)


def _new_page(doc: pymupdf.Document) -> pymupdf.Page:
    return doc.new_page(width=PAGE_W, height=PAGE_H)


def _build_dragon(path: Path) -> None:
    """(a) 6 pages, running header 'The Dragon Book', footer page numbers."""
    doc = pymupdf.open()
    for number in range(1, 7):
        page = _new_page(doc)
        page.insert_text((40, 30), "The Dragon Book", fontsize=9)
        if number == 1:
            _lines(page, 100, ["Chapter 1"], size=18)
            _prose(page, 130)
            # A body line that repeats the header text but sits outside the band (E-03).
            _lines(page, 260, ["The Dragon Book"])
        else:
            _prose(page)
        page.insert_text((190, 585), str(number + 40), fontsize=9)
    doc.save(str(path))
    doc.close()


def _build_hyphen(path: Path) -> None:
    """(b) 'trans-' / 'lation' across pages; 'well-' / 'known' compound within a page."""
    doc = pymupdf.open()
    page = _new_page(doc)
    _lines(
        page,
        120,
        [
            "Some well-known authors have written about this problem before.",
            "The book itself is well-",
            "known among translators and it deals with machine trans-",
        ],
    )
    page = _new_page(doc)
    _lines(
        page,
        120,
        [
            "lation as it is applied to long books that readers enjoy.",
            "This is the second page and it contains enough characters to count.",
        ],
    )
    doc.save(str(path))
    doc.close()


def _build_continuation(path: Path) -> None:
    """(c) A paragraph continuing across pages without a hyphen."""
    doc = pymupdf.open()
    page = _new_page(doc)
    _lines(
        page,
        120,
        [
            "This paragraph starts on the first page of the fixture document and",
            "the story continues on the next page without any",
        ],
    )
    page = _new_page(doc)
    y = _lines(page, 120, ["punctuation at all, which is common in printed books."])
    _lines(page, y + 14, ["A brand new paragraph follows it on the same page of the fixture."])
    doc.save(str(path))
    doc.close()


def _build_image_only(path: Path) -> None:
    """(d) Three pages; the middle one holds only an image."""
    doc = pymupdf.open()
    page = _new_page(doc)
    _lines(page, 120, ["First page text before the picture page."] + list(PROSE[:2]))
    page = _new_page(doc)
    page.insert_image(pymupdf.Rect(50, 50, 350, 350), stream=_png_bytes())
    page = _new_page(doc)
    _lines(page, 120, ["Third page text after the picture page."] + list(PROSE[2:4]))
    doc.save(str(path))
    doc.close()


def _build_no_text(path: Path) -> None:
    """(e) No text layer: a single inserted PNG."""
    doc = pymupdf.open()
    page = _new_page(doc)
    page.insert_image(pymupdf.Rect(50, 50, 350, 350), stream=_png_bytes())
    doc.save(str(path))
    doc.close()


def _build_headings(path: Path) -> None:
    """(f) Headings at 18/14 pt vs 10 pt body."""
    doc = pymupdf.open()
    page = _new_page(doc)
    y = _lines(page, 90, ["Part One"], size=18, step=30)
    y = _lines(page, y, ["Section Title"], size=14, step=20)
    y = _prose(page, y, PROSE[:3])
    y = _lines(page, y + 10, ["Another Section"], size=14, step=20)
    _prose(page, y, PROSE[3:])
    doc.save(str(path))
    doc.close()


def _build_code(path: Path) -> None:
    """(g) A monospace block between two paragraphs."""
    doc = pymupdf.open()
    page = _new_page(doc)
    y = _prose(page, 100, PROSE[:2])
    y = _lines(page, y + 14, ["def foo():", "    return 1", "print(foo())"], font="cour")
    _prose(page, y + 14, PROSE[2:])
    doc.save(str(path))
    doc.close()


def _build_footnotes(path: Path) -> None:
    """(h) Superscript reference in the body, small-font definitions at the bottom."""
    doc = pymupdf.open()
    page = _new_page(doc)
    _prose(page, 100, PROSE[:2])
    page.insert_htmlbox(
        pymupdf.Rect(40, 150, 360, 190),
        "The body sentence carries a reference<sup>1</sup> to a footnote below.",
        css="* {font-size:10px; font-family: sans-serif;}",
    )
    _prose(page, 200, PROSE[2:])
    _lines(page, 540, ["1 This is the footnote text.", "2 An orphan note."], size=7, step=10)
    doc.save(str(path))
    doc.close()


def _build_german(path: Path) -> None:
    """(i) German prose for language detection."""
    doc = pymupdf.open()
    for _ in range(2):
        page = _new_page(doc)
        _prose(page, 120, GERMAN)
    doc.save(str(path))
    doc.close()


def _build_lists(path: Path) -> None:
    doc = pymupdf.open()
    page = _new_page(doc)
    y = _prose(page, 100, PROSE[:2])
    y = _lines(page, y + 14, ["1. First item of the list", "2. Second item of the list"])
    _lines(page, y, ["- nested bullet under the second item"], x=52)
    doc.save(str(path))
    doc.close()


def _build_table(path: Path) -> None:
    doc = pymupdf.open()
    page = _new_page(doc)
    _prose(page, 100, PROSE[:3])
    shape = page.new_shape()
    for y in (300.0, 320.0, 340.0):
        shape.draw_line((40, y), (240, y))
    for x in (40.0, 140.0, 240.0):
        shape.draw_line((x, 300), (x, 340))
    shape.finish(width=0.5)
    shape.commit()
    for x, y, text in ((44, 314, "Name"), (144, 314, "Value"), (44, 334, "alpha"), (144, 334, "1")):
        page.insert_text((x, y), text, fontsize=9)
    _prose(page, 380, PROSE[3:])
    doc.save(str(path))
    doc.close()


BUILDERS: Dict[str, Callable[[Path], None]] = {
    "dragon": _build_dragon,
    "hyphen": _build_hyphen,
    "continuation": _build_continuation,
    "image_only": _build_image_only,
    "no_text": _build_no_text,
    "headings": _build_headings,
    "code": _build_code,
    "footnotes": _build_footnotes,
    "german": _build_german,
    "lists": _build_lists,
    "table": _build_table,
}


@pytest.fixture(scope="session")
def fixture_pdfs(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    root = tmp_path_factory.mktemp("fixture_pdfs")
    paths: Dict[str, Path] = {}
    for name, builder in BUILDERS.items():
        target = root / f"{name}.pdf"
        builder(target)
        paths[name] = target
    return paths


class _Extracted:
    """Session cache of v1 extraction results keyed by fixture name.

    The v1 tests describe the ``--no-figures`` behaviour (AC US-14/4), so the cache runs
    the extractor with figures disabled; the F1 tests below enable them explicitly.
    """

    def __init__(self, pdfs: Dict[str, Path]) -> None:
        self._pdfs = pdfs
        self._cache: Dict[str, ExtractedDocument] = {}

    def __call__(self, name: str) -> ExtractedDocument:
        if name not in self._cache:
            options = ExtractOptions(figures=FigureOptions(enabled=False))
            result = PyMuPDFExtractor().extract(self._pdfs[name], options)
            self._cache[name] = unwrap(result)
        return self._cache[name]


@pytest.fixture(scope="session")
def extracted(fixture_pdfs: Dict[str, Path]) -> _Extracted:
    return _Extracted(fixture_pdfs)


def assert_bt_markdown(md: str) -> None:
    """The BT-Markdown output contract (design doc 02, section 4.1)."""
    assert md.endswith("\n") and not md.endswith("\n\n")
    assert "\t" not in md and "\r" not in md
    assert "\n\n\n" not in md
    assert not md.startswith("\n")
    in_fence = False
    for line in md.split("\n"):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            assert line == line.rstrip(), f"trailing whitespace: {line!r}"


def _lines_of(md: str) -> List[str]:
    return md.split("\n")


# ---------------------------------------------------------------------------
# Fixture-driven extractor tests (design row for test_extractor_cleanup.py)
# ---------------------------------------------------------------------------


def test_running_header_and_footer_removed(extracted: _Extracted) -> None:
    doc = extracted("dragon")
    md = doc.markdown
    assert_bt_markdown(md)
    lines = _lines_of(md)
    # Exactly one survivor: the body line outside the band (E-03 protection).
    assert lines.count("The Dragon Book") == 1
    assert "the dragon book" in doc.removed_headers_footers
    assert "# Chapter 1" in lines
    assert all(not cleanup.is_page_number(line) for line in lines)
    assert not any(line.strip() in {str(n) for n in range(41, 47)} for line in lines)
    assert doc.extractor_name == "pymupdf" and doc.fallback_used is False
    assert len(doc.pages) == 6 and not any(p.skipped for p in doc.pages)
    assert doc.detected_language == "en"


def test_page_numbers_stripped(extracted: _Extracted) -> None:
    md = extracted("dragon").markdown
    for number in range(41, 47):
        assert f"\n{number}\n" not in md and not md.startswith(f"{number}\n")


def test_hyphen_rejoin_and_compound_preserved(extracted: _Extracted) -> None:
    md = extracted("hyphen").markdown
    assert_bt_markdown(md)
    assert "machine translation as it is applied" in md
    assert "trans-" not in md and "trans- lation" not in md
    assert "is well-known among translators" in md
    assert "wellknown" not in md


def test_cross_page_paragraph_merge(extracted: _Extracted) -> None:
    md = extracted("continuation").markdown
    assert_bt_markdown(md)
    assert "without any punctuation at all, which is common" in md
    blocks = md.strip("\n").split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].startswith("This paragraph starts") and blocks[0].endswith("printed books.")
    assert blocks[1].startswith("A brand new paragraph")


def test_image_only_page_skipped(extracted: _Extracted) -> None:
    doc = extracted("image_only")
    assert [p.skipped for p in doc.pages] == [False, True, False]
    assert doc.pages[1].had_images == 1 and doc.pages[1].char_count == 0
    assert "First page text" in doc.markdown and "Third page text" in doc.markdown


def test_no_text_layer(fixture_pdfs: Dict[str, Path]) -> None:
    pdf = fixture_pdfs["no_text"]
    before = sorted(pdf.parent.iterdir())
    result = PyMuPDFExtractor().extract(pdf, ExtractOptions())
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.NO_TEXT_LAYER
    assert result.error.scope is ErrorScope.USER
    assert "OCR" in result.error.message and "marker" in result.error.message
    assert sorted(pdf.parent.iterdir()) == before  # nothing written next to the input


def test_heading_tiers(extracted: _Extracted) -> None:
    md = extracted("headings").markdown
    assert_bt_markdown(md)
    lines = _lines_of(md)
    assert "# Part One" in lines
    assert "## Section Title" in lines
    assert "## Another Section" in lines
    assert not any(line.startswith("###") for line in lines)


def test_code_fence(extracted: _Extracted) -> None:
    md = extracted("code").markdown
    assert_bt_markdown(md)
    assert "```\ndef foo():\n    return 1\nprint(foo())\n```" in md
    assert md.count("```") == 2


def test_footnotes_to_markdown(extracted: _Extracted) -> None:
    doc = extracted("footnotes")
    md = doc.markdown
    assert_bt_markdown(md)
    assert doc.footnote_mode == "markdown"
    blocks = md.strip("\n").split("\n\n")
    ref_index = next(i for i, b in enumerate(blocks) if "reference[^1] to a footnote" in b)
    assert blocks[ref_index + 1] == "[^1]: This is the footnote text."
    assert blocks[-2:] == ["## Notes", "[^2]: An orphan note."]


def test_language_detection_en_vs_de(extracted: _Extracted) -> None:
    english = extracted("dragon")
    german = extracted("german")
    assert english.detected_language == "en" and english.language_confidence >= 0.15
    assert german.detected_language == "de"
    assert isinstance(check_english(english.markdown, allow_non_english=False), Ok)
    refused = check_english(german.markdown, allow_non_english=False)
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.NON_ENGLISH_SOURCE
    assert refused.error.scope is ErrorScope.USER
    assert "de" in refused.error.message
    allowed = check_english(german.markdown, allow_non_english=True)
    assert isinstance(allowed, Ok) and allowed.value[0] == "de"


@pytest.mark.skipif(MarkerExtractor.is_available(), reason="marker-pdf is installed here")
def test_marker_unavailable_is_reported(fixture_pdfs: Dict[str, Path]) -> None:
    result = get_extractor("marker")
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.EXTRACTOR_UNAVAILABLE
    assert result.error.scope is ErrorScope.USER
    # Direct use is equally safe: no exception, a classified error instead.
    direct = MarkerExtractor().extract(fixture_pdfs["dragon"], ExtractOptions())
    assert isinstance(direct, Err) and direct.error.code is ErrorCode.EXTRACTOR_UNAVAILABLE


def test_registry_names_and_unknown() -> None:
    assert list_extractors() == ["pymupdf", "marker"]
    pymupdf_result = get_extractor("pymupdf")
    assert isinstance(pymupdf_result, Ok) and isinstance(pymupdf_result.value, PyMuPDFExtractor)
    unknown = get_extractor("tesseract")
    assert isinstance(unknown, Err) and unknown.error.code is ErrorCode.PROVIDER_CONFIG


def test_lists(extracted: _Extracted) -> None:
    md = extracted("lists").markdown
    assert_bt_markdown(md)
    assert "1. First item of the list\n2. Second item of the list\n  - nested bullet" in md


def test_table(extracted: _Extracted) -> None:
    md = extracted("table").markdown
    assert_bt_markdown(md)
    assert "| Name | Value |\n| --- | --- |\n| alpha | 1 |" in md
    assert "\nName\n" not in md and "\nalpha\n" not in md  # cell text not duplicated


def test_image_placeholder_and_count(extracted: _Extracted) -> None:
    doc = extracted("image_only")
    # The image sits on a skipped page, so no placeholder is emitted (E-04).
    assert doc.image_count == 0 and "<!-- image:" not in doc.markdown


def test_progress_callback(fixture_pdfs: Dict[str, Path]) -> None:
    calls: List[Tuple[int, int]] = []
    result = PyMuPDFExtractor().extract(
        fixture_pdfs["dragon"], ExtractOptions(), progress=lambda d, t: calls.append((d, t))
    )
    assert isinstance(result, Ok)
    assert calls == [(i, 6) for i in range(1, 7)]


def test_input_validation(tmp_path: Path) -> None:
    extractor = PyMuPDFExtractor()
    missing = extractor.extract(tmp_path / "missing.pdf", ExtractOptions())
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.INPUT_NOT_FOUND
    text_file = tmp_path / "notes.txt"
    text_file.write_text("hello", encoding="utf-8")
    not_pdf = extractor.extract(text_file, ExtractOptions())
    assert isinstance(not_pdf, Err) and not_pdf.error.code is ErrorCode.INPUT_NOT_PDF
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"this is not a pdf at all")
    no_magic = extractor.extract(fake, ExtractOptions())
    assert isinstance(no_magic, Err) and no_magic.error.code is ErrorCode.INPUT_NOT_PDF
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4 garbage garbage garbage")
    unreadable = extractor.extract(corrupt, ExtractOptions())
    assert isinstance(unreadable, Err)
    assert unreadable.error.code in (ErrorCode.INPUT_UNREADABLE, ErrorCode.NO_TEXT_LAYER)
    assert unreadable.error.scope is ErrorScope.USER


# ---------------------------------------------------------------------------
# cleanup.py unit tests (no PDF involved)
# ---------------------------------------------------------------------------


def test_normalize_pattern_masks_digits() -> None:
    a = cleanup.normalize_pattern("Chapter 3 — The Dragon | 47")
    b = cleanup.normalize_pattern("Chapter 3 — The Dragon | 48")
    assert a == b == "chapter # the dragon #"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("47", True),
        ("Page 12", True),
        ("xii", True),
        ("3 / 120", True),
        ("3 of 120", True),
        ("Chapter 3", False),
        ("47 dragons", False),
        ("", False),
    ],
)
def test_is_page_number(text: str, expected: bool) -> None:
    assert cleanup.is_page_number(text) is expected


def test_detect_running_patterns_threshold() -> None:
    pages = [["The Dragon Book", "12"], ["The Dragon Book"], ["The Dragon Book", "Once"], ["x"]]
    patterns = cleanup.detect_running_patterns(pages, page_ratio=0.30)
    assert patterns == {"the dragon book"}  # 3 of 4 pages; page numbers ignored
    assert cleanup.detect_running_patterns(pages[:2], page_ratio=0.30) == set()
    assert cleanup.running_pattern_threshold(20, 0.30) == 6
    assert cleanup.running_pattern_threshold(4, 0.30) == 3


def test_in_band() -> None:
    assert cleanup.in_band((0, 10, 100, 30), 600, 0.12)
    assert cleanup.in_band((0, 560, 100, 590), 600, 0.12)
    assert not cleanup.in_band((0, 200, 100, 220), 600, 0.12)


def test_join_hyphenated_cases() -> None:
    compounds = {"well-known"}
    assert cleanup.join_hyphenated("machine trans-", "lation here", compounds) == (
        "machine translation here",
        None,
    )
    assert cleanup.join_hyphenated("it is well-", "known that", compounds) == (
        "it is well-known that",
        None,
    )
    joined, warning = cleanup.join_hyphenated("the Anglo-", "Saxon kings", compounds)
    assert joined == "the Anglo-Saxon kings" and warning is not None
    joined, warning = cleanup.join_hyphenated("vitamin B-", "complex", compounds)
    assert joined == "vitamin B-complex" and warning is not None
    assert cleanup.join_hyphenated("no hyphen", "here", compounds) == ("no hyphen here", None)
    assert cleanup.join_hyphenated("", "start", compounds) == ("start", None)


def test_collect_compounds_ignores_line_end_hyphen() -> None:
    compounds = cleanup.collect_compounds(["a well-known fact", "the book is well-", "known"])
    assert compounds == {"well-known"}


def test_join_lines_collapses_whitespace() -> None:
    text, warnings = cleanup.join_lines(["first  line", "  second line ", ""], set())
    assert text == "first line second line" and warnings == []


@pytest.mark.parametrize(
    "prev, nxt, expected",
    [
        ("continues without any", "punctuation at all.", True),
        ("ends with a period.", "new paragraph", False),
        ("ends without period", "Capitalized start", False),
        ("machine trans-", "lation", True),
        ("closing quote.”", "and then", False),
        ("", "text", False),
    ],
)
def test_should_merge_cross_page(prev: str, nxt: str, expected: bool) -> None:
    assert cleanup.should_merge_cross_page(prev, nxt) is expected


def test_heading_tiers_capped_at_three() -> None:
    tiers = cleanup.assign_heading_tiers([24.0, 18.0, 14.2, 14.0, 12.0])
    assert tiers == {24.0: 1, 18.0: 2, 14.0: 3, 12.0: 3}


@pytest.mark.parametrize(
    "text, level",
    [("Chapter 3", 1), ("CHAPTER XII The Return", 1), ("3 Overview", 1), ("3.2 Title", 2),
     ("3.2.1 Deep", 3), ("3.2.1.4 Deeper", 3), ("Plain Title", None)],
)
def test_numbered_heading_level(text: str, level: Optional[int]) -> None:
    assert cleanup.numbered_heading_level(text) == level


def test_heading_candidate_rules() -> None:
    assert cleanup.is_heading_candidate(["Section Title"], 14.0, False, 10.0, 1.15)
    assert not cleanup.is_heading_candidate(["Ends with period."], 14.0, False, 10.0, 1.15)
    assert not cleanup.is_heading_candidate(["a", "b", "c"], 14.0, False, 10.0, 1.15)
    assert not cleanup.is_heading_candidate(["Body size"], 10.0, False, 10.0, 1.15)
    assert cleanup.is_heading_candidate(
        ["Bold Title"], 10.0, True, 10.0, 1.15, followed_by_paragraph=True
    )
    assert not cleanup.is_heading_candidate(["x" * 121], 14.0, False, 10.0, 1.15)


def test_body_font_size_weighted_mode() -> None:
    assert cleanup.body_font_size([(18.0, 20), (10.0, 400), (7.0, 50)]) == 10.0
    assert cleanup.body_font_size([]) == 0.0


@pytest.mark.parametrize(
    "text, expected",
    [
        ("- item", ("-", "item")),
        ("• bullet", ("-", "bullet")),
        ("3) third", ("3.", "third")),
        ("12. twelfth", ("12.", "twelfth")),
        ("a) lettered", ("-", "lettered")),
        ("-not a list", None),
        ("plain text", None),
    ],
)
def test_parse_list_marker(text: str, expected: Optional[Tuple[str, str]]) -> None:
    assert cleanup.parse_list_marker(text) == expected


def test_misc_helpers() -> None:
    assert cleanup.is_monospace_font("Courier-Bold") and cleanup.is_monospace_font("DejaVuSansMono")
    assert not cleanup.is_monospace_font("Helvetica")
    assert cleanup.looks_like_caption("Figure 3: The dragon")
    assert cleanup.looks_like_caption("Fig. 2")
    assert not cleanup.looks_like_caption("Figures are fun")
    assert cleanup.parse_footnote_definition("1 Some note.") == ("1", "Some note.")
    assert cleanup.parse_footnote_definition("* Star note") == ("*", "Star note")
    assert cleanup.parse_footnote_definition("Not a note") is None
    assert cleanup.is_footnote_marker("12") and not cleanup.is_footnote_marker("th")
    assert cleanup.list_level(52.0, 40.0, 10.0) == 1 and cleanup.list_level(40.0, 40.0, 10.0) == 0
    assert cleanup.is_title_case("The Dragon Book") and not cleanup.is_title_case("the dragon book")


# ---------------------------------------------------------------------------
# langdetect unit tests
# ---------------------------------------------------------------------------


def test_detect_language_plain_text() -> None:
    lang, ratio = detect_language(" ".join(PROSE))
    assert lang == "en" and ratio >= 0.15
    assert detect_language(" ".join(GERMAN))[0] == "de"
    assert detect_language("Bu kitap bir çeviri ve bu da onun için önemli bir şey.")[0] == "tr"
    assert detect_language("") == (None, 0.0)
    assert detect_language("12345 67890") == (None, 0.0)


# ---------------------------------------------------------------------------
# normalizer unit tests
# ---------------------------------------------------------------------------


def test_normalize_markdown_contract() -> None:
    raw = (
        "Title\n=====\n\n\nA hard-wrapped\nparagraph that\ncontinues here.\n\n\n\n"
        "Sub\n---\n\n~~~python\n\tcode()\n~~~\n\n![The caption](img.png)\n\n"
        "Some <b>bold</b> text &amp; more.<br>Next line.\n\n47\n\n"
        "- one\n- two\n  lazy continuation\n\n\ttabbed paragraph\n"
    )
    md, warnings = normalize_markdown(raw)
    assert_bt_markdown(md)
    assert md == (
        "# Title\n\nA hard-wrapped paragraph that continues here.\n\n## Sub\n\n"
        "```python\n    code()\n```\n\n<!-- image:1 -->\n\nThe caption\n\n"
        "Some bold text & more. Next line.\n\n- one\n- two lazy continuation\n\n"
        "tabbed paragraph\n"
    )
    assert any("page-number" in w for w in warnings)


def test_normalize_markdown_hr_and_fences() -> None:
    md, warnings = normalize_markdown("para\n\n---\n\n```\nunterminated\n")
    assert md == "para\n\n---\n\n```\nunterminated\n```\n"
    assert any("unterminated" in w for w in warnings)


def test_normalize_markdown_tables_quotes_footnotes() -> None:
    raw = "| a | b |\n| 1 | 2 |\n\n> quoted\n> lines\n\n[^n]: note text\n    continued\n"
    md, warnings = normalize_markdown(raw)
    assert md == (
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n> quoted\n> lines\n\n[^n]: note text continued\n"
    )
    assert any("delimiter" in w for w in warnings)


def test_ensure_bt_markdown_idempotent() -> None:
    messy = "\n\n# H\r\n\r\n\r\npara  \n\n\n\n```\n\tkeep\n```\n\n\n"
    once = ensure_bt_markdown(messy)
    assert once == "# H\n\npara\n\n```\n    keep\n```\n"
    assert ensure_bt_markdown(once) == once
    assert_bt_markdown(once)


# ---------------------------------------------------------------------------
# v1.1 F1: figure preservation end-to-end (design doc 06, §8.2; BA US-14..US-17)
# ---------------------------------------------------------------------------

from book_translator.domain.bt_syntax import parse_image_line, parse_legend_marker  # noqa: E402
from book_translator.domain.models import FigureKind, FigureRecord  # noqa: E402
from tests.conftest import (  # noqa: E402
    LEFT_COLUMN_BOTTOM,
    LEFT_COLUMN_TOP,
    RIGHT_COLUMN_BOTTOM,
    RIGHT_COLUMN_TOP,
)

TEST_DOC_LABELS = [
    "2D (what?)", "3D (where?)", "2. Image formation", "3. Image processing",
    "4. Model fitting and optimization", "5. Deep learning", "6. Recognition",
    "7. Feature detection and matching", "8. Image alignment and stitching",
    "9. Motion estimation", "10. Computational photography", "11. Structure from motion and SLAM",
    "12. Depth estimation", "13. 3D reconstruction", "14. Image-based rendering",
]


class _Sink:
    """In-memory figure sink recording what the extractor hands over."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root
        self.calls: List[Tuple[FigureRecord, bytes]] = []

    def __call__(self, record: FigureRecord, png: bytes) -> Any:
        self.calls.append((record, png))
        target = (self.root or Path(".")) / Path(*record.file.split("/"))
        if self.root is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
        return Ok(target)


def extract_figures(
    pdf: Path, options: Optional[FigureOptions] = None, sink: Optional[_Sink] = None
) -> Tuple[ExtractedDocument, _Sink]:
    sink = sink or _Sink()
    result = PyMuPDFExtractor().extract(
        pdf,
        ExtractOptions(figures=options or FigureOptions(), min_chars_per_page=0),
        figure_sink=sink,
    )
    return unwrap(result), sink


def _blocks(md: str) -> List[str]:
    return md.strip("\n").split("\n\n")


def _image_blocks(md: str) -> List[Tuple[int, str]]:
    """``(block index, block)`` of every image line in the markdown."""
    return [(i, b) for i, b in enumerate(_blocks(md)) if parse_image_line(b) is not None]


def test_vector_diagram_page_two_is_one_figure(figure_pdfs: Dict[str, Path]) -> None:
    """AC US-15/1-2, US-16/1 (placement), E5: the taxonomy diagram of docs/test_doc.pdf."""
    doc, sink = extract_figures(figure_pdfs["vector_diagram"])
    assert [p.figures for p in doc.pages] == [0, 1, 0]
    assert not any(p.skipped for p in doc.pages)
    assert len(doc.figures) == 1 and len(sink.calls) == 1
    figure = doc.figures[0]
    assert figure.page == 2 and figure.kind is FigureKind.VECTOR and figure.image_id == 1
    assert figure.file == "images/p002-f01.png"
    x0, y0, x1, y1 = figure.region
    # covers the dashed axis, boxes, arrows (union 106.1/146.4/492.6/590.9) and the axis titles
    assert x0 <= 106.1 and y0 <= 120.4 and x1 >= 492.6 and y1 >= 590.9
    assert y0 > 57.5  # running header "1.3 Book overview 19" excluded (E-23)
    assert y1 < 615.2  # caption block excluded
    assert figure.caption_present and figure.label_count == 15 and figure.labels_more == 0
    assert sorted(figure.labels) == sorted(TEST_DOC_LABELS)
    assert figure.labels[:2] == ("2D (what?)", "3D (where?)")  # reading order: top row first
    assert figure.dpi == 200 and not figure.dpi_reduced
    assert figure.bytes == len(sink.calls[0][1]) > 10_000

    md = doc.markdown
    assert_bt_markdown(md)
    blocks = _blocks(md)
    (index, image_line), = _image_blocks(md)
    assert image_line == '<!-- image:1 src="images/p002-f01.png" -->'
    caption = blocks[index + 1]
    assert caption.startswith("Figure 1.12 A taxonomy of the topics covered in this book")
    assert caption.endswith("widely used in subsequent chapters.")
    assert "\n" not in caption  # one paragraph, never a heading
    legend = blocks[index + 2].split("\n")
    assert parse_legend_marker(legend[0]) == (1, 0)
    assert legend[1:] == [f"- {label}" for label in figure.labels]
    prose = "\n".join(b for i, b in enumerate(blocks) if i not in (index, index + 1, index + 2))
    for label in TEST_DOC_LABELS:
        assert f"\n{label}\n" not in f"\n{prose}\n", label
        assert f"# {label}" not in prose, label
    assert "## 2D (what?)" not in md and "## 3D (where?)" not in md


def test_no_figures_is_byte_identical_to_v1(figure_pdfs: Dict[str, Path]) -> None:
    """AC US-14/4, FR-39: the v1 extraction of docs/test_doc.pdf is reproduced byte for byte.

    The reference is a frozen copy under ``tests/fixtures`` (CR-68): the live artefact in
    ``docs/test_doc_output`` is refreshed by manual runs and must not drive a test."""
    v1_artifact = Path(__file__).parent / "fixtures" / "test_doc_v1_source_book.md"
    sink = _Sink()
    doc, _ = extract_figures(figure_pdfs["vector_diagram"], FigureOptions(enabled=False), sink)
    assert doc.markdown.encode("utf-8") == v1_artifact.read_bytes()
    assert doc.figures == () and sink.calls == [] and doc.image_count == 0
    assert "<!-- image:" not in doc.markdown
    # Bare tokens survive on fixtures with raster images (v1 semantics incl. E-04 skip).
    off = FigureOptions(enabled=False)
    raster = unwrap(PyMuPDFExtractor().extract(figure_pdfs["raster_caption_above"],
                                               ExtractOptions(figures=off)))
    assert "<!-- image:1 -->" in raster.markdown and 'src="' not in raster.markdown


def test_decorative_page_has_no_figure(figure_pdfs: Dict[str, Path]) -> None:
    """AC US-15/3, E-24."""
    doc, sink = extract_figures(figure_pdfs["decorative_rules"])
    assert doc.figures == () and sink.calls == []
    assert "<!-- image:" not in doc.markdown
    assert "| Name | Value |" in doc.markdown
    assert "first bullet item" in doc.markdown


def test_two_figures_each_with_own_caption(figure_pdfs: Dict[str, Path]) -> None:
    """AC US-15/4, E-25."""
    doc, sink = extract_figures(figure_pdfs["two_figures"])
    assert [f.file for f in doc.figures] == ["images/p001-f01.png", "images/p001-f02.png"]
    assert [f.image_id for f in doc.figures] == [1, 2]
    blocks = _blocks(doc.markdown)
    images = _image_blocks(doc.markdown)
    assert len(images) == 2
    (i1, _), (i2, _) = images
    assert blocks[i1 + 1].startswith("Figure 3.1 The first diagram")
    assert blocks[i2 + 1].startswith("Figure 3.2 The second diagram")
    prose_between = blocks[i1 + 3 : i2]
    assert any("The old house stood" in b for b in prose_between)
    for label in ("Alpha node", "Zeta node"):
        assert f"- {label}" in doc.markdown
        assert f"\n{label}\n" not in doc.markdown
    assert doc.figures[0].labels == ("Alpha node", "Beta node", "Gamma node")


def test_subfigures_merge_by_caption(figure_pdfs: Dict[str, Path]) -> None:
    """E-25: two adjacent drawings with one '(a) ... (b)' caption become one figure."""
    doc, _ = extract_figures(figure_pdfs["subfigures"])
    assert len(doc.figures) == 1
    figure = doc.figures[0]
    assert "merged_by_caption" in figure.warnings
    assert figure.caption_present and set(figure.labels) >= {"Left A", "Right C"}
    x0, _, x1, _ = figure.region
    assert x0 <= 40 and x1 >= 350


def test_two_column_text_is_fully_recovered(figure_pdfs: Dict[str, Path]) -> None:
    """AC US-15/5, E-26: column text above/below a spanning figure stays in the flow."""
    doc, _ = extract_figures(figure_pdfs["two_column_spanning"])
    assert len(doc.figures) == 1
    md = doc.markdown
    columns = (*LEFT_COLUMN_TOP, *RIGHT_COLUMN_TOP, *LEFT_COLUMN_BOTTOM, *RIGHT_COLUMN_BOTTOM)
    for sentence in columns:
        assert sentence in md, sentence
    assert doc.figures[0].labels == ("Wide A", "Wide B", "Wide C", "Wide D")
    x0, y0, x1, y1 = doc.figures[0].region
    assert y0 > 90 and y1 < 410  # region limited to the drawing + inner labels


def test_full_page_figure_is_not_skipped(figure_pdfs: Dict[str, Path]) -> None:
    """AC US-15/6, E-27, C8."""
    doc, _ = extract_figures(figure_pdfs["full_page_figure"])
    assert [p.skipped for p in doc.pages] == [False, False]
    assert [p.figures for p in doc.pages] == [0, 1]
    blocks = _blocks(doc.markdown)
    (index, image_line), = _image_blocks(doc.markdown)
    assert image_line == '<!-- image:1 src="images/p002-f01.png" -->'
    assert blocks[index + 1].startswith("Figure 6.1 A full-page figure")


def test_raster_caption_above_and_orphan_caption(figure_pdfs: Dict[str, Path]) -> None:
    """E-30: caption searched above too; a caption without a figure stays a paragraph."""
    doc, _ = extract_figures(figure_pdfs["raster_caption_above"])
    assert len(doc.figures) == 1
    figure = doc.figures[0]
    assert figure.kind is FigureKind.RASTER and figure.page == 1 and figure.caption_present
    blocks = _blocks(doc.markdown)
    (index, _), = _image_blocks(doc.markdown)
    assert blocks[index + 1].startswith("Table 2.1 Values measured")
    assert "Figure 2.2 A caption without any figure on this page." in blocks
    assert doc.pages[1].figures == 0


def test_image_only_page_becomes_a_raster_figure(fixture_pdfs: Dict[str, Path]) -> None:
    """C8 vs v1 E-04: with figures enabled the picture page is kept, with --no-figures skipped."""
    doc, sink = extract_figures(fixture_pdfs["image_only"])
    assert [p.skipped for p in doc.pages] == [False, False, False]
    assert doc.pages[1].figures == 1 and doc.figures[0].kind is FigureKind.RASTER
    assert not doc.figures[0].caption_present and doc.figures[0].labels == ()
    assert "<!-- legend:" not in doc.markdown
    v1 = unwrap(PyMuPDFExtractor().extract(fixture_pdfs["image_only"],
                                           ExtractOptions(figures=FigureOptions(enabled=False))))
    assert [p.skipped for p in v1.pages] == [False, True, False]


def test_table_wins_over_figure(figure_pdfs: Dict[str, Path]) -> None:
    """E-32: drawings inside a detected table are never a figure."""
    doc, _ = extract_figures(figure_pdfs["table_page"])
    assert doc.figures == ()
    assert "| Name | Value |" in doc.markdown


def test_mixed_figure(figure_pdfs: Dict[str, Path]) -> None:
    """E-31: a drawing cluster containing a raster photo is one mixed region."""
    doc, _ = extract_figures(figure_pdfs["mixed_figure"])
    assert len(doc.figures) == 1 and doc.figures[0].kind is FigureKind.MIXED
    assert doc.image_count == 1  # the raster got no separate bare token
    assert doc.markdown.count("<!-- image:") == 1


def test_text_box_and_formula_are_not_figures(
    figure_pdfs: Dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """E-28, E-29."""
    boxed, _ = extract_figures(figure_pdfs["text_box"])
    assert boxed.figures == () and "A boxed sidebar of pure text" in boxed.markdown
    with caplog.at_level("INFO"):
        formula, _ = extract_figures(figure_pdfs["formula_page"])
    assert formula.figures == ()
    assert any(getattr(r, "event", "") == "figure_candidate_rejected" for r in caplog.records)


def test_extraction_is_deterministic(figure_pdfs: Dict[str, Path]) -> None:
    """AC US-14/3, NFR-17: names, count, order, PNG bytes and records repeat exactly."""
    for name in ("vector_diagram", "two_figures"):
        first, sink1 = extract_figures(figure_pdfs[name])
        second, sink2 = extract_figures(figure_pdfs[name])
        assert first.markdown == second.markdown
        assert first.figures == second.figures
        assert [r.file for r, _ in sink1.calls] == [r.file for r, _ in sink2.calls]
        assert [png for _, png in sink1.calls] == [png for _, png in sink2.calls]


@pytest.mark.parametrize("dpi", [72, 200, 300])
def test_pixel_size_matches_region_and_dpi(figure_pdfs: Dict[str, Path], dpi: int) -> None:
    """AC US-17/1 with the +/- 2 px tolerance of E4."""
    doc, sink = extract_figures(figure_pdfs["vector_diagram"], FigureOptions(dpi=dpi))
    figure = doc.figures[0]
    x0, y0, x1, y1 = figure.region
    assert figure.dpi == dpi
    assert abs(figure.width_px - round((x1 - x0) * dpi / 72)) <= 2
    assert abs(figure.height_px - round((y1 - y0) * dpi / 72)) <= 2
    assert sink.calls[0][1][:8] == b"\x89PNG\r\n\x1a\n"


def test_dpi_backs_off_to_the_floor_on_size_limit(figure_pdfs: Dict[str, Path]) -> None:
    """AC US-17/3, FR-38, E-34: ladder 200 -> 150 -> 112 -> 96, reported."""
    doc, _ = extract_figures(figure_pdfs["vector_diagram"], FigureOptions(max_bytes=20_000))
    figure = doc.figures[0]
    assert figure.dpi == 96 and figure.dpi_reduced
    assert "size_limit_exceeded" in figure.warnings
    fits, _ = extract_figures(figure_pdfs["vector_diagram"], FigureOptions(max_bytes=120_000))
    assert fits.figures[0].dpi == 150 and fits.figures[0].dpi_reduced
    assert "size_limit_exceeded" not in fits.figures[0].warnings


def test_legend_options(figure_pdfs: Dict[str, Path]) -> None:
    """AC US-16/3-4: legend off -> no marker; limit -> truncated with more="K" and a warning.

    Addendum A: ``FigureOptions.legend`` stays an extractor-level switch; the CLI/settings
    always extract the legend (translation vehicle) and ``--figure-legend`` only controls
    whether the list is rendered in the outputs.
    """
    off, _ = extract_figures(figure_pdfs["vector_diagram"], FigureOptions(legend=False))
    assert "<!-- legend:" not in off.markdown
    assert off.figures[0].labels == () and off.figures[0].label_count == 0
    limited, _ = extract_figures(figure_pdfs["vector_diagram"], FigureOptions(legend_limit=5))
    figure = limited.figures[0]
    assert len(figure.labels) == 5 and figure.labels_more == 10
    assert '<!-- legend:1 more="10" -->' in limited.markdown
    assert any(w.startswith("legend_truncated:2:1") for w in limited.warnings)


def test_excluded_page_and_marker_warning(figure_pdfs: Dict[str, Path]) -> None:
    """Q16 manual override; D16 marker warning."""
    doc, sink = extract_figures(figure_pdfs["vector_diagram"],
                                FigureOptions(exclude_pages=frozenset({2})))
    assert doc.figures == () and sink.calls == []
    assert doc.figure_pages_skipped == ((2, "excluded"),)
    assert "## 2D (what?)" in doc.markdown  # v1 behaviour on the excluded page
    if not MarkerExtractor.is_available():
        result = MarkerExtractor().extract(figure_pdfs["two_figures"], ExtractOptions())
        assert isinstance(result, Err)  # unavailable, but the call shape (figure_sink) is accepted


def test_sink_error_aborts_extraction(figure_pdfs: Dict[str, Path]) -> None:
    """A failing sink is reported unchanged (the orchestrator maps it to FIGURE_SINK_FAILED)."""

    def failing(record: FigureRecord, png: bytes) -> Any:
        return err(ErrorCode.FIGURE_SINK_FAILED, "disk full", ErrorScope.JOB_FATAL)

    result = PyMuPDFExtractor().extract(
        figure_pdfs["two_figures"], ExtractOptions(min_chars_per_page=0), figure_sink=failing
    )
    assert isinstance(result, Err) and result.error.code is ErrorCode.FIGURE_SINK_FAILED


# ---------------------------------------------------------------------------
# Addendum A (2026-09-18): label geometry in figures.json, source_profile.json
# ---------------------------------------------------------------------------

from book_translator.domain.models import LabelBox, SourceProfile  # noqa: E402
from book_translator.extractors.cleanup import (  # noqa: E402
    Line,
    RawBlock,
    RawPage,
    Span,
    compute_source_profile,
)
from book_translator.extractors.figure_inventory import (  # noqa: E402
    figures_file_from_document,
    load_figures_file,
    save_figures_file,
)
from book_translator.extractors.source_profile import (  # noqa: E402
    SOURCE_PROFILE_FILE_NAME,
    load_source_profile,
    save_source_profile,
)


def test_label_boxes_carry_geometry_and_style(figure_pdfs: Dict[str, Path]) -> None:
    """Every emitted legend label has a box with the style the source used."""
    doc, _ = extract_figures(figure_pdfs["vector_diagram"])
    figure = doc.figures[0]
    assert [box.text for box in figure.label_boxes] == list(figure.labels)
    x0, y0, x1, y1 = figure.region
    for box in figure.label_boxes:
        assert x0 <= box.bbox[0] < box.bbox[2] <= x1 and y0 <= box.bbox[1] < box.bbox[3] <= y1
        assert box.font_size == 13.0 and box.family == "serif" and box.color == "#000000"
    boxes = {box.text: box for box in figure.label_boxes}
    assert boxes["2. Image formation"].alignment == "center"  # centred in its drawing box
    assert not boxes["2. Image formation"].bold  # dominant style; "2." is the bold prefix
    two_lines = boxes["13. 3D reconstruction"]
    assert two_lines.bbox[3] - two_lines.bbox[1] > 1.8 * 13.0  # both lines of the label
    limited, _ = extract_figures(figure_pdfs["vector_diagram"], FigureOptions(legend_limit=5))
    assert len(limited.figures[0].label_boxes) == 5  # only labels that are in the legend


def test_label_boxes_round_trip_through_figures_json(
    figure_pdfs: Dict[str, Path], tmp_path: Path
) -> None:
    doc, _ = extract_figures(figure_pdfs["vector_diagram"])
    path = tmp_path / "figures.json"
    unwrap(save_figures_file(path, figures_file_from_document(doc, FigureOptions())))
    loaded = unwrap(load_figures_file(path))
    assert loaded.records()[0].label_boxes == doc.figures[0].label_boxes
    assert isinstance(loaded.records()[0].label_boxes[0], LabelBox)
    # an inventory written before Addendum A (no label_boxes key) still loads
    import json as json_module

    payload = json_module.loads(path.read_text("utf-8"))
    for entry in payload["figures"]:
        entry.pop("label_boxes")
    path.write_text(json_module.dumps(payload), encoding="utf-8")
    assert unwrap(load_figures_file(path)).records()[0].label_boxes == ()


def test_source_profile_of_the_test_document(figure_pdfs: Dict[str, Path]) -> None:
    doc, _ = extract_figures(figure_pdfs["vector_diagram"])
    profile = doc.source_profile
    assert profile is not None
    assert (profile.page_width_pt, profile.page_height_pt) == pytest.approx((595.28, 790.87))
    assert profile.body_font_size_pt == 10.5 and profile.serif and profile.pages_measured == 3
    top, right, bottom, left = profile.margins_pt
    assert top == pytest.approx(43.7, abs=0.5) and left == pytest.approx(57.2, abs=0.5)
    assert right == pytest.approx(58.5, abs=0.5) and 60 < bottom < 110


def test_compute_source_profile_is_pure_and_robust() -> None:
    def page(number: int, x0: float, y0: float, x1: float, y1: float, font: str) -> RawPage:
        bbox = (x0, y0, x1, y1)
        line = Line((Span("Body text of the page.", 11.0, font, False, False, False, bbox),), bbox)
        return RawPage(number, 400.0, 600.0, (RawBlock(number, bbox, (line,)),), (), ())

    pages = [page(1, 50, 60, 350, 540, "Helvetica"), page(2, 52, 62, 348, 538, "Helvetica"),
             RawPage(3, 400.0, 600.0, (), (), ())]  # a page without text is not measured
    profile = compute_source_profile(pages, 11.0)
    assert profile == SourceProfile(400.0, 600.0, 11.0, (61.0, 51.0, 61.0, 51.0), False, 2)
    empty = compute_source_profile([], 0.0)
    assert empty.page_width_pt == 0.0 and empty.margins_pt == (0.0, 0.0, 0.0, 0.0) and empty.serif


def test_source_profile_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / SOURCE_PROFILE_FILE_NAME
    profile = SourceProfile(595.28, 790.87, 10.5, (43.67, 58.54, 73.23, 57.19), True, 3)
    assert unwrap(save_source_profile(path, profile, extractor="pymupdf")) > 0
    assert unwrap(load_source_profile(path)) == profile
    path.write_text("{not json", encoding="utf-8")
    broken = load_source_profile(path)
    assert isinstance(broken, Err) and broken.error.code is ErrorCode.STATE_CORRUPT
    path.write_text('{"version": 1, "page_width_pt": 0, "page_height_pt": 10, '
                    '"body_font_size_pt": 10, "margins_pt": [1, 1, 1, 1]}', encoding="utf-8")
    assert isinstance(load_source_profile(path), Err)
    missing = load_source_profile(tmp_path / "absent.json")
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.INPUT_UNREADABLE


# ---------------------------------------------------------------------------
# CR-38: figures never take prose out of the reflow output (real PDFs)
# ---------------------------------------------------------------------------

_CR38_PROSE = (
    "The quick brown fox jumps over the lazy dog and keeps running through the "
    "forest until the evening light fades behind the distant hills of the valley. "
)


def _tinted_box(page: pymupdf.Page, rect: pymupdf.Rect, fill: Tuple[float, float, float]) -> None:
    """Fill + four separate border lines (5 paths): the usual output for a note box."""
    page.draw_rect(rect, color=None, fill=fill)
    for a, b in ((rect.tl, rect.tr), (rect.tr, rect.br), (rect.br, rect.bl), (rect.bl, rect.tl)):
        page.draw_line(a, b, width=1)


def build_ocr_sandwich(path: Path) -> Path:
    """Three pages with a full-page raster under the text (scanned book with OCR layer)."""
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 85), False)
    pixmap.clear_with(245)
    png = pixmap.tobytes("png")
    doc = pymupdf.open()
    for n in range(3):
        page = doc.new_page(width=420, height=595)
        page.insert_image(page.rect, stream=png)
        for k in range(5):
            top = 60 + k * 95
            page.insert_textbox(pymupdf.Rect(40, top, 380, top + 90),
                                f"PARA{n}K{k} " + _CR38_PROSE * 2, fontsize=10, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


def build_shaded_sidebar(path: Path) -> Path:
    """A shaded note box (5 paths) around a 12-line paragraph on the middle page."""
    doc = pymupdf.open()
    for n in range(3):
        page = doc.new_page(width=420, height=595)
        page.insert_textbox(pymupdf.Rect(40, 60, 380, 200), f"PARA{n}K0 " + _CR38_PROSE * 3,
                            fontsize=10, fontname="helv")
        if n == 1:
            _tinted_box(page, pymupdf.Rect(40, 220, 380, 400), (0.85, 0.9, 1.0))
            page.insert_textbox(pymupdf.Rect(50, 228, 370, 395),
                                "PARA1K1 SIDEBAR " + _CR38_PROSE * 4, fontsize=10, fontname="helv")
        page.insert_textbox(pymupdf.Rect(40, 420, 380, 540), f"PARA{n}K2 " + _CR38_PROSE * 2,
                            fontsize=10, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


def build_framed_page(path: Path) -> Path:
    """A tinted frame with a border around the whole text of the middle page."""
    doc = pymupdf.open()
    for n in range(3):
        page = doc.new_page(width=420, height=595)
        if n == 1:
            _tinted_box(page, pymupdf.Rect(30, 80, 390, 515), (1.0, 0.97, 0.85))
        for k in range(5):
            top = 90 + k * 84
            page.insert_textbox(pymupdf.Rect(40, top, 380, top + 80),
                                f"PARA{n}K{k} " + _CR38_PROSE * 2, fontsize=10, fontname="helv")
    doc.save(str(path))
    doc.close()
    return path


def _paragraphs(markdown: str) -> List[str]:
    return [b for b in _blocks(markdown) if b.startswith("PARA")]


@pytest.mark.parametrize(
    ("builder", "expected"),
    [(build_ocr_sandwich, 15), (build_shaded_sidebar, 7), (build_framed_page, 15)],
)
def test_figure_detection_never_swallows_prose(
    builder: Callable[[Path], Path], expected: int, tmp_path: Path
) -> None:
    """CR-38 (Blocker): with figures on, every paragraph of the ``--no-figures`` output is
    still a paragraph - no figure, no legend item holding page text."""
    pdf = builder(tmp_path / f"{builder.__name__}.pdf")
    with_figures, sink = extract_figures(pdf)
    without, _ = extract_figures(pdf, FigureOptions(enabled=False))
    assert len(_paragraphs(without.markdown)) == expected
    assert _paragraphs(with_figures.markdown) == _paragraphs(without.markdown)
    assert with_figures.figures == () and sink.calls == []
    assert not [b for b in _blocks(with_figures.markdown) if b.startswith("- ")]


def test_test_doc_figure_is_unchanged_by_the_text_safety_rules(
    figure_pdfs: Dict[str, Path],
) -> None:
    """CR-38 (d): page 2 of docs/test_doc.pdf is still one vector figure with 15 labels."""
    doc, _ = extract_figures(figure_pdfs["vector_diagram"])
    assert [(f.page, f.kind.value, f.label_count, f.labels_more) for f in doc.figures] == [
        (2, "vector", 15, 0)
    ]
    assert doc.figures[0].region == pytest.approx((102.121, 116.388, 496.633, 594.874), abs=0.01)
    assert not [w for w in doc.warnings if w.startswith("figure_page_dropped")]
