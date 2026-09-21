"""``glossary.json`` schema, validation, load/save (design doc 02, section 7.2).

The on-disk representation is a pydantic model (``GlossaryFile``); the rest of
the application works with the frozen domain ``GlossaryEntry``. Conversions in
both directions live here.

Validation rules (AC US-4/3):

* unknown ``version`` -> error;
* every entry needs a non-empty ``source`` of at most 200 characters and a
  ``status`` from the enum (``load_glossary_file`` can supply a default status
  for user files that omit it);
* ``approved`` requires a non-empty ``target``;
* ``target`` must not contain newlines;
* duplicate ``source`` (case-sensitive) within one file -> error naming the
  entry index and value.

All failures are ``Err(GLOSSARY_INVALID, USER)`` with
``context={"file": ..., "index": ..., "source": ...}`` (``index``/``source``
are ``None`` when the problem is not tied to one entry).
"""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

from ..domain.models import GlossaryEntry, GlossaryStatus
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err

GLOSSARY_FILE_VERSION = 1
GLOSSARY_FILE_NAME = "glossary.json"
MAX_SOURCE_LENGTH = 200

StatusValue = Literal["proposed", "approved", "rejected", "ambiguous"]
OriginValue = Literal["discovered", "user"]

_STATUS_TO_DOMAIN: Dict[str, GlossaryStatus] = {
    "proposed": GlossaryStatus.PROPOSED,
    "approved": GlossaryStatus.APPROVED,
    "rejected": GlossaryStatus.REJECTED,
    "ambiguous": GlossaryStatus.AMBIGUOUS,
}
_STATUS_FROM_DOMAIN: Dict[GlossaryStatus, StatusValue] = {
    GlossaryStatus.PROPOSED: "proposed",
    GlossaryStatus.APPROVED: "approved",
    GlossaryStatus.REJECTED: "rejected",
    GlossaryStatus.AMBIGUOUS: "ambiguous",
}


class GlossaryFileEntry(BaseModel):
    """One entry of ``glossary.json``. Field order is the on-disk key order."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)

    source: str
    target: str = ""
    count: int = Field(default=0, ge=0)
    # ``None`` only while loading a user file that omits the field; see
    # ``load_glossary_file(default_status=...)``. Never ``None`` after validation.
    status: Optional[StatusValue] = None
    origin: OriginValue = "discovered"
    note: str = ""

    @field_validator("source")
    @classmethod
    def _source_shape(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source must be a non-empty string")
        if len(stripped) > MAX_SOURCE_LENGTH:
            raise ValueError(f"source is longer than {MAX_SOURCE_LENGTH} characters")
        if "\n" in stripped or "\r" in stripped:
            raise ValueError("source must not contain newlines")
        return stripped

    @field_validator("target")
    @classmethod
    def _target_shape(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("target must not contain newlines")
        return value.strip()

    @model_validator(mode="after")
    def _approved_needs_target(self) -> "GlossaryFileEntry":
        if self.status == "approved" and not self.target:
            raise ValueError("an approved entry requires a non-empty target")
        return self

    def to_domain(self, *, origin: Optional[OriginValue] = None) -> GlossaryEntry:
        """Convert to the frozen domain entry (``status`` must be set)."""
        if self.status is None:
            raise ValueError("entry status is not set")
        return GlossaryEntry(
            source=self.source,
            target=self.target,
            count=self.count,
            status=_STATUS_TO_DOMAIN[self.status],
            origin=origin if origin is not None else self.origin,
            note=self.note,
        )

    @classmethod
    def from_domain(cls, entry: GlossaryEntry) -> "GlossaryFileEntry":
        return cls(
            source=entry.source,
            target=entry.target,
            count=entry.count,
            status=_STATUS_FROM_DOMAIN[entry.status],
            origin=entry.origin,
            note=entry.note,
        )


class GlossaryFile(BaseModel):
    """Top-level ``glossary.json`` document."""

    model_config = ConfigDict(extra="ignore")

    version: int
    source_lang: str = "en"
    target_lang: str = "tr"
    generated_at: str = ""
    entries: List[GlossaryFileEntry] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _known_version(cls, value: int) -> int:
        if value != GLOSSARY_FILE_VERSION:
            raise ValueError(
                f"unknown glossary file version {value} (expected {GLOSSARY_FILE_VERSION})"
            )
        return value

    @model_validator(mode="after")
    def _unique_sources(self) -> "GlossaryFile":
        seen: Dict[str, int] = {}
        for index, entry in enumerate(self.entries):
            first = seen.get(entry.source)
            if first is not None:
                raise PydanticCustomError(
                    "duplicate_source",
                    "duplicate source '{source}' at entry {index} (first seen at entry {first})",
                    {"source": entry.source, "index": index, "first": first},
                )
            seen[entry.source] = index
        return self

    def to_entries(self, *, origin: Optional[OriginValue] = None) -> List[GlossaryEntry]:
        return [entry.to_domain(origin=origin) for entry in self.entries]

    @classmethod
    def from_entries(
        cls,
        entries: Iterable[GlossaryEntry],
        *,
        source_lang: str = "en",
        target_lang: str = "tr",
        generated_at: Optional[str] = None,
    ) -> "GlossaryFile":
        return cls(
            version=GLOSSARY_FILE_VERSION,
            source_lang=source_lang,
            target_lang=target_lang,
            generated_at=generated_at if generated_at is not None else utc_timestamp(),
            entries=[GlossaryFileEntry.from_domain(entry) for entry in entries],
        )


def utc_timestamp() -> str:
    """``2026-09-17T10:00:00Z`` style timestamp (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _invalid(
    path: Path, message: str, *, index: Optional[int] = None, source: Optional[str] = None
) -> Err:
    return err(
        ErrorCode.GLOSSARY_INVALID,
        f"invalid glossary file {path.name}: {message}",
        ErrorScope.USER,
        context={"file": str(path), "index": index, "source": source},
    )


def _describe_validation_error(
    exc: ValidationError, raw: Any
) -> Tuple[str, Optional[int], Optional[str]]:
    """Turn the first pydantic error into ``(message, entry index, entry source)``."""
    first = exc.errors()[0]
    loc: Sequence[Any] = first.get("loc", ())
    ctx: Dict[str, Any] = dict(first.get("ctx") or {})
    message = str(first.get("msg", "validation error"))
    if message.startswith("Value error, "):
        message = message[len("Value error, ") :]
    if first.get("type") == "duplicate_source":
        return message, ctx.get("index"), ctx.get("source")
    index: Optional[int] = None
    source: Optional[str] = None
    if len(loc) >= 2 and loc[0] == "entries" and isinstance(loc[1], int):
        index = loc[1]
        field = ".".join(str(part) for part in loc[2:]) or "entry"
        message = f"entry {index} ({field}): {message}"
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if isinstance(entries, list) and index < len(entries) and isinstance(entries[index], dict):
            candidate = entries[index].get("source")
            source = candidate if isinstance(candidate, str) else None
    elif loc:
        message = f"{'.'.join(str(part) for part in loc)}: {message}"
    return message, index, source


def load_glossary_file(
    path: Path, *, default_status: Optional[StatusValue] = None
) -> Result[GlossaryFile]:
    """Read and validate ``path``.

    ``default_status`` fills a missing ``status`` (user files default to
    ``approved``); without it a missing status is an error.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _invalid(path, "file not found")
    except OSError as exc:
        return _invalid(path, f"cannot read file ({exc.__class__.__name__})")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return _invalid(path, f"not valid JSON ({exc})")
    if not isinstance(raw, dict):
        return _invalid(path, "top level must be a JSON object")
    entries = raw.get("entries")
    if default_status is not None and isinstance(entries, list):
        for item in entries:
            if isinstance(item, dict) and item.get("status") is None:
                item["status"] = default_status
    try:
        parsed = GlossaryFile.model_validate(raw)
    except ValidationError as exc:
        message, index, source = _describe_validation_error(exc, raw)
        return _invalid(path, message, index=index, source=source)
    for index, entry in enumerate(parsed.entries):
        if entry.status is None:
            return _invalid(
                path, f"entry {index} (status): field required", index=index, source=entry.source
            )
    return Ok(parsed)


def dump_glossary_json(file: GlossaryFile) -> str:
    """Serialize with a stable key order, indent 2, UTF-8 characters unescaped."""
    payload = file.model_dump(mode="json")
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def save_glossary_file(path: Path, file: GlossaryFile) -> Result[None]:
    """Write ``file`` atomically (temp file in the same directory + ``os.replace``)."""
    data = dump_glossary_json(file).encode("utf-8")
    tmp_name: Optional[str] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        return err(
            ErrorCode.INTERNAL,
            f"cannot write glossary file {path.name}: {exc.__class__.__name__}",
            ErrorScope.USER,
            context={"file": str(path)},
            cause=repr(exc),
        )
    finally:
        if tmp_name is not None:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
    return Ok(None)


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)
