"""BT-Markdown block parser and lossless chunker (design doc 02, section 4).

``parse_blocks`` produces ``Block`` spans that tile the document exactly;
``chunk`` packs them into ``Chunk`` objects whose ``source_text`` values
concatenate back to the input byte-for-byte (AC US-6/4). Oversize blocks are
split at sentence / item / row boundaries with parent linkage (4.4).

v1.1 (design doc 06, C1-C3): the image line and the legend marker come from
``domain.bt_syntax``. A legend marker followed by a list item opens a LIST block
whose first line is the marker; an orphan marker is a one-line, non-translatable
PARAGRAPH block. A legend block is packed with the immediately preceding caption
paragraph into one chunk and isolated from everything else (like a TABLE), so the
caption and the labels travel to the provider in one request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from ..domain.bt_syntax import IMAGE_LINE, LEGEND_MARKER, parse_legend_marker
from ..domain.models import Block, BlockKind, Chunk, ChunkKind, content_hash_of
from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err
from ..domain.sentences import sentence_spans

__all__ = ["ChunkResult", "chunk", "heading_outline", "legend_image_id", "parse_blocks"]

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_HR = re.compile(r"^ {0,3}(-{3,}|\*{3,}|_{3,})[ \t]*$")
_TABLE = re.compile(r"^ {0,3}\|")
_FOOTNOTE = re.compile(r"^\[\^[^\]\s]+\]:[ \t]")
_LIST_ITEM = re.compile(r"^( *)([-*+]|\d+[.)])[ \t]+")
_BLOCKQUOTE = re.compile(r"^ {0,3}>")
_WHITESPACE = re.compile(r"\s")

_MAX_HEADING_PATH_LEVEL = 3


@dataclass(frozen=True)
class ChunkResult:
    """Output of :func:`chunk`."""

    chunks: List[Chunk]
    blocks: List[Block]
    warnings: List[str]


# --------------------------------------------------------------------------- #
# 4.2 Block parser
# --------------------------------------------------------------------------- #


def _is_blank(line: str) -> bool:
    return line.strip() == ""


def _classify(line: str) -> Tuple[BlockKind, int]:
    """Kind and level of a non-blank line that starts a new block (grammar precedence 4.2)."""
    heading = _HEADING.match(line)
    if heading:
        return BlockKind.HEADING, len(heading.group(1))
    if _HR.match(line):
        return BlockKind.HR, 0
    if IMAGE_LINE.match(line):
        return BlockKind.IMAGE, 0
    if LEGEND_MARKER.match(line):
        # Opens a LIST block when a list item follows (decided by the parser's lookahead);
        # as a block *start* it always ends the previous block.
        return BlockKind.LIST, 0
    if _TABLE.match(line):
        return BlockKind.TABLE, 0
    if _FOOTNOTE.match(line):
        return BlockKind.FOOTNOTE, 0
    item = _LIST_ITEM.match(line)
    if item:
        return BlockKind.LIST, len(item.group(1)) // 2
    if _BLOCKQUOTE.match(line):
        return BlockKind.BLOCKQUOTE, 0
    return BlockKind.PARAGRAPH, 0


def _continues(kind: BlockKind, line: str) -> bool:
    """True when ``line`` (non-blank) extends an open block of ``kind``."""
    if kind == BlockKind.TABLE:
        return bool(_TABLE.match(line))
    if kind == BlockKind.LIST:
        return bool(_LIST_ITEM.match(line)) or line[0] in " \t"
    if kind == BlockKind.BLOCKQUOTE:
        return bool(_BLOCKQUOTE.match(line))
    if kind == BlockKind.PARAGRAPH:
        # Fence state has the highest precedence (4.2): an opener always ends the paragraph.
        return not _FENCE_OPEN.match(line) and _classify(line)[0] == BlockKind.PARAGRAPH
    return False


def _translatable(kind: BlockKind) -> bool:
    return kind not in (BlockKind.CODE, BlockKind.IMAGE, BlockKind.HR, BlockKind.BLANK)


def legend_image_id(markdown: str, block: Block) -> Optional[int]:
    """Image id of a legend LIST block (its first line is a legend marker), else ``None``."""
    if block.kind != BlockKind.LIST:
        return None
    first_line = markdown[block.start : block.end].split("\n", 1)[0]
    parsed = parse_legend_marker(first_line.rstrip("\r"))
    return parsed[0] if parsed is not None else None


def _parse_blocks(markdown: str) -> Tuple[List[Block], List[str]]:
    """Line-oriented single pass; blocks tile ``markdown`` exactly."""
    blocks: List[Block] = []
    warnings: List[str] = []
    kind: Optional[BlockKind] = None
    level = 0
    start = 0
    translatable: Optional[bool] = None  # per-block override (orphan legend marker)
    open_block = False  # no blank line seen since the block started
    fence: Optional[str] = None
    pos = 0

    def close(end: int) -> None:
        nonlocal kind, translatable
        if kind is not None:
            blocks.append(
                Block(
                    block_id=len(blocks) + 1,
                    kind=kind,
                    start=start,
                    end=end,
                    level=level,
                    translatable=_translatable(kind) if translatable is None else translatable,
                )
            )
        kind = None
        translatable = None

    lines = markdown.splitlines(keepends=True)
    for index, line in enumerate(lines):
        line_start = pos
        pos += len(line)
        if fence is not None:
            stripped = line.strip()
            if stripped.startswith(fence) and stripped.strip(fence[0]) == "":
                fence = None
                open_block = False
            continue
        if _is_blank(line):
            if kind is None:
                kind, level, start = BlockKind.BLANK, 0, line_start
            open_block = False
            continue
        if kind is not None and open_block and _continues(kind, line):
            if kind == BlockKind.LIST:
                item = _LIST_ITEM.match(line)
                if item:
                    level = max(level, len(item.group(1)) // 2)
            continue
        close(line_start)
        fence_match = _FENCE_OPEN.match(line)
        if fence_match:
            kind, level, start = BlockKind.CODE, 0, line_start
            fence = fence_match.group(1)
            open_block = True
            continue
        kind, level = _classify(line)
        start = line_start
        if LEGEND_MARKER.match(line):
            has_items = index + 1 < len(lines) and _LIST_ITEM.match(lines[index + 1]) is not None
            if not has_items:
                # Orphan marker: one-line PARAGRAPH block, never sent to the provider.
                kind, translatable, open_block = BlockKind.PARAGRAPH, False, False
                continue
        open_block = kind in (
            BlockKind.TABLE,
            BlockKind.LIST,
            BlockKind.BLOCKQUOTE,
            BlockKind.PARAGRAPH,
        )
    if fence is not None:
        warnings.append(f"unterminated code fence starting at offset {start}; runs to end of file")
    close(len(markdown))
    return blocks, warnings


def parse_blocks(markdown: str) -> List[Block]:
    """Parse BT-Markdown into blocks that tile the document (design doc 02, 4.2)."""
    return _parse_blocks(markdown)[0]


def heading_outline(markdown: str) -> List[Tuple[int, str]]:
    """``(level, title)`` of every ATX heading in document order."""
    outline: List[Tuple[int, str]] = []
    for block in parse_blocks(markdown):
        if block.kind == BlockKind.HEADING:
            title = _heading_title(markdown[block.start : block.end])
            outline.append((block.level, title))
    return outline


def _heading_title(text: str) -> str:
    match = _HEADING.match(text.split("\n", 1)[0])
    return match.group(2).strip() if match else text.strip()


# --------------------------------------------------------------------------- #
# 4.4 Oversize block split
# --------------------------------------------------------------------------- #


def _hard_split(text: str, limit: int) -> List[Tuple[int, int]]:
    """Split ``text`` at the last whitespace before ``limit`` repeatedly (last resort)."""
    spans: List[Tuple[int, int]] = []
    start = 0
    while len(text) - start > limit:
        cut = -1
        for match in _WHITESPACE.finditer(text, start, start + limit):
            cut = match.end()
        if cut <= start:
            cut = start + limit
        spans.append((start, cut))
        start = cut
    spans.append((start, len(text)))
    return spans


def _pack_spans(spans: List[Tuple[int, int]], limit: int) -> List[Tuple[int, int]]:
    """Greedily merge adjacent spans up to ``limit`` characters."""
    pieces: List[Tuple[int, int]] = []
    for start, end in spans:
        if pieces and end - pieces[-1][0] <= limit:
            pieces[-1] = (pieces[-1][0], end)
        else:
            pieces.append((start, end))
    return pieces


def _split_prose(text: str, limit: int, warnings: List[str], label: str) -> List[Tuple[int, int]]:
    """Sentence-boundary split of ``text`` (paragraph/blockquote/footnote/heading)."""
    units: List[Tuple[int, int]] = []
    for start, end in sentence_spans(text):
        if end - start > limit:
            warnings.append(f"{label}: sentence of {end - start} chars split at whitespace")
            units.extend((start + s, start + e) for s, e in _hard_split(text[start:end], limit))
        else:
            units.append((start, end))
    return _pack_spans(units, limit)


def _line_spans(text: str) -> List[Tuple[int, int]]:
    """Line spans of ``text``; whitespace-only lines are absorbed into the previous line."""
    spans: List[Tuple[int, int]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        if spans and line.strip() == "":
            spans[-1] = (spans[-1][0], pos + len(line))
        else:
            spans.append((pos, pos + len(line)))
        pos += len(line)
    return spans


def _split_list(text: str, limit: int, warnings: List[str], label: str) -> List[Tuple[int, int]]:
    """Split at top-level item boundaries; nested items stay with their parent item."""
    lines = _line_spans(text)
    indents = [
        len(m.group(1))
        for m in (_LIST_ITEM.match(text[s:e]) for s, e in lines)
        if m is not None
    ]
    top = min(indents) if indents else 0
    items: List[Tuple[int, int]] = []
    for start, end in lines:
        match = _LIST_ITEM.match(text[start:end])
        if not items or (match is not None and len(match.group(1)) == top):
            items.append((start, end))
        else:
            items[-1] = (items[-1][0], end)
    units: List[Tuple[int, int]] = []
    for start, end in items:
        if end - start > limit:
            warnings.append(f"{label}: list item of {end - start} chars split inside the item")
            units.extend(
                (start + s, start + e)
                for s, e in _split_prose(text[start:end], limit, warnings, label)
            )
        else:
            units.append((start, end))
    return _pack_spans(units, limit)


_DELIMITER_ROW = re.compile(r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")


def _split_table(text: str, limit: int, warnings: List[str], label: str) -> List[Tuple[int, int]]:
    """Split at row boundaries; the header (+ delimiter row) belongs to sub-chunk 0 only."""
    lines = _line_spans(text)
    header_rows = 1
    if len(lines) > 1 and _DELIMITER_ROW.match(text[lines[1][0] : lines[1][1]].rstrip("\n")):
        header_rows = 2
    header = (lines[0][0], lines[header_rows - 1][1]) if len(lines) >= header_rows else lines[0]
    units: List[Tuple[int, int]] = []
    for start, end in [header, *lines[header_rows:]]:
        if end - start > limit:
            warnings.append(f"{label}: table row of {end - start} chars split at whitespace")
            units.extend((start + s, start + e) for s, e in _hard_split(text[start:end], limit))
        else:
            units.append((start, end))
    return _pack_spans(units, limit)


def _split_block(
    block: Block, text: str, limit: int, warnings: List[str]
) -> List[Tuple[int, int]]:
    label = f"block {block.block_id} ({block.kind.value})"
    if block.kind == BlockKind.LIST:
        return _split_list(text, limit, warnings, label)
    if block.kind == BlockKind.TABLE:
        return _split_table(text, limit, warnings, label)
    return _split_prose(text, limit, warnings, label)


# --------------------------------------------------------------------------- #
# 4.3 Packing
# --------------------------------------------------------------------------- #

_SINGLE_KIND = {
    BlockKind.HEADING: ChunkKind.HEADING,
    BlockKind.PARAGRAPH: ChunkKind.TEXT,
    BlockKind.BLOCKQUOTE: ChunkKind.TEXT,
    BlockKind.BLANK: ChunkKind.TEXT,
    BlockKind.LIST: ChunkKind.LIST,
    BlockKind.TABLE: ChunkKind.TABLE,
    BlockKind.FOOTNOTE: ChunkKind.FOOTNOTE,
    BlockKind.CODE: ChunkKind.CODE,
    BlockKind.IMAGE: ChunkKind.IMAGE,
    BlockKind.HR: ChunkKind.HR,
}


def _chunk_kind(blocks: List[Block]) -> ChunkKind:
    content = [b for b in blocks if b.kind != BlockKind.BLANK] or blocks
    if len(content) == 1:
        return _SINGLE_KIND[content[0].kind]
    if all(b.kind == BlockKind.HEADING for b in content):
        return ChunkKind.HEADING
    return ChunkKind.TEXT


class _HeadingTracker:
    """Nearest H1/H2/H3 titles (``Chunk.heading_path``)."""

    def __init__(self, markdown: str) -> None:
        self._markdown = markdown
        self._stack: List[Tuple[int, str]] = []

    def observe(self, block: Block) -> None:
        if block.kind != BlockKind.HEADING or block.level > _MAX_HEADING_PATH_LEVEL:
            return
        while self._stack and self._stack[-1][0] >= block.level:
            self._stack.pop()
        self._stack.append((block.level, _heading_title(self._markdown[block.start : block.end])))

    def path(self) -> Tuple[str, ...]:
        return tuple(title for _, title in self._stack)


class _Packer:
    def __init__(self, markdown: str, min_chars: int, max_chars: int) -> None:
        self.markdown = markdown
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.chunks: List[Chunk] = []
        self.warnings: List[str] = []
        self.current: List[Block] = []
        self.headings = _HeadingTracker(markdown)

    # -- helpers ----------------------------------------------------------- #

    @property
    def size(self) -> int:
        return sum(b.end - b.start for b in self.current)

    def _emit(
        self,
        blocks: List[Block],
        kind: ChunkKind,
        translatable: bool,
        warnings: List[str],
        *,
        parent: Optional[Block] = None,
        sub_index: Optional[int] = None,
        sub_count: Optional[int] = None,
        span: Optional[Tuple[int, int]] = None,
    ) -> None:
        start, end = span if span is not None else (blocks[0].start, blocks[-1].end)
        text = self.markdown[start:end]
        path = self.headings.path()
        for block in blocks:
            if block.kind != BlockKind.HEADING:
                break
            self.headings.observe(block)
            path = self.headings.path()
        for block in blocks:
            self.headings.observe(block)
        self.chunks.append(
            Chunk(
                chunk_id=len(self.chunks) + 1,
                order=len(self.chunks) + 1,
                kind=kind,
                translatable=translatable,
                source_text=text,
                content_hash=content_hash_of(text),
                char_count=len(text),
                parent_block_id=parent.block_id if parent else None,
                sub_index=sub_index,
                sub_count=sub_count,
                heading_path=path,
                warnings=tuple(warnings),
            )
        )

    def flush(
        self, reason: str, *, keep_headings: bool = True, extra_warnings: Sequence[str] = ()
    ) -> None:
        """Close ``current``; trailing headings move to the next chunk when allowed."""
        if not self.current:
            return
        carried: List[Block] = []
        if keep_headings:
            while self.current and self.current[-1].kind == BlockKind.HEADING:
                carried.insert(0, self.current.pop())
            if not self.current:
                self.current = carried
                return
        blocks = self.current
        self.current = carried
        warnings: List[str] = []
        size = blocks[-1].end - blocks[0].start
        translatable = any(b.translatable for b in blocks)
        if reason != "eof" and translatable and size < self.min_chars:
            warnings.append(f"undersize:{reason}")
        if translatable and size > self.max_chars:
            warnings.append(f"oversize:{reason}")
        if reason != "eof" and blocks[-1].kind == BlockKind.HEADING:
            warnings.append(f"heading_stranded:{reason}")
        warnings.extend(extra_warnings)
        self._emit(blocks, _chunk_kind(blocks), translatable, warnings)

    # -- main loop ---------------------------------------------------------- #

    def run(self, blocks: List[Block]) -> None:
        for index, block in enumerate(blocks):
            size = block.end - block.start
            successor = blocks[index + 1] if index + 1 < len(blocks) else None
            if block.kind == BlockKind.BLANK:
                self.current.append(block)  # leading blank lines ride along with the next chunk
                continue
            if not block.translatable:
                self.flush("non_translatable", keep_headings=False)
                self.current.append(block)
                self.flush("non_translatable", keep_headings=False)
                continue
            if size > self.max_chars:
                self.flush("before_oversize", keep_headings=False)
                self._emit_split(block)
                continue
            if block.kind == BlockKind.TABLE:
                self.flush("before_table", keep_headings=False)
                self.current.append(block)
                self.flush("table")
                continue
            legend_id = legend_image_id(self.markdown, block)
            if legend_id is not None:
                # The legend may only join the caption paragraph isolated just before it
                # (see below); anything else in ``current`` is closed first (C3).
                predecessor = blocks[index - 1] if index > 0 else None
                caption_pending = (
                    predecessor is not None
                    and predecessor.kind == BlockKind.PARAGRAPH
                    and bool(self.current)
                    and self.current[-1] is predecessor
                )
                if self.current and not caption_pending:
                    self.flush("before_legend", keep_headings=False)
                self.current.append(block)
                self.flush("legend", extra_warnings=[f"legend:{legend_id}"])
                continue
            if (
                block.kind == BlockKind.PARAGRAPH
                and successor is not None
                and legend_image_id(self.markdown, successor) is not None
            ):
                # Caption paragraph: isolate it so the legend shares the chunk with it only.
                self.flush("before_legend", keep_headings=False)
                self.current.append(block)
                continue
            if self.size + size <= self.max_chars:
                self.current.append(block)
                if self.size >= self.min_chars and block.kind != BlockKind.HEADING:
                    self.flush("packed")
            elif (self.min_chars - self.size) <= (self.size + size - self.max_chars):
                self.flush("short_choice")
                self.current.append(block)
                if self.size >= self.min_chars and block.kind != BlockKind.HEADING:
                    self.flush("packed")
            else:
                self.current.append(block)
                self.flush("long_choice")
        self.flush("eof", keep_headings=False)

    def _emit_split(self, block: Block) -> None:
        text = self.markdown[block.start : block.end]
        pieces = _split_block(block, text, self.max_chars, self.warnings)
        kind = _SINGLE_KIND[block.kind]
        if kind == ChunkKind.HEADING:
            kind = ChunkKind.TEXT
        for index, (start, end) in enumerate(pieces):
            warnings: List[str] = []
            if end - start < self.min_chars:
                warnings.append("undersize:sub_chunk")
            if end - start > self.max_chars:
                warnings.append("oversize:indivisible")
            self._emit(
                [block],
                kind,
                True,
                warnings,
                parent=block,
                sub_index=index,
                sub_count=len(pieces),
                span=(block.start + start, block.start + end),
            )


# --------------------------------------------------------------------------- #
# 4.5 Entry point with lossless verification
# --------------------------------------------------------------------------- #


def chunk(
    markdown: str,
    *,
    min_chars: int = 1000,
    max_chars: int = 1500,
    provider_limit: Optional[int] = None,
) -> Result[ChunkResult]:
    """Split ``markdown`` into chunks (design doc 02, 4.3-4.5).

    ``provider_limit`` (``capabilities.max_chars_per_request``) lowers the effective
    maximum. The join of all ``source_text`` values is verified against the input;
    a mismatch is a ``CHUNK_INTEGRITY`` / ``JOB_FATAL`` error and nothing is returned.
    """
    if max_chars < 1 or min_chars < 0:
        return err(ErrorCode.PROVIDER_CONFIG, "chunk sizes must be positive", ErrorScope.USER)
    effective_max = max_chars if provider_limit is None else min(max_chars, provider_limit)
    effective_max = max(1, effective_max)
    warnings: List[str] = []
    if provider_limit is not None and provider_limit < max_chars:
        warnings.append(f"provider limit {provider_limit} lowers max_chars {max_chars}")
    effective_min = min(min_chars, effective_max)
    if effective_min != min_chars:
        warnings.append(f"min_chars {min_chars} lowered to {effective_min} (effective max)")

    blocks, parse_warnings = _parse_blocks(markdown)
    warnings.extend(parse_warnings)
    if not markdown:
        return Ok(ChunkResult(chunks=[], blocks=blocks, warnings=warnings))

    packer = _Packer(markdown, effective_min, effective_max)
    packer.run(blocks)
    warnings.extend(packer.warnings)

    joined = "".join(c.source_text for c in packer.chunks)
    if joined != markdown:
        return err(
            ErrorCode.CHUNK_INTEGRITY,
            "chunk join does not reproduce the source markdown "
            f"({len(joined)} vs {len(markdown)} chars)",
            ErrorScope.JOB_FATAL,
        )
    for index, c in enumerate(packer.chunks, start=1):
        if c.chunk_id != index or c.order != index:
            return err(
                ErrorCode.CHUNK_INTEGRITY,
                f"chunk ids not contiguous at position {index}",
                ErrorScope.JOB_FATAL,
            )
    return Ok(ChunkResult(chunks=packer.chunks, blocks=blocks, warnings=warnings))
