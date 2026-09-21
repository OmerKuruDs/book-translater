"""Pure helpers for overlay batching (design doc 06, §4.6).

No I/O and no repository access: the adapter in ``units.py`` feeds these functions
with what it read from the ``OverlayBlockRepository``.
"""

from __future__ import annotations

from typing import Mapping, Sequence, Tuple

__all__ = [
    "DEFAULT_CONTEXT_CHARS",
    "DEFAULT_MAX_UNITS",
    "DEFAULT_MIN_CHARS",
    "apportion",
    "batch_pages",
    "context_window",
    "max_units_for",
]

DEFAULT_MAX_UNITS = 50
DEFAULT_MIN_CHARS = 1000
DEFAULT_CONTEXT_CHARS = 4000


def max_units_for(max_texts_per_request: int, unit_cap: int | None = None) -> int:
    """``min(50, provider max texts, --limit remainder)`` units per claim (§4.6)."""
    value = min(DEFAULT_MAX_UNITS, max(1, max_texts_per_request))
    if unit_cap is not None:
        value = min(value, max(0, unit_cap))
    return value


def batch_pages(pages: Sequence[int]) -> Tuple[int, ...]:
    """Distinct page numbers of a batch in ascending order."""
    return tuple(sorted(set(pages)))


def context_window(
    page_texts: Mapping[int, Sequence[str]],
    pages: Sequence[int],
    unit_texts: Sequence[str],
    limit: int = DEFAULT_CONTEXT_CHARS,
) -> str:
    """Page text of the batch pages, windowed to ``limit`` chars around the batch units.

    ``page_texts`` holds the translatable source texts of each page in reading order
    (``OverlayBlockRepository.page_texts``). The texts are joined by newlines in page
    order; when the result is longer than ``limit`` the window is centred on the span
    covered by the batch's own texts and cut at whitespace where possible (D14).
    """
    if limit <= 0:
        return ""
    parts: list[str] = []
    for page in batch_pages(pages):
        parts.extend(t for t in page_texts.get(page, ()) if t)
    joined = "\n".join(parts)
    if len(joined) <= limit:
        return joined
    first = -1
    last = -1
    for text in unit_texts:
        if not text:
            continue
        position = joined.find(text)
        if position < 0:
            continue
        first = position if first < 0 else min(first, position)
        last = max(last, position + len(text))
    if first < 0:
        first, last = 0, min(len(joined), limit)
    centre = (first + last) // 2
    start = max(0, min(centre - limit // 2, len(joined) - limit))
    end = min(len(joined), start + limit)
    # Cut at whitespace boundaries inside the window when they are near the edges.
    if start > 0:
        boundary = joined.find(" ", start, min(end, start + 80))
        if boundary >= 0:
            start = boundary + 1
    if end < len(joined):
        boundary = joined.rfind(" ", max(start, end - 80), end)
        if boundary > start:
            end = boundary
    return joined[start:end].strip()


def apportion(total: int, weights: Sequence[int]) -> Tuple[int, ...]:
    """Split ``total`` over ``weights`` proportionally (integers; remainder to the last
    positive weight). Used for the provider's batch-level ``chars_billed`` (§4.6)."""
    if not weights:
        return ()
    denominator = sum(max(0, w) for w in weights)
    if denominator <= 0 or total <= 0:
        return tuple(0 for _ in weights)
    shares = [total * max(0, w) // denominator for w in weights]
    remainder = total - sum(shares)
    for index in range(len(weights) - 1, -1, -1):
        if weights[index] > 0:
            shares[index] += remainder
            break
    return tuple(shares)
