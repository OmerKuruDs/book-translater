"""Shared image-line / legend-marker grammar (design doc 06, §3.1)."""

from __future__ import annotations

import pytest

from book_translator.domain.bt_syntax import (
    format_image_line,
    format_legend_marker,
    is_image_line,
    is_legend_marker,
    parse_image_line,
    parse_legend_marker,
)


def test_v1_token_parses_as_placeholder() -> None:
    parsed = parse_image_line("<!-- image:3 -->")
    assert parsed is not None
    assert (parsed.image_id, parsed.src) == (3, None)
    assert format_image_line(3) == "<!-- image:3 -->"


def test_v1_1_line_carries_src_and_round_trips() -> None:
    line = format_image_line(7, "images/p002-f01.png")
    assert line == '<!-- image:7 src="images/p002-f01.png" -->'
    parsed = parse_image_line(line + "  ")
    assert parsed is not None
    assert (parsed.image_id, parsed.src) == (7, "images/p002-f01.png")
    assert is_image_line(line)


@pytest.mark.parametrize(
    "line",
    [
        "<!-- image:x -->",
        "<!-- image:3 --> trailing",
        " <!-- image:3 -->",
        '<!-- image:3 src=images -->',
    ],
)
def test_non_matching_lines(line: str) -> None:
    assert parse_image_line(line) is None
    assert not is_image_line(line)


def test_unknown_attributes_are_ignored_and_bad_src_rejected() -> None:
    parsed = parse_image_line('<!-- image:2 alt="x" src="a.png" -->')
    assert parsed is not None and parsed.src == "a.png"
    with pytest.raises(ValueError):
        format_image_line(1, 'a"b.png')


def test_legend_marker_round_trip() -> None:
    assert format_legend_marker(4) == "<!-- legend:4 -->"
    assert format_legend_marker(4, 12) == '<!-- legend:4 more="12" -->'
    assert parse_legend_marker("<!-- legend:4 -->") == (4, 0)
    assert parse_legend_marker('<!-- legend:4 more="12" -->') == (4, 12)
    assert parse_legend_marker("<!-- image:4 -->") is None
    assert is_legend_marker('<!-- legend:9 more="1" -->')
