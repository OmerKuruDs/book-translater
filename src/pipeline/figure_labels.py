"""Figure labels translated in place (design doc 06, Addendum A).

The legend block of ``source_book.md`` is the *translation vehicle* for figure labels:
after assembly every legend item reads ``- <source> — <target>``. This module extracts
those pairs, maps them onto the ``label_boxes`` of ``figures.json`` and, when the
legend list is not wanted in the outputs (``--figure-legend`` off, the default), strips
the legend blocks from the assembled Markdown. Pure functions only.

v1.1 fix round (CR-57/CR-58): a legend is stripped per figure (only when its translated
PNG was really produced), the image line of a painted figure is re-pointed at the
``*.tr.png`` file in the *translated* document only, and nothing inside a code fence is
ever touched (blank lines included).
"""

from __future__ import annotations

import re
from typing import Collection, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.bt_syntax import (
    format_image_line,
    is_legend_marker,
    parse_image_line,
    parse_legend_marker,
)
from ..domain.models import FigureRecord
from ..domain.overlay import LabelReplacement, OverlayAlignment
from .assembler import LEGEND_SEPARATOR

__all__ = [
    "TRANSLATED_SUFFIX",
    "build_replacements",
    "legend_ids",
    "legend_pairs",
    "rewrite_image_sources",
    "strip_legends",
    "translated_figure_name",
]

TRANSLATED_SUFFIX = ".tr.png"
"""Suffix of a translated figure written next to the English original (CR-57)."""

_ITEM = re.compile(r"^[ \t]*[-*+][ \t]+(.*?)[ \t]*$")
_ESCAPED_ORDINAL = re.compile(r"^(\d+)\\([.)])")
_MORE_NOTE = re.compile(r"^\*… ve \d+ etiket daha\*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")


def _unescape(label: str) -> str:
    return _ESCAPED_ORDINAL.sub(lambda m: f"{m.group(1)}{m.group(2)}", label, count=1)


def _legend_blocks(lines: Sequence[str]) -> List[Tuple[int, int, int]]:
    """``(marker line index, end index, image id)`` of every legend block outside fences.

    ``end`` is the first line after the items (and after the truncation note when the
    note directly follows the items).
    """
    blocks: List[Tuple[int, int, int]] = []
    in_fence = False
    fence_char = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_char = True, marker[0]
            elif marker[0] == fence_char:
                in_fence = False
            index += 1
            continue
        if in_fence or not is_legend_marker(line):
            index += 1
            continue
        parsed = parse_legend_marker(line)
        end = index + 1
        while end < len(lines) and _ITEM.match(lines[end]):
            end += 1
        if end + 1 < len(lines) and lines[end] == "" and _MORE_NOTE.match(lines[end + 1]):
            end += 2
        elif end < len(lines) and _MORE_NOTE.match(lines[end]):
            end += 1
        blocks.append((index, end, parsed[0] if parsed else 0))
        index = end
    return blocks


def legend_pairs(markdown: str) -> Dict[int, Dict[str, str]]:
    """``image_id -> {source label: translated label}`` from the paired legend blocks.

    Items without the pairing separator (an unpaired legend, ``legend_mismatch``) are
    ignored so a failed pairing never paints a wrong label into a figure.
    """
    lines = markdown.split("\n")
    pairs: Dict[int, Dict[str, str]] = {}
    for start, end, image_id in _legend_blocks(lines):
        mapping = pairs.setdefault(image_id, {})
        for line in lines[start + 1 : end]:
            item = _ITEM.match(line)
            if item is None:
                continue
            source, separator, target = item.group(1).partition(LEGEND_SEPARATOR)
            if not separator or not target.strip():
                continue
            mapping.setdefault(_unescape(source.strip()), _unescape(target.strip()))
    return pairs


def legend_ids(markdown: str) -> List[int]:
    """Image ids that own a legend block (outside code fences), in document order."""
    ids: List[int] = []
    for _, _, image_id in _legend_blocks(markdown.split("\n")):
        if image_id not in ids:
            ids.append(image_id)
    return ids


def strip_legends(markdown: str, image_ids: Optional[Collection[int]] = None) -> str:
    """Remove the legend blocks (marker, items, truncation note) of ``image_ids`` - every
    legend when ``None`` - and collapse the blank lines *at the removed block only*, so the
    neighbouring paragraphs stay separated by one blank line.

    Nothing else is rewritten: fenced code (its blank lines included) and every other run
    of blank lines in the document stay byte-identical (CR-58).
    """
    lines = markdown.split("\n")
    blocks = [
        block for block in _legend_blocks(lines) if image_ids is None or block[2] in image_ids
    ]
    if not blocks:
        return markdown
    for start, end, _ in reversed(blocks):
        del lines[start:end]
        # The junction sits outside every fence (the marker was), so the blank lines
        # collapsed here can never belong to a code block.
        while start < len(lines) and lines[start] == "" and (
            start == 0 or lines[start - 1] == ""
        ):
            del lines[start]
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


def translated_figure_name(file: str) -> str:
    """``images/p002-f01.png`` -> ``images/p002-f01.tr.png`` (CR-57)."""
    stem = file[:-4] if file.lower().endswith(".png") else file
    return f"{stem}{TRANSLATED_SUFFIX}"


def rewrite_image_sources(markdown: str, sources: Mapping[int, str]) -> str:
    """Point the image line of every id in ``sources`` at its new ``src`` (outside fences).

    Used on the assembled *translated* document only: ``source_book.md`` and
    ``figures.json`` keep referencing the English original (CR-57).
    """
    if not sources:
        return markdown
    lines = markdown.split("\n")
    in_fence = False
    fence_char = ""
    for index, line in enumerate(lines):
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_char = True, marker[0]
            elif marker[0] == fence_char:
                in_fence = False
            continue
        if in_fence:
            continue
        parsed = parse_image_line(line.strip())
        if parsed is None or parsed.src is None or parsed.image_id not in sources:
            continue
        lines[index] = format_image_line(parsed.image_id, sources[parsed.image_id])
    return "\n".join(lines)


_ALIGNMENTS: Mapping[str, OverlayAlignment] = {a.value: a for a in OverlayAlignment}


def build_replacements(
    record: FigureRecord, pairs: Mapping[str, str]
) -> List[LabelReplacement]:
    """``LabelReplacement`` per label box whose source text has a translation."""
    replacements: List[LabelReplacement] = []
    for box in record.label_boxes:
        target = pairs.get(box.text)
        if not target or target == box.text:
            continue
        replacements.append(
            LabelReplacement(
                bbox=box.bbox,
                text=target,
                font_size=box.font_size,
                bold=box.bold,
                italic=box.italic,
                family=box.family,
                color=box.color,
                alignment=_ALIGNMENTS.get(box.alignment, OverlayAlignment.LEFT),
                prefix_style=box.prefix_style,  # CR-97: keep the bold running number
            )
        )
    return replacements

