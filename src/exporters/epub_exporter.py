"""EPUB exporter built on ``ebooklib`` (design doc 02, section 8.3).

Chapters are split on ATX headings of ``options.chapter_split_level`` (default
H1; when fewer than two such headings exist the next level is used). Front
matter before the first split heading becomes chapter 0, "Başlangıç". Each
chapter is converted to HTML separately so that footnote links stay inside the
chapter file; footnote definitions are collected from the whole document and
attached to every chapter that references them.

v1.1 (design doc 06, 3.3): every image line whose file exists under the target
directory is packaged as an ``EpubImage`` (``images/...``) and referenced from
the chapter XHTML by the same relative path; a missing file keeps the v1
placeholder and reports ``figure_missing:N``.
"""

from __future__ import annotations

import html
import io
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Dict, List, Optional, Sequence, Tuple

from ebooklib import epub  # type: ignore[import-untyped]

from ..domain.models import AssembledDocument
from ..domain.result import Err, ErrorCode, ErrorScope, Ok, Result, err
from .base import (
    BaseExporter,
    ExportArtifact,
    ExportOptions,
    atomic_write_bytes,
    image_lines,
    load_stylesheet,
    markdown_to_html,
    register,
    resolve_figure,
)

FRONT_MATTER_TITLE = "Başlangıç"
CSS_FILE_NAME = "style/book.css"
IMAGE_MEDIA_TYPES: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}

_ATX = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FOOTNOTE_DEF = re.compile(r"^\[\^([^\]\s]+)\]:[ \t]?(.*)$")
_FOOTNOTE_REF = re.compile(r"\[\^([^\]\s]+)\](?!:)")
_HTML_HEADING = re.compile(r"<h([1-6])(\s[^>]*)?>(.*?)</h\1>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_INLINE_MARKUP = re.compile(r"[*_`]+")


@dataclass
class _Chapter:
    index: int
    title: str
    lines: List[str] = field(default_factory=list)
    headings: List[Tuple[int, str, str]] = field(default_factory=list)  # (level, title, id)

    @property
    def file_name(self) -> str:
        return f"ch{self.index:03d}.xhtml"

    @property
    def uid(self) -> str:
        return f"ch{self.index:03d}"


def _heading_title(raw: str) -> str:
    return _INLINE_MARKUP.sub("", raw).strip()


def _heading_levels(lines: Sequence[str]) -> List[int]:
    levels: List[int] = []
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _ATX.match(line)
        if match:
            levels.append(len(match.group(1)))
    return levels


def choose_split_level(lines: Sequence[str], preferred: int) -> int:
    """``preferred`` when at least two headings of that level exist, else the next level."""
    levels = _heading_levels(lines)
    level = max(1, min(6, preferred))
    while level < 6 and sum(1 for lvl in levels if lvl == level) < 2:
        level += 1
    if sum(1 for lvl in levels if lvl == level) < 2:
        return max(1, min(6, preferred))
    return level


def _extract_footnotes(lines: Sequence[str]) -> Tuple[List[str], Dict[str, List[str]]]:
    """Remove footnote definitions from ``lines``; return (body lines, {id: definition lines})."""
    body: List[str] = []
    definitions: Dict[str, List[str]] = {}
    current: Optional[str] = None
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            current = None
            body.append(line)
            continue
        if in_fence:
            body.append(line)
            continue
        match = _FOOTNOTE_DEF.match(line)
        if match:
            current = match.group(1)
            definitions.setdefault(current, []).append(line)
            continue
        if current is not None and (line.startswith("    ") or line.startswith("\t")):
            definitions[current].append(line)
            continue
        if current is not None and line.strip() == "":
            # a blank line may separate continuation paragraphs; keep it in the body too
            definitions[current].append(line)
            body.append(line)
            continue
        current = None
        body.append(line)
    return body, definitions


def split_chapters(markdown_text: str, split_level: int) -> List[_Chapter]:
    """Split BT-Markdown into chapters at headings of ``split_level``."""
    lines = markdown_text.split("\n")
    chapters: List[_Chapter] = []
    current = _Chapter(index=0, title=FRONT_MATTER_TITLE)
    in_fence = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        elif not in_fence:
            match = _ATX.match(line)
            if match and len(match.group(1)) == split_level:
                chapters.append(current)
                current = _Chapter(index=len(chapters), title=_heading_title(match.group(2)))
        current.lines.append(line)
    chapters.append(current)
    if chapters and chapters[0].index == 0 and not any(
        line.strip() for line in chapters[0].lines
    ):
        chapters = chapters[1:]
        for offset, chapter in enumerate(chapters):
            chapter.index = offset
    return chapters


@dataclass(frozen=True)
class _Figure:
    image_id: int
    src: str
    path: Path


def referenced_figures(markdown_text: str, base_dir: Path) -> List[_Figure]:
    """Figures whose file exists under ``base_dir``, first image id per file, in order."""
    figures: List[_Figure] = []
    seen: set[str] = set()
    for line in image_lines(markdown_text):
        path = resolve_figure(line.src, base_dir)
        if path is None or line.src is None or line.src in seen:
            continue
        seen.add(line.src)
        figures.append(_Figure(image_id=line.image_id, src=line.src, path=path))
    return figures


def _render_chapter(
    chapter: _Chapter, footnotes: Dict[str, List[str]], base_dir: Optional[Path] = None
) -> Tuple[str, List[str]]:
    body, _local = _extract_footnotes(chapter.lines)
    text = "\n".join(body)
    referenced = [ref for ref in dict.fromkeys(_FOOTNOTE_REF.findall(text)) if ref in footnotes]
    if referenced:
        text = text.rstrip("\n") + "\n\n" + "\n".join(
            "\n".join(footnotes[ref]).rstrip("\n") for ref in referenced
        ) + "\n"
    render = markdown_to_html(text, base_dir=base_dir)
    rendered = render.html
    counter = 0

    def add_id(match: "re.Match[str]") -> str:
        nonlocal counter
        counter += 1
        level = int(match.group(1))
        attrs = match.group(2) or ""
        inner = match.group(3)
        heading_id = f"bt-{chapter.index:03d}-{counter}"
        title = html.unescape(_TAG.sub("", inner)).strip()
        chapter.headings.append((level, title, heading_id))
        if " id=" in attrs:
            return match.group(0)
        return f'<h{level} id="{heading_id}"{attrs}>{inner}</h{level}>'

    return _HTML_HEADING.sub(add_id, rendered), list(render.warnings)


def _build_toc(chapters: Sequence[_Chapter], split_level: int) -> List[object]:
    """Chapters as top-level entries; the next heading level (and, after an H2
    fallback, the lone H1 inside the front matter) as children."""
    child_levels = {split_level + 1, split_level - 1} - {0}
    toc: List[object] = []
    for chapter in chapters:
        children: List[object] = []
        for level, title, heading_id in chapter.headings:
            if level in child_levels:
                children.append(epub.Link(f"{chapter.file_name}#{heading_id}", title, heading_id))
        if children:
            toc.append((epub.Section(chapter.title, href=chapter.file_name), children))
        else:
            toc.append(epub.Link(chapter.file_name, chapter.title, chapter.uid))
    return toc


@register
class EpubExporter(BaseExporter):
    name: ClassVar[str] = "epub"
    file_suffix: ClassVar[str] = ".epub"
    optional: ClassVar[bool] = False

    @classmethod
    def is_available(cls) -> bool:
        return True

    def export(
        self, document: AssembledDocument, target_dir: Path, options: ExportOptions
    ) -> Result[ExportArtifact]:
        if not document.markdown.strip():
            return err(
                ErrorCode.EXPORT_FAILED,
                "empty document; nothing to export (run `translate` first)",
                ErrorScope.JOB_FATAL,
            )
        warnings: List[str] = []
        lines = document.markdown.split("\n")
        split_level = choose_split_level(lines, options.chapter_split_level)
        if split_level != options.chapter_split_level:
            warnings.append(f"chapter_split_fallback:h{split_level}")
        _body, footnotes = _extract_footnotes(lines)
        chapters = split_chapters(document.markdown, split_level)
        if not chapters:
            chapters = [_Chapter(index=0, title=FRONT_MATTER_TITLE, lines=[""])]

        metadata = document.metadata
        job_id = metadata.get("job_id") or ""
        first_h1 = next((title for level, title in document.heading_outline if level == 1), None)
        source_file = metadata.get("source_file") or metadata.get("input_path") or ""
        input_stem = Path(source_file).stem if source_file else ""
        title = (
            options.title or metadata.get("title") or first_h1 or input_stem or "translated_book"
        )
        author = options.author or metadata.get("author")
        identifier = uuid.uuid5(uuid.NAMESPACE_URL, job_id or title)

        try:
            book = epub.EpubBook()
            book.set_identifier(f"urn:uuid:{identifier}")
            book.set_title(title)
            book.set_language(options.language)
            if author:
                book.add_author(author)
            if source_file:
                book.add_metadata("DC", "source", Path(source_file).name)

            css_item = epub.EpubItem(
                uid="style",
                file_name=CSS_FILE_NAME,
                media_type="text/css",
                content=load_stylesheet(options.css_path).encode("utf-8"),
            )
            book.add_item(css_item)

            for figure in referenced_figures(document.markdown, target_dir):
                book.add_item(
                    epub.EpubImage(
                        uid=f"img{figure.image_id}",
                        file_name=figure.src,
                        media_type=IMAGE_MEDIA_TYPES.get(
                            figure.path.suffix.lower(), "image/png"
                        ),
                        content=figure.path.read_bytes(),
                    )
                )

            items: List[object] = []
            for chapter in chapters:
                content, chapter_warnings = _render_chapter(chapter, footnotes, target_dir)
                warnings.extend(chapter_warnings)
                item = epub.EpubHtml(
                    uid=chapter.uid,
                    file_name=chapter.file_name,
                    title=chapter.title,
                    lang=options.language,
                )
                item.set_content(content)
                item.add_item(css_item)
                book.add_item(item)
                items.append(item)

            book.toc = _build_toc(chapters, split_level)
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())
            book.spine = ["nav", *items]

            buffer = io.BytesIO()
            epub.write_epub(buffer, book, {"raise_exceptions": True})
            data = buffer.getvalue()
        except Exception as exc:  # ebooklib raises plain exceptions on malformed input
            return err(
                ErrorCode.EXPORT_FAILED,
                f"EPUB build failed: {exc.__class__.__name__}",
                ErrorScope.JOB_FATAL,
                cause=repr(exc),
            )

        path = self.output_path(target_dir)
        written = atomic_write_bytes(path, data)
        if isinstance(written, Err):
            return written
        try:
            epub.read_epub(str(path), {"ignore_ncx": False})
        except Exception as exc:
            try:
                path.unlink()
            except OSError:
                pass
            return err(
                ErrorCode.EXPORT_FAILED,
                f"EPUB smoke check failed: {exc.__class__.__name__}",
                ErrorScope.JOB_FATAL,
                context={"path": str(path)},
                cause=repr(exc),
            )
        return Ok(
            ExportArtifact(
                path=path, format=self.name, bytes_written=written.value, warnings=warnings
            )
        )
