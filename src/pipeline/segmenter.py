"""Chunk → provider texts and back (design doc 02, section 4.6).

The chunk text is re-parsed with the block parser and turned into ordered
``Segment`` items. Markdown syntax (heading hashes, list markers, table pipes,
blockquote markers, footnote labels, code fences) stays local in ``prefix`` /
``suffix``; only the human-readable parts are handed to the provider. By
construction ``reassemble(segmented, [s.text for s in segmented.segments])``
reproduces the chunk text exactly.

v1.1 (design doc 06, C4): in a legend LIST block the marker line stays in the
first item's ``prefix`` (it is never a segment) and ``Segmented.context`` carries
the caption paragraph that precedes the legend in the same chunk, so the worker
can send ``[caption, *labels]`` as one request with the caption as context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..domain.bt_syntax import is_legend_marker
from ..domain.models import Block, BlockKind
from .chunker import legend_image_id, parse_blocks

__all__ = ["Segment", "Segmented", "reassemble", "segment"]

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_LIST_ITEM = re.compile(r"^( *)([-*+]|\d+[.)])[ \t]+")
_BLOCKQUOTE = re.compile(r"^ {0,3}>[ ]?")
_FOOTNOTE = re.compile(r"^\[\^[^\]\s]+\]:[ \t]+")
_DELIMITER_ROW = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_PIPE = re.compile(r"(?<!\\)\|")


@dataclass(frozen=True)
class Segment:
    """One provider text with the Markdown syntax that surrounds it kept locally."""

    text: str
    prefix: str
    suffix: str


@dataclass(frozen=True)
class Segmented:
    """Ordered segments of one chunk; ``tail`` is the text after the last segment.

    ``context`` is the caption paragraph of a legend chunk (design doc 06, C4) and
    ``None`` for every other chunk; providers with ``supports_context`` receive it.
    """

    chunk_text: str
    segments: List[Segment]
    tail: str
    context: Optional[str] = None


def _stripped_span(text: str, start: int, end: int) -> Tuple[int, int]:
    """Narrow ``[start, end)`` to the non-whitespace content."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _line_spans(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    pos = start
    for line in text[start:end].splitlines(keepends=True):
        spans.append((pos, pos + len(line)))
        pos += len(line)
    return spans


def _table_cells(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Content spans of the non-empty cells of one table line."""
    line_end = end
    while line_end > start and text[line_end - 1] in "\r\n":
        line_end -= 1
    pipes = [start + m.start() for m in _PIPE.finditer(text[start:line_end])]
    if not pipes:
        return [_stripped_span(text, start, line_end)]
    bounds = [start - 1, *pipes, line_end]
    cells: List[Tuple[int, int]] = []
    for left, right in zip(bounds, bounds[1:], strict=False):
        cell_start, cell_end = _stripped_span(text, left + 1, right)
        if cell_end > cell_start:
            cells.append((cell_start, cell_end))
    return cells


def _text_spans(kind: BlockKind, text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Translatable spans inside the block ``[start, end)`` of ``text``."""
    spans: List[Tuple[int, int]] = []
    lines = _line_spans(text, start, end)
    if kind == BlockKind.HEADING:
        line_start, line_end = lines[0]
        match = _HEADING.match(text[line_start:line_end].rstrip("\r\n"))
        if match:
            spans.append((line_start + match.start(2), line_start + match.end(2)))
        rest = lines[1:]
        kind = BlockKind.PARAGRAPH
    elif kind == BlockKind.FOOTNOTE:
        line_start, line_end = lines[0]
        match = _FOOTNOTE.match(text[line_start:line_end])
        content = line_start + (match.end() if match else 0)
        spans.append(_stripped_span(text, content, line_end))
        rest = lines[1:]
        kind = BlockKind.PARAGRAPH
    else:
        rest = lines
        if kind == BlockKind.LIST and lines:
            first = text[lines[0][0] : lines[0][1]].rstrip("\r\n")
            if is_legend_marker(first):
                rest = lines[1:]  # the legend marker stays in the first item's prefix
    for index, (line_start, line_end) in enumerate(rest):
        line = text[line_start:line_end]
        if kind == BlockKind.TABLE:
            if index == 1 and _DELIMITER_ROW.match(line.rstrip("\r\n")):
                continue
            spans.extend(_table_cells(text, line_start, line_end))
            continue
        content = line_start
        if kind == BlockKind.LIST:
            item = _LIST_ITEM.match(line)
            if item:
                content = line_start + item.end()
        elif kind == BlockKind.BLOCKQUOTE:
            quote = _BLOCKQUOTE.match(line)
            if quote:
                content = line_start + quote.end()
        spans.append(_stripped_span(text, content, line_end))
    return [(s, e) for s, e in spans if e > s]


def segment(chunk_text: str) -> Segmented:
    """Split ``chunk_text`` into provider segments (design doc 02, 4.6)."""
    segments: List[Segment] = []
    pending = 0  # start of text not yet assigned to a segment prefix
    context: Optional[str] = None
    previous: Optional[Block] = None
    for block in parse_blocks(chunk_text):
        if (
            legend_image_id(chunk_text, block) is not None
            and previous is not None
            and previous.kind == BlockKind.PARAGRAPH
            and previous.translatable
        ):
            context = chunk_text[previous.start : previous.end].strip()
        previous = block
        if not block.translatable:
            continue
        spans = _text_spans(block.kind, chunk_text, block.start, block.end)
        for index, (start, end) in enumerate(spans):
            if index == len(spans) - 1:
                suffix_end = block.end
            else:
                newline = chunk_text.find("\n", end, block.end)
                line_end = block.end if newline == -1 else newline + 1
                suffix_end = line_end if spans[index + 1][0] >= line_end else end
            segments.append(
                Segment(
                    text=chunk_text[start:end],
                    prefix=chunk_text[pending:start],
                    suffix=chunk_text[end:suffix_end],
                )
            )
            pending = suffix_end
    return Segmented(
        chunk_text=chunk_text, segments=segments, tail=chunk_text[pending:], context=context
    )


def reassemble(segmented: Segmented, translated_texts: Sequence[str]) -> str:
    """Re-apply prefixes/suffixes around the translated texts.

    Raises ``ValueError`` when the number of texts does not match; callers pass
    exactly one translation per segment.
    """
    if len(translated_texts) != len(segmented.segments):
        raise ValueError(
            f"expected {len(segmented.segments)} translated texts, got {len(translated_texts)}"
        )
    parts = [
        seg.prefix + text + seg.suffix
        for seg, text in zip(segmented.segments, translated_texts, strict=True)
    ]
    return "".join(parts) + segmented.tail
