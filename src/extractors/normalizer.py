"""Markdown -> BT-Markdown normalizer (design doc 02, sections 4.1 and 6.2).

``normalize_markdown`` turns arbitrary Markdown (marker output) into the
constrained BT-Markdown subset the chunker expects. ``ensure_bt_markdown`` is
a cheap idempotent safety pass used by the PyMuPDF serializer.
"""

from __future__ import annotations

import html
import re
from typing import List, Optional, Tuple

from ..domain.bt_syntax import IMAGE_LINE, format_image_line
from .cleanup import collect_compounds, is_page_number, join_hyphenated

IMAGE_TOKEN_RE = IMAGE_LINE  # shared grammar (design doc 06, C1); bare and ``src`` forms

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^`~\s]*)\s*$")
_ATX_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT_H1_RE = re.compile(r"^=+\s*$")
_SETEXT_H2_RE = re.compile(r"^-+\s*$")
_HR_RE = re.compile(r"^\s{0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$")
_IMAGE_LINE_RE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)]*)\)\s*$")
_IMAGE_INLINE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
_TABLE_DELIM_RE = re.compile(r"^\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t ]+")


def _strip_html(text: str) -> str:
    """Remove HTML tags/comments (except image tokens) and unescape entities."""
    if IMAGE_TOKEN_RE.match(text.strip()):
        return text.strip()
    cleaned = _HTML_BR_RE.sub(" ", text)
    cleaned = _HTML_COMMENT_RE.sub("", cleaned)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    return html.unescape(cleaned)


def _expand_tabs(line: str) -> str:
    return line.replace("\t", "    ")


class _Normalizer:
    """Single-pass line state machine producing BT-Markdown blocks."""

    def __init__(self, text: str) -> None:
        self.lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.blocks: List[str] = []
        self.warnings: List[str] = []
        self.image_counter = 0
        self.compounds = collect_compounds(self.lines)
        self._paragraph: List[str] = []

    # -- helpers -----------------------------------------------------------

    def _flush_paragraph(self) -> None:
        if not self._paragraph:
            return
        text = ""
        for raw in self._paragraph:
            cleaned = _WS_RE.sub(" ", raw).strip()
            if not cleaned:
                continue
            if not text:
                text = cleaned
                continue
            text, warning = join_hyphenated(text, cleaned, self.compounds)
            if warning:
                self.warnings.append(warning)
        self._paragraph = []
        if text:
            self.blocks.append(text)

    def _emit(self, block: str) -> None:
        self._flush_paragraph()
        self.blocks.append(block)

    def _next_image_token(self) -> str:
        self.image_counter += 1
        return format_image_line(self.image_counter, None)

    def _peek(self, index: int) -> Optional[str]:
        return self.lines[index] if index < len(self.lines) else None

    # -- main loop -----------------------------------------------------------

    def run(self) -> Tuple[str, List[str]]:
        i = 0
        n = len(self.lines)
        while i < n:
            raw = self.lines[i]
            fence = _FENCE_RE.match(raw)
            if fence:
                i = self._consume_fence(i, fence.group(2), fence.group(3))
                continue
            line = _expand_tabs(raw)
            stripped = line.strip()
            if not stripped:
                self._flush_paragraph()
                i += 1
                continue
            if IMAGE_TOKEN_RE.match(stripped):
                self._emit(stripped)
                i += 1
                continue
            atx = _ATX_RE.match(stripped)
            if atx:
                title = _strip_html(atx.group(2)).strip()
                if title:
                    self._emit(f"{atx.group(1)} {title}")
                i += 1
                continue
            if _HR_RE.match(line) and not self._paragraph:
                self._emit("---")
                i += 1
                continue
            image = _IMAGE_LINE_RE.match(line)
            if image:
                self._emit(self._next_image_token())
                caption = _strip_html(image.group(1)).strip()
                if caption:
                    self.blocks.append(caption)
                i += 1
                continue
            if stripped.startswith("|"):
                i = self._consume_table(i)
                continue
            footnote = _FOOTNOTE_DEF_RE.match(stripped)
            if footnote:
                i = self._consume_footnote(i, footnote.group(1), footnote.group(2))
                continue
            if _LIST_RE.match(line):
                i = self._consume_list(i)
                continue
            if _QUOTE_RE.match(line):
                i = self._consume_quote(i)
                continue
            # Setext heading: the next line is an underline of = or -.
            nxt = self._peek(i + 1)
            if nxt is not None and not self._paragraph:
                if _SETEXT_H1_RE.match(nxt):
                    self._emit(f"# {_strip_html(stripped)}")
                    i += 2
                    continue
                if _SETEXT_H2_RE.match(nxt):
                    self._emit(f"## {_strip_html(stripped)}")
                    i += 2
                    continue
            if is_page_number(stripped):
                self.warnings.append(f"page-number line removed: {stripped}")
                i += 1
                continue
            self._paragraph.append(self._inline(line))
            i += 1
        self._flush_paragraph()
        text = "\n\n".join(self.blocks)
        return ensure_bt_markdown(text), self.warnings

    def _inline(self, line: str) -> str:
        """Inline images -> alt text; HTML stripped."""

        def _alt(match: "re.Match[str]") -> str:
            return match.group(1)

        replaced = _IMAGE_INLINE_RE.sub(_alt, line)
        if replaced != line:
            self.warnings.append("inline image replaced by its alt text")
        return _strip_html(replaced)

    # -- block consumers -------------------------------------------------------

    def _consume_fence(self, start: int, marker: str, info: str) -> int:
        char = marker[0]
        min_len = len(marker)
        body: List[str] = []
        i = start + 1
        closed = False
        while i < len(self.lines):
            candidate = self.lines[i]
            close = _FENCE_RE.match(candidate)
            if close and close.group(2)[0] == char and len(close.group(2)) >= min_len:
                if not close.group(3):
                    closed = True
                    i += 1
                    break
            if candidate.startswith("\t"):
                candidate = candidate.replace("\t", "    ", 1)
            body.append(candidate)
            i += 1
        if not closed:
            self.warnings.append("unterminated code fence; closed at end of document")
            while body and not body[-1].strip():
                body.pop()
        fence_line = f"```{info}" if info else "```"
        self._emit("\n".join([fence_line, *body, "```"]))
        return i

    def _consume_table(self, start: int) -> int:
        rows: List[str] = []
        i = start
        while i < len(self.lines):
            line = _expand_tabs(self.lines[i]).strip()
            if not line.startswith("|"):
                break
            rows.append(_strip_html(line))
            i += 1
        if len(rows) == 1 or not _TABLE_DELIM_RE.match(rows[1]):
            columns = max(1, rows[0].count("|") - 1)
            rows.insert(1, "|" + "|".join(["---"] * columns) + "|")
            self.warnings.append("table without delimiter row; delimiter inserted")
        self._emit("\n".join(rows))
        return i

    def _consume_footnote(self, start: int, ident: str, first: str) -> int:
        parts = [first.strip()] if first.strip() else []
        i = start + 1
        while i < len(self.lines):
            line = _expand_tabs(self.lines[i])
            if not line.strip() or not line.startswith(" "):
                break
            parts.append(line.strip())
            i += 1
        text = _strip_html(" ".join(parts)).strip()
        self._emit(f"[^{ident}]: {text}")
        return i

    def _consume_list(self, start: int) -> int:
        items: List[str] = []
        i = start
        while i < len(self.lines):
            line = _expand_tabs(self.lines[i])
            if not line.strip():
                break
            match = _LIST_RE.match(line)
            if match:
                indent = len(match.group(1))
                marker = match.group(2)
                normalized_marker = "-" if marker in "-*+" else marker.replace(")", ".")
                level = indent // 2
                content = self._inline(match.group(3)).strip()
                items.append(f"{'  ' * level}{normalized_marker} {content}")
            elif line.startswith(" ") and items:
                # Continuation line: fold into the previous item.
                items[-1] = f"{items[-1]} {self._inline(line).strip()}"
            else:
                break
            i += 1
        self._emit("\n".join(items))
        return i

    def _consume_quote(self, start: int) -> int:
        quoted: List[str] = []
        i = start
        while i < len(self.lines):
            line = _expand_tabs(self.lines[i])
            match = _QUOTE_RE.match(line)
            if not match or not line.strip():
                break
            quoted.append(f"> {self._inline(match.group(1)).strip()}".rstrip())
            i += 1
        self._emit("\n".join(quoted))
        return i


def normalize_markdown(text: str) -> Tuple[str, List[str]]:
    """Convert arbitrary Markdown into BT-Markdown; returns ``(markdown, warnings)``."""
    return _Normalizer(text).run()


def ensure_bt_markdown(text: str) -> str:
    """Final safety pass: ``\\n`` newlines, no tabs at line start, one blank line
    between blocks, no trailing whitespace outside fences, exactly one trailing ``\\n``.

    Idempotent; does not change block structure.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    blank_pending = False
    for raw in lines:
        fence = _FENCE_RE.match(raw)
        if in_fence:
            if fence and fence.group(2)[0] == fence_char and len(fence.group(2)) >= fence_len:
                in_fence = False
            line = raw
            if line.startswith("\t"):
                line = line.replace("\t", "    ", 1)
            out.append(line)
            continue
        if fence:
            in_fence = True
            fence_char = fence.group(2)[0]
            fence_len = len(fence.group(2))
            if blank_pending and out:
                out.append("")
            blank_pending = False
            out.append("```" + fence.group(3))
            continue
        line = _expand_tabs(raw).rstrip()
        if not line.strip():
            blank_pending = True
            continue
        if blank_pending and out:
            out.append("")
        blank_pending = False
        out.append(line)
    if in_fence:
        out.append("```")
    return "\n".join(out).strip("\n") + "\n"
