"""Glossary discovery, schema, precedence, effective hash, post-check (design doc 02, 7 / 11)."""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any, Dict, List

import pytest

from book_translator.domain.models import EffectiveGlossary, GlossaryEntry, GlossaryStatus
from book_translator.domain.result import Err, ErrorCode, ErrorScope, Ok, unwrap
from book_translator.glossary._stoplist import HONORIFICS, STOP_WORDS
from book_translator.glossary.discovery import discover_terms, strip_untranslatable
from book_translator.glossary.manager import (
    build_or_update,
    effective_glossary,
    glossary_path,
    load_user_glossary,
    merge_with_precedence,
)
from book_translator.glossary.schema import (
    GLOSSARY_FILE_VERSION,
    GlossaryFile,
    GlossaryFileEntry,
    load_glossary_file,
    save_glossary_file,
)
from book_translator.pipeline.glossary_check import check_chunk, target_stem, turkish_casefold

# ---------------------------------------------------------------------------
# helpers


def entry(
    source: str,
    target: str = "",
    *,
    status: GlossaryStatus = GlossaryStatus.APPROVED,
    origin: str = "discovered",
    count: int = 1,
) -> GlossaryEntry:
    return GlossaryEntry(
        source=source,
        target=target,
        count=count,
        status=status,
        origin="user" if origin == "user" else "discovered",
    )


def effective(*pairs: tuple[str, str]) -> EffectiveGlossary:
    return effective_glossary([entry(source, target) for source, target in pairs])


def by_source(entries: List[GlossaryEntry]) -> Dict[str, GlossaryEntry]:
    return {e.source: e for e in entries}


def write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def file_payload(
    entries: List[Dict[str, Any]], version: int = GLOSSARY_FILE_VERSION
) -> Dict[str, Any]:
    return {
        "version": version,
        "source_lang": "en",
        "target_lang": "tr",
        "generated_at": "2026-09-17T10:00:00Z",
        "entries": entries,
    }


VALLEY_ENTRY = {"source": "Valley", "target": "Vadi", "count": 0, "status": "approved"}

DRAGON_TEXT = (
    "# The Dragon Book\n\n"
    "The Dragon flew over the valley. However, the Dragon was tired. Then the Dragon slept.\n\n"
    "Suddenly the Dragon woke up and the Dragon roared. The valley echoed.\n"
)


# ---------------------------------------------------------------------------
# discovery


def test_stoplist_is_reasonably_sized_and_has_no_names() -> None:
    assert len(STOP_WORDS) >= 350
    assert {"The", "However", "Chapter", "January", "Monday", "Figure"} <= STOP_WORDS
    assert {"Mr", "Dr", "Duke"} <= HONORIFICS
    assert not {"Will", "May", "Mark"} & STOP_WORDS


def test_discovery_applies_stoplist() -> None:
    entries = discover_terms(DRAGON_TEXT, min_count=3)
    sources = [e.source for e in entries]
    assert "Dragon" in sources
    for noise in ("The", "However", "Then", "Suddenly", "The Dragon", "Dragon Book", "Book"):
        assert noise not in sources
    dragon = by_source(entries)["Dragon"]
    assert dragon.count == 6
    assert dragon.status is GlossaryStatus.PROPOSED
    assert dragon.target == ""
    assert dragon.origin == "discovered"


def test_discovery_honors_min_count() -> None:
    text = "We met Zorblax today. Later Zorblax left; nobody saw Zorblax again.\n"
    assert [e.source for e in discover_terms(text, min_count=3)] == ["Zorblax"]
    assert discover_terms(text, min_count=4) == []
    text_two = "Kalidor arrived. Then Kalidor spoke.\n"
    assert discover_terms(text_two, min_count=3) == []
    assert [e.source for e in discover_terms(text_two, min_count=2)] == ["Kalidor"]


def test_sentence_initial_only_words_are_excluded() -> None:
    text = (
        "Bananas are yellow. Bananas grow on trees. Bananas taste sweet. Bananas rot fast.\n"
        "Zeppelins fly. The Zeppelins are old. Zeppelins are slow. Two Zeppelins crashed.\n"
    )
    sources = {e.source: e for e in discover_terms(text, min_count=3)}
    assert "Bananas" not in sources  # only ever sentence-initial
    assert "Zeppelins" in sources  # mid-sentence at least once; initial counts add up
    assert sources["Zeppelins"].count == 4


def test_ambiguity_flag_for_common_words() -> None:
    text = (
        "Will opened the door. She looked at Will and smiled at Will again.\n"
        "I will go, you will stay, and they will wait; nobody will argue.\n"
        "Dragon roared and the Dragon flew; every Dragon sleeps.\n"
    )
    entries = by_source(discover_terms(text, min_count=3))
    assert entries["Will"].status is GlossaryStatus.AMBIGUOUS
    assert entries["Will"].note == "also a common word"
    assert entries["Dragon"].status is GlossaryStatus.PROPOSED


def test_ngram_absorption_new_york_absorbs_york() -> None:
    text = (
        "We flew to New York. In New York it rained; New York never sleeps. "
        "Everyone loves New York, and New York loves everyone.\n"
    )
    sources = [e.source for e in discover_terms(text, min_count=3)]
    assert "New York" in sources
    assert "York" not in sources
    assert "New" not in sources


def test_ngram_absorption_keeps_subgram_below_ratio() -> None:
    text = (
        "The Duke of York left. Later York burned. In York we stayed. York is old. "
        "York has walls. New York is far, and New York is loud; New York is big.\n"
    )
    sources = {e.source: e.count for e in discover_terms(text, min_count=3)}
    assert "New York" in sources
    assert "York" in sources  # 5 of 8 occurrences are outside "New York"


def test_joiner_ngrams_and_honorifics() -> None:
    text = (
        "The Duke of York rode out. The Duke of York was late. Nobody liked the Duke of York.\n"
        "Mr Darcy bowed. Everyone watched Mr Darcy; even Mr Darcy noticed. Mr left. Mr sat.\n"
    )
    sources = {e.source for e in discover_terms(text, min_count=3)}
    assert "Duke of York" in sources
    assert "Mr Darcy" in sources
    assert "Mr" not in sources


def test_discovery_sorted_by_count_desc_and_ignores_code_and_urls() -> None:
    text = (
        "Alpha met Beta. Alpha saw Beta twice; Alpha and Beta and Alpha.\n"
        "```\nGamma Gamma Gamma Gamma\n```\n"
        "See `Delta Delta Delta` and https://Epsilon.example/Epsilon/Epsilon page.\n"
        "<!-- image:3 -->\n"
    )
    entries = discover_terms(text, min_count=3)
    counts = [e.count for e in entries]
    assert counts == sorted(counts, reverse=True)
    sources = [e.source for e in entries]
    assert sources[0] == "Alpha"
    assert "Gamma" not in sources and "Delta" not in sources and "Epsilon" not in sources
    assert "Gamma" not in strip_untranslatable(text)


# ---------------------------------------------------------------------------
# schema


def test_schema_roundtrip_and_stable_key_order(tmp_path: Path) -> None:
    file = GlossaryFile.from_entries(
        [entry("Dragon", "Ejderha", count=42), entry("Will", status=GlossaryStatus.AMBIGUOUS)],
        generated_at="2026-09-17T10:00:00Z",
    )
    path = tmp_path / "glossary.json"
    assert isinstance(save_glossary_file(path, file), Ok)
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert "Ejderha".encode("utf-8") in raw  # ensure_ascii=False
    text = raw.decode("utf-8")
    assert list(json.loads(text)) == [
        "version", "source_lang", "target_lang", "generated_at", "entries"
    ]
    assert list(json.loads(text)["entries"][0]) == [
        "source", "target", "count", "status", "origin", "note"
    ]
    loaded = unwrap(load_glossary_file(path))
    assert loaded == file
    assert loaded.to_entries()[0] == entry("Dragon", "Ejderha", count=42)


def test_schema_unknown_version(tmp_path: Path) -> None:
    path = write_json(tmp_path / "g.json", file_payload([], version=99))
    result = load_glossary_file(path)
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.GLOSSARY_INVALID
    assert result.error.scope is ErrorScope.USER
    assert "version" in result.error.message
    assert result.error.context == {"file": str(path), "index": None, "source": None}


def test_schema_invalid_json_and_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    result = load_glossary_file(path)
    assert isinstance(result, Err) and result.error.code is ErrorCode.GLOSSARY_INVALID
    assert "JSON" in result.error.message
    missing = load_glossary_file(tmp_path / "nope.json")
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.GLOSSARY_INVALID


def test_schema_approved_requires_target_names_entry(tmp_path: Path) -> None:
    payload = file_payload(
        [
            {"source": "Dragon", "target": "Ejderha", "status": "approved"},
            {"source": "Sword", "target": "", "status": "approved"},
        ]
    )
    result = load_glossary_file(write_json(tmp_path / "g.json", payload))
    assert isinstance(result, Err)
    assert result.error.context["index"] == 1
    assert result.error.context["source"] == "Sword"
    assert "entry 1" in result.error.message and "target" in result.error.message


@pytest.mark.parametrize(
    "bad, fragment",
    [
        ({"source": "", "status": "proposed"}, "source"),
        ({"source": "x" * 201, "status": "proposed"}, "200"),
        ({"source": "Dragon", "status": "maybe"}, "status"),
        ({"source": "Dragon", "target": "Ejder\nha", "status": "proposed"}, "newline"),
        ({"source": "Dragon", "target": "Ejderha"}, "status"),
    ],
)
def test_schema_entry_rules(tmp_path: Path, bad: Dict[str, Any], fragment: str) -> None:
    payload = file_payload([{"source": "Ok", "target": "Tamam", "status": "approved"}, bad])
    result = load_glossary_file(write_json(tmp_path / "g.json", payload))
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.GLOSSARY_INVALID
    assert result.error.context["index"] == 1
    assert fragment.lower() in result.error.message.lower()


def test_schema_duplicate_source_names_index_and_value(tmp_path: Path) -> None:
    payload = file_payload(
        [
            {"source": "Dragon", "target": "Ejderha", "status": "approved"},
            {"source": "dragon", "target": "ejderha", "status": "approved"},  # different case: ok
            {"source": "Dragon", "target": "Kertenkele", "status": "approved"},
        ]
    )
    result = load_glossary_file(write_json(tmp_path / "g.json", payload))
    assert isinstance(result, Err)
    expected_context = {"file": str(tmp_path / "g.json"), "index": 2, "source": "Dragon"}
    assert result.error.context == expected_context
    assert "'Dragon'" in result.error.message and "entry 2" in result.error.message
    with pytest.raises(ValueError):
        GlossaryFile(
            version=1,
            entries=[
                GlossaryFileEntry(source="A", status="proposed"),
                GlossaryFileEntry(source="A", status="proposed"),
            ],
        )


# ---------------------------------------------------------------------------
# precedence, user file, effective glossary


def test_user_file_missing_status_defaults_to_approved(tmp_path: Path) -> None:
    payload = file_payload(
        [
            {"source": "Dragon", "target": "Ejderha"},
            {"source": "Will", "target": "", "status": "ambiguous", "origin": "discovered"},
        ]
    )
    entries = unwrap(load_user_glossary(write_json(tmp_path / "user.json", payload)))
    assert entries[0].status is GlossaryStatus.APPROVED
    assert all(e.origin == "user" for e in entries)
    assert entries[1].status is GlossaryStatus.AMBIGUOUS


def test_user_entry_wins_on_identical_source() -> None:
    discovered = [
        entry("Dragon", "Kertenkele", status=GlossaryStatus.PROPOSED, count=42),
        entry("Sword", "Kılıç", count=7),
    ]
    user = [entry("Dragon", "Ejderha", origin="user"), entry("Castle", "Kale", origin="user")]
    merged = by_source(merge_with_precedence(discovered, user))
    assert merged["Dragon"] == user[0]
    assert merged["Sword"] == discovered[1]
    assert merged["Castle"].origin == "user"
    assert len(merged) == 3


def test_effective_glossary_filters_sorts_and_hashes_stably() -> None:
    base = [
        entry("Dragon", "Ejderha"),
        entry("Red Dragon", "Kızıl Ejderha"),
        entry("Sword", "Kılıç"),
        entry("Will", "", status=GlossaryStatus.AMBIGUOUS),
        entry("Castle", "", status=GlossaryStatus.APPROVED),  # approved without target -> dropped
        entry("Knight", "Şövalye", status=GlossaryStatus.REJECTED),
    ]
    first = effective_glossary(base)
    assert [e.source for e in first.entries] == ["Red Dragon", "Dragon", "Sword"]
    shuffled = list(base)
    random.Random(7).shuffle(shuffled)
    for _ in range(5):
        random.shuffle(shuffled)
        again = effective_glossary(shuffled)
        assert again.glossary_hash == first.glossary_hash
        assert again.entries == first.entries
    assert len(first.glossary_hash) == 64
    changed = effective_glossary([*base, entry("Tower", "Kule")])
    assert changed.glossary_hash != first.glossary_hash


def test_effective_glossary_nfc_normalizes() -> None:
    decomposed = "Ejderha" + "ğ"  # g + combining breve
    composed = "Ejderhağ"  # ğ
    a = effective_glossary([entry("Dragon", decomposed)])
    b = effective_glossary([entry("Dragon", composed)])
    assert a.glossary_hash == b.glossary_hash
    assert a.entries[0].target == composed


# ---------------------------------------------------------------------------
# build_or_update


def test_build_writes_once_and_refuses_overwrite_without_force(tmp_path: Path) -> None:
    file = unwrap(build_or_update(tmp_path, DRAGON_TEXT, force=False, min_count=3))
    path = glossary_path(tmp_path)
    assert path.exists()
    assert [e.source for e in file.entries] == ["Dragon"]
    edited = json.loads(path.read_text(encoding="utf-8"))
    edited["entries"][0]["target"] = "Ejderha"
    edited["entries"][0]["status"] = "approved"
    edited["entries"].append(VALLEY_ENTRY)
    write_json(path, edited)
    more = DRAGON_TEXT + "Kalidor. Kalidor. Kalidor.\n"
    again = unwrap(build_or_update(tmp_path, more, force=False))
    assert [e.source for e in again.entries] == ["Dragon", "Valley"]
    assert json.loads(path.read_text(encoding="utf-8")) == edited


def test_build_with_force_merges_preserving_user_edits(tmp_path: Path) -> None:
    unwrap(build_or_update(tmp_path, DRAGON_TEXT, force=False))
    path = glossary_path(tmp_path)
    edited = json.loads(path.read_text(encoding="utf-8"))
    edited["entries"][0].update({"target": "Ejderha", "status": "approved", "note": "checked"})
    edited["entries"].append(VALLEY_ENTRY)
    write_json(path, edited)
    more = DRAGON_TEXT + "Kalidor came. Then Kalidor fought the Dragon and Kalidor won.\n"
    merged = unwrap(build_or_update(tmp_path, more, force=True))
    entries = {e.source: e for e in merged.entries}
    assert entries["Dragon"].target == "Ejderha"
    assert entries["Dragon"].status == "approved"
    assert entries["Dragon"].note == "checked"
    assert entries["Dragon"].count == 7  # re-discovered count
    assert entries["Valley"].target == "Vadi"  # hand-added entry survives
    assert "Kalidor" in entries and entries["Kalidor"].status == "proposed"
    on_disk = unwrap(load_glossary_file(path))
    assert on_disk == merged


def test_build_refuses_when_existing_file_is_invalid(tmp_path: Path) -> None:
    write_json(glossary_path(tmp_path), file_payload([], version=5))
    result = build_or_update(tmp_path, DRAGON_TEXT, force=True)
    assert isinstance(result, Err) and result.error.code is ErrorCode.GLOSSARY_INVALID


def test_save_is_atomic_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "glossary.json"
    original = GlossaryFile.from_entries([entry("Dragon", "Ejderha")], generated_at="t0")
    assert isinstance(save_glossary_file(path, original), Ok)
    before = path.read_bytes()

    def boom(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    result = save_glossary_file(path, GlossaryFile.from_entries([entry("Sword", "Kılıç")]))
    assert isinstance(result, Err)
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["glossary.json"]


# ---------------------------------------------------------------------------
# post-check (7.6)


def test_turkish_casefold_and_stem() -> None:
    assert turkish_casefold("İSTANBUL") == "istanbul"
    assert turkish_casefold("ISPARTA") == "ısparta"
    assert target_stem("Ejderha") == "Ejder"
    assert target_stem("Kale") == "Kale"
    assert target_stem("Ok") == "Ok"


def test_post_check_tolerates_suffixes_with_turkish_folding() -> None:
    glossary = effective(("Dragon", "Ejderha"), ("Istanbul", "İstanbul"), ("Sword", "Kılıç"))
    warnings, flag = check_chunk(
        "The Dragon of Istanbul held a Sword.",
        "istanbul'un Ejderhanın kılıcı vardı.",
        glossary,
        strict=True,
    )
    assert warnings == []
    assert flag is False


def test_post_check_reports_misses_and_strict_flag() -> None:
    glossary = effective(("Dragon", "Ejderha"), ("Castle", "Kale"))
    warnings, flag = check_chunk("The Dragon slept.", "Kertenkele uyudu.", glossary, strict=False)
    assert warnings == ["glossary_miss:Dragon"]
    assert flag is False
    warnings, flag = check_chunk("The Dragon slept.", "Kertenkele uyudu.", glossary, strict=True)
    assert warnings == ["glossary_miss:Dragon"]
    assert flag is True


def test_post_check_uses_word_boundaries() -> None:
    glossary = effective(("Art", "Sanat"))
    warnings, _ = check_chunk("Arthur painted.", "Arthur resim yaptı.", glossary, strict=True)
    assert warnings == []


def test_post_check_structure_markers_flag_regardless_of_strict() -> None:
    glossary = effective(("Dragon", "Ejderha"))
    warnings, flag = check_chunk(
        "The **Dragon** is *bold* with `code`[^1].",
        "Ejderha *kalın* ve `kod`[^1].",
        glossary,
        strict=False,
    )
    assert warnings == ["structure_mismatch:**"]
    assert flag is True
    warnings, flag = check_chunk("a *b* c", "a b c", EffectiveGlossary((), "0" * 64), strict=False)
    assert warnings == ["structure_mismatch:*"]
    assert flag is True


# ---------------------------------------------------------------------------
# v1.1 figures (design doc 06, 3.3 / FR-33): legend blocks and image lines are not candidates


def test_discovery_skips_legend_blocks_and_image_lines() -> None:
    text = (
        "Alpha met Beta. Alpha saw Beta twice; Alpha and Beta and Alpha.\n"
        '<!-- image:3 src="images/p002-f01.png" -->\n'
        "Figure 3: Gamma Delta appears here.\n"
        '<!-- legend:3 more="1" -->\n- Gamma Delta\n- Gamma Delta Node\n- Gamma\n\n'
        "Gamma stays in prose. Gamma again, and Gamma once more.\n"
        "<!-- legend:9 -->\nOrphan Zeta Zeta Zeta line.\n\nZeta Zeta Zeta.\n"
    )
    stripped = strip_untranslatable(text)
    assert stripped.count("\n") == text.count("\n")  # line positions are kept
    assert "Node" not in stripped and "images/" not in stripped and "legend" not in stripped
    assert "Gamma stays in prose" in stripped
    assert "Orphan" not in stripped and "Zeta Zeta Zeta." in stripped
    entries = {e.source: e.count for e in discover_terms(text, min_count=3)}
    assert "Gamma Delta" not in entries and "Node" not in entries
    assert entries["Gamma"] >= 3 and entries["Alpha"] >= 3
    assert entries.get("Zeta") == 3  # the orphan-marker line (3 more) is excluded
