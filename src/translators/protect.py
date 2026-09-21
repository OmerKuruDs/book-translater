"""Inline protection of non-translatable spans (design doc 02, section 4.6).

Inline code spans, bare URLs, link/image targets and footnote references are
replaced by placeholders before a segment is sent to the provider and restored
afterwards. DeepL gets XML tags (``<x id="n"/>`` with ``ignore_tags=["x"]``);
every other provider gets sentinel tokens (``⟦n⟧``).
"""

from __future__ import annotations

import re
from typing import Dict, List, Literal, Tuple

from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err

__all__ = ["ProtectMode", "protect", "restore", "restore_lenient", "strip_control_chars"]

ProtectMode = Literal["xml", "sentinel"]

_CODE_SPAN = re.compile(r"(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)")
_LINK_TARGET = re.compile(r"(?<=\])\([^()\s]*(?:\([^()\s]*\)[^()\s]*)*(?:\s+\"[^\"]*\")?\)")
_BARE_URL = re.compile(r"(?<![\w/])(?:https?://|www\.)[^\s<>\"'`)\]]+")
_FOOTNOTE_REF = re.compile(r"\[\^[^\]\s]+\](?!:)")
_TRAILING_PUNCT = ".,;:!?"

_XML_PLACEHOLDER = re.compile(r"<x\s+id=[\"']?(\d+)[\"']?\s*/?>(?:\s*</x\s*>)?")
_SENTINEL_PLACEHOLDER = re.compile(r"⟦\s*(\d+)\s*⟧")


_XML_VALID_RANGES = (
    (0x09, 0x09), (0x0A, 0x0A), (0x0D, 0x0D), (0x20, 0xD7FF),
    (0xE000, 0xFFFD), (0x10000, 0x10FFFF),
)
_XML_INVALID = re.compile(
    "[^" + "".join(chr(lo) + "-" + chr(hi) for lo, hi in _XML_VALID_RANGES) + "]"
)
"""Characters XML 1.0 forbids: the C0 controls except tab / LF / CR, and the two
non-characters. DeepL is called with ``tag_handling="xml"``, so a single one of these
anywhere in a request makes its parser reject the *whole* request (HTTP 400, "Tag
handling parsing failed") - every unit of the batch fails, not just the one that carries
it. They are not real content: PDFs built from Computer Modern (LaTeX) map some math
glyphs onto codepoints like U+0014 / U+0015, and the text layer hands them back."""


def strip_control_chars(text: str) -> str:
    """``text`` without the characters XML 1.0 forbids (see :data:`_XML_INVALID`).

    Applied to everything sent to a provider - the payload and the context window. The
    stored source text is left as it is, so hashes and the source PDF stay untouched."""
    return _XML_INVALID.sub("", text)


def _placeholder(mode: ProtectMode, index: int) -> str:
    return f'<x id="{index}"/>' if mode == "xml" else f"⟦{index}⟧"


def _find_spans(text: str) -> List[Tuple[int, int]]:
    """Non-overlapping spans to protect, in document order (code spans win)."""
    taken: List[Tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= s or start >= e for s, e in taken)

    for match in _CODE_SPAN.finditer(text):
        taken.append((match.start(), match.end()))
    for pattern in (_LINK_TARGET, _FOOTNOTE_REF, _BARE_URL):
        for match in pattern.finditer(text):
            start, end = match.start(), match.end()
            if pattern is _BARE_URL:
                while end > start and text[end - 1] in _TRAILING_PUNCT:
                    end -= 1
            if end > start and free(start, end):
                taken.append((start, end))
    taken.sort()
    return taken


def protect(text: str, mode: ProtectMode) -> Tuple[str, Dict[str, str]]:
    """Replace protected spans with numbered placeholders; return text and the map."""
    text = strip_control_chars(text)
    mapping: Dict[str, str] = {}
    out: List[str] = []
    pos = 0
    for index, (start, end) in enumerate(_find_spans(text), start=1):
        token = _placeholder(mode, index)
        mapping[token] = text[start:end]
        out.append(text[pos:start])
        out.append(token)
        pos = end
    out.append(text[pos:])
    return "".join(out), mapping


def _canonicalize(text: str, mapping: Dict[str, str]) -> str:
    """Normalize provider-mangled placeholders (`<x id='1'></x>`, `⟦ 1 ⟧`) to our form."""
    if not mapping:
        return text
    mode: ProtectMode = "xml" if next(iter(mapping)).startswith("<x") else "sentinel"
    pattern = _XML_PLACEHOLDER if mode == "xml" else _SENTINEL_PLACEHOLDER
    return pattern.sub(lambda m: _placeholder(mode, int(m.group(1))), text)


def _counts(text: str, mapping: Dict[str, str]) -> Dict[str, int]:
    return {token: text.count(token) for token in mapping}


def _replace_all(text: str, mapping: Dict[str, str]) -> str:
    # Longest token first so `⟦1⟧` never matches inside `⟦12⟧` (it cannot, but be safe).
    for token in sorted(mapping, key=len, reverse=True):
        text = text.replace(token, mapping[token])
    return text


def restore(text: str, mapping: Dict[str, str]) -> Result[str]:
    """Put the original spans back; every placeholder must occur exactly once."""
    text = _canonicalize(text, mapping)
    counts = _counts(text, mapping)
    missing = [t for t, n in counts.items() if n == 0]
    duplicated = [t for t, n in counts.items() if n > 1]
    if missing or duplicated:
        return err(
            ErrorCode.PROVIDER_EMPTY_RESPONSE,
            f"placeholders altered by the provider: {len(missing)} missing, "
            f"{len(duplicated)} duplicated",
            ErrorScope.CHUNK_RETRYABLE,
            context={"missing": len(missing), "duplicated": len(duplicated)},
        )
    return Ok(_replace_all(text, mapping))


def restore_lenient(text: str, mapping: Dict[str, str]) -> Tuple[str, List[str]]:
    """Best-effort restore after the retry budget: missing spans are appended at the end."""
    text = _canonicalize(text, mapping)
    counts = _counts(text, mapping)
    warnings: List[str] = []
    appended: List[str] = []
    for index, (token, original) in enumerate(mapping.items(), start=1):
        if counts[token] == 0:
            warnings.append(f"placeholder_missing:{index}")
            appended.append(original)
        elif counts[token] > 1:
            warnings.append(f"placeholder_duplicated:{index}")
    restored = _replace_all(text, mapping)
    if appended:
        restored = restored.rstrip() + " " + " ".join(appended)
    return restored, warnings
