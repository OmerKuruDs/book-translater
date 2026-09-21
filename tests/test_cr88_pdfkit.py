"""CR-88: a page copy per render job (``pdfkit.api.doc_page_copy``).

The batch figure renderer applies redactions and writes text on a live page before it
rasterizes the region, so jobs that share one document are not independent. This test pins
the primitive the batch path needs: what a job does to its copy never reaches the source
document or another copy of the same page.
"""

from __future__ import annotations

from pathlib import Path

from book_translator.pdfkit.api import (
    close_document,
    doc_page,
    doc_page_copy,
    doc_page_count,
    open_document,
    page_add_redact_annot,
    page_apply_redactions,
    page_plain_text,
    page_rotation,
    page_size,
    rect,
)

TEST_DOC = Path(__file__).resolve().parent.parent / "docs" / "test_doc.pdf"


def test_a_page_copy_is_independent_of_the_source_document() -> None:
    source = open_document(str(TEST_DOC))
    try:
        original = doc_page(source, 1)
        before = page_plain_text(original)
        assert before  # the reference page carries text to lose

        first = doc_page_copy(source, 1)
        second = doc_page_copy(source, 1)
        try:
            assert doc_page_count(first) == 1
            page = doc_page(first, 0)
            assert page_size(page) == page_size(original)
            assert page_rotation(page) == page_rotation(original)

            page_add_redact_annot(page, rect(0.0, 0.0, *page_size(page)), fill=False)
            page_apply_redactions(page)
            assert page_plain_text(page) == ""
            # neither the source nor a sibling copy sees the redaction
            assert page_plain_text(doc_page(source, 1)) == before
            assert page_plain_text(doc_page(second, 0)) == before
        finally:
            close_document(first)
            close_document(second)
    finally:
        close_document(source)
