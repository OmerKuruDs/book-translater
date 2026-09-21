"""Runtime settings (design doc 02, section 9.4).

Precedence: CLI flag > process environment > ``.env`` in the current directory
> defaults. ``DEEPL_API_KEY`` / ``DEEPL_SERVER_URL`` are read without the
``BOOK_TRANSLATOR_`` prefix; everything else carries it.

The DeepL key is a ``SecretStr``: it is never printed by ``repr`` and must never
be logged or persisted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import FrozenSet, List, Literal, Optional, Set, Tuple

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err

ProviderName = Literal["deepl", "local"]
ExtractorName = Literal["pymupdf", "marker"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
PdfEngine = Literal["native", "weasyprint"]

SOURCE_PAGE_SIZE = "source"
"""``--pdf-page-size`` / ``--pdf-margin`` value meaning "as in the source PDF" (Addendum A)."""


class Settings(BaseSettings):
    """Process-wide configuration; validated once before any stage runs."""

    model_config = SettingsConfigDict(
        env_prefix="BOOK_TRANSLATOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    deepl_api_key: Optional[SecretStr] = Field(default=None, validation_alias="DEEPL_API_KEY")
    deepl_server_url: Optional[str] = Field(default=None, validation_alias="DEEPL_SERVER_URL")

    provider: ProviderName = "deepl"
    extractor: ExtractorName = "pymupdf"
    concurrency: int = Field(default=4, ge=1, le=64)
    max_retries: int = Field(default=5, ge=0, le=50)
    chunk_min: int = Field(default=1000, ge=100)
    chunk_max: int = Field(default=1500, ge=200)
    backoff_base_s: float = Field(default=2.0, gt=0)
    backoff_cap_s: float = Field(default=60.0, gt=0)
    local_model_dir: Path = Path.home() / ".cache" / "book-translator" / "models"
    local_model: str = "opus-mt-tc-big-en-tr"
    local_device: Literal["cpu", "cuda"] = "cpu"
    log_level: LogLevel = "INFO"
    lease_stale_s: int = Field(default=60, ge=5)
    grace_s: int = Field(default=30, ge=0)
    # v1.1 F1 figure preservation (design doc 06, §7.1); ``BOOK_TRANSLATOR_FIGURE_*``
    figures: bool = True  # False == --no-figures (v1 behaviour)
    figure_dpi: int = Field(default=200, ge=72, le=600)
    figure_legend: bool = False  # Addendum A: render the legend list in outputs (off)
    figure_legend_limit: int = Field(default=40, ge=0)
    figure_exclude_pages: str = ""  # "2,5-7"
    figure_max_mb: float = Field(default=8.0, gt=0)
    figure_min_area_pct: float = Field(default=2.0, ge=0)
    figure_min_paths: int = Field(default=5, ge=1)
    figure_merge_gap_pt: float = Field(default=12.0, ge=0)
    figure_border_pt: float = Field(default=4.0, ge=0)
    # v1.1 F2 overlay PDF (design doc 06, §7.1; Addendum A); ``BOOK_TRANSLATOR_OVERLAY_*``
    overlay_min_scale: float = Field(default=0.65, gt=0, le=1.0)  # review threshold (Q13)
    overlay_floor_scale: float = Field(default=0.5, gt=0, le=1.0)  # hard shrink floor
    overlay_batch_min_chars: int = Field(default=1000, ge=0)  # §4.6 batch claim
    overlay_context_chars: int = Field(default=4000, ge=0)  # §4.6 page-text context window
    # v1.1 F3 native reflowed PDF (design doc 06, §7.1, D12; Addendum A): the page size,
    # margins and body font follow the *source* PDF unless the user chooses otherwise.
    pdf_engine: PdfEngine = "native"
    pdf_page_size: str = Field(default=SOURCE_PAGE_SIZE, min_length=1)
    pdf_margin: str = SOURCE_PAGE_SIZE  # "source" or 1, 2 or 4 CSS-style values (mm / pt)

    @field_validator("deepl_api_key")
    @classmethod
    def _key_shape(cls, value: Optional[SecretStr]) -> Optional[SecretStr]:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        if not raw or any(ch.isspace() for ch in raw):
            raise ValueError("DEEPL_API_KEY must be non-empty and contain no whitespace")
        return SecretStr(raw)

    def validate_for_provider(self) -> Result[None]:
        """Fail-fast checks that need no network (AC US-9/1, US-9/2)."""
        overlay = self.validate_overlay_scales()
        if isinstance(overlay, Err):
            return overlay
        if self.chunk_min > self.chunk_max:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                "BOOK_TRANSLATOR_CHUNK_MIN must be <= BOOK_TRANSLATOR_CHUNK_MAX",
                ErrorScope.USER,
            )
        if self.provider == "deepl" and self.deepl_api_key is None:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                "DEEPL_API_KEY is not set (environment variable or .env)",
                ErrorScope.USER,
            )
        return Ok(None)

    def validate_overlay_scales(self) -> Result[None]:
        """``--overlay-floor-scale`` must not exceed ``--overlay-min-scale`` (doc 06, 7.1)."""
        if self.overlay_floor_scale > self.overlay_min_scale:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                "--overlay-floor-scale must be <= --overlay-min-scale",
                ErrorScope.USER,
            )
        return Ok(None)


def load_settings(**overrides: object) -> Result[Settings]:
    """Build ``Settings`` from env/.env plus explicit overrides (CLI flags).

    ``None`` overrides are ignored so callers can pass typer options through verbatim.
    """
    values = {k: v for k, v in overrides.items() if v is not None}
    try:
        settings = Settings(**values)  # type: ignore[arg-type]
    except ValueError as exc:  # pydantic ValidationError is a ValueError subclass
        return err(
            ErrorCode.PROVIDER_CONFIG,
            f"invalid configuration: {describe_validation_error(exc)}",
            ErrorScope.USER,
        )
    return Ok(settings)


def describe_validation_error(exc: ValueError) -> str:
    """Field names and reasons only; the offending input (possibly a secret) is never echoed."""
    if isinstance(exc, ValidationError):
        parts: List[str] = []
        for item in exc.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in item["loc"]) or "settings"
            parts.append(f"{location}: {item['msg']}")
        if parts:
            return "; ".join(parts)
    return f"{type(exc).__name__} while reading the settings"


_PAGE_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def parse_page_ranges(text: str) -> Result[FrozenSet[int]]:
    """``"2,5-7"`` -> ``{2, 5, 6, 7}``; empty text -> empty set (``--figure-exclude-pages``)."""
    pages: Set[int] = set()
    for part in text.split(","):
        item = part.strip()
        if not item:
            continue
        match = _PAGE_RANGE_RE.match(item)
        if match is None:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                f"invalid page range {item!r}; use forms like \"2,5-7\"",
                ErrorScope.USER,
            )
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        if start < 1 or end < start:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                f"invalid page range {item!r}; pages start at 1 and ranges ascend",
                ErrorScope.USER,
            )
        if end - start > 100_000:
            return err(
                ErrorCode.PROVIDER_CONFIG, f"page range {item!r} is too wide", ErrorScope.USER
            )
        pages.update(range(start, end + 1))
    return Ok(frozenset(pages))


_MARGIN_VALUE_RE = re.compile(r"^(\d+(?:\.\d+)?)(mm|pt)?$", re.IGNORECASE)


def margins_from_source(text: str) -> bool:
    """``--pdf-margin source`` (case-insensitive) asks for the source margins."""
    return text.strip().lower() == SOURCE_PAGE_SIZE
_PT_TO_MM = 25.4 / 72.0


def parse_pdf_margin(text: str) -> Result[Tuple[float, float, float, float]]:
    """CSS-style margins -> ``(top, right, bottom, left)`` in millimetres (``--pdf-margin``).

    One value applies to all sides, two are ``vertical horizontal``, four are
    ``top right bottom left``. Each value is a non-negative number with ``mm``
    (default) or ``pt``. The value ``"source"`` (the default) is not parsed here: it means
    the margins of the source PDF (``source_profile.json``) and is resolved by the
    export stage; callers check :func:`margins_from_source` first.
    """
    parts = text.replace(",", " ").split()
    if len(parts) not in (1, 2, 4):
        return err(
            ErrorCode.PROVIDER_CONFIG,
            f"invalid --pdf-margin {text!r}: give 1, 2 or 4 values such as \"18mm 15mm 20mm 15mm\"",
            ErrorScope.USER,
        )
    values: List[float] = []
    for part in parts:
        match = _MARGIN_VALUE_RE.match(part)
        if match is None:
            return err(
                ErrorCode.PROVIDER_CONFIG,
                f"invalid --pdf-margin value {part!r}: use a number with mm or pt (e.g. 15mm)",
                ErrorScope.USER,
            )
        number = float(match.group(1))
        unit = (match.group(2) or "mm").lower()
        values.append(number * _PT_TO_MM if unit == "pt" else number)
    if len(values) == 1:
        values = values * 4
    elif len(values) == 2:
        values = [values[0], values[1], values[0], values[1]]
    return Ok((values[0], values[1], values[2], values[3]))
