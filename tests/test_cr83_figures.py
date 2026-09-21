"""CR-83 regression on the reference document: page 2 of ``docs/test_doc.pdf``.

``tests/test_figures.py`` pins the page-level safety net on hand-built pages. This test
pins the numbers the net actually measures on the reference page, because the old rule
(absorbed characters against *every* character of the page) cleared it by three points -
0.366 against a 0.40 limit - and nothing made that margin visible. Two labels more, or a
shorter caption, and the reference document itself would have lost its only figure.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from book_translator.domain.result import Err
from book_translator.extractors.base import ExtractOptions, FigureOptions
from book_translator.extractors.pymupdf_extractor import PyMuPDFExtractor

FIGURES_LOGGER = "book_translator.extractors.figures"


def _page_text_records(caplog: pytest.LogCaptureFixture) -> Dict[int, Any]:
    """The ``figure_page_text`` measurement of the safety net, per page."""
    return {
        int(record.page): record  # type: ignore[attr-defined]
        for record in caplog.records
        if getattr(record, "event", "") == "figure_page_text"
    }


def test_reference_page_two_keeps_its_figure_with_the_measured_margin(
    figure_pdfs: Dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """CR-83: the one figure of ``docs/test_doc.pdf`` survives, and the measurement that
    decides it is pinned - the legacy ratio (0.366) sat just under the old 0.40 limit,
    while the values the net looks at now (no absorbed prose, 15 % text fill) are far from
    theirs."""
    with caplog.at_level(logging.DEBUG, logger=FIGURES_LOGGER):
        result = PyMuPDFExtractor().extract(
            figure_pdfs["vector_diagram"],
            ExtractOptions(figures=FigureOptions(), min_chars_per_page=0),
        )
    assert not isinstance(result, Err), result
    document = result.value
    assert [(f.page, f.kind.value, f.label_count) for f in document.figures] == [(2, "vector", 15)]
    dropped: List[str] = [w for w in document.warnings if w.startswith("figure_page_dropped")]
    assert dropped == []

    record = _page_text_records(caplog)[2]
    assert record.absorbed_chars == 326 and record.page_chars == 890
    # what the old rule compared: 0.366 of a 0.40 limit, the whole of CR-83's evidence
    assert record.absorbed_ratio == pytest.approx(0.366, abs=0.002)
    # what the net compares now: prose that left the flow, and how full of text the figure is
    assert record.absorbed_prose_chars == 0
    assert record.fill == pytest.approx(0.146, abs=0.005)
