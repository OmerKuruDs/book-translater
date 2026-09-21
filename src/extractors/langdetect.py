"""Stopword-ratio language detection (design doc 02, section 6.3).

No external dependency: the first 20 000 word tokens of the text are matched
against per-language stopword lists; the language with the highest hit ratio
wins. Good enough for "is this book English?".
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err
from ._stopwords import LANGUAGE_ORDER, STOPWORDS

MAX_TOKENS = 20_000
ENGLISH_MIN_RATIO = 0.15

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def language_ratios(text: str) -> Dict[str, float]:
    """Stopword hit ratio per language over the first ``MAX_TOKENS`` word tokens."""
    tokens = [m.group(0).lower() for m in _TOKEN_RE.finditer(text)][:MAX_TOKENS]
    if not tokens:
        return {lang: 0.0 for lang in LANGUAGE_ORDER}
    total = len(tokens)
    ratios: Dict[str, float] = {}
    for lang in LANGUAGE_ORDER:
        words = STOPWORDS[lang]
        hits = sum(1 for t in tokens if t in words)
        ratios[lang] = hits / total
    return ratios


def detect_language(text: str) -> Tuple[Optional[str], float]:
    """Return ``(language_code, ratio)`` of the winning language, or ``(None, 0.0)``.

    Ties are broken deterministically by ``LANGUAGE_ORDER`` (English first).
    """
    ratios = language_ratios(text)
    best_lang: Optional[str] = None
    best_ratio = 0.0
    for lang in LANGUAGE_ORDER:
        ratio = ratios[lang]
        if ratio > best_ratio:
            best_lang = lang
            best_ratio = ratio
    if best_lang is None:
        return None, 0.0
    return best_lang, round(best_ratio, 4)


def check_english(
    text: str, allow_non_english: bool
) -> Result[Tuple[Optional[str], float]]:
    """Apply the 6.3 decision rule: winner must be ``en`` with ratio >= 0.15.

    Otherwise ``Err(NON_ENGLISH_SOURCE, USER)`` unless ``allow_non_english`` is set,
    in which case the detection result is returned and the caller stores a warning.
    """
    ratios = language_ratios(text)
    detected, confidence = detect_language(text)
    english_ratio = ratios.get("en", 0.0)
    is_english = detected == "en" and english_ratio >= ENGLISH_MIN_RATIO
    if is_english or allow_non_english:
        return Ok((detected, confidence))
    shown = detected if detected is not None else "unknown"
    return err(
        ErrorCode.NON_ENGLISH_SOURCE,
        (
            f"source text does not look English (detected: {shown}, confidence "
            f"{confidence:.2f}, english ratio {english_ratio:.2f}); pass "
            "--allow-non-english to continue anyway"
        ),
        ErrorScope.USER,
        context={"detected_language": shown, "confidence": confidence},
    )
