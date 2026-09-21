"""Markdown exporter: writes ``translated_book.md`` (design doc 02, section 8.2).

v1.1 (design doc 06, 3.3): an image line whose ``src`` file exists under the
target directory becomes ``![Şekil N](images/...)``; otherwise the v1 placeholder
text is written and ``figure_missing:N`` is reported. Legend marker lines are
dropped from the ``.md`` (the paired list stays). ``render_placeholders=False``
writes the assembled markdown byte-for-byte (identity round trip).
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, List, Optional, Tuple

from ..domain.bt_syntax import is_legend_marker, parse_image_line
from ..domain.models import AssembledDocument
from ..domain.result import Err, Ok, Result
from .base import (
    FIGURE_ALT_TEXT,
    BaseExporter,
    ExportArtifact,
    ExportOptions,
    atomic_write_bytes,
    register,
    resolve_figure,
)

MARKDOWN_PLACEHOLDER = "*[Şekil {n} — görsel dahil edilmedi]*"
_FENCE_MARKERS = ("```", "~~~")


def render_image_placeholders(
    markdown_text: str, *, base_dir: Optional[Path] = None
) -> Tuple[str, List[str]]:
    """Image lines -> ``![Şekil N](src)`` or ``*[Şekil N — görsel dahil edilmedi]*``.

    Legend marker lines are removed. Returns the text and ``figure_missing:N``
    warnings for image lines that reference a file that is not under ``base_dir``.
    """
    out: List[str] = []
    warnings: List[str] = []
    in_fence = False
    for line in markdown_text.split("\n"):
        if line.lstrip().startswith(_FENCE_MARKERS):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        stripped = line.strip()
        if is_legend_marker(stripped):
            continue
        parsed = parse_image_line(stripped)
        if parsed is None:
            out.append(line)
            continue
        if parsed.src is not None and resolve_figure(parsed.src, base_dir) is not None:
            out.append(f"![{FIGURE_ALT_TEXT.format(n=parsed.image_id)}]({parsed.src})")
            continue
        if parsed.src is not None:
            warnings.append(f"figure_missing:{parsed.image_id}")
        out.append(line.replace(stripped, MARKDOWN_PLACEHOLDER.format(n=parsed.image_id), 1))
    return "\n".join(out), warnings


@register
class MarkdownExporter(BaseExporter):
    name: ClassVar[str] = "markdown"
    file_suffix: ClassVar[str] = ".md"
    optional: ClassVar[bool] = False

    def __init__(self, render_placeholders: bool = True) -> None:
        # False -> output is byte-identical to the assembled markdown (identity round trip).
        self.render_placeholders = render_placeholders

    @classmethod
    def is_available(cls) -> bool:
        return True

    def export(
        self, document: AssembledDocument, target_dir: Path, options: ExportOptions
    ) -> Result[ExportArtifact]:
        text = document.markdown
        warnings: List[str] = []
        if self.render_placeholders:
            text, warnings = render_image_placeholders(text, base_dir=target_dir)
        path = self.output_path(target_dir)
        written = atomic_write_bytes(path, text.encode("utf-8"))
        if isinstance(written, Err):
            return written
        return Ok(
            ExportArtifact(
                path=path, format=self.name, bytes_written=written.value, warnings=warnings
            )
        )
