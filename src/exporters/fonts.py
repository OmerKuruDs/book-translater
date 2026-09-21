"""Font resolution for the native PDF engine (design doc 06, 3.5, D9; FR-53, E-55).

Without ``--pdf-font`` the generic CSS families are used and MuPDF supplies its
built-in Charis SIL (serif), Nimbus Sans and Nimbus Mono PS (evidence E1: all of
them cover the Turkish alphabet). A user font file is validated *before any
rendering*: it must be a TTF/OTF that pymupdf can open and it must have a glyph
for every character of :data:`TURKISH_PROBE`; otherwise ``FONT_UNSUPPORTED``
(USER scope, exit 1) is returned with the bundled default suggested.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from ..pdfkit import api as pdfkit

TURKISH_PROBE = "çğıİöşüÇĞİÖŞÜ"
"""Characters every PDF font must cover (NFR-25)."""

USER_FONT_FAMILY = "BookFont"
"""CSS family name declared by ``@font-face`` for a ``--pdf-font`` file."""

FONT_SUFFIXES = frozenset({".ttf", ".otf"})

_DEFAULT_HINT = "omit --pdf-font to use the built-in Charis SIL / Nimbus fonts"


@dataclass(frozen=True)
class FontSpec:
    """How the engine names and locates the body font."""

    family_css: str
    """``'serif'`` (built-in) or ``'BookFont'`` (user file)."""
    archive_dir: Optional[Path]
    """Directory added to the ``pymupdf.Archive`` so ``@font-face`` ``url()`` resolves."""
    face_css: str
    """``""`` or the ``@font-face`` rule for the user font."""
    family_name: str = ""
    """Face name reported by pymupdf (informational, e.g. ``"Arial Regular"``)."""

    @property
    def is_builtin(self) -> bool:
        return self.archive_dir is None


BUILTIN_FONT = FontSpec(family_css="serif", archive_dir=None, face_css="")


def _unsupported(message: str, font_path: Path, missing: Optional[str] = None) -> Result[None]:
    context = {"font": str(font_path)}
    if missing:
        context["missing_glyphs"] = missing
    return err(
        ErrorCode.FONT_UNSUPPORTED,
        f"--pdf-font {font_path.name}: {message}; {_DEFAULT_HINT}",
        ErrorScope.USER,
        context=context,
    )


def validate_turkish_coverage(font_path: Path) -> Result[None]:
    """Reject a font that is not a readable TTF/OTF or lacks a Turkish glyph (FR-53)."""
    if not font_path.is_file():
        return _unsupported("file not found", font_path)
    if font_path.suffix.lower() not in FONT_SUFFIXES:
        return _unsupported(
            f"unsupported format {font_path.suffix or '(none)'!r}; use a .ttf or .otf file",
            font_path,
        )
    try:
        font = pdfkit.font_from_file(str(font_path))
    except Exception as exc:  # noqa: BLE001 - pymupdf raises its own hierarchy
        return _unsupported(f"not a usable font file ({exc.__class__.__name__})", font_path)
    missing: List[str] = []
    try:
        for char in TURKISH_PROBE:
            if not pdfkit.font_has_glyph(font, char):
                missing.append(char)
    except Exception as exc:  # noqa: BLE001
        return _unsupported(f"glyph lookup failed ({exc.__class__.__name__})", font_path)
    if missing:
        joined = "".join(missing)
        return _unsupported(f"missing Turkish glyphs {joined!r}", font_path, joined)
    return Ok(None)


def _css_url(name: str) -> str:
    return name.replace("\\", "\\\\").replace('"', '\\"')


def resolve_font(font_path: Optional[Path]) -> Result[FontSpec]:
    """``None`` -> :data:`BUILTIN_FONT`; a file -> validated :class:`FontSpec` with
    an ``@font-face`` rule whose ``url()`` is the bare file name (resolved through the
    archive directory)."""
    if font_path is None:
        return Ok(BUILTIN_FONT)
    checked = validate_turkish_coverage(font_path)
    if isinstance(checked, Err):
        return checked
    resolved = font_path.resolve()
    name = ""
    try:
        name = pdfkit.font_name(pdfkit.font_from_file(str(resolved)))
    except Exception:  # noqa: BLE001 - informational only
        name = ""
    face = (
        f'@font-face {{ font-family: "{USER_FONT_FAMILY}"; '
        f'src: url("{_css_url(resolved.name)}"); }}'
    )
    return Ok(
        FontSpec(
            family_css=USER_FONT_FAMILY,
            archive_dir=resolved.parent,
            face_css=face,
            family_name=name,
        )
    )


def font_css(font: FontSpec, body_family: Optional[str] = None) -> str:
    """Stylesheet tail pinning the families the engine renders with.

    Built-in: generic ``serif`` body, ``sans-serif`` headings, ``monospace`` code
    (MuPDF maps these to Charis SIL / Nimbus Sans / Nimbus Mono PS); ``body_family="sans"``
    (a sans-serif source, ``source_profile.json``) sets the body in ``sans-serif`` too. A
    user font replaces the body and heading families; code stays monospace.
    """
    if font.is_builtin:
        body = "sans-serif" if (body_family or "").lower() == "sans" else "serif"
        return (
            f"html, body {{ font-family: {body}; }}\n"
            "h1, h2, h3, h4, h5, h6, .untranslated > blockquote { font-family: sans-serif; }\n"
            "pre, code { font-family: monospace; }\n"
        )
    return (
        f"{font.face_css}\n"
        f'html, body, h1, h2, h3, h4, h5, h6, .untranslated > blockquote '
        f'{{ font-family: "{font.family_css}"; }}\n'
        "pre, code { font-family: monospace; }\n"
    )
