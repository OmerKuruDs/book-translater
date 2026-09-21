"""Sentence splitting shared by the chunker (oversize paragraph split) and the
glossary discovery (design doc 02, sections 4.4 and 7.1).

The split is *lossless*: ``"".join(split_sentences(text)) == text``. Whitespace
after a terminator stays at the end of the preceding sentence. Boundaries are
never placed inside inline code spans or inside link syntax ``[text](target)``.
Standard library only.
"""

from __future__ import annotations

import re
from typing import List, Tuple

# Terminator followed by optional closing quotes/brackets, then whitespace.
_TERMINATOR = re.compile("[.!?…]+[\"'”’)\\]]*\\s+")

# Abbreviations that end with a period but do not end a sentence (compared
# case-insensitively against the word before the period, trailing dots removed).
ABBREVIATIONS = frozenset(
    {
        "e.g", "i.e", "etc", "vs", "cf", "no", "nos", "mr", "mrs", "ms", "dr", "prof",
        "sr", "jr", "st", "mt", "fig", "figs", "eq", "vol", "ch", "chap", "sec", "pp",
        "p", "ed", "eds", "al", "inc", "ltd", "co", "corp", "jan", "feb", "mar", "apr",
        "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "approx", "dept",
        "univ", "gen", "col", "lt", "sgt", "capt", "rev", "hon", "u.s", "u.k",
    }
)

_WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z.]*)$")
_INLINE_CODE = re.compile(r"`+[^`]*`+")
_LINK = re.compile(r"\[[^\]\n]*\]\([^)\n]*\)")


def _protected_spans(text: str) -> List[Tuple[int, int]]:
    """Half-open spans of inline code and link syntax where no split may occur."""
    spans: List[Tuple[int, int]] = []
    for pattern in (_INLINE_CODE, _LINK):
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))
    spans.sort()
    return spans


def _inside(pos: int, spans: List[Tuple[int, int]]) -> bool:
    return any(start < pos < end for start, end in spans)


def _is_abbreviation(text: str, terminator_start: int) -> bool:
    """True when the period at ``terminator_start`` ends an abbreviation or an initial."""
    if text[terminator_start] != ".":
        return False
    match = _WORD_BEFORE.search(text, 0, terminator_start)
    if not match:
        return False
    word = match.group(1)
    if len(word) == 1 and word.isupper():
        return True  # initial: "A. Smith"
    return word.lower().rstrip(".") in ABBREVIATIONS


def sentence_spans(text: str) -> List[Tuple[int, int]]:
    """Return ``(start, end)`` spans that tile ``text`` exactly, one per sentence."""
    if not text:
        return []
    protected = _protected_spans(text)
    spans: List[Tuple[int, int]] = []
    start = 0
    for match in _TERMINATOR.finditer(text):
        if _inside(match.start(), protected) or _inside(match.end(), protected):
            continue
        if _is_abbreviation(text, match.start()):
            continue
        end = match.end()
        if end >= len(text):
            break
        spans.append((start, end))
        start = end
    spans.append((start, len(text)))
    return spans


def split_sentences(text: str) -> List[str]:
    """Split ``text`` into sentences; concatenation reproduces ``text`` byte-for-byte."""
    return [text[s:e] for s, e in sentence_spans(text)]
