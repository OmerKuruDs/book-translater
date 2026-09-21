"""Exporter interface, registry and shared helpers (design doc 02, sections 2.4 and 8).

Exporters consume an ``AssembledDocument`` and write ``translated_book.<suffix>``
into a target directory. Writes go through :func:`atomic_write_bytes` so a
failing write never leaves a partial target file (E-18).

v1.1 (design doc 06, 3.3): an image line with ``src`` whose file exists under the
output directory renders as ``<figure><img/></figure>``; otherwise the v1
placeholder is kept and ``figure_missing:N`` is reported (FR-36, E-33). A legend
marker + list is wrapped in ``<div class="figure-legend">``; an orphan marker
is dropped.
"""

from __future__ import annotations

import html
import os
import re
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import ClassVar, Dict, List, Optional, Tuple, Type

import markdown as markdown_lib

from ..domain.bt_syntax import ImageLine, is_image_line, is_legend_marker, parse_image_line
from ..domain.models import AssembledDocument
from ..domain.result import ErrorCode, ErrorScope, Ok, Result, err

OUTPUT_STEM = "translated_book"
"""Base file name of every export artifact (``<output>/translated_book.<suffix>``)."""

FIGURE_PLACEHOLDER_TEXT = "Şekil {n} — görsel dahil edilmedi"
"""User-facing (Turkish) placeholder for an image that is not part of the export."""

FIGURE_ALT_TEXT = "Şekil {n}"
"""User-facing (Turkish) alt text of a rendered figure."""

LEGEND_MORE_NOTE = re.compile(r"^\*… ve \d+ etiket daha\*[ \t]*$")
"""The assembler's truncation note (``pipeline.assembler.LEGEND_MORE_NOTE``), kept inside
the legend wrapper; duplicated here because exporters must not import the pipeline."""

_LEGEND_ITEM = re.compile(r"^[ \t]*[-*+][ \t]+")

UNTRANSLATED_START = re.compile(r"^> \*\*\[UNTRANSLATED — chunk (\d+) — .*\]\*\*\s*$")
UNTRANSLATED_END = re.compile(r"^> \*\*\[END UNTRANSLATED — chunk (\d+)\]\*\*\s*$")

MARKDOWN_EXTENSIONS: List[str] = ["fenced_code", "tables", "footnotes", "sane_lists", "md_in_html"]

_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_BLOCKQUOTE_PREFIX = re.compile(r"^(?:[ ]{0,3}>[ ]?)+")
_BARE_AMPERSAND = re.compile(
    r"&(?!(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6});)"
)


@dataclass(frozen=True)
class ExportOptions:
    title: Optional[str] = None
    author: Optional[str] = None
    language: str = "tr"
    chapter_split_level: int = 1  # H1; auto-falls back to H2
    css_path: Optional[Path] = None
    font_path: Optional[Path] = None  # PDF font override
    # v1.1 F3 (design doc 06, C9): native reflowed PDF page design.
    # The page design follows the *source* unless the user asks otherwise: with
    # ``page_size == "source"`` the engine uses ``page_rect_pt`` (the source page size
    # passed in by the pipeline); ``body_font_pt``/``margins_pt`` are likewise the
    # source-derived values and, when set, win over ``margins_mm`` and the stylesheet.
    pdf_engine: str = "native"  # "native" | "weasyprint"
    page_size: str = "source"  # "source" or a pymupdf paper name (A4, Letter, "a5-l" ...)
    # top, right, bottom, left: the user's explicit ``--pdf-margin`` only. ``None`` (the
    # default) means "as in the source" (``margins_pt``); when neither is known the engine
    # uses 10 % of the page and reports ``pdf_defaults_assumed`` - there is no built-in
    # margin any more (Addendum A.3, CR-66).
    margins_mm: Optional[Tuple[float, float, float, float]] = None
    page_rect_pt: Optional[Tuple[float, float]] = None  # source page (width, height) in pt
    body_font_pt: Optional[float] = None  # source body font size in pt
    margins_pt: Optional[Tuple[float, float, float, float]] = None  # top, right, bottom, left
    # "serif" | "sans": dominant body family of the source (``source_profile.json`` field
    # ``serif``); ``None`` keeps the stylesheet's serif body. Ignored with ``font_path``.
    body_family: Optional[str] = None


@dataclass(frozen=True)
class ExportArtifact:
    path: Path
    format: str
    bytes_written: int
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class HtmlRender:
    """Result of :func:`markdown_to_html`: the fragment and ``figure_missing:N`` warnings."""

    html: str
    warnings: Tuple[str, ...] = ()


class BaseExporter(ABC):
    """One output format. Subclasses register themselves with :func:`register`."""

    name: ClassVar[str]  # "markdown" | "epub" | "pdf"
    file_suffix: ClassVar[str]
    optional: ClassVar[bool]  # True -> unavailable means "partial", not failure

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool: ...

    @abstractmethod
    def export(
        self, document: AssembledDocument, target_dir: Path, options: ExportOptions
    ) -> Result[ExportArtifact]: ...

    @classmethod
    def output_path(cls, target_dir: Path) -> Path:
        return target_dir / f"{OUTPUT_STEM}{cls.file_suffix}"

    @classmethod
    def engines(cls) -> Dict[str, bool]:
        """Rendering engines of this exporter and their availability (design doc 06, D10).

        Empty for single-engine exporters; the ``pdf`` exporter reports
        ``{"native": True, "weasyprint": <importable>}`` for the ``exporters`` command.
        """
        return {}


_REGISTRY: Dict[str, Type[BaseExporter]] = {}


def register(cls: Type[BaseExporter]) -> Type[BaseExporter]:
    """Class decorator adding an exporter to the registry under ``cls.name``."""
    _REGISTRY[cls.name] = cls
    return cls


def _ensure_registered() -> None:
    # Imported lazily: the concrete modules import this module at load time.
    from . import epub_exporter, markdown_exporter, pdf_exporter  # noqa: F401


def list_exporters() -> List[str]:
    """Registered exporter names in registration order."""
    _ensure_registered()
    return list(_REGISTRY)


def get_exporter(name: str) -> Result[BaseExporter]:
    """Instantiate the exporter called ``name`` (default constructor)."""
    _ensure_registered()
    cls = _REGISTRY.get(name)
    if cls is None:
        return err(
            ErrorCode.EXPORTER_UNAVAILABLE,
            f"unknown exporter {name!r} (available: {', '.join(_REGISTRY)})",
            ErrorScope.USER,
            context={"exporter": name},
        )
    return Ok(cls())


def atomic_write_bytes(path: Path, data: bytes) -> Result[int]:
    """Write ``data`` to ``path`` via a temp file in the same directory + ``os.replace``.

    On any failure the temp file is removed and ``path`` is left as it was
    (E-18: a disk-full or permission error never leaves a partial target).
    Returns the number of bytes written.
    """
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
    return Ok(len(data))


def figure_placeholder_html(number: str) -> str:
    text = FIGURE_PLACEHOLDER_TEXT.format(n=number)
    return f'<p class="figure-placeholder">{html.escape(text)}</p>'


def figure_html(image_id: int, src: str) -> str:
    alt = FIGURE_ALT_TEXT.format(n=image_id)
    return (
        f'<figure class="figure"><img src="{html.escape(src, quote=True)}" '
        f'alt="{html.escape(alt, quote=True)}"/></figure>'
    )


def resolve_figure(src: Optional[str], base_dir: Optional[Path]) -> Optional[Path]:
    """File of an image line's ``src`` when it is a plain relative path under ``base_dir``.

    ``None`` for the v1 placeholder form (no ``src``), when no ``base_dir`` is known,
    when the path is absolute or climbs out of the directory, or when the file is
    missing (E-33: the caller falls back to the placeholder and warns).

    ``src`` is a forward-slash path written by the tool. Backslashes, drive letters and
    other ``:`` forms are refused on every platform (``joinpath`` would interpret them on
    Windows), and the resolved file must still lie under ``base_dir`` (symlinks, CR-59).
    """
    if not src or base_dir is None:
        return None
    if "\\" in src or ":" in src or "\x00" in src:
        return None
    posix = PurePosixPath(src)
    if posix.is_absolute() or not posix.parts or ".." in posix.parts:
        return None
    path = base_dir.joinpath(*posix.parts)
    try:
        if not path.is_file() or not path.resolve().is_relative_to(base_dir.resolve()):
            return None
    except OSError:
        return None
    return path


def _fence_toggle(line: str, state: Tuple[bool, str]) -> Tuple[bool, str]:
    """Update ``(in_fence, fence_char)`` for ``line``; the opener and closer are matched."""
    fence = _FENCE.match(line)
    if not fence:
        return state
    marker = fence.group(1)
    in_fence, fence_char = state
    if not in_fence:
        return True, marker[0]
    if marker[0] == fence_char:
        return False, ""
    return state


def image_lines(markdown_text: str) -> List[ImageLine]:
    """Every image line outside code fences, in document order."""
    found: List[ImageLine] = []
    state: Tuple[bool, str] = (False, "")
    for line in markdown_text.split("\n"):
        new_state = _fence_toggle(line, state)
        if new_state != state or state[0]:
            state = new_state
            continue
        parsed = parse_image_line(line.strip())
        if parsed is not None:
            found.append(parsed)
    return found


def _render_image_lines(
    markdown_text: str, base_dir: Optional[Path]
) -> Tuple[str, List[str]]:
    """Image lines -> ``<figure>`` (file present) or the v1 placeholder (+ warning)."""
    out: List[str] = []
    warnings: List[str] = []
    state: Tuple[bool, str] = (False, "")
    for line in markdown_text.split("\n"):
        new_state = _fence_toggle(line, state)
        if new_state != state or state[0]:
            state = new_state
            out.append(line)
            continue
        parsed = parse_image_line(line.strip())
        if parsed is None:
            out.append(line)
            continue
        if resolve_figure(parsed.src, base_dir) is not None and parsed.src is not None:
            out.extend(["", figure_html(parsed.image_id, parsed.src), ""])
            continue
        if parsed.src is not None:
            warnings.append(f"figure_missing:{parsed.image_id}")
        out.extend(["", figure_placeholder_html(str(parsed.image_id)), ""])
    return "\n".join(out), warnings


def _wrap_legend_blocks(markdown_text: str) -> str:
    """Legend marker + list (+ truncation note) -> ``<div class="figure-legend" markdown="1">``.

    An orphan marker (no list item on the next line) is dropped (design doc 06, 3.1).
    """
    lines = markdown_text.split("\n")
    out: List[str] = []
    state: Tuple[bool, str] = (False, "")
    index = 0
    while index < len(lines):
        line = lines[index]
        new_state = _fence_toggle(line, state)
        if new_state != state or state[0]:
            state = new_state
            out.append(line)
            index += 1
            continue
        if not is_legend_marker(line.strip()):
            out.append(line)
            index += 1
            continue
        index += 1
        if index >= len(lines) or not _LEGEND_ITEM.match(lines[index]):
            continue  # orphan marker
        out.extend(['<div class="figure-legend" markdown="1">', ""])
        while index < len(lines) and lines[index].strip():
            out.append(lines[index])
            index += 1
        if (
            index + 1 < len(lines)
            and not lines[index].strip()
            and LEGEND_MORE_NOTE.match(lines[index + 1])
        ):
            out.extend(["", lines[index + 1]])
            index += 2
        out.extend(["", "</div>"])
    return "\n".join(out)


def _wrap_untranslated_blocks(markdown_text: str) -> str:
    """Wrap assembler marker blocks in ``<div class="untranslated" markdown="1">``."""
    lines = markdown_text.split("\n")
    out: List[str] = []
    in_fence = False
    open_block = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        if not in_fence:
            if UNTRANSLATED_START.match(line) and not open_block:
                out.append('<div class="untranslated" markdown="1">')
                out.append("")
                open_block = True
            elif UNTRANSLATED_END.match(line) and open_block:
                out.append(line)
                out.append("")
                out.append("</div>")
                open_block = False
                continue
        out.append(line)
    if open_block:
        out.append("")
        out.append("</div>")
    return "\n".join(out)


def _escape_text(text: str) -> str:
    """``&``, ``<``, ``>`` -> entities; text that is already an entity is kept as it is."""
    return _BARE_AMPERSAND.sub("&amp;", text).replace("<", "&lt;").replace(">", "&gt;")


def _escape_prose_line(line: str) -> str:
    """Escape raw HTML characters outside inline code spans, keeping a blockquote prefix."""
    quote = _BLOCKQUOTE_PREFIX.match(line)
    prefix = quote.group(0) if quote else ""
    body = line[len(prefix) :]
    out: List[str] = [prefix]
    pos = 0
    for match in _CODE_SPAN.finditer(body):
        out.append(_escape_text(body[pos : match.start()]))
        out.append(match.group(0))
        pos = match.end()
    out.append(_escape_text(body[pos:]))
    return "".join(out)


def escape_prose(markdown_text: str) -> str:
    """Escape ``<``, ``>`` and ``&`` in prose so book text can never become HTML (NFR-06).

    Fenced code blocks, inline code spans, image lines and legend marker lines are
    left untouched: python-markdown escapes code content itself and the two comment
    lines are rewritten by :func:`markdown_to_html` afterwards.
    """
    out: List[str] = []
    in_fence = False
    fence_char = ""
    for line in markdown_text.split("\n"):
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence, fence_char = True, marker[0]
                out.append(line)
                continue
            if marker[0] == fence_char:
                in_fence = False
                out.append(line)
                continue
        if in_fence or is_image_line(line.strip()) or is_legend_marker(line.strip()):
            out.append(line)
            continue
        out.append(_escape_prose_line(line))
    return "\n".join(out)


def markdown_to_html(markdown_text: str, *, base_dir: Optional[Path] = None) -> HtmlRender:
    """BT-Markdown -> HTML fragment (``markdown`` library, design doc 02, 8.3 step 1).

    Prose is HTML-escaped first (:func:`escape_prose`); image lines become
    ``<figure><img/></figure>`` when their file exists under ``base_dir`` and the
    localized placeholder otherwise (``figure_missing:N`` in ``warnings`` when a file
    was referenced); legend blocks are wrapped in ``.figure-legend`` and assembler
    marker blocks in ``.untranslated`` divs so the stylesheet can style them.
    """
    text = escape_prose(markdown_text)
    text, warnings = _render_image_lines(text, base_dir)
    text = _wrap_legend_blocks(text)
    text = _wrap_untranslated_blocks(text)
    rendered = markdown_lib.markdown(text, extensions=MARKDOWN_EXTENSIONS, output_format="xhtml")
    return HtmlRender(html=rendered, warnings=tuple(warnings))


def load_stylesheet(css_path: Optional[Path] = None) -> str:
    """Return the bundled ``assets/book.css`` or the user override when readable."""
    if css_path is not None:
        try:
            return css_path.read_text(encoding="utf-8")
        except OSError:
            pass
    return (Path(__file__).parent / "assets" / "book.css").read_text(encoding="utf-8")
