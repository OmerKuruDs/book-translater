"""Post-translation glossary and structure verification (design doc 02, section 7.6).

Provider-independent: for every effective glossary entry whose ``source``
occurs in the chunk source (word boundary, case-sensitive), the translation
must contain the ``target`` *stem* — its first ``max(4, len(target) - 2)``
characters — compared after Turkish-aware case folding (``I -> ı``, ``İ -> i``,
then ``lower()``), which tolerates agglutinative suffixes (``Ejderha`` in
``Ejderhanın``). A miss yields ``glossary_miss:<source>``; ``review_flag`` is
raised for misses only under ``strict``.

The marker-count check (E-17) compares ``**``, ``*``, `````, ``[^`` counts
between source and translation; a mismatch yields ``structure_mismatch:<marker>``
and always raises ``review_flag``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from ..domain.models import EffectiveGlossary

MARKERS: Tuple[str, ...] = ("**", "*", "`", "[^")


def turkish_casefold(text: str) -> str:
    """Lowercase with Turkish dotted/dotless I semantics."""
    return text.replace("I", "ı").replace("İ", "i").lower()


def target_stem(target: str) -> str:
    """First ``max(4, len(target) - 2)`` characters of ``target``."""
    return target[: max(4, len(target) - 2)]


def _source_present(source: str, text: str) -> bool:
    pattern = r"(?<!\w)" + re.escape(source) + r"(?!\w)"
    return re.search(pattern, text) is not None


def count_markers(text: str) -> Dict[str, int]:
    """Occurrences of each marker; single ``*`` excludes the stars inside ``**``."""
    double = text.count("**")
    return {
        "**": double,
        "*": text.count("*") - 2 * double,
        "`": text.count("`"),
        "[^": text.count("[^"),
    }


def check_chunk(
    source_text: str,
    translated_text: str,
    glossary: EffectiveGlossary,
    *,
    strict: bool,
) -> Tuple[List[str], bool]:
    """Return ``(warnings, review_flag)`` for one translated chunk."""
    warnings: List[str] = []
    review = False
    folded_translation = turkish_casefold(translated_text)
    for entry in glossary.entries:
        if not entry.target or not _source_present(entry.source, source_text):
            continue
        stem = turkish_casefold(target_stem(entry.target))
        if stem not in folded_translation:
            warnings.append(f"glossary_miss:{entry.source}")
            if strict:
                review = True
    source_counts = count_markers(source_text)
    translated_counts = count_markers(translated_text)
    for marker in MARKERS:
        if source_counts[marker] != translated_counts[marker]:
            warnings.append(f"structure_mismatch:{marker}")
            review = True
    return warnings, review
