"""Glossary stage orchestration helpers (design doc 02, section 7.3).

* ``build_or_update`` writes ``<output>/glossary.json`` (never overwrites without
  ``force``; with ``force`` it re-discovers and keeps ``target``/``status``/``note``
  of entries whose ``source`` already exists, plus entries that were not
  re-discovered, so hand-added terms survive).
* ``load_user_glossary`` loads ``--glossary my_terms.json`` (missing ``status``
  defaults to ``approved``; ``origin`` forced to ``user``).
* ``merge_with_precedence``: on an identical ``source`` the user entry wins (E-10).
* ``effective_glossary``: the entries a provider actually receives, with a
  stable hash.

Effective-glossary ordering (the hash depends on it): entries are filtered to
``status == APPROVED and target != ""``, ``source``/``target`` are NFC
normalized, duplicates by ``source`` keep the last occurrence, then the list is
sorted by ``(-len(source), source)`` — longest source first, ties broken by the
code-point order of ``source``. ``glossary_hash`` is
``sha256("\\n".join(f"{source}\\t{target}"))`` over that sorted list, so any
input order yields the same hash.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..domain.models import EffectiveGlossary, GlossaryEntry, GlossaryStatus
from ..domain.result import Err, Ok, Result
from .discovery import DEFAULT_MIN_COUNT, discover_terms
from .schema import (
    GLOSSARY_FILE_NAME,
    GlossaryFile,
    load_glossary_file,
    nfc,
    save_glossary_file,
)


def glossary_path(output_dir: Path) -> Path:
    return output_dir / GLOSSARY_FILE_NAME


def _merge_existing(
    discovered: Iterable[GlossaryEntry], existing: Iterable[GlossaryEntry]
) -> List[GlossaryEntry]:
    """Re-discovered counts with the user's ``target``/``status``/``note`` preserved."""
    old: Dict[str, GlossaryEntry] = {entry.source: entry for entry in existing}
    merged: List[GlossaryEntry] = []
    seen: set[str] = set()
    for entry in discovered:
        previous = old.get(entry.source)
        if previous is not None:
            entry = GlossaryEntry(
                source=entry.source,
                target=previous.target,
                count=entry.count,
                status=previous.status,
                origin=previous.origin,
                note=previous.note,
            )
        merged.append(entry)
        seen.add(entry.source)
    for source, previous in old.items():
        if source not in seen:
            merged.append(previous)
    return merged


def build_or_update(
    output_dir: Path,
    markdown: str,
    *,
    force: bool = False,
    min_count: int = DEFAULT_MIN_COUNT,
) -> Result[GlossaryFile]:
    """Discover terms from ``markdown`` and write ``glossary.json`` into ``output_dir``.

    An existing file is returned untouched unless ``force`` (AC US-13/2).
    """
    path = glossary_path(output_dir)
    existing: Optional[GlossaryFile] = None
    if path.exists():
        loaded = load_glossary_file(path)
        if isinstance(loaded, Err):
            return loaded
        if not force:
            return loaded
        existing = loaded.value
    discovered = discover_terms(markdown, min_count=min_count)
    entries = _merge_existing(discovered, existing.to_entries()) if existing else discovered
    file = GlossaryFile.from_entries(
        entries,
        source_lang=existing.source_lang if existing else "en",
        target_lang=existing.target_lang if existing else "tr",
    )
    saved = save_glossary_file(path, file)
    if isinstance(saved, Err):
        return saved
    return Ok(file)


def load_user_glossary(path: Path) -> Result[List[GlossaryEntry]]:
    """Load a user-supplied glossary; missing ``status`` -> approved, origin ``user``."""
    loaded = load_glossary_file(path, default_status="approved")
    if isinstance(loaded, Err):
        return loaded
    return Ok(loaded.value.to_entries(origin="user"))


def merge_with_precedence(
    discovered: Iterable[GlossaryEntry], user: Iterable[GlossaryEntry]
) -> List[GlossaryEntry]:
    """Union of both lists; on an identical ``source`` the user entry replaces the other."""
    merged: Dict[str, GlossaryEntry] = {}
    for entry in discovered:
        merged[entry.source] = entry
    for entry in user:
        merged[entry.source] = entry
    return list(merged.values())


def effective_glossary(entries: Iterable[GlossaryEntry]) -> EffectiveGlossary:
    """Approved entries with a target, NFC normalized, longest source first, hashed."""
    by_source: Dict[str, GlossaryEntry] = {}
    for entry in entries:
        if entry.status is not GlossaryStatus.APPROVED:
            continue
        source = nfc(entry.source.strip())
        target = nfc(entry.target.strip())
        if not source or not target:
            continue
        by_source[source] = GlossaryEntry(
            source=source,
            target=target,
            count=entry.count,
            status=entry.status,
            origin=entry.origin,
            note=entry.note,
        )
    ordered = sorted(by_source.values(), key=lambda e: (-len(e.source), e.source))
    digest = hashlib.sha256(
        "\n".join(f"{e.source}\t{e.target}" for e in ordered).encode("utf-8")
    ).hexdigest()
    return EffectiveGlossary(entries=tuple(ordered), glossary_hash=digest)
