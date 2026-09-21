"""Shared BT-Markdown syntax for the image line and the legend marker (design doc 06, §3.1).

Every module that recognises or emits these lines (normalizer, extractor, chunker,
segmenter, assembler, exporters, glossary discovery) must use this module so the
grammar has exactly one definition. Standard library only.

Grammar (one line each, nothing else on the line):

    <!-- image:N -->                          v1 token: placeholder without a file
    <!-- image:N src="images/p002-f01.png" -->  v1.1: figure with a rendered file
    <!-- legend:N more="K" -->                  v1.1: legend marker, first line of a LIST block
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

IMAGE_LINE = re.compile(
    r'^<!--\s*image:(?P<id>\d+)(?P<attrs>(?:\s+[a-z]+="[^"\n]*")*)\s*-->[ \t]*$'
)
LEGEND_MARKER = re.compile(r'^<!--\s*legend:(?P<id>\d+)(?:\s+more="(?P<more>\d+)")?\s*-->[ \t]*$')
_ATTR = re.compile(r'([a-z]+)="([^"\n]*)"')


@dataclass(frozen=True)
class ImageLine:
    """A parsed image line; ``src`` is ``None`` for the v1 placeholder form."""

    image_id: int
    src: Optional[str]
    raw: str


def parse_image_line(line: str) -> Optional[ImageLine]:
    """Return the parsed image line or ``None`` when ``line`` is not one."""
    match = IMAGE_LINE.match(line)
    if match is None:
        return None
    attrs = dict(_ATTR.findall(match.group("attrs") or ""))
    src = attrs.get("src") or None
    return ImageLine(image_id=int(match.group("id")), src=src, raw=line)


def format_image_line(image_id: int, src: Optional[str] = None) -> str:
    """Emit the image line; ``src=None`` gives the v1 placeholder token."""
    if src:
        if '"' in src or "\n" in src:
            raise ValueError("image src must not contain quotes or newlines")
        return f'<!-- image:{image_id} src="{src}" -->'
    return f"<!-- image:{image_id} -->"


def parse_legend_marker(line: str) -> Optional[Tuple[int, int]]:
    """Return ``(image_id, more)`` for a legend marker line, else ``None``."""
    match = LEGEND_MARKER.match(line)
    if match is None:
        return None
    more = match.group("more")
    return int(match.group("id")), int(more) if more is not None else 0


def format_legend_marker(image_id: int, more: int = 0) -> str:
    """Emit the legend marker line (``more`` omitted when zero)."""
    if more > 0:
        return f'<!-- legend:{image_id} more="{more}" -->'
    return f"<!-- legend:{image_id} -->"


def is_image_line(line: str) -> bool:
    return IMAGE_LINE.match(line) is not None


def is_legend_marker(line: str) -> bool:
    return LEGEND_MARKER.match(line) is not None
