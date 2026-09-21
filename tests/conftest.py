"""Shared fixtures: in-memory and temp-file state databases, a synthetic chunk factory."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

import pytest
from sqlalchemy import event

from book_translator.database.repository import (
    ChunkRepository,
    JobRecord,
    JobRepository,
    JobSpec,
    LeaseRepository,
)
from book_translator.database.session import Database, close_database, open_database
from book_translator.domain.models import Chunk, ChunkKind, content_hash_of
from book_translator.domain.result import unwrap

TOOL_VERSION = "0.1.0-test"
RUN_ID = "abc123def456"
GLOSSARY_HASH = "g" * 64


def make_chunk(
    chunk_id: int,
    text: Optional[str] = None,
    *,
    kind: ChunkKind = ChunkKind.TEXT,
    translatable: bool = True,
    parent_block_id: Optional[int] = None,
    sub_index: Optional[int] = None,
    sub_count: Optional[int] = None,
    heading_path: Sequence[str] = ("Chapter 1",),
    warnings: Sequence[str] = (),
    char_count: Optional[int] = None,
    content_hash: Optional[str] = None,
) -> Chunk:
    """Synthetic chunk with a consistent hash/char_count unless overridden."""
    source = text if text is not None else f"Paragraph {chunk_id} of the synthetic book.\n\n"
    return Chunk(
        chunk_id=chunk_id,
        order=chunk_id,
        kind=kind,
        translatable=translatable,
        source_text=source,
        content_hash=content_hash if content_hash is not None else content_hash_of(source),
        char_count=char_count if char_count is not None else len(source),
        parent_block_id=parent_block_id,
        sub_index=sub_index,
        sub_count=sub_count,
        heading_path=tuple(heading_path),
        warnings=tuple(warnings),
    )


def make_chunks(n: int) -> List[Chunk]:
    return [make_chunk(i) for i in range(1, n + 1)]


@contextmanager
def capture_statements(db: Database) -> Iterator[List[str]]:
    """Collect every SQL statement sent to the DBAPI cursor (including BEGIN variants)."""
    statements: List[str] = []

    def before(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", before)


def raw_rows(db: Database, sql: str, params: Tuple[Any, ...] = ()) -> List[Tuple[Any, ...]]:
    """Run a raw SQL statement outside the repositories and return all rows as tuples."""
    with db.engine.connect() as conn:
        result = conn.exec_driver_sql(sql, params)
        rows = [tuple(row) for row in result.all()] if result.returns_rows else []
        conn.commit()
    return rows


def raw_row_dict(db: Database, table: str, where: str, params: Tuple[Any, ...]) -> dict[str, Any]:
    with db.engine.connect() as conn:
        result = conn.exec_driver_sql(f"SELECT * FROM {table} WHERE {where}", params)
        row = result.mappings().one()
        data = dict(row)
        conn.rollback()
    return data


@pytest.fixture
def mem_db() -> Iterator[Database]:
    db = unwrap(open_database(":memory:", tool_version=TOOL_VERSION))
    try:
        yield db
    finally:
        close_database(db)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "translation_state.db"


@pytest.fixture
def file_db(db_path: Path) -> Iterator[Database]:
    db = unwrap(open_database(db_path, tool_version=TOOL_VERSION))
    try:
        yield db
    finally:
        close_database(db)


@pytest.fixture
def job(mem_db: Database) -> JobRecord:
    return unwrap(
        JobRepository(mem_db).get_or_create_job(
            JobSpec(
                input_path="C:/books/input.pdf", input_sha256="a" * 64, tool_version=TOOL_VERSION
            )
        )
    )


@pytest.fixture
def chunks(mem_db: Database) -> ChunkRepository:
    return ChunkRepository(mem_db, run_id=RUN_ID, glossary_hash=GLOSSARY_HASH)


@pytest.fixture
def jobs(mem_db: Database) -> JobRepository:
    return JobRepository(mem_db)


@pytest.fixture
def leases(mem_db: Database) -> LeaseRepository:
    return LeaseRepository(mem_db)


@pytest.fixture
def seeded(job: JobRecord, chunks: ChunkRepository) -> JobRecord:
    """Job with 10 PENDING chunks (ids 1..10)."""
    assert unwrap(chunks.replace_all(job.id, make_chunks(10))) == 10
    return job


# --------------------------------------------------------------------------- #
# Phase B test doubles (design doc 02, section 11)
# --------------------------------------------------------------------------- #

import asyncio  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Union  # noqa: E402

from book_translator.config import Settings  # noqa: E402
from book_translator.domain.models import EffectiveGlossary, GlossaryStrategy  # noqa: E402
from book_translator.domain.result import (  # noqa: E402
    AppError,
    Err,
    ErrorCode,
    ErrorScope,
    Ok,
    Result,
    err,
)
from book_translator.translators.base import (  # noqa: E402
    BaseTranslator,
    GlossaryBinding,
    ProviderResponse,
    TranslationRequest,
    TranslatorCapabilities,
)

Outcome = Union[str, Tuple[str, Optional[float]]]
"""Scripted provider outcome: "ok", "err_429", ("err_429", retry_after_s), "err_5xx", ..."""

DEFAULT_CAPABILITIES = TranslatorCapabilities(
    supports_glossary=False,
    supports_batch=True,
    max_chars_per_request=100_000,
    max_texts_per_request=50,
    supports_context=False,
    supports_tag_protection=False,
)


class FakeTranslator(BaseTranslator):
    """Scripted provider: records calls and concurrency, replays outcomes per chunk."""

    name = "fake"
    capabilities = DEFAULT_CAPABILITIES

    def __init__(
        self,
        script: Optional[Dict[int, Sequence[Outcome]]] = None,
        *,
        latency: float = 0.0,
        capabilities: Optional[TranslatorCapabilities] = None,
        strategy: GlossaryStrategy = GlossaryStrategy.NONE,
        prepare_error: Optional[AppError] = None,
    ) -> None:
        self.script: Dict[int, List[Outcome]] = {k: list(v) for k, v in (script or {}).items()}
        self.latency = latency
        if capabilities is not None:
            self.capabilities = capabilities
        self.strategy = strategy
        self.prepare_error = prepare_error
        self.calls: List[Tuple[int, int, List[str]]] = []  # (chunk_id, attempt, texts)
        self.current = 0
        self.max_concurrency = 0
        self.prepared: List[EffectiveGlossary] = []
        self.closed = False

    @classmethod
    def create(cls, settings: Settings) -> Result[BaseTranslator]:
        return Ok(cls())

    async def prepare(self, glossary: EffectiveGlossary, run_id: str) -> Result[GlossaryBinding]:
        self.prepared.append(glossary)
        if self.prepare_error is not None:
            return Err(self.prepare_error)
        return Ok(GlossaryBinding(self.strategy, None, glossary.glossary_hash))

    def render(self, text: str) -> str:
        return "TR:" + text

    def _next_outcome(self, chunk_id: int) -> Outcome:
        queued = self.script.get(chunk_id)
        if queued:
            return queued.pop(0)
        return "ok"

    def _apply(self, outcome: Outcome, texts: Sequence[str]) -> Result[ProviderResponse]:
        name, retry_after = (outcome, None) if isinstance(outcome, str) else outcome
        if name == "ok":
            return Ok(
                ProviderResponse(
                    texts=[self.render(t) for t in texts],
                    chars_billed=sum(len(t) for t in texts),
                    latency_ms=int(self.latency * 1000),
                )
            )
        if name == "err_429":
            return err(
                ErrorCode.PROVIDER_RATE_LIMITED,
                "rate limited (HTTP 429)",
                ErrorScope.CHUNK_RETRYABLE,
                retry_after_s=retry_after,
                context={"http_status": 429},
            )
        if name == "err_5xx":
            return err(
                ErrorCode.PROVIDER_TRANSIENT,
                "provider error (HTTP 503)",
                ErrorScope.CHUNK_RETRYABLE,
                context={"http_status": 503},
            )
        if name == "err_empty":
            return Ok(ProviderResponse(texts=["" for _ in texts], chars_billed=0, latency_ms=0))
        if name == "err_456":
            return err(
                ErrorCode.PROVIDER_QUOTA,
                "quota exceeded (HTTP 456)",
                ErrorScope.JOB_FATAL,
                context={"http_status": 456},
            )
        if name == "err_403":
            return err(
                ErrorCode.PROVIDER_AUTH,
                "forbidden (HTTP 403)",
                ErrorScope.JOB_FATAL,
                context={"http_status": 403},
            )
        if name == "err_400":
            return err(
                ErrorCode.PROVIDER_BAD_REQUEST,
                "bad request (HTTP 400)",
                ErrorScope.CHUNK_FATAL,
                context={"http_status": 400},
            )
        if name == "raise_exception":
            raise RuntimeError("scripted provider crash")
        raise ValueError(f"unknown scripted outcome {name!r}")

    async def translate(
        self, texts: Sequence[str], request: TranslationRequest
    ) -> Result[ProviderResponse]:
        self.calls.append((request.chunk_id, request.attempt, list(texts)))
        self.current += 1
        self.max_concurrency = max(self.max_concurrency, self.current)
        try:
            if self.latency:
                await asyncio.sleep(self.latency)
            return self._apply(self._next_outcome(request.chunk_id), texts)
        finally:
            self.current -= 1

    async def aclose(self) -> None:
        self.closed = True


class IdentityTranslator(FakeTranslator):
    """Returns the input texts unchanged (end-to-end invariant)."""

    name = "identity"

    def render(self, text: str) -> str:
        return text


class CountingRepository(ChunkRepository):
    """``ChunkRepository`` that counts ``claim_pending`` / ``complete`` calls (NFR-03)."""

    def __init__(self, db: Database, run_id: str, glossary_hash: Optional[str]) -> None:
        super().__init__(db, run_id, glossary_hash)
        self.claim_calls = 0
        self.complete_calls = 0
        self.claimed_ids: List[int] = []

    def claim_pending(
        self, job_id: str, limit: int, now: datetime, run_id: str
    ) -> Result[List[Chunk]]:
        self.claim_calls += 1
        result = super().claim_pending(job_id, limit, now, run_id)
        if isinstance(result, Ok):
            self.claimed_ids.extend(c.chunk_id for c in result.value)
        return result

    def complete(self, job_id: str, chunk_id: int, result: Any) -> Result[bool]:
        self.complete_calls += 1
        return super().complete(job_id, chunk_id, result)


def translator_factory(
    translator: BaseTranslator,
) -> Callable[[str, Settings], Result[BaseTranslator]]:
    """``get_translator``-compatible factory that always returns ``translator``."""

    def factory(name: str, settings: Settings) -> Result[BaseTranslator]:
        return Ok(translator)

    return factory


class FakeClock:
    """Virtual clock: ``sleep`` advances ``now`` instead of waiting."""

    def __init__(self, start: Optional[datetime] = None) -> None:
        self.now = start or datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
        self.sleeps: List[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)


def make_settings(**overrides: Any) -> Settings:
    """Settings for orchestrator tests: key present, no grace wait, small chunks."""
    values: Dict[str, Any] = {
        "deepl_api_key": "test-key",
        "grace_s": 0,
        "concurrency": 4,
        "chunk_min": 100,
        "chunk_max": 200,
        "backoff_base_s": 1.0,
        "backoff_cap_s": 8.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# v1.1 F1 fixture PDFs (design doc 06, §8.1) built with PyMuPDF drawing primitives
# --------------------------------------------------------------------------- #

import pymupdf  # noqa: E402

FIG_PAGE_W, FIG_PAGE_H = 400.0, 600.0
FIG_BODY = 10.0
TEST_DOC_PDF = Path(__file__).resolve().parent.parent / "docs" / "test_doc.pdf"

FIG_PROSE = [
    "The old house stood at the end of the lane, and nobody had lived in it for years.",
    "Every evening the wind came down from the hills and rattled the loose shutters.",
    "The children of the village told each other stories about the people who once",
    "lived there, and about the lights that some of them claimed to have seen inside.",
]
LEFT_COLUMN_TOP = ["Left column above the figure.", "It has two short lines."]
RIGHT_COLUMN_TOP = ["Right column above the figure.", "Also two short lines here."]
LEFT_COLUMN_BOTTOM = ["Left column below the figure.", "Text continues after it."]
RIGHT_COLUMN_BOTTOM = ["Right column below the figure.", "And ends the page nicely."]


def _fig_png_bytes(size: int = 60, value: int = 120) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, size, size), False)
    pix.clear_with(value)
    data: bytes = pix.tobytes("png")
    return data


def _fig_lines(
    page: "pymupdf.Page",
    y: float,
    texts: Sequence[str],
    *,
    size: float = FIG_BODY,
    x: float = 40.0,
    step: Optional[float] = None,
) -> float:
    gap = step if step is not None else size * 1.4
    for text in texts:
        page.insert_text((x, y), text, fontsize=size, fontname="helv")
        y += gap
    return y


def _fig_page(doc: "pymupdf.Document") -> "pymupdf.Page":
    return doc.new_page(width=FIG_PAGE_W, height=FIG_PAGE_H)


def draw_diagram(
    page: "pymupdf.Page",
    x: float,
    y: float,
    w: float,
    h: float,
    labels: Sequence[str] = ("Alpha node", "Beta node", "Gamma node"),
) -> None:
    """Staggered boxed labels joined by diagonal arrows: ``3 * n - 2`` stroke paths.

    The boxes zig-zag horizontally (like a flow chart) so ``find_tables`` never mistakes
    the drawing for a grid.
    """
    n = len(labels)
    box_h = h / (2 * n - 1)
    box_w = w * 0.6
    prev: Tuple[float, float] = (x, y)
    for i, label in enumerate(labels):
        top = y + i * 2 * box_h
        left = x + (w - box_w) * (i % 2)
        page.draw_rect(pymupdf.Rect(left, top, left + box_w, top + box_h), color=(0, 0, 0),
                       width=1)
        page.insert_text((left + 8, top + box_h / 2 + 3), label, fontsize=9, fontname="helv")
        cx = left + box_w / 2
        if i:
            page.draw_line(prev, (cx, top), color=(0, 0, 0), width=1)
            page.draw_polyline(
                [(cx - 4, top - 4), (cx, top), (cx + 4, top - 4)], color=(0, 0, 0), width=1
            )
        prev = (cx, top + box_h)


def _build_decorative_rules(path: Path) -> None:
    """Header rule, footnote separator, underlined link, drawn bullets, table borders: no figure."""
    doc = pymupdf.open()
    for number in range(1, 3):
        page = _fig_page(doc)
        page.insert_text((40, 30), "Running header of the fixture", fontsize=9)
        page.draw_line((40, 36), (360, 36), color=(0, 0, 0), width=0.5)
        y = _fig_lines(page, 100, FIG_PROSE)
        page.insert_text((40, y), "A link to the project site.", fontsize=FIG_BODY)
        page.draw_line((40, y + 2), (150, y + 2), color=(0, 0, 1), width=0.5)
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(40, y - 9, 150, y + 3),
                          "uri": "https://example.org"})
        y += 20
        for i, item in enumerate(("first bullet item", "second bullet item", "third one")):
            page.draw_circle((44, y + i * 14 - 3), 1.5, color=(0, 0, 0), fill=(0, 0, 0))
            page.insert_text((52, y + i * 14), item, fontsize=FIG_BODY)
        y += 60
        for row in range(3):
            page.draw_line((40, y + row * 20), (240, y + row * 20), color=(0, 0, 0), width=0.5)
        for col in (40.0, 140.0, 240.0):
            page.draw_line((col, y), (col, y + 40), color=(0, 0, 0), width=0.5)
        for (cx, cy, text) in ((44, y + 14, "Name"), (144, y + 14, "Value"),
                               (44, y + 34, "alpha"), (144, y + 34, "1")):
            page.insert_text((cx, cy), text, fontsize=9)
        _fig_lines(page, y + 70, FIG_PROSE[:2])
        page.draw_line((40, 540), (200, 540), color=(0, 0, 0), width=0.4)
        page.insert_text((40, 552), "1 A footnote under the separator line.", fontsize=7)
        page.insert_text((190, 585), str(number), fontsize=9)
    doc.save(str(path))
    doc.close()


def _build_two_figures(path: Path) -> None:
    doc = pymupdf.open()
    page = _fig_page(doc)
    draw_diagram(page, 60, 50, 200, 120)
    page.insert_text((40, 190), "Figure 3.1 The first diagram of the chapter.", fontsize=FIG_BODY)
    _fig_lines(page, 225, FIG_PROSE[:3])
    draw_diagram(page, 60, 300, 200, 120, labels=("Delta node", "Epsilon node", "Zeta node"))
    page.insert_text((40, 440), "Figure 3.2 The second diagram of the chapter.", fontsize=FIG_BODY)
    _fig_lines(page, 475, FIG_PROSE[3:])
    doc.save(str(path))
    doc.close()


def _build_subfigures(path: Path) -> None:
    doc = pymupdf.open()
    page = _fig_page(doc)
    _fig_lines(page, 60, FIG_PROSE[:2])
    draw_diagram(page, 40, 110, 140, 150, labels=("Left A", "Left B", "Left C"))
    draw_diagram(page, 210, 110, 140, 150, labels=("Right A", "Right B", "Right C"))
    page.insert_text((40, 280), "Figure 3.4 (a) The left part and (b) the right part.",
                     fontsize=FIG_BODY)
    _fig_lines(page, 320, FIG_PROSE[2:])
    doc.save(str(path))
    doc.close()


def _build_two_column_spanning(path: Path) -> None:
    doc = pymupdf.open()
    page = _fig_page(doc)
    _fig_lines(page, 60, LEFT_COLUMN_TOP, size=9, x=30)
    _fig_lines(page, 60, RIGHT_COLUMN_TOP, size=9, x=210)
    draw_diagram(page, 60, 130, 280, 220, labels=("Wide A", "Wide B", "Wide C", "Wide D"))
    page.insert_text((40, 372), "Figure 5.1 A figure spanning both columns.", fontsize=FIG_BODY)
    _fig_lines(page, 420, LEFT_COLUMN_BOTTOM, size=9, x=30)
    _fig_lines(page, 420, RIGHT_COLUMN_BOTTOM, size=9, x=210)
    doc.save(str(path))
    doc.close()


def _build_full_page_figure(path: Path) -> None:
    doc = pymupdf.open()
    page = _fig_page(doc)
    _fig_lines(page, 100, FIG_PROSE)
    page = _fig_page(doc)
    draw_diagram(page, 40, 40, 320, 460, labels=("One", "Two", "Three", "Four", "Five"))
    page.insert_text((40, 530), "Figure 6.1 A full-page figure with a caption only.",
                     fontsize=FIG_BODY)
    doc.save(str(path))
    doc.close()


def _build_raster_caption_above(path: Path) -> None:
    doc = pymupdf.open()
    page = _fig_page(doc)
    _fig_lines(page, 60, FIG_PROSE[:1])
    page.insert_text((40, 100), "Table 2.1 Values measured in the laboratory.", fontsize=FIG_BODY)
    page.insert_image(pymupdf.Rect(50, 115, 250, 315), stream=_fig_png_bytes())
    _fig_lines(page, 350, FIG_PROSE[1:])
    page = _fig_page(doc)
    page.insert_text((40, 100), "Figure 2.2 A caption without any figure on this page.",
                     fontsize=FIG_BODY)
    _fig_lines(page, 130, FIG_PROSE)
    doc.save(str(path))
    doc.close()


def _build_table_page(path: Path) -> None:
    doc = pymupdf.open()
    page = _fig_page(doc)
    _fig_lines(page, 100, FIG_PROSE[:3])
    shape = page.new_shape()
    for y in (300.0, 320.0, 340.0):
        shape.draw_line((40, y), (240, y))
    for x in (40.0, 140.0, 240.0):
        shape.draw_line((x, 300), (x, 340))
    shape.finish(width=0.5)
    shape.commit()
    for x, y, text in ((44, 314, "Name"), (144, 314, "Value"), (44, 334, "alpha"), (144, 334, "1")):
        page.insert_text((x, y), text, fontsize=9)
    _fig_lines(page, 380, FIG_PROSE[3:])
    doc.save(str(path))
    doc.close()


def _build_mixed_figure(path: Path) -> None:
    doc = pymupdf.open()
    page = _fig_page(doc)
    _fig_lines(page, 60, FIG_PROSE[:2])
    draw_diagram(page, 40, 110, 190, 150)
    page.insert_image(pymupdf.Rect(240, 130, 340, 230), stream=_fig_png_bytes())
    page.insert_text((40, 285), "Figure 4.1 A drawing next to a photograph.", fontsize=FIG_BODY)
    _fig_lines(page, 320, FIG_PROSE[2:])
    doc.save(str(path))
    doc.close()


def _build_text_box(path: Path) -> None:
    doc = pymupdf.open()
    page = _fig_page(doc)
    _fig_lines(page, 60, FIG_PROSE[:2])
    page.draw_rect(pymupdf.Rect(35, 105, 365, 160), color=(0, 0, 0), width=1)
    _fig_lines(page, 120, ["A boxed sidebar of pure text sits inside a single rectangle.",
                          "It is not a figure: the border is the only drawing here."])
    _fig_lines(page, 200, FIG_PROSE[2:])
    doc.save(str(path))
    doc.close()


def _build_formula_page(path: Path) -> None:
    doc = pymupdf.open()
    page = _fig_page(doc)
    _fig_lines(page, 60, FIG_PROSE[:2])
    # Three small vector glyphs (a fraction bar and two radicals) inline with the text.
    page.draw_rect(pymupdf.Rect(150, 96, 158, 104), color=(0, 0, 0), fill=(0, 0, 0))
    page.draw_rect(pymupdf.Rect(162, 96, 170, 104), color=(0, 0, 0), fill=(0, 0, 0))
    page.draw_rect(pymupdf.Rect(174, 96, 182, 104), color=(0, 0, 0), fill=(0, 0, 0))
    _fig_lines(page, 130, FIG_PROSE[2:])
    doc.save(str(path))
    doc.close()


FIGURE_BUILDERS: Dict[str, Callable[[Path], None]] = {
    "decorative_rules": _build_decorative_rules,
    "two_figures": _build_two_figures,
    "subfigures": _build_subfigures,
    "two_column_spanning": _build_two_column_spanning,
    "full_page_figure": _build_full_page_figure,
    "raster_caption_above": _build_raster_caption_above,
    "table_page": _build_table_page,
    "mixed_figure": _build_mixed_figure,
    "text_box": _build_text_box,
    "formula_page": _build_formula_page,
}


@pytest.fixture(scope="session")
def figure_pdfs(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    """F1 fixture PDFs of design doc 06 §8.1 plus ``vector_diagram`` (docs/test_doc.pdf)."""
    root = tmp_path_factory.mktemp("figure_pdfs")
    paths: Dict[str, Path] = {}
    for name, builder in FIGURE_BUILDERS.items():
        target = root / f"{name}.pdf"
        builder(target)
        paths[name] = target
    paths["vector_diagram"] = TEST_DOC_PDF
    return paths


# --------------------------------------------------------------------------- #
# v1.1 F2 overlay fixtures (design doc 06, §8.1) and the counting overlay repository
# --------------------------------------------------------------------------- #

from book_translator.database.repository import OverlayBlockRepository  # noqa: E402
from book_translator.domain.overlay import OverlayBlock  # noqa: E402

OVERLAY_HEADER = "Overlay fixture running header"
OVERLAY_JUSTIFIED = (
    "The committee met on the first morning of the month and discussed the long report "
    "in great detail before anyone was allowed to leave the crowded room, because every "
    "member wanted to be heard and nobody was willing to give up the floor to another."
)
OVERLAY_CENTERED = "A centred epigraph of the chapter\nspread over two short lines"
OVERLAY_RIGHT = "Signed by the author himself\nin the winter of that year"
OVERLAY_ITALIC = "An entirely italic remark stands here alone."
OVERLAY_HYPHEN = ["The investigation was remark-", "able in every single respect."]
OVERLAY_FRAGMENT = "This sentence continues on the next"
OVERLAY_CONTINUATION = "page and it ends right here."
OVERLAY_FOOTNOTE = "1 A footnote at the bottom of the page."


def _build_overlay_styles(path: Path) -> None:
    """Bold heading, justified / centred / right-aligned / italic blocks, a bold "1." prefix,
    a hyphenated line break, a footnote, a running header, page numbers and a sentence
    that continues on the next page (design doc 06, §8.1 ``overlay_styles``)."""
    doc = pymupdf.open()
    page = _fig_page(doc)
    page.insert_text((40, 30), OVERLAY_HEADER, fontsize=9, fontname="helv")
    page.insert_text((40, 80), "Chapter One Styles", fontsize=16, fontname="hebo")
    page.insert_textbox(pymupdf.Rect(40, 100, 360, 170), OVERLAY_JUSTIFIED, fontsize=10,
                        fontname="tiro", align=pymupdf.TEXT_ALIGN_JUSTIFY)
    page.insert_textbox(pymupdf.Rect(40, 190, 360, 225), OVERLAY_CENTERED, fontsize=10,
                        fontname="tiro", align=pymupdf.TEXT_ALIGN_CENTER)
    page.insert_textbox(pymupdf.Rect(40, 245, 360, 280), OVERLAY_RIGHT, fontsize=10,
                        fontname="tiro", align=pymupdf.TEXT_ALIGN_RIGHT)
    page.insert_text((40, 310), OVERLAY_ITALIC, fontsize=10, fontname="tiit")
    page.insert_text((40, 345), "1.", fontsize=10, fontname="tibo")
    page.insert_text((52, 345), "First item of the numbered list", fontsize=10, fontname="tiro")
    _fig_lines(page, 385, OVERLAY_HYPHEN, size=10, step=12.5)
    page.insert_text((40, 440), OVERLAY_FRAGMENT, fontsize=10, fontname="tiro")
    page.insert_text((40, 560), OVERLAY_FOOTNOTE, fontsize=7, fontname="tiro")
    page.insert_text((195, 588), "7", fontsize=9, fontname="helv")
    page = _fig_page(doc)
    page.insert_text((40, 30), OVERLAY_HEADER, fontsize=9, fontname="helv")
    page.insert_text((40, 80), OVERLAY_CONTINUATION, fontsize=10, fontname="tiro")
    _fig_lines(page, 110, FIG_PROSE)
    page.insert_text((195, 588), "8", fontsize=9, fontname="helv")
    page = _fig_page(doc)
    page.insert_text((40, 30), OVERLAY_HEADER, fontsize=9, fontname="helv")
    _fig_lines(page, 80, FIG_PROSE)
    page.insert_text((195, 588), "9", fontsize=9, fontname="helv")
    doc.save(str(path))
    doc.close()


def _build_overlay_no_text(path: Path) -> None:
    """A scanned (image-only) page between two text pages (E-47)."""
    doc = pymupdf.open()
    page = _fig_page(doc)
    _fig_lines(page, 100, FIG_PROSE)
    page = _fig_page(doc)
    page.insert_image(pymupdf.Rect(40, 60, 360, 540), stream=_fig_png_bytes())
    page = _fig_page(doc)
    _fig_lines(page, 100, FIG_PROSE[:2])
    doc.save(str(path))
    doc.close()


def _build_overlay_encrypted(path: Path) -> None:
    """``overlay_styles`` saved with an owner password and no modify permission (E-49)."""
    plain = path.with_name("overlay_encrypted_plain.pdf")
    _build_overlay_styles(plain)
    doc = pymupdf.open(str(plain))
    doc.save(
        str(path),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="",
        permissions=int(pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ACCESSIBILITY),
    )
    doc.close()


OVERLAY_BUILDERS: Dict[str, Callable[[Path], None]] = {
    "overlay_styles": _build_overlay_styles,
    "overlay_no_text": _build_overlay_no_text,
    "overlay_encrypted": _build_overlay_encrypted,
}


@pytest.fixture(scope="session")
def overlay_pdfs(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Path]:
    """F2 fixture PDFs of design doc 06 §8.1 plus ``vector_diagram`` and ``table_page``."""
    root = tmp_path_factory.mktemp("overlay_pdfs")
    paths: Dict[str, Path] = {}
    for name, builder in OVERLAY_BUILDERS.items():
        target = root / f"{name}.pdf"
        builder(target)
        paths[name] = target
    table = root / "table_page.pdf"
    _build_table_page(table)
    paths["table_page"] = table
    paths["vector_diagram"] = TEST_DOC_PDF
    return paths


class CountingOverlayRepository(OverlayBlockRepository):
    """``OverlayBlockRepository`` counting batch claims / completes (NFR-32, AC US-20/1)."""

    def __init__(self, db: Database, run_id: str, glossary_hash: Optional[str]) -> None:
        super().__init__(db, run_id, glossary_hash)
        self.claim_calls = 0
        self.complete_calls = 0
        self.claimed_ids: List[int] = []
        self.batches: List[List[int]] = []

    def claim_pending_batch(
        self, job_id: str, max_units: int, min_chars: int, now: datetime, run_id: str
    ) -> Result[List[OverlayBlock]]:
        self.claim_calls += 1
        result = super().claim_pending_batch(job_id, max_units, min_chars, now, run_id)
        if isinstance(result, Ok) and result.value:
            ids = [u.unit_id for u in result.value]
            self.claimed_ids.extend(ids)
            self.batches.append(ids)
        return result

    def complete(self, job_id: str, unit_id: int, result: Any) -> Result[bool]:
        self.complete_calls += 1
        return super().complete(job_id, unit_id, result)


# --------------------------------------------------------------------------- #
# v1.1 fix round: one page, ten body paragraphs -> one 10-unit overlay batch (CR-43 / CR-44)
# --------------------------------------------------------------------------- #

TEN_UNIT_PARAGRAPHS = [
    (
        f"Paragraph number {index} explains how the calibration of the camera works in",
        f"practice and why step {index} matters for every later stage of the pipeline.",
    )
    for index in range(1, 11)
]


def _build_ten_units(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    y = 160.0
    for first, second in TEN_UNIT_PARAGRAPHS:
        page.insert_text((72, y), first, fontsize=10, fontname="helv")
        page.insert_text((72, y + 13), second, fontsize=10, fontname="helv")
        y += 52.0
    doc.save(str(path))
    doc.close()


@pytest.fixture(scope="session")
def ten_unit_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A single page with ten two-line body paragraphs (one overlay batch of 10 units)."""
    target = tmp_path_factory.mktemp("ten_units") / "ten_units.pdf"
    _build_ten_units(target)
    return target
