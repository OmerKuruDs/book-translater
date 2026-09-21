"""Capitalized n-gram / acronym term discovery (design doc 02, section 7.1).

Pipeline:

0. Strip code fences, inline code, image lines, legend blocks (marker line up
   to the next blank line; FR-33) and URLs from the markdown.
1. Split each line into sentences (shared ``domain.sentences`` splitter) and
   tokenize with positions; the first word token of a sentence is
   *sentence-initial*.
2. Candidate n-grams (n = 1..3) whose tokens are all Capitalized or acronyms,
   optionally joined by ``of|the|de|von|van`` between two tokens
   (``Duke of York``). Sentence-initial unigrams are counted separately.
3. Stop-list: n-grams made only of stop-list tokens, or starting/ending with a
   function word, are discarded (``The Dragon`` -> only ``Dragon`` survives).
4. ``count = mid_sentence + sentence_initial`` but ``mid_sentence >= 1`` is required.
5. Unigrams whose lowercase form occurs at least ``count`` times as a normal
   word are ``AMBIGUOUS`` (``Will``/``will``), otherwise ``PROPOSED``.
6. ``min_count`` threshold; a longer n-gram absorbs a sub-gram when >= 80 % of the
   sub-gram's occurrences lie inside occurrences of longer surviving n-grams.
7. Sorted by count desc (then source asc for determinism); ``target`` empty;
   ``origin = "discovered"``.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Set, Tuple

from ..domain.bt_syntax import is_image_line, is_legend_marker
from ..domain.models import GlossaryEntry, GlossaryStatus
from ..domain.sentences import sentence_spans
from ._stoplist import HONORIFICS, STOP_TOKENS, STOP_WORDS

DEFAULT_MIN_COUNT = 3
MAX_NGRAM = 3
ABSORB_RATIO = 0.8
JOINERS = frozenset({"of", "the", "de", "von", "van"})

_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,}).*$", re.MULTILINE)
_INLINE_CODE = re.compile(r"`+[^`\n]*`+")
_URL = re.compile(r"(?:https?|ftp)://\S+|www\.\S+")
_LINK_TARGET = re.compile(r"\]\([^)\n]*\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WORD = re.compile(r"[A-Za-z][A-Za-z’'\-]*")
_CAPITALIZED = re.compile(r"[A-Z][a-z’'\-]+(?:[A-Z][a-z’'\-]+)*")
_ACRONYM = re.compile(r"[A-Z]{2,6}")
_LOWER = re.compile(r"[a-z][a-z’'\-]*")
_GAP = re.compile(r"[ \t*_]*")
_POSSESSIVE = re.compile(r"(?:'s|’s|')$")


@dataclass(frozen=True)
class _Token:
    text: str
    start: int
    end: int
    sentence_initial: bool


def strip_untranslatable(markdown: str) -> str:
    """Remove fenced code, inline code, image lines, legend blocks, URLs and link targets.

    Line count and positions of the remaining lines are preserved (blank lines stand
    in for removed ones). A legend block runs from its marker line to the next blank
    line (design doc 06, 3.3): figure labels are never glossary candidates.
    """
    lines = markdown.split("\n")
    kept: List[str] = []
    in_fence = False
    in_legend = False
    fence_marker = ""
    for line in lines:
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)
            if not in_fence:
                in_fence, fence_marker = True, marker[0]
                kept.append("")
                continue
            if marker[0] == fence_marker:
                in_fence = False
                kept.append("")
                continue
        if in_fence:
            kept.append("")
            continue
        stripped = line.rstrip("\r")
        if is_legend_marker(stripped):
            in_legend = True
            kept.append("")
            continue
        if in_legend:
            if stripped.strip():
                kept.append("")
                continue
            in_legend = False
        if is_image_line(stripped):
            kept.append("")
            continue
        kept.append(line)
    text = "\n".join(kept)
    text = _HTML_COMMENT.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _LINK_TARGET.sub("] ", text)
    text = _URL.sub(" ", text)
    return text


def _normalize_token(raw: str) -> str:
    return _POSSESSIVE.sub("", raw)


def _tokenize_line(line: str) -> List[_Token]:
    """Word tokens of one line with sentence-initial marks."""
    tokens: List[_Token] = []
    for start, end in sentence_spans(line):
        first = True
        for match in _WORD.finditer(line, start, end):
            text = _normalize_token(match.group(0))
            if not text:
                continue
            tokens.append(_Token(text, match.start(), match.start() + len(text), first))
            first = False
    return tokens


def _is_term_token(text: str) -> bool:
    return bool(_CAPITALIZED.fullmatch(text) or _ACRONYM.fullmatch(text))


def _passes_stoplist(words: Sequence[str]) -> bool:
    if all(word in STOP_TOKENS for word in words):
        return False
    if words[0] in STOP_WORDS or words[-1] in STOP_WORDS:
        return False
    if len(words) == 1 and words[0] in HONORIFICS:
        return False
    return True


@dataclass
class _Candidate:
    words: Tuple[str, ...]
    mid: int = 0
    initial: int = 0
    # (line index, first token index, last token index) per occurrence
    spans: List[Tuple[int, int, int]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return self.mid + self.initial

    @property
    def source(self) -> str:
        return " ".join(self.words)


def _collect(
    lines: Sequence[str],
) -> Tuple[Dict[Tuple[str, ...], _Candidate], "Counter[str]"]:
    """Scan every line; return candidates keyed by word tuple and lowercase word counts."""
    candidates: Dict[Tuple[str, ...], _Candidate] = {}
    lowercase: Counter[str] = Counter()
    for line_no, line in enumerate(lines):
        tokens = _tokenize_line(line)
        for token in tokens:
            if _LOWER.fullmatch(token.text):
                lowercase[token.text] += 1
        n_tokens = len(tokens)
        for i, first in enumerate(tokens):
            if not _is_term_token(first.text):
                continue
            words: List[str] = [first.text]
            last_index = i
            prev = first
            j = i + 1
            # grow the n-gram while the next token (optionally after one joiner) qualifies
            while len(words) <= MAX_NGRAM:
                _record(candidates, words, line_no, i, last_index, first.sentence_initial)
                if len(words) == MAX_NGRAM or j >= n_tokens:
                    break
                nxt = tokens[j]
                if not _GAP.fullmatch(line, prev.end, nxt.start) or nxt.sentence_initial:
                    break
                if nxt.text in JOINERS and j + 1 < n_tokens:
                    after = tokens[j + 1]
                    if (
                        _GAP.fullmatch(line, nxt.end, after.start)
                        and not after.sentence_initial
                        and _is_term_token(after.text)
                    ):
                        words.extend([nxt.text, after.text])
                        last_index = j + 1
                        prev = after
                        j += 2
                        continue
                    break
                if not _is_term_token(nxt.text):
                    break
                words.append(nxt.text)
                last_index = j
                prev = nxt
                j += 1
    return candidates, lowercase


def _term_words(words: Sequence[str]) -> Tuple[str, ...]:
    return tuple(word for word in words if word not in JOINERS)


def _record(
    candidates: Dict[Tuple[str, ...], _Candidate],
    words: Sequence[str],
    line_no: int,
    first_index: int,
    last_index: int,
    sentence_initial: bool,
) -> None:
    term = _term_words(words)
    if not _passes_stoplist(term):
        return
    key = tuple(words)
    candidate = candidates.get(key)
    if candidate is None:
        candidate = _Candidate(key)
        candidates[key] = candidate
    if len(term) == 1 and sentence_initial:
        candidate.initial += 1
    else:
        candidate.mid += 1
    candidate.spans.append((line_no, first_index, last_index))


def _absorb(qualified: List[_Candidate]) -> List[_Candidate]:
    """Drop sub-grams that occur >= ABSORB_RATIO of the time inside longer survivors."""
    by_length = sorted(qualified, key=lambda c: (-len(c.words), c.source))
    covered: Dict[Tuple[int, int], List[Tuple[int, int]]] = defaultdict(list)
    survivors: List[_Candidate] = []
    for candidate in by_length:
        inside = 0
        for line_no, start, end in candidate.spans:
            for span_start, span_end in covered.get((line_no, start), ()):
                if span_start <= start and end <= span_end and (span_end - span_start) > (
                    end - start
                ):
                    inside += 1
                    break
        if candidate.count and inside / candidate.count >= ABSORB_RATIO:
            continue
        survivors.append(candidate)
        for line_no, start, end in candidate.spans:
            for index in range(start, end + 1):
                covered[(line_no, index)].append((start, end))
    return survivors


def discover_terms(markdown: str, *, min_count: int = DEFAULT_MIN_COUNT) -> List[GlossaryEntry]:
    """Discover glossary candidates in BT-Markdown ``markdown``."""
    if min_count < 1:
        min_count = 1
    text = strip_untranslatable(markdown)
    candidates, lowercase = _collect(text.split("\n"))
    qualified = [
        candidate
        for candidate in candidates.values()
        if candidate.mid >= 1 and candidate.count >= min_count
    ]
    survivors = _absorb(qualified)
    entries: List[GlossaryEntry] = []
    seen: Set[str] = set()
    for candidate in sorted(survivors, key=lambda c: (-c.count, c.source)):
        source = candidate.source
        if source in seen:
            continue
        seen.add(source)
        status = GlossaryStatus.PROPOSED
        if len(candidate.words) == 1 and lowercase[source.lower()] >= candidate.count:
            status = GlossaryStatus.AMBIGUOUS
        entries.append(
            GlossaryEntry(
                source=source,
                target="",
                count=candidate.count,
                status=status,
                origin="discovered",
                note="also a common word" if status is GlossaryStatus.AMBIGUOUS else "",
            )
        )
    return entries
