"""``source_profile.json`` schema and I/O (design doc 06, Addendum A).

Written by the ``extract`` stage next to ``figures.json``; read by the ``export`` stage
so the reflow PDF reuses the page size, body font size and margins of the source
unless the user gave ``--pdf-page-size`` / ``--pdf-margin`` explicitly. Version 1.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict, ValidationError

from ..domain.models import SourceProfile
from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err

__all__ = [
    "SOURCE_PROFILE_FILE_NAME",
    "SOURCE_PROFILE_VERSION",
    "SourceProfileFile",
    "load_source_profile",
    "save_source_profile",
]

SOURCE_PROFILE_FILE_NAME = "source_profile.json"
SOURCE_PROFILE_VERSION = 1


class SourceProfileFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = SOURCE_PROFILE_VERSION
    page_width_pt: float
    page_height_pt: float
    body_font_size_pt: float
    margins_pt: Tuple[float, float, float, float]  # top, right, bottom, left
    serif: bool = True
    pages_measured: int = 0
    extractor: Optional[str] = None

    def to_profile(self) -> SourceProfile:
        return SourceProfile(
            page_width_pt=self.page_width_pt,
            page_height_pt=self.page_height_pt,
            body_font_size_pt=self.body_font_size_pt,
            margins_pt=self.margins_pt,
            serif=self.serif,
            pages_measured=self.pages_measured,
        )

    @classmethod
    def from_profile(cls, profile: SourceProfile, extractor: Optional[str]) -> "SourceProfileFile":
        return cls(
            page_width_pt=profile.page_width_pt,
            page_height_pt=profile.page_height_pt,
            body_font_size_pt=profile.body_font_size_pt,
            margins_pt=profile.margins_pt,
            serif=profile.serif,
            pages_measured=profile.pages_measured,
            extractor=extractor,
        )


def load_source_profile(path: Path) -> Result[SourceProfile]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return err(
            ErrorCode.INPUT_UNREADABLE,
            f"cannot read {path.name}: {exc.__class__.__name__}",
            ErrorScope.USER,
            cause=repr(exc),
        )
    try:
        data = SourceProfileFile.model_validate_json(raw)
    except ValidationError as exc:
        return err(
            ErrorCode.STATE_CORRUPT,
            f"{path.name} is not a valid source profile: "
            f"{'; '.join(str(e['msg']) for e in exc.errors()[:3])}",
            ErrorScope.USER,
        )
    except ValueError as exc:
        return err(
            ErrorCode.STATE_CORRUPT,
            f"{path.name} is not valid JSON: {type(exc).__name__}",
            ErrorScope.USER,
            cause=repr(exc)[:200],
        )
    if data.version != SOURCE_PROFILE_VERSION:
        return err(
            ErrorCode.STATE_CORRUPT,
            f"{path.name} has unsupported version {data.version} "
            f"(expected {SOURCE_PROFILE_VERSION})",
            ErrorScope.USER,
        )
    if data.page_width_pt <= 0 or data.page_height_pt <= 0:
        return err(
            ErrorCode.STATE_CORRUPT, f"{path.name} has a non-positive page size", ErrorScope.USER
        )
    return Ok(data.to_profile())


def save_source_profile(
    path: Path, profile: SourceProfile, extractor: Optional[str] = None
) -> Result[int]:
    """Atomic UTF-8 write (temp file + ``os.replace``); returns bytes written."""
    data = SourceProfileFile.from_profile(profile, extractor)
    payload = json.dumps(data.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    encoded = payload.encode("utf-8")
    tmp_name: Optional[str] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    except OSError as exc:
        return err(
            ErrorCode.EXPORT_FAILED,
            f"cannot write {path.name}: {exc.__class__.__name__}: {exc.strerror or exc}",
            ErrorScope.JOB_FATAL,
            context={"path": str(path)},
            cause=repr(exc),
        )
    finally:
        if tmp_name is not None:
            try:
                os.remove(tmp_name)
            except OSError:
                pass
    return Ok(len(encoded))
