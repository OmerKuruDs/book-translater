"""Ordered chunk stream -> ``AssembledDocument`` (design doc 02, section 8.1).

``assemble`` consumes any ``Iterable[Chunk]`` ordered by ``order`` (in production
``ChunkRepository.iter_ordered``) and never materializes the chunk list: only
the growing output text, per-group scratch state and the summary lists are kept
(E-19).

Per chunk:

* COMPLETED               -> ``translated_text``
* ``translatable=False``  -> ``source_text`` verbatim
* FAILED + allow_partial  -> marker block wrapping the untranslated source
* FAILED otherwise        -> ``Err(EXPORT_REFUSED_PARTIAL, USER)``
* PENDING / PROCESSING    -> same code, "run translate first"
* no chunks at all        -> same code (the job was never translated)

Sub-chunks of one parent block are concatenated before the structural parity
check (NFR-06): heading levels in sequence, list-item counts and fence count of
the source vs the translated text of each group; a mismatch adds a warning and
puts the group's chunk ids into ``review_chunk_ids``.

Ordered-list markers are only counted where CommonMark would actually start a
list item (start of the fragment, after a blank line, inside a list, or number
``1`` which may interrupt a paragraph). When the source group has no ordered
list at all, a translated paragraph that starts with a Turkish ordinal
("3. Bölüm, …" for "Chapter 3 …") would still be rendered as ``<ol start="3">``;
``assemble`` therefore escapes such markers (``3\\. Bölüm``) so the Markdown and
the EPUB both keep the paragraph (CR-34). The escape rule and the count rule are
the same scanner, so a translation identical to its source is never rewritten.

Legend pairing (design doc 06, C10): when a group's source holds a legend block
(``<!-- legend:N more="K" -->`` + ``- label`` lines) the source labels are zipped
with the translated labels into ``- English — Türkçe`` items; a leading ordinal
in either half is escaped so the item never turns into an ordered list. The
marker line is kept (exporters render it), ``*… ve K etiket daha*`` is appended
when labels were truncated, and an item-count mismatch keeps the translated lines
with a ``legend_mismatch:N`` warning plus a review flag. ``pair_legends=False``
copies the group verbatim (identity round trip).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from ..domain.bt_syntax import parse_legend_marker
from ..domain.models import AssembledDocument, Chunk, ChunkStatus
from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err

log = logging.getLogger("book_translator.pipeline.assembler")

LEGEND_SEPARATOR = " — "
"""Separator between the source label and its translation in a paired legend item."""

LEGEND_MORE_NOTE = "*… ve {k} etiket daha*"
"""User-facing (Turkish) note appended to a truncated legend (AC US-16/4)."""

_ATX = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_BULLET_ITEM = re.compile(r"^[ \t]*[-*+][ \t]+\S")
_ORDERED_ITEM = re.compile(r"^([ \t]*)(\d{1,9})([.)])([ \t]+\S)")
_INLINE_MARKUP = re.compile(r"[*_`]+")
_MAX_LIST_INDENT = 3  # four spaces or more after a blank line is indented code, not a list
_LEGEND_ITEM = re.compile(r"^[ \t]*[-*+][ \t]+(.*?)[ \t]*$")
_LEADING_ORDINAL = re.compile(r"^(\d+)([.)])(?=\s|$)")


@dataclass(frozen=True)
class Structure:
    """Structural fingerprint of a markdown fragment (ATX headings, list items, fences)."""

    headings: Tuple[Tuple[int, str], ...]  # (level, title) in order
    bullet_items: int
    ordered_items: int
    fences: int

    @property
    def heading_levels(self) -> Tuple[int, ...]:
        return tuple(level for level, _ in self.headings)

    @property
    def list_items(self) -> int:
        """Total list-item count (bullet + ordered), kept for callers of the old field."""
        return self.bullet_items + self.ordered_items


@dataclass(frozen=True)
class _Scan:
    structure: Structure
    ordered_lines: Tuple[int, ...]  # indices (in ``text.split("\n")``) of counted ordered items


def _starts_ordered_item(indent: str, number: str, *, prev_blank: bool, in_list: bool) -> bool:
    """CommonMark: ``N.`` / ``N)`` starts an item after a blank line, inside a list, or (only
    for ``1``) by interrupting a paragraph; deeper indentation after a blank line is code."""
    if in_list:
        return True
    if len(indent) > _MAX_LIST_INDENT:
        return False
    return prev_blank or number == "1"


def _scan(text: str) -> _Scan:
    headings: List[Tuple[int, str]] = []
    bullet_items = 0
    ordered_lines: List[int] = []
    fences = 0
    in_fence = False
    fence_char = ""
    prev_blank = True  # the start of the fragment behaves like a preceding blank line
    in_list = False  # the previous non-blank line was a list item or its continuation
    for index, line in enumerate(text.split("\n")):
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_char = True, marker[0]
                fences += 1
                prev_blank, in_list = False, False
                continue
            if marker[0] == fence_char:
                in_fence = False
                prev_blank = False
                continue
        if in_fence:
            continue
        if not line.strip():
            prev_blank = True
            continue
        heading = _ATX.match(line)
        if heading:
            title = _INLINE_MARKUP.sub("", heading.group(2)).strip()
            headings.append((len(heading.group(1)), title))
            prev_blank, in_list = False, False
            continue
        if _BULLET_ITEM.match(line):
            bullet_items += 1
            prev_blank, in_list = False, True
            continue
        ordered = _ORDERED_ITEM.match(line)
        if ordered and _starts_ordered_item(
            ordered.group(1), ordered.group(2), prev_blank=prev_blank, in_list=in_list
        ):
            ordered_lines.append(index)
            prev_blank, in_list = False, True
            continue
        # prose, or an indented continuation of the current list item
        in_list = in_list and line[0] in " \t"
        prev_blank = False
    structure = Structure(
        headings=tuple(headings),
        bullet_items=bullet_items,
        ordered_items=len(ordered_lines),
        fences=fences,
    )
    return _Scan(structure=structure, ordered_lines=tuple(ordered_lines))


def scan_structure(text: str) -> Structure:
    """Line scanner for headings/fences/list items; lines inside fences are ignored."""
    return _scan(text).structure


def escape_ordinals(text: str) -> Tuple[str, int]:
    """Backslash-escape every ordered-list marker that would start a list item.

    ``"3. Bölüm, …"`` becomes ``"3\\. Bölüm, …"``; CommonMark and python-markdown render
    the escaped marker as literal text, so the paragraph survives in Markdown and EPUB.
    Returns the rewritten text and the number of escaped lines (0 → ``text`` unchanged).
    """
    ordered_lines = _scan(text).ordered_lines
    if not ordered_lines:
        return text, 0
    lines = text.split("\n")
    for index in ordered_lines:
        match = _ORDERED_ITEM.match(lines[index])
        if match is None:  # pragma: no cover - the scanner matched the same line
            continue
        lines[index] = (
            f"{match.group(1)}{match.group(2)}\\{match.group(3)}{lines[index][match.end(3):]}"
        )
    return "\n".join(lines), len(ordered_lines)


@dataclass(frozen=True)
class _Legend:
    """A legend block found in a text: marker line index, ids and the item labels."""

    marker_index: int
    image_id: int
    more: int
    items: Tuple[str, ...]
    end_index: int  # index of the first line after the items


def _find_legend(lines: Sequence[str]) -> Optional[_Legend]:
    """First legend block outside code fences, or ``None``."""
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
        parsed = parse_legend_marker(line)
        if parsed is None:
            continue
        items: List[str] = []
        end = index + 1
        while end < len(lines):
            item = _LEGEND_ITEM.match(lines[end])
            if item is None:
                break
            items.append(item.group(1))
            end += 1
        return _Legend(index, parsed[0], parsed[1], tuple(items), end)
    return None


def escape_label(label: str) -> str:
    """Escape a leading ``N.`` / ``N)`` so a paired item cannot start an ordered list."""
    return _LEADING_ORDINAL.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", label, count=1)


def pair_legend(source: str, translated: str) -> Tuple[str, Optional[int]]:
    """Zip the source legend labels with the translated ones (design doc 06, 3.3).

    Returns the rewritten text and ``None``, or the untouched text and the image id
    when the source has a legend that cannot be paired (marker missing in the
    translation, different image id or item count).
    """
    src = _find_legend(source.split("\n"))
    if src is None or not src.items:
        return translated, None
    lines = translated.split("\n")
    found = _find_legend(lines)
    if found is None or found.image_id != src.image_id or len(found.items) != len(src.items):
        return translated, src.image_id
    paired = [
        f"- {escape_label(label)}{LEGEND_SEPARATOR}{escape_label(target)}"
        for label, target in zip(src.items, found.items, strict=True)
    ]
    out = [*lines[: found.marker_index], lines[found.marker_index], *paired]
    if src.more > 0:
        out.extend(["", LEGEND_MORE_NOTE.format(k=src.more)])
    out.extend(lines[found.end_index :])
    return "\n".join(out), None


def _marker_block(chunk: Chunk) -> str:
    reason = " ".join((chunk.last_error or "unknown error").split()) or "unknown error"
    source = chunk.source_text
    if not source.endswith("\n\n"):
        source = source.rstrip("\n") + "\n\n"
    return (
        f"> **[UNTRANSLATED — chunk {chunk.chunk_id} — {reason}]**\n\n"
        f"{source}"
        f"> **[END UNTRANSLATED — chunk {chunk.chunk_id}]**\n\n"
    )


@dataclass
class _Group:
    """Consecutive sub-chunks of one parent block (or a single chunk)."""

    parent_block_id: Optional[int]
    chunk_ids: List[int] = field(default_factory=list)
    source: List[str] = field(default_factory=list)
    translated: List[str] = field(default_factory=list)
    check: bool = True  # False when a member was copied verbatim / marked


def assemble(
    chunks: Iterable[Chunk],
    *,
    allow_partial: bool,
    metadata: Mapping[str, str],
    source_markdown: Optional[str] = None,
    pair_legends: bool = True,
) -> Result[AssembledDocument]:
    """Join ``chunks`` (ordered by ``order``) into the translated document.

    ``source_markdown`` (optional) enables two whole-document checks: the joined
    ``source_text`` must reproduce it exactly (lossless chunking), and the global
    heading sequence of source vs translation is compared once more.
    ``pair_legends=False`` leaves legend groups exactly as translated (identity
    round trip); the default pairs source and translated labels (C10).
    """
    parts: List[str] = []
    failed_ids: List[int] = []
    review_ids: List[int] = []
    warnings: List[str] = []
    source_digest = hashlib.sha256()
    last_order = 0
    group: Optional[_Group] = None

    def flush(current: Optional[_Group]) -> None:
        """Repair ordinals, run the parity check and append the group's text to ``parts``."""
        if current is None:
            return
        translated = "".join(current.translated)
        if not current.check:
            parts.append(translated)
            return
        source_text = "".join(current.source)
        source_struct = scan_structure(source_text)
        label = (
            f"chunk {current.chunk_ids[0]}"
            if len(current.chunk_ids) == 1
            else f"chunks {current.chunk_ids[0]}-{current.chunk_ids[-1]}"
        )
        mismatch = False
        if pair_legends:
            translated, unpaired = pair_legend(source_text, translated)
            if unpaired is not None:
                warnings.append(f"legend_mismatch:{unpaired}")
                mismatch = True
                log.warning(
                    "legend %d in %s could not be paired; translated lines kept",
                    unpaired,
                    label,
                    extra={"event": "legend_mismatch", "chunk_id": current.chunk_ids[0]},
                )
        if source_struct.ordered_items == 0:
            translated, escaped = escape_ordinals(translated)
            if escaped:
                log.debug(
                    "escaped %d ordinal marker(s) at paragraph start in %s",
                    escaped,
                    label,
                    extra={"event": "ordinal_escaped", "chunk_id": current.chunk_ids[0]},
                )
        parts.append(translated)
        translated_struct = scan_structure(translated)
        if source_struct.heading_levels != translated_struct.heading_levels:
            warnings.append(
                f"structure_mismatch:headings:{label}:"
                f"source={list(source_struct.heading_levels)} "
                f"translated={list(translated_struct.heading_levels)}"
            )
            mismatch = True
        if source_struct.bullet_items != translated_struct.bullet_items or (
            source_struct.ordered_items > 0
            and source_struct.ordered_items != translated_struct.ordered_items
        ):
            warnings.append(
                f"structure_mismatch:list_items:{label}:"
                f"source={source_struct.list_items} translated={translated_struct.list_items}"
            )
            mismatch = True
        if source_struct.fences != translated_struct.fences:
            warnings.append(
                f"structure_mismatch:fences:{label}:"
                f"source={source_struct.fences} translated={translated_struct.fences}"
            )
            mismatch = True
        if mismatch:
            for chunk_id in current.chunk_ids:
                if chunk_id not in review_ids:
                    review_ids.append(chunk_id)

    for chunk in chunks:
        if chunk.order <= last_order:
            return err(
                ErrorCode.INTERNAL,
                f"chunks are not strictly ordered "
                f"(chunk {chunk.chunk_id} after order {last_order})",
                ErrorScope.JOB_FATAL,
                context={"chunk_id": chunk.chunk_id},
            )
        last_order = chunk.order
        source_digest.update(chunk.source_text.encode("utf-8"))

        if chunk.translatable and chunk.status in (ChunkStatus.PENDING, ChunkStatus.PROCESSING):
            return err(
                ErrorCode.EXPORT_REFUSED_PARTIAL,
                f"chunk {chunk.chunk_id} is {chunk.status.value}; run `translate` first",
                ErrorScope.USER,
                context={"chunk_id": chunk.chunk_id, "status": chunk.status.value},
            )

        if group is None or chunk.parent_block_id is None or (
            chunk.parent_block_id != group.parent_block_id
        ):
            flush(group)
            group = _Group(parent_block_id=chunk.parent_block_id)
        group.chunk_ids.append(chunk.chunk_id)
        group.source.append(chunk.source_text)

        if chunk.review_flag and chunk.chunk_id not in review_ids:
            review_ids.append(chunk.chunk_id)

        if not chunk.translatable:
            text = chunk.source_text
        elif chunk.status is ChunkStatus.FAILED:
            if not allow_partial:
                return err(
                    ErrorCode.EXPORT_REFUSED_PARTIAL,
                    f"chunk {chunk.chunk_id} is FAILED; re-run `translate` or export with "
                    "--allow-partial",
                    ErrorScope.USER,
                    context={"chunk_id": chunk.chunk_id, "status": chunk.status.value},
                )
            failed_ids.append(chunk.chunk_id)
            text = _marker_block(chunk)
            group.check = False
        elif chunk.translated_text is None:
            return err(
                ErrorCode.INTERNAL,
                f"chunk {chunk.chunk_id} is COMPLETED without translated text",
                ErrorScope.JOB_FATAL,
                context={"chunk_id": chunk.chunk_id},
            )
        else:
            text = chunk.translated_text
        group.translated.append(text)
    flush(group)
    if last_order == 0:
        return err(
            ErrorCode.EXPORT_REFUSED_PARTIAL,
            "no chunks in the state database; run `translate` first",
            ErrorScope.USER,
        )

    markdown = "".join(parts)
    outline = scan_structure(markdown)
    if source_markdown is not None:
        expected = hashlib.sha256(source_markdown.encode("utf-8")).hexdigest()
        if expected != source_digest.hexdigest():
            warnings.append("source_mismatch:joined chunk sources differ from source_markdown")
        source_levels = scan_structure(source_markdown).heading_levels
        if source_levels != outline.heading_levels:
            warnings.append(
                f"structure_mismatch:headings:document:source={len(source_levels)} "
                f"translated={len(outline.heading_levels)}"
            )
    return Ok(
        AssembledDocument(
            markdown=markdown,
            metadata=dict(metadata),
            failed_chunk_ids=failed_ids,
            review_chunk_ids=review_ids,
            heading_outline=list(outline.headings),
            warnings=warnings,
        )
    )
