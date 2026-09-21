"""Orchestrator: runner, retry/backoff, classification, resume, lock, cancel (design 5, 11)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import pytest

from book_translator.database.repository import (
    ChunkRepository,
    JobPatch,
    JobRepository,
    JobSpec,
    LeaseHolder,
    LeaseRepository,
)
from book_translator.database.session import Database, close_database, open_database
from book_translator.domain.models import (
    Chunk,
    ChunkKind,
    ChunkStatus,
    ExtractedDocument,
    GlossaryStrategy,
    PageInfo,
    TranslationResult,
)
from book_translator.domain.result import (
    AppError,
    Err,
    ErrorCode,
    ErrorScope,
    Ok,
    Result,
    err,
    unwrap,
)
from book_translator.exporters.base import get_exporter
from book_translator.extractors.base import BaseExtractor, ExtractOptions
from book_translator.pipeline.backoff import AdaptiveLimiter
from book_translator.pipeline.orchestrator import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_LOCKED,
    EXIT_PARTIAL,
    EXIT_PAUSED,
    EXIT_SUCCESS,
    Orchestrator,
    ProgressEvent,
    exit_code_for,
    sha256_file,
    sha256_text,
)
from book_translator.translators.base import BaseTranslator
from tests.conftest import (
    TOOL_VERSION,
    CountingRepository,
    FakeClock,
    FakeTranslator,
    IdentityTranslator,
    make_chunk,
    make_settings,
    raw_row_dict,
    raw_rows,
    translator_factory,
)

FAKE_PDF = b"%PDF-1.4\n% synthetic input for orchestrator tests\n"
ENGLISH = (
    "The dragon flew over the town while the people watched from the walls of the castle "
    "and the wind was cold in the evening air. "
)


# --------------------------------------------------------------------------- #
# Workspace helpers
# --------------------------------------------------------------------------- #


@dataclass
class Workspace:
    input_pdf: Path
    output: Path

    @property
    def db_path(self) -> Path:
        return self.output / "translation_state.db"

    @property
    def source(self) -> Path:
        return self.output / "source_book.md"


def paragraphs(n: int) -> Tuple[str, List[Chunk]]:
    chunks = [make_chunk(i) for i in range(1, n + 1)]
    return "".join(c.source_text for c in chunks), chunks


def make_workspace(tmp_path: Path, markdown: Optional[str]) -> Workspace:
    input_pdf = tmp_path / "input.pdf"
    input_pdf.write_bytes(FAKE_PDF)
    output = tmp_path / "out"
    output.mkdir()
    ws = Workspace(input_pdf=input_pdf, output=output)
    if markdown is not None:
        ws.source.write_text(markdown, encoding="utf-8")
    return ws


def seed(
    ws: Workspace,
    chunks: Sequence[Chunk],
    *,
    completed: Iterable[int] = (),
    processing: Iterable[int] = (),
    failed: Iterable[int] = (),
    provider: Optional[str] = None,
) -> str:
    """Create the job (hashes bound to the workspace files) and the chunk rows."""
    db = unwrap(open_database(ws.db_path, tool_version=TOOL_VERSION))
    try:
        jobs = JobRepository(db)
        job = unwrap(
            jobs.get_or_create_job(
                JobSpec(str(ws.input_pdf), unwrap(sha256_file(ws.input_pdf)), TOOL_VERSION)
            )
        )
        md = ws.source.read_text(encoding="utf-8")
        unwrap(
            jobs.update_job(
                job.id,
                JobPatch(
                    source_md_sha256=sha256_text(md),
                    chunk_min_chars=100,
                    chunk_max_chars=200,
                    status="CHUNKED",
                    provider=provider,
                ),
            )
        )
        repo = ChunkRepository(db, "seed", None)
        unwrap(repo.replace_all(job.id, list(chunks)))
        done, busy, bad = set(completed), set(processing), set(failed)
        if done or busy or bad:
            from book_translator.database.session import utcnow

            unwrap(repo.claim_pending(job.id, len(chunks), utcnow(), "seed"))
            for chunk_id in sorted(done):
                unwrap(
                    repo.complete(
                        job.id,
                        chunk_id,
                        TranslationResult(
                            chunk_id, f"OLD:{chunk_id}\n\n", "fake", 10, 10, 1, 1,
                            GlossaryStrategy.NONE, (), False,
                        ),
                    )
                )
            for chunk_id in sorted(bad):
                unwrap(
                    repo.fail(
                        job.id,
                        chunk_id,
                        AppError(ErrorCode.PROVIDER_TRANSIENT, "old failure",
                                 ErrorScope.CHUNK_RETRYABLE),
                    )
                )
            rest = [c.chunk_id for c in chunks if c.chunk_id not in done | busy | bad]
            unwrap(repo.release(job.id, rest))
        return job.id
    finally:
        close_database(db)


async def fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


def build(
    ws: Workspace,
    translator: FakeTranslator,
    *,
    clock: Optional[FakeClock] = None,
    settings: Any = None,
    run_id: str = "run000000001",
    **kwargs: Any,
) -> Tuple[Orchestrator, FakeClock]:
    clock = clock or FakeClock()
    orchestrator = Orchestrator(
        settings or make_settings(),
        ws.output,
        tool_version=TOOL_VERSION,
        run_id=run_id,
        translator_factory=translator_factory(translator),
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )
    return orchestrator, clock


def chunk_rows(ws: Workspace) -> Dict[int, Chunk]:
    db = unwrap(open_database(ws.db_path, tool_version=TOOL_VERSION))
    try:
        (job_id,) = raw_rows(db, "SELECT id FROM jobs")[0]
        return {c.chunk_id: c for c in ChunkRepository(db, "view", None).iter_ordered(job_id)}
    finally:
        close_database(db)


def job_row(ws: Workspace) -> Dict[str, Any]:
    db = unwrap(open_database(ws.db_path, tool_version=TOOL_VERSION))
    try:
        return raw_row_dict(db, "jobs", "singleton = 1", ())
    finally:
        close_database(db)


def run_rows(ws: Workspace) -> List[Tuple[Any, ...]]:
    db = unwrap(open_database(ws.db_path, tool_version=TOOL_VERSION))
    try:
        return raw_rows(db, "SELECT command, outcome, exit_code FROM runs ORDER BY started_at")
    finally:
        close_database(db)


def with_db(ws: Workspace) -> Database:
    return unwrap(open_database(ws.db_path, tool_version=TOOL_VERSION))


# --------------------------------------------------------------------------- #
# Runner behaviour
# --------------------------------------------------------------------------- #


async def test_concurrency_never_exceeds_permits(tmp_path: Path) -> None:
    md, chunks = paragraphs(20)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator(latency=0.005)
    events: List[ProgressEvent] = []
    orch, _ = build(ws, fake, settings=make_settings(concurrency=3), progress=events.append)

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_SUCCESS
    assert 2 <= fake.max_concurrency <= 3
    rows = chunk_rows(ws)
    assert all(c.status is ChunkStatus.COMPLETED for c in rows.values())
    assert rows[7].translated_text == "TR:Paragraph 7 of the synthetic book.\n\n"
    assert events and events[-1].completed == 20 and events[-1].total == 20
    assert summary.counts.completed == 20 and summary.chars_sent_this_run > 0
    assert json.loads(ws.output.joinpath("summary.json").read_text("utf-8"))["exit_code"] == 0
    assert run_rows(ws) == [("translate", "SUCCESS", 0)]
    assert job_row(ws)["status"] == "TRANSLATED"


async def test_rate_limit_reschedules_and_backoff_survives_restart(tmp_path: Path) -> None:
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator({1: [("err_429", 7.0)]})
    orch, clock = build(ws, fake, settings=make_settings(concurrency=4))
    start = clock.now

    summary = unwrap(await orch.translate(ws.input_pdf, limit=1))

    assert summary.exit_code == EXIT_SUCCESS  # nothing failed; chunk 1 is waiting
    row = chunk_rows(ws)[1]
    assert row.status is ChunkStatus.PENDING
    # a 429 never spends the stored retry budget - that one is for errors the unit
    # itself causes, and "slow down" is not one of them
    assert row.retry_count == 0
    assert row.next_attempt_at is not None and row.next_attempt_at >= start + timedelta(seconds=7)
    assert row.last_error and "429" in row.last_error
    assert fake.calls == [(1, 1, ["Paragraph 1 of the synthetic book."])]
    assert ("rate_limited", 2) in orch.limiter_events
    assert not clock.sleeps

    # Simulated restart three seconds later: the persisted next_attempt_at is honoured.
    later = FakeClock(start + timedelta(seconds=3))
    orch2, _ = build(ws, fake, clock=later, run_id="run000000002")
    summary2 = unwrap(await orch2.translate(ws.input_pdf))

    assert summary2.exit_code == EXIT_SUCCESS
    assert later.sleeps and later.sleeps[0] == pytest.approx(
        (row.next_attempt_at - (start + timedelta(seconds=3))).total_seconds()
    )
    assert later.now >= row.next_attempt_at
    assert len([c for c in fake.calls if c[0] == 1]) == 2  # the chunk was tried again
    rows = chunk_rows(ws)
    assert all(c.status is ChunkStatus.COMPLETED for c in rows.values())
    assert rows[1].retry_count == 0


async def test_retries_exhausted_marks_failed_and_job_continues(tmp_path: Path) -> None:
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator({2: ["err_5xx", "err_5xx", "err_5xx"]})
    orch, clock = build(ws, fake, settings=make_settings(max_retries=2))

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_PARTIAL
    assert summary.failed_chunk_ids == [2]
    rows = chunk_rows(ws)
    assert rows[2].status is ChunkStatus.FAILED
    assert rows[2].retry_count == 2
    assert rows[2].last_error and "retries exhausted after 3 attempts" in rows[2].last_error
    assert rows[1].status is ChunkStatus.COMPLETED and rows[3].status is ChunkStatus.COMPLETED
    assert [c for c, _, _ in fake.calls].count(2) == 3
    assert len(clock.sleeps) == 2  # two backoff waits, no real sleeping
    assert run_rows(ws) == [("translate", "PARTIAL", 2)]


@pytest.mark.parametrize("outcome", ["err_456", "err_403"])
async def test_job_fatal_pauses_job_and_releases_chunk(tmp_path: Path, outcome: str) -> None:
    md, chunks = paragraphs(5)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator({1: [outcome]})
    orch, _ = build(ws, fake, settings=make_settings(concurrency=1))

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_PAUSED
    assert any(w.startswith("paused:") for w in summary.warnings)
    rows = chunk_rows(ws)
    assert rows[1].status is ChunkStatus.PENDING and rows[1].retry_count == 0
    assert all(c.status is ChunkStatus.PENDING for c in rows.values())
    assert len(fake.calls) == 1
    assert job_row(ws)["status"] == "PAUSED"
    assert run_rows(ws) == [("translate", "PAUSED", 3)]


class AlwaysRateLimited(FakeTranslator):
    """A provider that says "slow down" and never stops saying it."""

    def _next_outcome(self, chunk_id: int) -> Any:
        return "err_429"


async def test_rate_limit_never_fails_a_unit_and_pauses_instead(tmp_path: Path) -> None:
    """The 364-page regression: 2066 units died as FAILED on HTTP 429 alone.

    A rate limit is the provider asking for time, so it may not consume the failure budget
    (``max_retries=0`` here proves the two budgets are separate) and it may not end as
    FAILED. When even the rate-limit budget is spent the *job* stops, with every unit still
    PENDING and its retry count untouched, ready for ``resume``.
    """
    md, chunks = paragraphs(1)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = AlwaysRateLimited()
    orch, clock = build(
        ws,
        fake,
        settings=make_settings(concurrency=1, max_retries=0, rate_limit_max_retries=3),
    )

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_PAUSED
    assert any("rate limited after 3 waits" in w for w in summary.warnings)
    assert len(fake.calls) == 4  # the first call plus the three waits of the budget
    assert [attempt for _, attempt, _ in fake.calls] == [1, 2, 3, 4]
    rows = chunk_rows(ws)
    assert rows[1].status is ChunkStatus.PENDING  # never FAILED
    assert rows[1].retry_count == 0  # the failure budget was never touched
    assert job_row(ws)["status"] == "PAUSED"
    assert len(clock.sleeps) == 3

    # resume: the work is intact, a provider that answers finishes the job
    orch2, _ = build(ws, FakeTranslator(), run_id="run000000002")
    summary2 = unwrap(await orch2.translate(ws.input_pdf))

    assert summary2.exit_code == EXIT_SUCCESS
    assert all(c.status is ChunkStatus.COMPLETED for c in chunk_rows(ws).values())


async def test_rate_limit_backoff_has_a_floor_under_the_jitter(tmp_path: Path) -> None:
    """Full jitter draws from ``[0, ceiling]``; without a floor a 429 retries at once."""
    md, chunks = paragraphs(1)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    orch, clock = build(
        ws,
        FakeTranslator({1: ["err_429", "err_429"]}),
        settings=make_settings(concurrency=1, rate_limit_min_delay_s=4.0),
        rng=random.Random(1),  # a real rng: the draws are small, the floor is not
    )

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_SUCCESS
    assert len(clock.sleeps) == 2 and all(delay >= 4.0 for delay in clock.sleeps)


async def test_rate_limit_cools_the_whole_pool_off(tmp_path: Path) -> None:
    """Halving permits stops helping at 1; the cool-off is what holds the pool back."""
    limiter = AdaptiveLimiter(4, now=lambda: 100.0, cool_off_s=7.0)
    assert limiter.permits == 4
    await limiter.acquire()  # nothing to wait for yet
    limiter.release()

    limiter.on_rate_limited()

    slept: List[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)
        limiter._cool_off_until = float("-inf")  # the clock is frozen; end the wait

    limiter._sleep = record  # type: ignore[assignment]
    await limiter.acquire()
    limiter.release()
    assert slept == [7.0]
    assert limiter.permits == 2


async def test_bad_request_fails_immediately(tmp_path: Path) -> None:
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator({1: ["err_400"]})
    orch, _ = build(ws, fake)

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_PARTIAL
    rows = chunk_rows(ws)
    assert rows[1].status is ChunkStatus.FAILED and rows[1].retry_count == 0
    assert rows[1].last_error and "400" in rows[1].last_error
    assert [c for c, _, _ in fake.calls].count(1) == 1
    assert rows[2].status is ChunkStatus.COMPLETED and rows[3].status is ChunkStatus.COMPLETED


async def test_empty_response_retries_once_then_review_flag(tmp_path: Path) -> None:
    md, chunks = paragraphs(2)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator({1: ["err_empty", "err_empty"], 2: ["err_empty"]})
    orch, _ = build(ws, fake)

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_SUCCESS
    rows = chunk_rows(ws)
    # chunk 1: empty twice -> kept untranslated with the review flag (E-16, 5.3)
    assert rows[1].status is ChunkStatus.COMPLETED
    assert rows[1].review_flag is True
    assert rows[1].retry_count == 1
    assert rows[1].translated_text == chunks[0].source_text
    assert any(w.startswith("empty_response") for w in rows[1].warnings)
    # chunk 2: empty once, fine on retry
    assert rows[2].status is ChunkStatus.COMPLETED and rows[2].review_flag is False
    assert rows[2].retry_count == 1 and rows[2].translated_text.startswith("TR:")
    assert summary.review_chunk_ids == [1]


async def test_non_translatable_chunks_never_reach_the_provider(tmp_path: Path) -> None:
    code = make_chunk(2, "```python\nprint('x')\n```\n\n", kind=ChunkKind.CODE, translatable=False)
    chunks = [make_chunk(1), code, make_chunk(3)]
    md = "".join(c.source_text for c in chunks)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator()
    orch, _ = build(ws, fake)

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_SUCCESS
    assert sorted(c for c, _, _ in fake.calls) == [1, 3]
    rows = chunk_rows(ws)
    assert rows[2].status is ChunkStatus.COMPLETED
    assert rows[2].translated_text == code.source_text
    assert summary.chars_sent_this_run == sum(len(t) for _, _, ts in fake.calls for t in ts)


async def test_resume_sends_exactly_the_incomplete_chunks(tmp_path: Path) -> None:
    md, chunks = paragraphs(100)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=range(1, 41))
    fake = FakeTranslator()
    counting: List[CountingRepository] = []

    def repo_factory(db: Database, run_id: str, glossary_hash: Optional[str]) -> ChunkRepository:
        repo = CountingRepository(db, run_id, glossary_hash)
        counting.append(repo)
        return repo

    orch, _ = build(ws, fake, chunk_repository_factory=repo_factory)
    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_SUCCESS
    sent = [c for c, _, _ in fake.calls]
    assert len(sent) == 60 and sorted(sent) == list(range(41, 101))
    assert counting[0].complete_calls == 60
    assert sorted(counting[0].claimed_ids) == list(range(41, 101))
    rows = chunk_rows(ws)
    assert rows[1].translated_text == "OLD:1\n\n"  # untouched
    assert all(c.status is ChunkStatus.COMPLETED for c in rows.values())
    assert summary.counts.completed == 100 and summary.chars_sent_this_run > 0


async def test_processing_rows_are_requeued_at_start(tmp_path: Path) -> None:
    md, chunks = paragraphs(10)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, processing=[3, 4])
    assert chunk_rows(ws)[3].status is ChunkStatus.PROCESSING
    fake = FakeTranslator()
    orch, _ = build(ws, fake)

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_SUCCESS
    assert {3, 4} <= {c for c, _, _ in fake.calls}
    assert all(c.status is ChunkStatus.COMPLETED for c in chunk_rows(ws).values())


async def test_retry_failed_resets_failed_chunks(tmp_path: Path) -> None:
    md, chunks = paragraphs(4)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1, 2], failed=[3])
    fake = FakeTranslator()
    orch, _ = build(ws, fake)

    refused = unwrap(await orch.translate(ws.input_pdf))
    assert refused.exit_code == EXIT_PARTIAL  # FAILED rows are terminal without the flag
    assert [c for c, _, _ in fake.calls] == [4]

    orch2, _ = build(ws, fake, run_id="run000000002")
    summary = unwrap(await orch2.translate(ws.input_pdf, retry_failed=True))
    assert summary.exit_code == EXIT_SUCCESS
    assert chunk_rows(ws)[3].status is ChunkStatus.COMPLETED


# --------------------------------------------------------------------------- #
# Pre-flight
# --------------------------------------------------------------------------- #


async def test_input_hash_mismatch_refused_then_fresh_archives(tmp_path: Path) -> None:
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1])
    ws.input_pdf.write_bytes(FAKE_PDF + b"% changed\n")
    fake = FakeTranslator()
    orch, _ = build(ws, fake)

    refused = await orch.translate(ws.input_pdf)
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.STATE_HASH_MISMATCH
    assert exit_code_for(refused.error) == 5
    assert "--fresh" in refused.error.message
    assert fake.calls == []

    orch2, _ = build(ws, fake, run_id="run000000002")
    summary = unwrap(await orch2.translate(ws.input_pdf, fresh=True))
    assert summary.exit_code == EXIT_SUCCESS
    archives = list((ws.output / "state-archive").iterdir())
    assert len(archives) == 1 and (archives[0] / "translation_state.db").exists()
    assert ws.source.exists()  # translate --fresh keeps source_book.md
    assert any("archived" in w for w in summary.warnings)
    assert job_row(ws)["input_sha256"] == unwrap(sha256_file(ws.input_pdf))
    # the archived state still holds the old job, the new one has re-chunked everything
    assert len(fake.calls) == len(chunk_rows(ws))


async def test_provider_mismatch_refused_unless_switch_allowed(tmp_path: Path) -> None:
    md, chunks = paragraphs(2)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1], provider="deepl")
    fake = FakeTranslator()
    orch, _ = build(ws, fake)

    refused = await orch.translate(ws.input_pdf)
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.STATE_PROVIDER_MISMATCH
    assert exit_code_for(refused.error) == 5
    assert fake.calls == []

    orch2, _ = build(ws, fake, run_id="run000000002")
    summary = unwrap(await orch2.translate(ws.input_pdf, allow_provider_switch=True))
    assert summary.exit_code == EXIT_SUCCESS
    assert any(w.startswith("provider_switch:") for w in summary.warnings)
    assert job_row(ws)["provider"] == "fake"
    rows = raw_rows(with_db_closing(ws), "SELECT provider_switch_allowed FROM runs")
    assert rows == [(1,)]


def with_db_closing(ws: Workspace) -> Database:
    # Small helper for one-shot raw reads; the engine is disposed by GC/OS at process end.
    return with_db(ws)


async def test_lease_blocks_second_runner_until_force_unlock(tmp_path: Path) -> None:
    md, chunks = paragraphs(2)
    ws = make_workspace(tmp_path, md)
    job_id = seed(ws, chunks)
    db = with_db(ws)
    try:
        other = LeaseHolder(pid=os.getpid() + 4242, host=socket.gethostname(), run_id="other")
        assert unwrap(LeaseRepository(db).acquire(job_id, other, 60)) is True
    finally:
        close_database(db)
    fake = FakeTranslator()
    orch, _ = build(ws, fake)

    locked = await orch.translate(ws.input_pdf)
    assert isinstance(locked, Err)
    assert locked.error.code is ErrorCode.STATE_LOCKED
    assert exit_code_for(locked.error) == EXIT_LOCKED
    assert str(os.getpid() + 4242) in locked.error.message
    assert fake.calls == []

    orch2, _ = build(ws, fake, run_id="run000000002")
    summary = unwrap(await orch2.translate(ws.input_pdf, force_unlock=True))
    assert summary.exit_code == EXIT_SUCCESS
    db = with_db(ws)
    try:
        assert unwrap(LeaseRepository(db).get(job_id)) is None  # released on exit
    finally:
        close_database(db)


async def test_missing_source_and_missing_key_fail_fast(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    fake = FakeTranslator()
    orch, _ = build(ws, fake)
    missing = await orch.translate(ws.input_pdf)
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.STATE_NOT_FOUND
    assert not ws.db_path.exists()

    orch2, _ = build(ws, fake, settings=make_settings(deepl_api_key=None))
    no_key = await orch2.translate(ws.input_pdf)
    assert isinstance(no_key, Err) and no_key.error.code is ErrorCode.PROVIDER_CONFIG
    assert "DEEPL_API_KEY" in no_key.error.message


# --------------------------------------------------------------------------- #
# Cancellation, limiter, limit, dry run, heartbeat
# --------------------------------------------------------------------------- #


async def test_cancellation_releases_in_flight_chunks(tmp_path: Path) -> None:
    md, chunks = paragraphs(6)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator(latency=0.3)
    orch, _ = build(ws, fake, settings=make_settings(concurrency=2, grace_s=0))

    async def interrupt() -> None:
        await asyncio.sleep(0.05)
        orch.cancel.set()

    interrupter = asyncio.create_task(interrupt())
    summary = unwrap(await orch.translate(ws.input_pdf))
    await interrupter

    assert summary.exit_code == EXIT_INTERRUPTED
    assert len(fake.calls) == 2  # two were in flight, nothing else was claimed
    rows = chunk_rows(ws)
    assert all(c.status is ChunkStatus.PENDING for c in rows.values())
    assert all(c.retry_count == 0 for c in rows.values())
    assert fake.current == 0
    assert run_rows(ws) == [("translate", "INTERRUPTED", 130)]
    assert any("interrupted" in w for w in summary.warnings)


async def test_limiter_halves_permits_on_rate_limit(tmp_path: Path) -> None:
    md, chunks = paragraphs(8)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator({1: ["err_429"], 2: ["err_429"]})
    orch, _ = build(ws, fake, settings=make_settings(concurrency=4))

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_SUCCESS
    assert orch.limiter_events[:2] == [("rate_limited", 2), ("rate_limited", 1)]
    assert all(c.status is ChunkStatus.COMPLETED for c in chunk_rows(ws).values())
    assert raw_rows(with_db_closing(ws), "SELECT rate_limited_count FROM runs") == [(2,)]


async def test_limit_caps_claims_this_run(tmp_path: Path) -> None:
    md, chunks = paragraphs(10)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator()
    orch, _ = build(ws, fake, settings=make_settings(concurrency=8))

    summary = unwrap(await orch.translate(ws.input_pdf, limit=3))

    assert summary.exit_code == EXIT_SUCCESS
    assert sorted(c for c, _, _ in fake.calls) == [1, 2, 3]
    assert summary.counts.completed == 3 and summary.counts.pending == 7
    assert job_row(ws)["status"] == "TRANSLATING"


async def test_dry_run_sends_nothing(tmp_path: Path) -> None:
    md, chunks = paragraphs(5)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1])
    fake = FakeTranslator()
    orch, _ = build(ws, fake)

    summary = unwrap(await orch.translate(ws.input_pdf, dry_run=True))

    assert summary.exit_code == EXIT_SUCCESS
    assert fake.calls == [] and fake.prepared == []
    assert summary.counts.pending == 4 and summary.chars_sent_this_run == 0
    dry = [w for w in summary.warnings if w.startswith("dry_run:")]
    assert dry and "4 chunks pending" in dry[0]
    assert (ws.output / "summary.json").exists()
    assert all(
        c.status is (ChunkStatus.COMPLETED if c.chunk_id == 1 else ChunkStatus.PENDING)
        for c in chunk_rows(ws).values()
    )


async def test_lease_lost_during_run_stops_with_exit_4(tmp_path: Path) -> None:
    md, chunks = paragraphs(4)
    ws = make_workspace(tmp_path, md)
    job_id = seed(ws, chunks)
    fake = FakeTranslator(latency=0.08)
    orch, _ = build(
        ws, fake, settings=make_settings(concurrency=1, grace_s=0), heartbeat_interval_s=0.01
    )

    async def steal() -> None:
        await asyncio.sleep(0.03)
        db = with_db(ws)
        try:
            unwrap(LeaseRepository(db).force_break(job_id))
        finally:
            close_database(db)

    thief = asyncio.create_task(steal())
    summary = unwrap(await orch.translate(ws.input_pdf))
    await thief

    assert summary.exit_code == EXIT_LOCKED
    assert any(w.startswith("lease_lost:") for w in summary.warnings)
    rows = chunk_rows(ws)
    assert not any(c.status is ChunkStatus.PROCESSING for c in rows.values())
    assert summary.counts.completed < 4


async def test_worker_exception_becomes_chunk_fatal(tmp_path: Path) -> None:
    md, chunks = paragraphs(2)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator({1: ["raise_exception"]})
    orch, _ = build(ws, fake)

    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_PARTIAL
    rows = chunk_rows(ws)
    assert rows[1].status is ChunkStatus.FAILED
    assert rows[1].last_error and "internal error" in rows[1].last_error
    assert rows[2].status is ChunkStatus.COMPLETED


# --------------------------------------------------------------------------- #
# End-to-end: identity round trip, export, composite run, extract fallback, status
# --------------------------------------------------------------------------- #

IDENTITY_DOC = (
    "# Chapter One\n\n"
    "The dragon flew over the town while the people watched from the walls of the castle "
    "and the wind was cold.\n\n"
    "## Section A\n\n"
    "- alpha item is here with `code` inside\n"
    "- beta item links to [the docs](https://example.com/docs) for details\n\n"
    "<!-- image:1 -->\n\n"
    "```python\nprint('x')  # untouched\n```\n\n"
    "| Name | Value |\n|------|-------|\n| alpha | 1 |\n| beta | 2 |\n\n"
    "> A quoted line of text that should survive the round trip unchanged.\n\n"
    "# Chapter Two\n\n"
    "Second chapter text with a footnote[^1] and Turkish letters çğıİöşü in the prose.\n\n"
    "[^1]: Footnote text here.\n"
)


async def test_identity_round_trip_through_chunker_and_export(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, IDENTITY_DOC)
    fake = IdentityTranslator()
    orch, _ = build(ws, fake, settings=make_settings(chunk_min=100, chunk_max=200))

    summary = unwrap(await orch.translate(ws.input_pdf))
    assert summary.exit_code == EXIT_SUCCESS
    assert summary.counts.total > 1 and summary.counts.completed == summary.counts.total
    assert summary.source_md_sha256 == sha256_text(IDENTITY_DOC)
    assert job_row(ws)["chunk_min_chars"] == 100

    orch2, _ = build(ws, fake, run_id="run000000002")
    exported = unwrap(orch2.export(formats=["md"], render_placeholders=False))
    assert exported.exit_code == EXIT_SUCCESS
    translated = ws.output / "translated_book.md"
    assert exported.outputs == {"markdown": str(translated)}
    assert translated.read_text(encoding="utf-8") == IDENTITY_DOC
    assert exported.failed_chunk_ids == [] and exported.warnings == []
    assert job_row(ws)["status"] == "EXPORTED"

    exported_again = unwrap(build(ws, fake, run_id="run000000003")[0].export(formats=["md"]))
    assert "Şekil 1" in translated.read_text(encoding="utf-8")
    assert exported_again.exit_code == EXIT_SUCCESS


async def test_export_refuses_partial_unless_allowed(tmp_path: Path) -> None:
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1, 3], failed=[2])
    fake = FakeTranslator()
    orch, _ = build(ws, fake)

    refused = orch.export(formats=["md"])
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.EXPORT_REFUSED_PARTIAL
    assert exit_code_for(refused.error) == EXIT_PARTIAL

    partial = unwrap(build(ws, fake, run_id="run000000002")[0].export(
        formats=["md"], allow_partial=True
    ))
    assert partial.exit_code == EXIT_PARTIAL
    assert partial.failed_chunk_ids == [2]
    text = (ws.output / "translated_book.md").read_text(encoding="utf-8")
    assert "[UNTRANSLATED — chunk 2" in text and "OLD:1" in text


def test_export_without_state_is_a_user_error(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    orch, _ = build(ws, FakeTranslator())
    missing = orch.export()
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.STATE_NOT_FOUND
    assert not ws.db_path.exists()


class FakeExtractor(BaseExtractor):
    name = "pymupdf"
    supports_ocr = False
    supports_footnotes = False

    def __init__(self, markdown: str, error: Optional[AppError] = None) -> None:
        self.markdown = markdown
        self.error = error
        self.calls = 0

    @classmethod
    def is_available(cls) -> bool:
        return True

    def extract(
        self,
        pdf_path: Path,
        options: ExtractOptions,
        progress: Any = None,
        figure_sink: Any = None,
    ) -> Result[ExtractedDocument]:
        self.calls += 1
        if self.error is not None:
            return Err(self.error)
        return Ok(
            ExtractedDocument(
                markdown=self.markdown,
                extractor_name=self.name,
                fallback_used=False,
                pages=[PageInfo(1, len(self.markdown), False, 0)],
                detected_language=None,
                language_confidence=0.0,
                removed_headers_footers=[],
                footnote_mode="none",
                image_count=0,
                warnings=["fixture"],
            )
        )


def test_extract_marker_unavailable_falls_back_to_pymupdf(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    pymupdf = FakeExtractor(ENGLISH * 3 + "\n")

    def factory(name: str) -> Result[BaseExtractor]:
        if name == "marker":
            return err(ErrorCode.EXTRACTOR_UNAVAILABLE, "install [marker]", ErrorScope.USER)
        return Ok(pymupdf)

    orch, _ = build(ws, FakeTranslator(), extractor_factory=factory)
    outcome = unwrap(orch.extract(ws.input_pdf, extractor_name="marker"))

    assert outcome.fallback_used is True and outcome.extractor == "pymupdf"
    assert "extractor_fallback:marker_unavailable" in outcome.warnings
    assert outcome.detected_language == "en"
    assert ws.source.read_text(encoding="utf-8") == ENGLISH * 3 + "\n"
    row = job_row(ws)
    assert row["fallback_used"] == 1 and row["extractor_name"] == "pymupdf"
    assert row["status"] == "EXTRACTED"
    assert run_rows(ws) == [("extract", "SUCCESS", 0)]


def test_extract_marker_failure_falls_back_and_non_english_is_refused(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    marker = FakeExtractor(
        "", err(ErrorCode.EXTRACTION_FAILED, "marker crashed", ErrorScope.JOB_FATAL).error
    )
    german = "Der Hund und die Katze sind in dem Haus und der Garten ist nicht groß. " * 5 + "\n"
    pymupdf = FakeExtractor(german)
    marker.name = "marker"  # type: ignore[misc]

    def factory(name: str) -> Result[BaseExtractor]:
        return Ok(marker if name == "marker" else pymupdf)

    orch, _ = build(ws, FakeTranslator(), extractor_factory=factory)
    refused = orch.extract(ws.input_pdf, extractor_name="marker")
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.NON_ENGLISH_SOURCE
    assert marker.calls == 1 and pymupdf.calls == 1
    assert not ws.db_path.exists()  # nothing persisted for a refused extraction

    accepted = unwrap(
        orch.extract(ws.input_pdf, extractor_name="marker", allow_non_english=True)
    )
    assert accepted.fallback_used is True
    assert any(w.startswith("non_english_source:de") for w in accepted.warnings)


async def test_run_chains_stages_with_severity_rule(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    doc = "".join(f"# Part {i}\n\n{ENGLISH * 2}\n\n" for i in range(1, 4))
    extractor = FakeExtractor(doc)
    fake = FakeTranslator({2: ["err_400"]})
    orch, _ = build(
        ws,
        fake,
        settings=make_settings(chunk_min=100, chunk_max=400),
        extractor_factory=lambda name: Ok(extractor),
    )

    summary = unwrap(await orch.run(ws.input_pdf, formats=["md"], min_count=2))

    assert summary.command == "run"
    assert summary.exit_code == EXIT_PARTIAL
    assert summary.failed_chunk_ids == [2]
    assert summary.outputs == {} and any(w.startswith("export:") for w in summary.warnings)
    assert (ws.output / "glossary.json").exists()
    assert run_rows(ws) == [("run", "PARTIAL", 2)]
    written = json.loads((ws.output / "summary.json").read_text("utf-8"))
    assert written["exit_code"] == 2 and written["command"] == "run"

    orch2, _ = build(
        ws,
        fake,
        settings=make_settings(chunk_min=100, chunk_max=400),
        extractor_factory=lambda name: Ok(extractor),
        run_id="run000000002",
    )
    partial = unwrap(await orch2.run(ws.input_pdf, formats=["md"], allow_partial=True))
    assert partial.exit_code == EXIT_PARTIAL
    assert "markdown" in partial.outputs
    assert "[UNTRANSLATED — chunk 2" in (ws.output / "translated_book.md").read_text("utf-8")
    assert len([c for c, _, _ in fake.calls if c == 2]) == 1  # FAILED is terminal


async def test_run_fails_before_extraction_without_provider_key(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    extractor = FakeExtractor(ENGLISH)
    orch, _ = build(
        ws,
        FakeTranslator(),
        settings=make_settings(deepl_api_key=None),
        extractor_factory=lambda name: Ok(extractor),
    )
    result = await orch.run(ws.input_pdf)
    assert isinstance(result, Err) and result.error.code is ErrorCode.PROVIDER_CONFIG
    assert extractor.calls == 0 and not ws.db_path.exists()


def test_status_reports_state_and_is_read_only(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    orch, _ = build(ws, FakeTranslator())
    empty = unwrap(orch.status())
    assert empty.exists is False and not ws.db_path.exists()

    md, chunks = paragraphs(6)
    ws.source.write_text(md, encoding="utf-8")
    job_id = seed(ws, chunks, completed=[1, 2], failed=[5], processing=[6], provider="fake")
    report = unwrap(build(ws, FakeTranslator(), run_id="run000000002")[0].status())

    assert report.exists and report.job_id == job_id
    assert report.provider == "fake" and report.job_status == "CHUNKED"
    assert report.input_sha256 == unwrap(sha256_file(ws.input_pdf))
    assert report.counts is not None
    assert (report.counts.completed, report.counts.failed, report.counts.processing) == (2, 1, 1)
    assert report.counts.pending == 2 and report.counts.total == 6
    assert report.failed_chunk_ids == [5]
    assert report.lease is None and report.last_run is None
    assert chunk_rows(ws)[6].status is ChunkStatus.PROCESSING  # status did not re-queue


def test_glossary_stage_writes_and_keeps_file(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, "The Dragon Tower stands. The Dragon Tower falls. "
                                  "The Dragon Tower is old and the Dragon Tower is tall.\n")
    orch, _ = build(ws, FakeTranslator())
    first = unwrap(orch.glossary(min_count=2))
    assert first.written is True and first.path.exists()
    (ws.output / "glossary.json").write_text(
        json.dumps({"version": 1, "entries": [
            {"source": "Dragon Tower", "target": "Ejderha Kulesi", "count": 4,
             "status": "approved", "origin": "user"}]}),
        encoding="utf-8",
    )
    kept = unwrap(orch.glossary(min_count=2))
    assert kept.written is False and kept.approved == 1
    forced = unwrap(orch.glossary(min_count=2, force=True))
    assert forced.written is True and forced.approved == 1  # user edits survive --force


# --------------------------------------------------------------------------- #
# Fix round (docs/04_code_review.md)
# --------------------------------------------------------------------------- #


class _EventCollector(logging.Handler):
    """Collects records of the ``book_translator`` tree regardless of propagation settings."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def collect_events() -> Iterator[_EventCollector]:
    from book_translator.logging_setup import ContextFilter

    logger = logging.getLogger("book_translator")
    handler = _EventCollector()
    handler.addFilter(ContextFilter())  # chunk_id / attempt as the JSONL handler sees them
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def seed_job_only(ws: Workspace) -> str:
    """Job row without chunks: the state after `extract` alone."""
    db = with_db(ws)
    try:
        jobs = JobRepository(db)
        job = unwrap(
            jobs.get_or_create_job(
                JobSpec(str(ws.input_pdf), unwrap(sha256_file(ws.input_pdf)), TOOL_VERSION)
            )
        )
        unwrap(jobs.update_job(job.id, JobPatch(status="EXTRACTED")))
        return job.id
    finally:
        close_database(db)


def extractor_build(ws: Workspace, extractor: FakeExtractor, run_id: str = "run000000001") -> Any:
    return build(
        ws, FakeTranslator(), extractor_factory=lambda name: Ok(extractor), run_id=run_id
    )[0]


def test_export_with_zero_chunks_is_refused_before_any_exporter_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CR-02: `export` right after `extract` -> exit 2, no artifact, no traceback."""
    md, _ = paragraphs(2)
    ws = make_workspace(tmp_path, md)
    seed_job_only(ws)
    resolved: List[str] = []

    def exporter_factory(name: str) -> Any:
        resolved.append(name)
        return get_exporter(name)

    orch, _ = build(ws, FakeTranslator(), exporter_factory=exporter_factory)
    refused = orch.export(formats=["md", "epub"])

    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.EXPORT_REFUSED_PARTIAL
    assert exit_code_for(refused.error) == EXIT_PARTIAL
    assert "translate" in refused.error.message
    assert resolved == []
    assert not (ws.output / "translated_book.md").exists()
    assert not (ws.output / "translated_book.epub").exists()
    assert run_rows(ws) == [("export", "PARTIAL", 2)]
    assert "Traceback" not in capsys.readouterr().err


class BrokenCompleteRepository(CountingRepository):
    """``complete`` fails the way a full disk would (JOB_FATAL); everything else is real."""

    def complete(self, job_id: str, chunk_id: int, result: Any) -> Result[bool]:
        self.complete_calls += 1
        return err(
            ErrorCode.STATE_CORRUPT,
            "complete: state database error (disk full)",
            ErrorScope.JOB_FATAL,
        )


async def test_state_write_failure_stops_the_run_with_exit_1(tmp_path: Path) -> None:
    """CR-03: a JOB_FATAL repository error ends the run; never exit 0 with PROCESSING rows."""
    md, chunks = paragraphs(6)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    repos: List[BrokenCompleteRepository] = []

    def factory(db: Database, run_id: str, glossary_hash: Optional[str]) -> ChunkRepository:
        repo = BrokenCompleteRepository(db, run_id, glossary_hash)
        repos.append(repo)
        return repo

    fake = FakeTranslator()
    orch, _ = build(
        ws,
        fake,
        settings=make_settings(concurrency=1, grace_s=30),
        chunk_repository_factory=factory,
    )
    summary = unwrap(await orch.translate(ws.input_pdf))

    assert summary.exit_code == EXIT_FAILURE
    assert any(w.startswith("state_error:") for w in summary.warnings)
    assert len(fake.calls) == 1 and repos[-1].claim_calls == 1  # nothing claimed afterwards
    assert repos[-1].complete_calls == 1
    assert summary.counts.processing == 1  # the row the failed write left behind
    assert run_rows(ws) == [("translate", "FAILED", 1)]
    assert job_row(ws)["status"] != "TRANSLATED"


def test_extract_archives_a_hand_edited_source_instead_of_overwriting(tmp_path: Path) -> None:
    """CR-06: a differing source_book.md is moved to state-archive/ and reported, never lost."""
    ws = make_workspace(tmp_path, None)
    doc = f"# Part 1\n\n{ENGLISH * 2}\n"
    extractor = FakeExtractor(doc)
    unwrap(extractor_build(ws, extractor).extract(ws.input_pdf))
    edited = doc.replace("Part 1", "Part One (edited by hand)")
    ws.source.write_text(edited, encoding="utf-8")

    again = unwrap(extractor_build(ws, extractor, "run000000002").extract(ws.input_pdf))

    assert ws.source.read_text(encoding="utf-8") == doc
    assert any("archived" in w and "source_book.md" in w for w in again.warnings)
    archived = list((ws.output / "state-archive").glob("*/source_book.md"))
    assert len(archived) == 1 and archived[0].read_text(encoding="utf-8") == edited
    assert not list((ws.output / "state-archive").glob("*/translation_state.db"))

    third = unwrap(extractor_build(ws, extractor, "run000000003").extract(ws.input_pdf))
    assert not any("archived" in w for w in third.warnings)  # identical content: nothing to do
    assert len(list((ws.output / "state-archive").glob("*/source_book.md"))) == 1


def test_extract_refuses_to_replace_source_after_translation_unless_fresh(
    tmp_path: Path,
) -> None:
    """CR-06: with completed chunks a changed extraction is refused (exit 5) unless --fresh."""
    md = f"# Part 1\n\n{ENGLISH * 2}\n"
    ws = make_workspace(tmp_path, md)
    _, chunks = paragraphs(3)
    seed(ws, chunks, completed=[1])
    extractor = FakeExtractor(md + "A paragraph a newer extractor found.\n")

    refused = extractor_build(ws, extractor).extract(ws.input_pdf)

    assert isinstance(refused, Err) and refused.error.code is ErrorCode.STATE_HASH_MISMATCH
    assert exit_code_for(refused.error) == 5 and "--fresh" in refused.error.message
    assert ws.source.read_text(encoding="utf-8") == md
    assert not (ws.output / "state-archive").exists()

    unwrap(extractor_build(ws, extractor, "run000000002").extract(ws.input_pdf, fresh=True))
    assert ws.source.read_text(encoding="utf-8") == extractor.markdown
    archives = list((ws.output / "state-archive").iterdir())
    assert len(archives) == 1
    assert (archives[0] / "source_book.md").read_text(encoding="utf-8") == md
    assert (archives[0] / "translation_state.db").exists()


async def test_lost_lease_cancels_immediately_without_grace_wait(tmp_path: Path) -> None:
    """CR-07: a lost lease is cancel-with-release; the grace period is not honoured."""
    md, chunks = paragraphs(4)
    ws = make_workspace(tmp_path, md)
    job_id = seed(ws, chunks)
    fake = FakeTranslator(latency=5.0)
    orch, _ = build(
        ws, fake, settings=make_settings(concurrency=1, grace_s=30), heartbeat_interval_s=0.01
    )

    async def steal() -> None:
        await asyncio.sleep(0.05)
        db = with_db(ws)
        try:
            unwrap(LeaseRepository(db).force_break(job_id))
        finally:
            close_database(db)

    thief = asyncio.create_task(steal())
    started = time.monotonic()
    summary = unwrap(await orch.translate(ws.input_pdf))
    await thief

    assert time.monotonic() - started < 3.0  # neither the 5 s request nor the 30 s grace
    assert summary.exit_code == EXIT_LOCKED
    assert fake.current == 0
    assert not any(c.status is ChunkStatus.PROCESSING for c in chunk_rows(ws).values())


async def test_heartbeat_continues_while_in_flight_chunks_drain(tmp_path: Path) -> None:
    """CR-07: the lease stays alive during the grace drain and in-flight chunks finish."""
    md, chunks = paragraphs(4)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    fake = FakeTranslator(latency=0.3)
    orch, _ = build(
        ws, fake, settings=make_settings(concurrency=2, grace_s=5), heartbeat_interval_s=0.02
    )

    async def interrupt() -> None:
        await asyncio.sleep(0.05)
        orch.cancel.set()

    with collect_events() as handler:
        interrupter = asyncio.create_task(interrupt())
        summary = unwrap(await orch.translate(ws.input_pdf))
        await interrupter

    assert summary.exit_code == EXIT_INTERRUPTED
    assert len(fake.calls) == 2 and summary.counts.completed == 2  # the grace let both finish
    events = [str(getattr(r, "event", "")) for r in handler.records]
    assert "grace_wait" in events
    assert "heartbeat" in events[events.index("grace_wait") :]


def test_grace_period_is_capped_below_lease_staleness(tmp_path: Path) -> None:
    """CR-07: --grace-seconds beyond lease_stale_s - 10 is capped (and logged)."""
    ws = make_workspace(tmp_path, None)
    capped, _ = build(ws, FakeTranslator(), settings=make_settings(grace_s=120, lease_stale_s=60))
    assert capped._effective_grace() == 50.0  # noqa: SLF001
    kept, _ = build(ws, FakeTranslator(), settings=make_settings(grace_s=20, lease_stale_s=60))
    assert kept._effective_grace() == 20.0  # noqa: SLF001


def test_fresh_keeps_glossary_json_in_place_and_warns(tmp_path: Path) -> None:
    """CR-23: --fresh archives a copy of glossary.json but leaves the user's file in place."""
    ws = make_workspace(tmp_path, None)
    extractor = FakeExtractor(f"# Part 1\n\n{ENGLISH * 2}\n")
    unwrap(extractor_build(ws, extractor).extract(ws.input_pdf))
    glossary = ws.output / "glossary.json"
    payload = json.dumps(
        {
            "version": 1,
            "entries": [
                {"source": "Dragon Tower", "target": "Ejderha Kulesi", "count": 4,
                 "status": "approved", "origin": "user"}
            ],
        }
    )
    glossary.write_text(payload, encoding="utf-8")

    outcome = unwrap(
        extractor_build(ws, extractor, "run000000002").extract(ws.input_pdf, fresh=True)
    )

    assert glossary.read_text(encoding="utf-8") == payload
    archives = list((ws.output / "state-archive").iterdir())
    assert len(archives) == 1
    assert (archives[0] / "glossary.json").read_text(encoding="utf-8") == payload
    assert (archives[0] / "translation_state.db").exists()
    assert any("glossary.json kept" in w and "1 approved" in w for w in outcome.warnings)


# --------------------------------------------------------------------------- #
# v1.1 F1: figure sink, figures.json, stale cleanup, --fresh, totals (design doc 06, §5.1)
# --------------------------------------------------------------------------- #

from book_translator.domain.models import FigureKind, FigureRecord  # noqa: E402
from book_translator.extractors.base import FigureOptions, FigureSink  # noqa: E402
from book_translator.extractors.figure_inventory import load_figures_file  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload" * 8


def figure_record(
    image_id: int = 1, page: int = 1, index: int = 1, dpi: int = 200
) -> FigureRecord:
    return FigureRecord(
        image_id=image_id, page=page, index_on_page=index, kind=FigureKind.VECTOR,
        region=(10.0, 20.0, 110.0, 120.0), file=f"images/p{page:03d}-f{index:02d}.png", dpi=dpi,
        width_px=278, height_px=278, bytes=len(PNG), dpi_reduced=dpi != 200, label_count=2,
        labels=("Alpha", "Beta"), labels_more=0, caption_present=True, warnings=(),
    )


class FigureFakeExtractor(FakeExtractor):
    """Fake that hands one figure to the sink when figures are enabled (like PyMuPDF)."""

    def __init__(self, markdown: str, records: Sequence[FigureRecord] = (figure_record(),)) -> None:
        super().__init__(markdown)
        self.records = list(records)
        self.sinks_seen = 0

    def extract(
        self,
        pdf_path: Path,
        options: ExtractOptions,
        progress: Any = None,
        figure_sink: Optional[FigureSink] = None,
    ) -> Result[ExtractedDocument]:
        base = super().extract(pdf_path, options, progress)
        if isinstance(base, Err) or not options.figures.enabled:
            return base
        if figure_sink is not None:
            self.sinks_seen += 1
            for record in self.records:
                written = figure_sink(record, PNG)
                if isinstance(written, Err):
                    return written
        doc = base.value
        return Ok(
            ExtractedDocument(
                markdown=doc.markdown, extractor_name=doc.extractor_name,
                fallback_used=False, pages=doc.pages, detected_language=None,
                language_confidence=0.0, removed_headers_footers=[], footnote_mode="none",
                image_count=len(self.records), warnings=list(doc.warnings),
                figures=tuple(self.records), figure_pages_skipped=((3, "excluded"),),
            )
        )


FIGURE_DOC = (
    '# Part 1\n\n<!-- image:1 src="images/p001-f01.png" -->\n\nFigure 1 Cap.\n\n'
    + ENGLISH * 2
    + "\n"
)


def test_extract_writes_images_inventory_and_removes_stale_files(tmp_path: Path) -> None:
    """§5.1: sink -> images/, figures.json after source_book.md, stale PNGs deleted (E-35)."""
    ws = make_workspace(tmp_path, None)
    (ws.output / "images").mkdir()
    (ws.output / "images" / "p009-f01.png").write_bytes(b"stale")
    (ws.output / "images" / "p009-f01.tr.png").write_bytes(b"stale translated")
    (ws.output / "images" / "my-cover.png").write_bytes(b"the user's own file")
    extractor = FigureFakeExtractor(FIGURE_DOC)
    orch = extractor_build(ws, extractor)

    outcome = unwrap(orch.extract(ws.input_pdf))

    assert extractor.sinks_seen == 1
    assert (ws.output / "images" / "p001-f01.png").read_bytes() == PNG
    assert not (ws.output / "images" / "p009-f01.png").exists()
    assert not (ws.output / "images" / "p009-f01.tr.png").exists()
    # CR-56: a file the tool did not create is never deleted; CR-62: it is reported
    assert (ws.output / "images" / "my-cover.png").read_bytes() == b"the user's own file"
    assert "figure_orphan:my-cover.png" in outcome.warnings
    assert not list(ws.output.glob("images.tmp-*"))  # CR-55: the staging directory is gone
    inventory = unwrap(load_figures_file(ws.output / "figures.json"))
    assert inventory.version == 1 and inventory.extractor == "pymupdf"
    assert inventory.dpi_requested == 200 and inventory.file_names() == ["p001-f01.png"]
    assert inventory.figures[0].labels == ["Alpha", "Beta"]
    assert inventory.totals.count == 1 and inventory.totals.vector == 1
    assert inventory.totals.largest_file == "p001-f01.png"
    assert inventory.totals.pages_skipped == [(3, "excluded")]
    assert outcome.figures is not None and outcome.figures.count == 1
    assert outcome.figures.total_bytes == len(PNG)
    assert run_rows(ws) == [("extract", "SUCCESS", 0)]

    report = unwrap(orch.status())
    assert report.figures is not None and report.figures.largest_file == "p001-f01.png"


def test_no_figures_writes_no_inventory_and_no_images(tmp_path: Path) -> None:
    """AC US-14/4: FigureOptions(enabled=False) leaves no images/ and no figures.json."""
    ws = make_workspace(tmp_path, None)
    extractor = FigureFakeExtractor(f"# Part 1\n\n{ENGLISH * 2}\n")
    orch = extractor_build(ws, extractor)
    outcome = unwrap(orch.extract(ws.input_pdf, figure_options=FigureOptions(enabled=False)))
    assert outcome.figures is None and extractor.sinks_seen == 0
    assert not (ws.output / "figures.json").exists() and not (ws.output / "images").exists()
    assert unwrap(orch.status()).figures is None


def test_sink_failure_is_figure_sink_failed_and_creates_no_db(tmp_path: Path) -> None:
    """E-57 semantics: images/ not writable -> FIGURE_SINK_FAILED (exit 1), no state written."""
    ws = make_workspace(tmp_path, None)
    (ws.output / "images").write_text("not a directory", encoding="utf-8")
    extractor = FigureFakeExtractor(FIGURE_DOC)
    result = extractor_build(ws, extractor).extract(ws.input_pdf)
    assert isinstance(result, Err) and result.error.code is ErrorCode.FIGURE_SINK_FAILED
    assert exit_code_for(result.error) == EXIT_FAILURE
    assert "p001-f01.png" in result.error.message
    assert not ws.db_path.exists() and not ws.source.exists()
    assert not (ws.output / "figures.json").exists()


def test_fresh_archives_images_directory_and_inventory(tmp_path: Path) -> None:
    """AC US-17/4, FR-40, C13: --fresh moves images/ and figures.json with the state."""
    ws = make_workspace(tmp_path, None)
    extractor = FigureFakeExtractor(FIGURE_DOC)
    unwrap(extractor_build(ws, extractor).extract(ws.input_pdf))
    (ws.output / "images" / "orphan.png").write_bytes(b"orphan")

    second = extractor_build(ws, extractor, "run000000002")
    outcome = unwrap(second.extract(ws.input_pdf, fresh=True))

    archives = list((ws.output / "state-archive").iterdir())
    assert len(archives) == 1
    archive = archives[0]
    assert (archive / "translation_state.db").exists()
    assert (archive / "source_book.md").exists()
    assert (archive / "figures.json").exists()
    assert (archive / "images" / "p001-f01.png").read_bytes() == PNG
    assert (archive / "images" / "orphan.png").exists()
    # the new extraction re-created everything in place
    assert (ws.output / "images" / "p001-f01.png").read_bytes() == PNG
    assert not (ws.output / "images" / "orphan.png").exists()
    assert (ws.output / "figures.json").exists()
    assert outcome.figures is not None and outcome.figures.count == 1


def test_changed_inventory_is_archived_like_a_changed_source(tmp_path: Path) -> None:
    """§5.1: a differing figures.json (e.g. another DPI) is archived; identical -> untouched."""
    ws = make_workspace(tmp_path, None)
    extractor = FigureFakeExtractor(FIGURE_DOC)
    unwrap(extractor_build(ws, extractor).extract(ws.input_pdf))
    again = unwrap(extractor_build(ws, extractor, "run000000002").extract(ws.input_pdf))
    assert not any("archived" in w for w in again.warnings)
    assert not (ws.output / "state-archive").exists()

    other = FigureFakeExtractor(FIGURE_DOC, records=[figure_record(dpi=150)])
    third = unwrap(extractor_build(ws, other, "run000000003").extract(ws.input_pdf))
    assert any("figures.json differed" in w for w in third.warnings)
    archived = list((ws.output / "state-archive").glob("*/figures.json"))
    assert len(archived) == 1
    assert unwrap(load_figures_file(archived[0])).figures[0].dpi == 200
    assert unwrap(load_figures_file(ws.output / "figures.json")).figures[0].dpi == 150
    assert not list((ws.output / "state-archive").glob("*/translation_state.db"))


async def test_run_summary_carries_figure_totals(tmp_path: Path) -> None:
    """AC US-17/2: summary.json and status --json expose the figure totals."""
    ws = make_workspace(tmp_path, None)
    extractor = FigureFakeExtractor(FIGURE_DOC)
    fake = FakeTranslator()
    orch, _ = build(
        ws, fake, settings=make_settings(chunk_min=100, chunk_max=400),
        extractor_factory=lambda name: Ok(extractor),
    )
    summary = unwrap(await orch.run(ws.input_pdf, formats=["md"], min_count=2))
    assert summary.figures is not None and summary.figures.count == 1
    written = json.loads((ws.output / "summary.json").read_text("utf-8"))
    assert written["figures"]["count"] == 1 and written["figures"]["vector"] == 1
    assert written["figures"]["largest_file"] == "p001-f01.png"
    # NFR-31: the provider sees the heading, the caption and the prose - nothing else. The
    # image line (token, file name) is never part of a payload.
    payloads = [t for _, _, texts in fake.calls for t in texts]
    assert payloads == ["Part 1", "Figure 1 Cap.", (ENGLISH * 2).strip()]


# --------------------------------------------------------------------------- #
# v1.1 F2: overlay mode (design doc 06, §3.6.2-3, §5.5-5.8, §8.2; Addendum A)
# --------------------------------------------------------------------------- #

import importlib.util  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402

from book_translator.database.repository import OverlayBlockRepository  # noqa: E402
from book_translator.domain.overlay import OverlayBlock, OverlayBlockKind  # noqa: E402
from book_translator.exporters.base import ExportArtifact, ExportOptions  # noqa: E402
from book_translator.extractors.overlay_extractor import OverlayOptions  # noqa: E402
from book_translator.extractors.source_profile import load_source_profile  # noqa: E402
from book_translator.pipeline.orchestrator import (  # noqa: E402
    EXIT_MISMATCH,
    MODE_OVERLAY,
    OVERLAY_FORMAT,
    default_formats,
    normalise_formats,
)
from book_translator.translators.base import TranslatorCapabilities  # noqa: E402
from tests.conftest import TEST_DOC_PDF, CountingOverlayRepository  # noqa: E402

HAS_OVERLAY_EXPORTER = (
    importlib.util.find_spec("book_translator.exporters.pdf_overlay_exporter") is not None
)
HAS_FIGURE_OVERLAY = (
    importlib.util.find_spec("book_translator.exporters.figure_overlay") is not None
)
TEST_DOC_HEADER_TEXTS = ("Book overview", "Introduction")
TEST_DOC_PAGE_NUMBERS = ("18", "19", "20")
CONTEXT_CAPABILITIES = TranslatorCapabilities(
    supports_glossary=False, supports_batch=True, max_chars_per_request=100_000,
    max_texts_per_request=50, supports_context=True, supports_tag_protection=False,
)


class ContextRecordingTranslator(FakeTranslator):
    """Fake with ``supports_context`` that keeps the context of every request."""

    capabilities = CONTEXT_CAPABILITIES

    def __init__(self) -> None:
        super().__init__()
        self.contexts: List[Optional[str]] = []

    async def translate(self, texts: Sequence[str], request: Any) -> Any:
        self.contexts.append(request.context)
        return await super().translate(texts, request)


class QuotaTranslator(FakeTranslator):
    def _next_outcome(self, chunk_id: int) -> Any:
        return "err_456"


def pdf_workspace(tmp_path: Path, pdf: Path = TEST_DOC_PDF) -> Workspace:
    """Workspace whose input is a real PDF (the overlay pass reads it)."""
    input_pdf = tmp_path / "input.pdf"
    shutil.copyfile(pdf, input_pdf)
    output = tmp_path / "out"
    output.mkdir()
    return Workspace(input_pdf=input_pdf, output=output)


def overlay_units(ws: Workspace) -> Dict[int, OverlayBlock]:
    db = unwrap(open_database(ws.db_path, tool_version=TOOL_VERSION))
    try:
        (job_id,) = raw_rows(db, "SELECT id FROM jobs")[0]
        repo = OverlayBlockRepository(db, "view", None)
        return {u.unit_id: u for u in repo.iter_ordered(job_id)}
    finally:
        close_database(db)


def payloads(fake: FakeTranslator) -> List[str]:
    return [text for _, _, texts in fake.calls for text in texts]


async def test_overlay_translate_completes_and_binds_the_job(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    fake = ContextRecordingTranslator()
    orch, _ = build(ws, fake, overlay_repository_factory=CountingOverlayRepository)

    summary = unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY))

    assert summary.exit_code == EXIT_SUCCESS and summary.mode == "overlay"
    assert summary.overlay is not None and summary.overlay.status == "OVERLAY_TRANSLATED"
    assert summary.counts.total == 43 and summary.counts.completed == 43
    assert summary.overlay.pages_total == 3 and summary.overlay.pages_completed == 3
    assert summary.overlay.translatable_units == 34 and summary.overlay.kept_units == 9
    units = overlay_units(ws)
    assert all(u.status is ChunkStatus.COMPLETED for u in units.values())
    kept = [u for u in units.values() if not u.translate]
    assert all(u.translated_text == u.source_text for u in kept)  # completed verbatim
    sent = [u for u in units.values() if u.translate]
    assert all(u.translated_text == "TR:" + u.source_text for u in sent)  # 1:1, restored

    row = job_row(ws)
    assert row["overlay_status"] == "OVERLAY_TRANSLATED" and row["status"] == "CREATED"
    assert row["overlay_unit_count"] == 43 and len(row["overlay_sha256"]) == 64
    assert row["overlay_translate_headers"] == 0 and row["overlay_keep_figure_text"] == 0
    db = with_db(ws)
    try:
        assert raw_rows(db, "SELECT command, mode, outcome, exit_code FROM runs") == [
            ("translate", "overlay", "SUCCESS", 0)
        ]
    finally:
        close_database(db)

    # FR-46: header units and page numbers are never sent by default
    sent_texts = payloads(fake)
    assert len(sent_texts) == len(sent) == 34  # one payload text per translatable unit
    plain = [u.source_text for u in sent if "http" not in u.source_text]  # no placeholders
    assert all(text in sent_texts for text in plain)
    for text in (*TEST_DOC_HEADER_TEXTS, *TEST_DOC_PAGE_NUMBERS, "1.3"):
        assert text not in sent_texts
    assert "2D (what?)" in sent_texts  # figure labels are translated in place by default
    # §4.6: one request per batch, context window <= 4 000 chars, never part of the payload
    assert len(fake.calls) == len(fake.contexts) <= 3
    assert all(c is not None and 0 < len(c) <= 4000 for c in fake.contexts)
    assert summary.chars_sent_this_run == sum(len(t) for t in sent_texts)
    written = json.loads((ws.output / "summary.json").read_text("utf-8"))
    assert written["mode"] == "overlay" and written["overlay"]["status"] == "OVERLAY_TRANSLATED"


async def test_overlay_headers_flag_sends_headers_but_never_page_numbers(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    fake = FakeTranslator()
    orch, _ = build(ws, fake)
    summary = unwrap(
        await orch.translate(
            ws.input_pdf, mode=MODE_OVERLAY,
            overlay_options=OverlayOptions(translate_headers=True),
        )
    )
    assert summary.exit_code == EXIT_SUCCESS
    sent_texts = payloads(fake)
    for text in TEST_DOC_HEADER_TEXTS:
        assert text in sent_texts
    for text in (*TEST_DOC_PAGE_NUMBERS, "1.3", "1"):
        assert text not in sent_texts  # page numbers and letter-less units never
    assert job_row(ws)["overlay_translate_headers"] == 1


async def test_overlay_keep_figure_text_sends_no_label(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    fake = FakeTranslator()
    orch, _ = build(ws, fake)
    unwrap(
        await orch.translate(
            ws.input_pdf, mode=MODE_OVERLAY,
            overlay_options=OverlayOptions(keep_figure_text=True),
        )
    )
    sent_texts = payloads(fake)
    labels = [u for u in overlay_units(ws).values() if u.kind is OverlayBlockKind.FIGURE_LABEL]
    assert len(labels) == 15
    assert all(u.source_text not in sent_texts for u in labels)
    assert all(u.keep_reason == "figure_text" and u.translated_text == u.source_text
               for u in labels)
    assert any(t.startswith("Figure 1.12") for t in sent_texts)  # the caption still is


async def test_overlay_resume_sends_no_duplicates(tmp_path: Path) -> None:
    """AC US-20/1, NFR-32: interrupt after K units, re-run -> 0 duplicate sends."""
    ws = pdf_workspace(tmp_path)
    first = FakeTranslator()
    repos: List[CountingOverlayRepository] = []

    def factory(db: Database, run_id: str, glossary_hash: Optional[str]) -> Any:
        repos.append(CountingOverlayRepository(db, run_id, glossary_hash))
        return repos[-1]

    orch, _ = build(ws, first, overlay_repository_factory=factory)
    partial = unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY, limit=6))
    assert partial.counts.completed == 6 and partial.counts.pending == 37
    assert job_row(ws)["overlay_status"] == "OVERLAY_TRANSLATING"

    second = FakeTranslator()
    orch2, _ = build(ws, second, run_id="run000000002", overlay_repository_factory=factory)
    done = unwrap(await orch2.translate(ws.input_pdf, mode=MODE_OVERLAY))
    assert done.counts.completed == 43 and done.exit_code == EXIT_SUCCESS

    claimed = [uid for repo in repos for uid in repo.claimed_ids]
    assert sorted(claimed) == list(range(1, 44))  # every unit claimed exactly once
    assert sum(repo.complete_calls for repo in repos) == 43
    sent = payloads(first) + payloads(second)
    assert len(sent) == len(set(sent)) == 34  # 0 duplicate sends


async def test_overlay_quota_pauses_with_exit_3(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    fake = QuotaTranslator()
    orch, _ = build(ws, fake)
    summary = unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY))
    assert summary.exit_code == EXIT_PAUSED
    assert any(w.startswith("paused:") for w in summary.warnings)
    row = job_row(ws)
    assert row["overlay_status"] == "OVERLAY_PAUSED" and row["status"] == "CREATED"
    units = overlay_units(ws)
    assert not any(u.status is ChunkStatus.PROCESSING for u in units.values())
    assert not any(u.status is ChunkStatus.COMPLETED and u.translate for u in units.values())
    assert exit_code_for(AppError(ErrorCode.PROVIDER_QUOTA, "q", ErrorScope.JOB_FATAL)) == 3


async def test_overlay_binding_mismatch_is_exit_5(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY, limit=3))

    switched, _ = build(ws, FakeTranslator(), run_id="run000000002")
    refused = await switched.translate(
        ws.input_pdf, mode=MODE_OVERLAY, overlay_options=OverlayOptions(translate_headers=True)
    )
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.STATE_HASH_MISMATCH
    assert exit_code_for(refused.error) == EXIT_MISMATCH
    assert "--overlay-translate-headers" in refused.error.message

    db = with_db(ws)
    try:
        raw_rows(db, "UPDATE overlay_blocks SET x1 = x1 + 5 WHERE id = 4")
    finally:
        close_database(db)
    tampered, _ = build(ws, FakeTranslator(), run_id="run000000003")
    refused = await tampered.translate(ws.input_pdf, mode=MODE_OVERLAY)
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.STATE_HASH_MISMATCH

    fresh, _ = build(ws, FakeTranslator(), run_id="run000000004")
    restarted = unwrap(
        await fresh.translate(
            ws.input_pdf, mode=MODE_OVERLAY, fresh=True,
            overlay_options=OverlayOptions(translate_headers=True),
        )
    )
    assert restarted.exit_code == EXIT_SUCCESS and restarted.counts.completed == 43


async def test_overlay_restricted_pdf_is_refused_before_any_provider_contact(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    ws = pdf_workspace(tmp_path, overlay_pdfs["overlay_encrypted"])
    fake = FakeTranslator()
    orch, _ = build(ws, fake)
    refused = await orch.translate(ws.input_pdf, mode=MODE_OVERLAY)
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.OVERLAY_PDF_RESTRICTED
    assert exit_code_for(refused.error) == EXIT_FAILURE
    assert fake.prepared == [] and fake.calls == []  # E-49: before any provider contact


async def test_overlay_dry_run_sends_nothing(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    fake = FakeTranslator()
    orch, _ = build(ws, fake)
    summary = unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY, dry_run=True))
    assert fake.calls == [] and fake.prepared == []
    assert summary.counts.pending == 43 and summary.mode == "overlay"
    assert any(w.startswith("dry_run: 34 overlay units pending") for w in summary.warnings)
    assert job_row(ws)["overlay_status"] == "OVERLAY_EXTRACTED"


async def test_export_refusals_between_the_two_passes(tmp_path: Path) -> None:
    """FR-49: every requested group is checked first; nothing is written on a refusal."""
    md, chunks = paragraphs(2)
    (tmp_path / "reflow").mkdir()
    reflow_ws = make_workspace(tmp_path / "reflow", md)
    seed(reflow_ws, chunks, completed=[1, 2])
    orch, _ = build(reflow_ws, FakeTranslator())
    refused = orch.export(formats=["md", "pdf-overlay"])
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.OVERLAY_STATE_MISSING
    assert exit_code_for(refused.error) == EXIT_FAILURE
    assert "translate --mode overlay" in refused.error.message
    assert not (reflow_ws.output / "translated_book.md").exists()  # nothing written
    assert not (reflow_ws.output / "translated_book.overlay.pdf").exists()

    (tmp_path / "overlay").mkdir()
    overlay_ws = pdf_workspace(tmp_path / "overlay")
    orch, _ = build(overlay_ws, FakeTranslator())
    unwrap(await orch.translate(overlay_ws.input_pdf, mode=MODE_OVERLAY))
    exporter, _ = build(overlay_ws, FakeTranslator(), run_id="run000000002")
    refused = exporter.export(formats=["md"])
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.OVERLAY_STATE_MISSING
    assert exit_code_for(refused.error) == EXIT_FAILURE and "reflow" in refused.error.message
    assert not (overlay_ws.output / "translated_book.md").exists()


async def test_export_refuses_an_unfinished_overlay_pass(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY, limit=4))
    exporter, _ = build(ws, FakeTranslator(), run_id="run000000002")
    refused = exporter.export(formats=["pdf-overlay"])
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.EXPORT_REFUSED_PARTIAL
    assert exit_code_for(refused.error) == EXIT_PARTIAL
    assert "--allow-partial" in refused.error.message
    bad_scale = exporter.export(formats=["pdf-overlay"], overlay_min_scale=0.4,
                                overlay_floor_scale=0.5)
    assert isinstance(bad_scale, Err) and bad_scale.error.code is ErrorCode.PROVIDER_CONFIG


def test_format_default_rule() -> None:
    """Addendum A: pdf-overlay joins the defaults whenever an overlay pass exists."""
    assert default_formats(True, False) == ["md", "epub"]
    assert default_formats(False, True) == [OVERLAY_FORMAT]
    assert default_formats(True, True) == ["md", "epub", OVERLAY_FORMAT]
    assert default_formats(False, False) == ["md", "epub"]  # the v1 refusal is reported
    assert normalise_formats(["MD", ".epub", "md", "pdf-overlay"]) == [
        "markdown", "epub", OVERLAY_FORMAT
    ]


async def test_run_overlay_refuses_reflow_formats_up_front(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    fake = FakeTranslator()
    orch, _ = build(ws, fake)
    refused = await orch.run(ws.input_pdf, mode=MODE_OVERLAY, formats=["md"])
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.OVERLAY_STATE_MISSING
    assert not ws.db_path.exists() and not ws.source.exists() and fake.calls == []


async def test_overlay_requests_and_chars_stay_close_to_the_reflow_pass(tmp_path: Path) -> None:
    """NFR-23: requests <= 2 x reflow chunk count and payload chars within +- 10 %."""
    (tmp_path / "reflow").mkdir()
    (tmp_path / "overlay").mkdir()
    settings = make_settings(chunk_min=1000, chunk_max=1500)
    reflow_ws = pdf_workspace(tmp_path / "reflow")
    reflow_fake = FakeTranslator()
    reflow, _ = build(reflow_ws, reflow_fake, settings=settings)
    unwrap(reflow.extract(reflow_ws.input_pdf, min_chars_per_page=0))
    reflow_summary = unwrap(await reflow.translate(reflow_ws.input_pdf))
    reflow_chunks = [c for c in chunk_rows(reflow_ws).values() if c.translatable]

    overlay_ws = pdf_workspace(tmp_path / "overlay")
    overlay_fake = FakeTranslator()
    overlay, _ = build(overlay_ws, overlay_fake, settings=settings)
    overlay_summary = unwrap(await overlay.translate(overlay_ws.input_pdf, mode=MODE_OVERLAY))

    assert len(overlay_fake.calls) <= 2 * len(reflow_chunks)
    ratio = overlay_summary.chars_sent_this_run / reflow_summary.chars_sent_this_run
    assert 0.9 <= ratio <= 1.1, ratio


class CapturingPdfExporter:
    """Stand-in for the ``pdf`` exporter that records the options it receives."""

    name = "pdf"
    file_suffix = ".pdf"
    optional = False
    seen: List[ExportOptions] = []

    @classmethod
    def is_available(cls) -> bool:
        return True

    def export(self, document: Any, target_dir: Path, options: ExportOptions) -> Any:
        CapturingPdfExporter.seen.append(options)
        return Ok(ExportArtifact(target_dir / "translated_book.pdf", "pdf", 0, []))


async def test_source_profile_is_written_and_consumed(tmp_path: Path) -> None:
    """Addendum A: the reflow PDF takes its page size, body font and margins from the source."""
    ws = pdf_workspace(tmp_path)
    CapturingPdfExporter.seen = []
    orch, _ = build(
        ws, IdentityTranslator(), settings=make_settings(chunk_min=1000, chunk_max=1500),
        exporter_factory=lambda name: Ok(CapturingPdfExporter()) if name == "pdf"
        else get_exporter(name),
    )
    unwrap(orch.extract(ws.input_pdf, min_chars_per_page=0))
    profile = unwrap(load_source_profile(ws.output / "source_profile.json"))
    assert (profile.page_width_pt, profile.page_height_pt) == pytest.approx((595.28, 790.87))
    assert profile.body_font_size_pt == 10.5 and profile.serif and profile.pages_measured == 3
    assert profile.margins_pt == pytest.approx((43.67, 58.54, 73.23, 57.19), abs=0.5)
    unwrap(await orch.translate(ws.input_pdf))

    unwrap(orch.export(formats=["pdf"]))  # defaults: page size and margins from the source
    options = CapturingPdfExporter.seen[-1]
    assert options.page_size == "source"
    assert options.page_rect_pt == pytest.approx((595.28, 790.87))
    assert options.body_font_pt == 10.5 and options.margins_pt == profile.margins_pt

    explicit = ExportOptions(page_size="A4", margins_mm=(10.0, 10.0, 10.0, 10.0))
    unwrap(orch.export(formats=["pdf"], options=explicit, margins_from_source=False))
    options = CapturingPdfExporter.seen[-1]
    assert options.page_rect_pt is None and options.margins_pt is None  # the user decides
    assert options.page_size == "A4" and options.body_font_pt == 10.5

    (ws.output / "source_profile.json").unlink()
    outcome = unwrap(orch.export(formats=["pdf"]))
    assert CapturingPdfExporter.seen[-1].page_rect_pt is None
    assert any(w.startswith("pdf_page_design:") for w in outcome.warnings)
    md_only = unwrap(orch.export(formats=["md"]))
    assert not any(w.startswith("pdf_page_design:") for w in md_only.warnings)


async def _translated_figure_workspace(tmp_path: Path) -> Tuple[Workspace, Orchestrator]:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator(), settings=make_settings(chunk_min=1000, chunk_max=1500))
    unwrap(orch.extract(ws.input_pdf, min_chars_per_page=0))
    inventory = unwrap(load_figures_file(ws.output / "figures.json"))
    assert len(inventory.figures[0].label_boxes) == 15  # Addendum A: geometry per label
    assert "<!-- legend:1 -->" in ws.source.read_text("utf-8")  # the translation vehicle
    unwrap(await orch.translate(ws.input_pdf))
    return ws, orch


async def test_translated_figure_step_is_skipped_when_the_module_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, orch = await _translated_figure_workspace(tmp_path)
    png = ws.output / "images" / "p002-f01.png"
    before = png.read_bytes()
    import book_translator.exporters as exporters_package

    monkeypatch.delattr(exporters_package, "figure_overlay", raising=False)
    monkeypatch.setitem(sys.modules, "book_translator.exporters.figure_overlay", None)
    outcome = unwrap(orch.export(formats=["md"]))
    assert any(w.startswith("figure_translate_unavailable") for w in outcome.warnings)
    assert png.read_bytes() == before  # the English PNG stays
    assert not (ws.output / "images" / "p002-f01.tr.png").exists()
    markdown = (ws.output / "translated_book.md").read_text("utf-8")
    # CR-57: the labels were not painted, so the (paid) translations stay in the legend list
    assert "figure_labels_not_painted:1" in outcome.warnings
    assert "2D (what?) — TR:2D (what?)" in markdown
    assert "images/p002-f01.png" in markdown and "p002-f01.tr.png" not in markdown

    with_legend = unwrap(orch.export(formats=["md"], figure_legend=True))
    assert with_legend.exit_code == EXIT_SUCCESS
    assert "figure_labels_not_painted:1" not in with_legend.warnings  # the list was asked for
    markdown = (ws.output / "translated_book.md").read_text("utf-8")
    assert "2D (what?) — TR:2D (what?)" in markdown


async def test_figure_labels_are_translated_into_a_tr_png_next_to_the_original(
    tmp_path: Path,
) -> None:
    """CR-57 with the real ``figure_overlay`` (no skip: the module always ships)."""
    assert HAS_FIGURE_OVERLAY
    ws, orch = await _translated_figure_workspace(tmp_path)
    png = ws.output / "images" / "p002-f01.png"
    before = png.read_bytes()
    inventory_before = (ws.output / "figures.json").read_bytes()
    source_before = ws.source.read_bytes()
    outcome = unwrap(orch.export(formats=["md"]))
    assert not any(w.startswith("figure_translate_") for w in outcome.warnings), outcome.warnings
    translated = ws.output / "images" / "p002-f01.tr.png"
    painted = translated.read_bytes()
    assert painted.startswith(b"\x89PNG") and painted != before
    # the English original, the inventory and source_book.md are untouched
    assert png.read_bytes() == before
    assert (ws.output / "figures.json").read_bytes() == inventory_before
    assert ws.source.read_bytes() == source_before
    # only the translated document points at the translated file
    markdown = (ws.output / "translated_book.md").read_text("utf-8")
    assert "images/p002-f01.tr.png" in markdown
    assert "images/p002-f01.png" in ws.source.read_text("utf-8")
    assert not any(w.startswith("figure_orphan:") for w in outcome.warnings)
    truncated = [w for w in outcome.warnings if w.startswith("figure_label_truncated:1:")]
    if truncated:  # a truncated label keeps its legend (the translation is never lost)
        assert "figure_labels_not_painted:1" in outcome.warnings and "<!-- legend:1" in markdown
    else:
        assert "<!-- legend:" not in markdown and "2D (what?) — " not in markdown

    # a later extract does not revert the translated document's figure (CR-57)
    unwrap(orch.extract(ws.input_pdf, min_chars_per_page=0))
    assert translated.read_bytes() == painted and png.read_bytes() == before


async def test_overlay_export_end_to_end(tmp_path: Path) -> None:
    """Real renderer (no skip: the module always ships). Q21 + CR-42 + CR-51."""
    assert HAS_OVERLAY_EXPORTER
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    summary = unwrap(await orch.run(ws.input_pdf, mode=MODE_OVERLAY, min_chars_per_page=0))
    assert list(summary.outputs) == [OVERLAY_FORMAT]  # default --format of overlay mode
    pdf = ws.output / "translated_book.overlay.pdf"
    review_file = ws.output / "overlay_review.json"
    assert pdf.is_file() and review_file.is_file()
    assert not (ws.output / "translated_book.md").exists()
    review = json.loads(review_file.read_text("utf-8"))
    # CR-51: the summary carries the very numbers of the review file
    assert summary.overlay is not None
    for key in ("placed", "shrunk_below_threshold", "could_not_fit", "kept_original"):
        assert getattr(summary.overlay, key) == review["counts"][key], key
    assert summary.overlay.placed > 0 and summary.overlay.review_path == str(review_file)
    written = json.loads((ws.output / "summary.json").read_text("utf-8"))
    assert written["overlay"]["placed"] == review["counts"]["placed"]
    assert written["overlay"]["review_path"] == str(review_file)
    # Q21: exit 2 exactly when a block could not be placed / a page was skipped
    partial = review["counts"]["could_not_fit"] > 0 or review["counts"]["pages_skipped"] > 0
    assert summary.exit_code == (EXIT_PARTIAL if partial else EXIT_SUCCESS)
    expected_status = "OVERLAY_TRANSLATED" if partial else "OVERLAY_EXPORTED"
    assert job_row(ws)["overlay_status"] == expected_status
    # CR-46: the review file names the pass it belongs to
    assert review["binding"]["job_id"] == summary.job_id
    assert review["binding"]["overlay_sha256"] == job_row(ws)["overlay_sha256"]
    report = unwrap(orch.status())
    assert report.overlay is not None and report.mode == "overlay"  # CR-42
    assert report.overlay["counts"]["completed"] == 43 and report.overlay["pages_total"] == 3
    assert report.overlay["output_path"] == str(pdf) and report.overlay["stale_outputs"] == []
    assert report.overlay["review_counts"]["placed"] == review["counts"]["placed"]
    assert report.overlay["review_counts"]["entries"] == len(review["entries"])
    db = with_db(ws)
    try:
        assert raw_rows(db, "SELECT command, mode FROM runs") == [("run", "overlay")]
    finally:
        close_database(db)

    # export without --format: only an overlay pass exists -> pdf-overlay (default rule)
    again, _ = build(ws, FakeTranslator(), run_id="run000000002")
    outcome = unwrap(again.export())
    assert list(outcome.outputs) == [OVERLAY_FORMAT]


async def test_status_reports_the_overlay_pass_and_review_counts(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    assert unwrap(orch.status()).overlay is None
    unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY, limit=5))
    (ws.output / "overlay_review.json").write_text(
        json.dumps({"version": 1, "counts": {"placed": 20}, "entries": [
            {"reason": "shrunk_below_threshold"}, {"reason": "fragment"}, {"reason": "fragment"},
        ]}),
        encoding="utf-8",
    )
    report = unwrap(orch.status())
    assert report.overlay is not None and report.mode == "overlay"
    assert report.overlay["status"] == "OVERLAY_TRANSLATING"
    assert report.overlay["counts"]["completed"] == 5 and report.overlay["counts"]["total"] == 43
    assert report.overlay["translatable_units"] == 34 and report.overlay["kept_units"] == 9
    assert report.overlay["review_counts"] == {
        "placed": 20, "entries": 3, "shrunk_below_threshold": 1, "fragment": 2,
    }
    assert report.counts is not None and report.counts.total == 0  # no reflow pass

    # BUG-02: several reason names are also count keys. The by-reason tally must fill in
    # the reasons the counts block lacks, never add on top of the ones it already has -
    # status used to report shrunk_below_threshold / could_not_fit / kept_original twice.
    (ws.output / "overlay_review.json").write_text(
        json.dumps({"version": 1,
                    "counts": {"placed": 20, "shrunk_below_threshold": 2,
                               "could_not_fit": 1, "kept_original": 4},
                    "entries": [{"reason": "shrunk_below_threshold"},
                                {"reason": "shrunk_below_threshold"},
                                {"reason": "could_not_fit"},
                                {"reason": "fragment"}]}),
        encoding="utf-8",
    )
    counts = unwrap(orch.status()).overlay["review_counts"]
    assert counts["shrunk_below_threshold"] == 2 and counts["could_not_fit"] == 1
    assert counts["kept_original"] == 4  # no entry carries it; the counts block still wins
    assert counts["fragment"] == 1  # only the tally knows this reason
    assert counts["entries"] == 4


def test_fresh_archives_the_overlay_artifacts(tmp_path: Path) -> None:
    """§5.8: --fresh moves the overlay PDF, the review list and the source profile too."""
    ws = make_workspace(tmp_path, None)
    extractor = FakeExtractor(f"# Part 1\n\n{ENGLISH * 2}\n")
    unwrap(extractor_build(ws, extractor).extract(ws.input_pdf))
    names = ("translated_book.overlay.pdf", "overlay_review.json", "source_profile.json")
    for name in names:
        (ws.output / name).write_bytes(b"old")
    unwrap(extractor_build(ws, extractor, run_id="run000000002").extract(ws.input_pdf, fresh=True))
    (archive,) = list((ws.output / "state-archive").iterdir())
    for name in names:
        assert (archive / name).read_bytes() == b"old" and not (ws.output / name).exists()


# --------------------------------------------------------------------------- #
# v1.1 fix round Q (docs/04_code_review.md "v1.1 Review": CR-39 ... CR-64, CR-68)
# --------------------------------------------------------------------------- #

import threading  # noqa: E402

from book_translator.pipeline import orchestrator as orchestrator_module  # noqa: E402


class FakeOverlayRenderer:
    """Stand-in for ``OverlayRenderer``: scripted placement counts, no PyMuPDF work."""

    calls: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    pages_skipped: Tuple[int, ...] = ()
    review: Tuple[Tuple[int, str, int, str, Optional[float]], ...] = ()

    def render(
        self,
        source_pdf: Path,
        pages: Any,
        blocks_by_page: Any,
        outline_map: Any,
        target_dir: Path,
        options: Any,
        progress: Any = None,
    ) -> Any:
        from book_translator.exporters.pdf_overlay_exporter import (
            OverlayArtifact,
            OverlayReviewEntry,
            OverlayReviewReason,
        )

        cls = FakeOverlayRenderer
        cls.calls.append({"source_pdf": Path(source_pdf), "options": options,
                          "progress": progress is not None})
        page_list = list(pages)
        for index in range(len(page_list)):
            if progress is not None:
                progress(index + 1, len(page_list))
        entries = tuple(
            OverlayReviewEntry(page, block_id, unit_id, OverlayReviewReason(reason), scale,
                               "SECRET SOURCE TEXT", "GIZLI CEVIRI")
            for page, block_id, unit_id, reason, scale in cls.review
        )
        counts = {"placed": 34, "shrunk_below_threshold": 0, "could_not_fit": 0,
                  "kept_original": 9, **cls.counts}
        pdf_path = target_dir / "translated_book.overlay.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake overlay\n")
        review_path = target_dir / "overlay_review.json"
        review_path.write_text(
            json.dumps({
                "version": 1,
                "counts": {**counts, "pages_skipped": len(cls.pages_skipped),
                           "entries": len(entries)},
                "entries": [{"page": e.page, "block_id": e.block_id, "unit_id": e.unit_id,
                             "reason": e.reason.value, "scale": e.scale} for e in entries],
            }),
            encoding="utf-8",
        )
        return Ok(
            OverlayArtifact(
                pdf_path=pdf_path, review_path=review_path, bytes_written=22,
                pages=len(page_list), placed=counts["placed"],
                shrunk_below_threshold=counts["shrunk_below_threshold"],
                could_not_fit=counts["could_not_fit"], kept_original=counts["kept_original"],
                pages_skipped=cls.pages_skipped, review=entries, warnings=(),
            )
        )


@pytest.fixture
def fake_renderer(monkeypatch: pytest.MonkeyPatch) -> type:
    import book_translator.exporters.pdf_overlay_exporter as exporter_module

    FakeOverlayRenderer.calls = []
    FakeOverlayRenderer.counts = {}
    FakeOverlayRenderer.pages_skipped = ()
    FakeOverlayRenderer.review = ()
    monkeypatch.setattr(exporter_module, "OverlayRenderer", FakeOverlayRenderer)
    return FakeOverlayRenderer


def output_files(ws: Workspace) -> List[str]:
    return sorted(p.name for p in ws.output.iterdir() if p.name.startswith("translated_book"))


# -- CR-39: export is bound to the source file -------------------------------- #


async def test_overlay_export_refuses_a_swapped_source_pdf_and_writes_nothing(
    tmp_path: Path, overlay_pdfs: Dict[str, Path], fake_renderer: type
) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY))
    shutil.copyfile(overlay_pdfs["overlay_styles"], ws.input_pdf)  # a different file, same path

    exporter, _ = build(ws, FakeTranslator(), run_id="run000000002")
    refused = exporter.export(formats=["pdf-overlay"])

    assert isinstance(refused, Err) and refused.error.code is ErrorCode.STATE_HASH_MISMATCH
    assert exit_code_for(refused.error) == EXIT_MISMATCH
    assert "export --input" in refused.error.message
    assert fake_renderer.calls == [] and output_files(ws) == []
    assert not (ws.output / "overlay_review.json").exists()
    assert job_row(ws)["overlay_status"] == "OVERLAY_TRANSLATED"
    assert run_rows(ws)[-1] == ("export", "FAILED", EXIT_MISMATCH)


async def test_export_input_flag_relocates_a_moved_source_and_still_checks_the_hash(
    tmp_path: Path, overlay_pdfs: Dict[str, Path], fake_renderer: type
) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY))
    moved = tmp_path / "elsewhere" / "renamed book.pdf"
    moved.parent.mkdir()
    shutil.move(str(ws.input_pdf), str(moved))

    missing = build(ws, FakeTranslator(), run_id="run000000002")[0].export(
        formats=["pdf-overlay"]
    )
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.INPUT_NOT_FOUND
    assert exit_code_for(missing.error) == EXIT_FAILURE
    assert "export --input" in missing.error.message and output_files(ws) == []

    wrong = build(ws, FakeTranslator(), run_id="run000000003")[0].export(
        formats=["pdf-overlay"], input_pdf=overlay_pdfs["overlay_styles"]
    )
    assert isinstance(wrong, Err) and wrong.error.code is ErrorCode.STATE_HASH_MISMATCH
    assert output_files(ws) == [] and fake_renderer.calls == []

    absent = build(ws, FakeTranslator(), run_id="run000000004")[0].export(
        formats=["pdf-overlay"], input_pdf=tmp_path / "no-such.pdf"
    )
    assert isinstance(absent, Err) and absent.error.code is ErrorCode.INPUT_NOT_FOUND

    outcome = unwrap(
        build(ws, FakeTranslator(), run_id="run000000005")[0].export(
            formats=["pdf-overlay"], input_pdf=moved
        )
    )
    assert outcome.exit_code == EXIT_SUCCESS
    assert fake_renderer.calls[-1]["source_pdf"] == moved.resolve()
    assert job_row(ws)["input_path"] == str(moved.resolve())  # remembered for the next export
    again = unwrap(build(ws, FakeTranslator(), run_id="run000000006")[0].export())
    assert again.exit_code == EXIT_SUCCESS and len(fake_renderer.calls) == 2


async def test_input_path_is_stored_resolved_so_export_is_cwd_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_renderer: type
) -> None:
    ws = pdf_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.translate(Path("input.pdf"), mode=MODE_OVERLAY))  # as typed: relative
    stored = Path(job_row(ws)["input_path"])
    assert stored.is_absolute() and stored == ws.input_pdf.resolve()

    other = tmp_path / "another" / "cwd"
    other.mkdir(parents=True)
    monkeypatch.chdir(other)
    outcome = unwrap(build(ws, FakeTranslator(), run_id="run000000002")[0].export())
    assert outcome.exit_code == EXIT_SUCCESS
    assert fake_renderer.calls[-1]["source_pdf"] == ws.input_pdf.resolve()


async def test_state_with_a_relative_input_path_heals_on_the_next_verified_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_renderer: type
) -> None:
    """A state written before CR-39 stores the path as typed (``docs\\test_doc.pdf``)."""
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY))
    db = with_db(ws)
    try:
        raw_rows(db, "UPDATE jobs SET input_path = 'input.pdf'")
    finally:
        close_database(db)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    refused = build(ws, FakeTranslator(), run_id="run000000002")[0].export()
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.INPUT_NOT_FOUND
    monkeypatch.chdir(tmp_path)
    unwrap(build(ws, FakeTranslator(), run_id="run000000003")[0].export())
    assert job_row(ws)["input_path"] == str(ws.input_pdf.resolve())


async def test_reflow_figure_step_degrades_on_a_changed_source_and_survives_a_missing_one(
    tmp_path: Path, overlay_pdfs: Dict[str, Path]
) -> None:
    """CR-92: a changed source costs the figure labels, not the document (the overlay group
    still refuses with exit 5 - see the swapped-source test above)."""
    ws, orch = await _translated_figure_workspace(tmp_path)
    original = ws.input_pdf.read_bytes()
    png = ws.output / "images" / "p002-f01.png"
    before = png.read_bytes()

    shutil.copyfile(overlay_pdfs["overlay_styles"], ws.input_pdf)
    degraded = unwrap(orch.export(formats=["md"]))
    assert degraded.exit_code == EXIT_PARTIAL
    assert list(degraded.outputs) == ["markdown"]
    assert any(
        w.startswith("figure_translate_failed:input_changed:input.pdf")
        and "export --input" in w
        for w in degraded.warnings
    ), degraded.warnings
    assert "figure_labels_not_painted:1" in degraded.warnings
    # the changed file is never read: no .tr.png, the English PNG untouched
    assert png.read_bytes() == before
    assert not (ws.output / "images" / "p002-f01.tr.png").exists()
    changed_md = (ws.output / "translated_book.md").read_text("utf-8")
    assert "2D (what?) — TR:2D (what?)" in changed_md  # the paid label translations survive
    (ws.output / "translated_book.md").unlink()

    ws.input_pdf.unlink()  # moved away: the reflow export itself does not need the PDF
    outcome = unwrap(orch.export(formats=["md"]))
    assert outcome.exit_code == EXIT_SUCCESS
    assert any(
        w.startswith("figure_translate_failed:input_missing:input.pdf") and "--input" in w
        for w in outcome.warnings
    )
    assert "figure_labels_not_painted:1" in outcome.warnings
    markdown = (ws.output / "translated_book.md").read_text("utf-8")
    assert "2D (what?) — TR:2D (what?)" in markdown  # the paid label translations survive
    assert png.read_bytes() == before

    explicit = orch.export(formats=["md"], input_pdf=ws.input_pdf)  # named, still missing
    assert isinstance(explicit, Err) and explicit.error.code is ErrorCode.INPUT_NOT_FOUND

    # CR-92: --input naming the wrong file does not lock the reflow export out either
    wrong = unwrap(orch.export(formats=["md"], input_pdf=overlay_pdfs["overlay_styles"]))
    assert wrong.exit_code == EXIT_PARTIAL and list(wrong.outputs) == ["markdown"]
    assert any(w.startswith("figure_translate_failed:input_changed:") for w in wrong.warnings)
    assert not (ws.output / "images" / "p002-f01.tr.png").exists()
    assert job_row(ws)["input_path"] != str(overlay_pdfs["overlay_styles"])  # not remembered
    ws.input_pdf.write_bytes(original)

    healed = unwrap(orch.export(formats=["md"]))  # the original file is back
    assert healed.exit_code == EXIT_SUCCESS
    assert (ws.output / "images" / "p002-f01.tr.png").exists()


# -- CR-42: runs.mode and the status counters -------------------------------- #


async def test_runs_mode_is_overlay_for_run_translate_and_export(
    tmp_path: Path, fake_renderer: type
) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.run(ws.input_pdf, mode=MODE_OVERLAY, min_chars_per_page=0))
    unwrap(build(ws, FakeTranslator(), run_id="run000000002")[0].export())  # default: overlay
    unwrap(
        await build(ws, FakeTranslator(), run_id="run000000003")[0].translate(
            ws.input_pdf, mode=MODE_OVERLAY
        )
    )
    unwrap(await build(ws, FakeTranslator(), run_id="run000000004")[0].translate(ws.input_pdf))
    unwrap(
        build(ws, FakeTranslator(), run_id="run000000005")[0].export(
            formats=["md", "pdf-overlay"]
        )
    )
    db = with_db(ws)
    try:
        rows = raw_rows(db, "SELECT id, command, mode FROM runs ORDER BY id")
    finally:
        close_database(db)
    assert rows == [
        ("run000000001", "run", "overlay"),
        ("run000000002", "export", "overlay"),
        ("run000000003", "translate", "overlay"),
        ("run000000004", "translate", "reflow"),
        ("run000000005", "export", "reflow"),  # mixed groups: not an overlay-only run
    ]


async def test_status_recovers_the_counters_of_an_unfinished_overlay_run(tmp_path: Path) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY, limit=5))
    db = with_db(ws)
    try:  # the process was killed: the run row never got its outcome
        raw_rows(db, "UPDATE runs SET outcome = NULL, exit_code = NULL, finished_at = NULL, "
                     "chunks_completed = 0")
    finally:
        close_database(db)
    report = unwrap(orch.status())
    assert report.last_run is not None and report.last_run["mode"] == "overlay"
    assert report.last_run["chunks_completed"] == 5 and report.last_run["chunks_failed"] == 0
    assert any("did not finish cleanly" in w for w in report.warnings)


# -- CR-43 / CR-44: one batch = one provider call, also on errors ------------ #


class FirstCallRateLimited(FakeTranslator):
    def _next_outcome(self, chunk_id: int) -> Any:
        return "err_429" if len(self.calls) == 1 else "ok"


class RescheduleRecorder(CountingOverlayRepository):
    whens: List[Any] = []

    def reschedule(
        self,
        job_id: str,
        unit_id: int,
        error: AppError,
        when: Any,
        bump_retry: bool = True,
    ) -> Result[bool]:
        RescheduleRecorder.whens.append(when)
        return super().reschedule(job_id, unit_id, error, when, bump_retry)


async def test_a_429_on_a_ten_unit_batch_costs_exactly_one_more_call(
    tmp_path: Path, ten_unit_pdf: Path
) -> None:
    ws = pdf_workspace(tmp_path, ten_unit_pdf)
    fake = FirstCallRateLimited()
    RescheduleRecorder.whens = []
    orch, _ = build(
        ws, fake, settings=make_settings(concurrency=8),
        overlay_repository_factory=RescheduleRecorder,
    )
    summary = unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY))

    assert summary.exit_code == EXIT_SUCCESS and summary.counts.completed == 10
    # one 429 + one retry of the *same* 10-unit batch: never 10 single-unit requests
    assert [(attempt, len(texts)) for _, attempt, texts in fake.calls] == [(1, 10), (2, 10)]
    assert orch.limiter_events == [("rate_limited", 4)]  # one reaction: 8 -> 4
    assert len(RescheduleRecorder.whens) == 10 and len(set(RescheduleRecorder.whens)) == 1
    db = with_db(ws)
    try:
        assert raw_rows(db, "SELECT provider_calls, rate_limited_count FROM runs") == [(2, 1)]
    finally:
        close_database(db)
    # the 429 is counted in the attempt number (2) but not in the stored retry budget
    assert all(u.retry_count == 0 for u in overlay_units(ws).values())


class AbortOnFirstComplete(CountingOverlayRepository):
    """Second Ctrl+C (abort) arrives while the first unit of a paid batch is being stored."""

    trigger: Any = None

    def complete(self, job_id: str, unit_id: int, result: Any) -> Result[bool]:
        if self.complete_calls == 0 and AbortOnFirstComplete.trigger is not None:
            AbortOnFirstComplete.trigger()
            time.sleep(0.05)  # let the event loop cancel the worker
        return super().complete(job_id, unit_id, result)


async def test_a_translated_batch_is_stored_completely_before_a_cancel_is_honoured(
    tmp_path: Path, ten_unit_pdf: Path
) -> None:
    ws = pdf_workspace(tmp_path, ten_unit_pdf)
    fake = FakeTranslator()
    orch, _ = build(ws, fake, overlay_repository_factory=AbortOnFirstComplete)
    loop = asyncio.get_running_loop()

    def trigger() -> None:
        loop.call_soon_threadsafe(orch.cancel.set)
        loop.call_soon_threadsafe(orch.abort.set)

    AbortOnFirstComplete.trigger = trigger
    try:
        summary = unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY))
    finally:
        AbortOnFirstComplete.trigger = None

    assert summary.exit_code == EXIT_INTERRUPTED and len(fake.calls) == 1
    units = overlay_units(ws)
    assert all(u.status is ChunkStatus.COMPLETED for u in units.values())  # none released
    second = FakeTranslator()
    resumed, _ = build(ws, second, run_id="run000000002")
    done = unwrap(await resumed.translate(ws.input_pdf, mode=MODE_OVERLAY))
    assert done.exit_code == EXIT_SUCCESS and second.calls == []  # nothing is paid twice


# -- CR-45: crash between replace_all and the binding patch ------------------ #


async def test_units_without_a_binding_are_replaced_instead_of_locking_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = pdf_workspace(tmp_path)
    real_update = JobRepository.update_job
    crashed: List[bool] = []

    def crashing_update(self: JobRepository, job_id: str, patch: JobPatch) -> Any:
        if "overlay_sha256" in patch.values() and not crashed:
            crashed.append(True)
            return err(ErrorCode.INTERNAL, "simulated crash", ErrorScope.JOB_FATAL)
        return real_update(self, job_id, patch)

    monkeypatch.setattr(JobRepository, "update_job", crashing_update)
    first = await build(ws, FakeTranslator())[0].translate(ws.input_pdf, mode=MODE_OVERLAY)
    assert isinstance(first, Err) and crashed == [True]
    row = job_row(ws)
    assert row["overlay_status"] == "NONE" and row["overlay_sha256"] is None
    assert len(overlay_units(ws)) == 43  # rows present, binding missing

    fake = FakeTranslator()
    resumed, _ = build(ws, fake, run_id="run000000002")
    summary = unwrap(await resumed.translate(ws.input_pdf, mode=MODE_OVERLAY))  # no --fresh
    assert summary.exit_code == EXIT_SUCCESS and summary.counts.completed == 43
    assert "overlay:unbound_units_replaced" in summary.warnings
    assert len(job_row(ws)["overlay_sha256"]) == 64
    assert not (ws.output / "state-archive").exists()


# -- CR-46: outputs of an archived pass --------------------------------------- #


async def test_translate_fresh_archives_the_overlay_outputs_and_status_ignores_stale_files(
    tmp_path: Path, fake_renderer: type
) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.run(ws.input_pdf, mode=MODE_OVERLAY, min_chars_per_page=0))
    report = unwrap(orch.status())
    assert report.overlay is not None and report.overlay["review_counts"]["placed"] == 34
    assert report.overlay["output_path"] is not None

    fresh, _ = build(ws, FakeTranslator(), run_id="run000000002")
    unwrap(await fresh.translate(ws.input_pdf, mode=MODE_OVERLAY, fresh=True, dry_run=True))
    (archive,) = list((ws.output / "state-archive").iterdir())
    for name in ("translated_book.overlay.pdf", "overlay_review.json"):
        assert (archive / name).is_file() and not (ws.output / name).exists()
    assert ws.source.exists()  # translate --fresh keeps the stage artifacts (v1)
    report = unwrap(fresh.status())
    assert report.overlay is not None and report.overlay["status"] == "OVERLAY_EXTRACTED"
    assert report.overlay["review_counts"] == {} and report.overlay["output_path"] is None

    # a stale file that was *not* archived (copied back by hand) is still not attributed
    shutil.copyfile(archive / "overlay_review.json", ws.output / "overlay_review.json")
    shutil.copyfile(archive / "translated_book.overlay.pdf",
                    ws.output / "translated_book.overlay.pdf")
    report = unwrap(fresh.status())
    assert report.overlay is not None
    assert report.overlay["review_counts"] == {} and report.overlay["output_path"] is None
    assert report.overlay["stale_outputs"] == [
        "translated_book.overlay.pdf", "overlay_review.json"
    ]
    # a review file without a binding (older version) is judged by its age
    legacy = ws.output / "overlay_review.json"
    legacy.write_text(json.dumps({"version": 1, "counts": {"placed": 7}}), encoding="utf-8")
    os.utime(legacy, (946684800, 946684800))  # 2000-01-01, older than the job row
    report = unwrap(fresh.status())
    assert report.overlay is not None and report.overlay["review_counts"] == {}


# -- CR-51 + Q21: placement counts and exit codes ----------------------------- #


async def test_summary_json_carries_the_placement_result_after_run_and_export(
    tmp_path: Path, fake_renderer: type
) -> None:
    fake_renderer.counts = {"placed": 30, "shrunk_below_threshold": 2}
    fake_renderer.review = (
        (1, "p001-b004", 4, "shrunk_below_threshold", 0.61),
        (2, "p002-b009", 21, "fragment", None),
    )
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    translated = unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY))
    assert translated.overlay is not None and translated.overlay.review_path is None
    before = json.loads((ws.output / "summary.json").read_text("utf-8"))
    assert before["overlay"]["placed"] == 0 and before["outputs"] == {}

    outcome = unwrap(build(ws, FakeTranslator(), run_id="run000000002")[0].export())
    # Q21: a non-empty review list with every block placed is exit 0
    assert outcome.exit_code == EXIT_SUCCESS and outcome.overlay_review_entries == 2
    assert outcome.overlay is not None and outcome.overlay.placed == 30
    after = json.loads((ws.output / "summary.json").read_text("utf-8"))
    assert after["overlay"]["placed"] == 30 and after["overlay"]["shrunk_below_threshold"] == 2
    assert after["overlay"]["kept_original"] == 9 and after["overlay"]["could_not_fit"] == 0
    assert after["overlay"]["status"] == "OVERLAY_EXPORTED"
    assert after["overlay"]["review_path"] == str(ws.output / "overlay_review.json")
    assert after["outputs"] == {"pdf-overlay": str(ws.output / "translated_book.overlay.pdf")}
    assert after["run_id"] == before["run_id"]  # the translate summary is kept otherwise

    (tmp_path / "composite").mkdir()
    ws2 = pdf_workspace(tmp_path / "composite")
    summary = unwrap(
        await build(ws2, FakeTranslator())[0].run(
            ws2.input_pdf, mode=MODE_OVERLAY, min_chars_per_page=0
        )
    )
    assert summary.overlay is not None and summary.overlay.placed == 30
    assert summary.overlay.status == "OVERLAY_EXPORTED"
    written = json.loads((ws2.output / "summary.json").read_text("utf-8"))
    assert written["overlay"]["placed"] == 30 and written["overlay"]["review_path"]


@pytest.mark.parametrize(
    ("counts", "pages_skipped", "expected", "status"),
    [
        ({}, (), EXIT_SUCCESS, "OVERLAY_EXPORTED"),
        ({"could_not_fit": 1, "placed": 33}, (), EXIT_PARTIAL, "OVERLAY_TRANSLATED"),
        ({}, (2,), EXIT_PARTIAL, "OVERLAY_TRANSLATED"),
    ],
)
async def test_q21_overlay_export_exit_codes(
    tmp_path: Path, fake_renderer: type, counts: Dict[str, int],
    pages_skipped: Tuple[int, ...], expected: int, status: str,
) -> None:
    fake_renderer.counts = counts
    fake_renderer.pages_skipped = pages_skipped
    fake_renderer.review = ((1, "p001-b004", 4, "fragment", None),)
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    summary = unwrap(await orch.run(ws.input_pdf, mode=MODE_OVERLAY, min_chars_per_page=0))
    assert summary.exit_code == expected
    assert job_row(ws)["overlay_status"] == status
    assert run_rows(ws) == [("run", "SUCCESS" if expected == 0 else "PARTIAL", expected)]


async def test_q21_allow_partial_overlay_export_is_exit_2(
    tmp_path: Path, fake_renderer: type
) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY, limit=4))
    outcome = unwrap(
        build(ws, FakeTranslator(), run_id="run000000002")[0].export(
            formats=["pdf-overlay"], allow_partial=True
        )
    )
    assert outcome.exit_code == EXIT_PARTIAL
    assert fake_renderer.calls[-1]["options"].allow_partial is True
    assert job_row(ws)["overlay_status"] != "OVERLAY_EXPORTED"


async def test_uniform_scale_reaches_the_renderer_when_it_knows_the_option(
    tmp_path: Path, fake_renderer: type
) -> None:
    import dataclasses as dc

    from book_translator.exporters.pdf_overlay_exporter import OverlayRenderOptions

    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY))
    unwrap(orch.export(overlay_uniform_scale=False))
    options = fake_renderer.calls[-1]["options"]
    if "uniform_scale" in {f.name for f in dc.fields(OverlayRenderOptions)}:
        assert options.uniform_scale is False
        unwrap(build(ws, FakeTranslator(), run_id="run000000002")[0].export())
        assert fake_renderer.calls[-1]["options"].uniform_scale is True  # default on
    else:  # renderer revision without the field: the export still works
        assert not hasattr(options, "uniform_scale")


# -- CR-55 / CR-56 / CR-62: images/ is never touched by a refused extract ------ #


def test_a_refused_extract_leaves_the_existing_figure_files_untouched(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    unwrap(extractor_build(ws, FigureFakeExtractor(FIGURE_DOC)).extract(ws.input_pdf))
    png = ws.output / "images" / "p001-f01.png"
    png.write_bytes(b"the PNG of the first extraction")  # distinguishable from the fake's
    inventory = (ws.output / "figures.json").read_bytes()
    db = with_db(ws)
    try:
        (job_id,) = raw_rows(db, "SELECT id FROM jobs")[0]
        repo = ChunkRepository(db, "seed", None)
        unwrap(repo.replace_all(job_id, [make_chunk(1)]))
        from book_translator.database.session import utcnow

        unwrap(repo.claim_pending(job_id, 1, utcnow(), "seed"))
        unwrap(repo.complete(job_id, 1, TranslationResult(
            1, "OLD\n\n", "fake", 10, 10, 1, 1, GlossaryStrategy.NONE, (), False)))
    finally:
        close_database(db)

    changed = FigureFakeExtractor(FIGURE_DOC + "A paragraph a newer extractor found.\n")
    refused = extractor_build(ws, changed, "run000000002").extract(ws.input_pdf)

    assert isinstance(refused, Err) and refused.error.code is ErrorCode.STATE_HASH_MISMATCH
    assert changed.sinks_seen == 1  # the figures *were* rendered ...
    assert png.read_bytes() == b"the PNG of the first extraction"  # ... but never promoted
    assert (ws.output / "figures.json").read_bytes() == inventory
    assert not list(ws.output.glob("images.tmp-*"))

    german = FigureFakeExtractor(
        "# Teil 1\n\n" + "Der Drache flog über die Stadt und die Leute sahen von den Mauern "
        "der Burg zu während der Wind am Abend kalt war. " * 6 + "\n"
    )
    refused = extractor_build(ws, german, "run000000003").extract(ws.input_pdf)
    assert isinstance(refused, Err) and png.read_bytes() == b"the PNG of the first extraction"
    assert not list(ws.output.glob("images.tmp-*"))


async def test_export_reports_orphan_files_in_images(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    extractor = FigureFakeExtractor(FIGURE_DOC)
    orch, _ = build(
        ws, FakeTranslator(), settings=make_settings(chunk_min=100, chunk_max=400),
        extractor_factory=lambda name: Ok(extractor),
    )
    unwrap(await orch.run(ws.input_pdf, formats=["md"], min_count=2))
    (ws.output / "images" / "leftover.png").write_bytes(b"x")
    outcome = unwrap(build(ws, FakeTranslator(), run_id="run000000002")[0].export(formats=["md"]))
    assert [w for w in outcome.warnings if w.startswith("figure_orphan:")] == [
        "figure_orphan:leftover.png"
    ]
    assert (ws.output / "images" / "leftover.png").exists() and outcome.exit_code == EXIT_SUCCESS


# -- CR-57: translated figures (scripted figure_overlay) ---------------------- #


async def test_a_truncated_or_failed_figure_keeps_its_legend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from book_translator.exporters import figure_overlay

    ws, orch = await _translated_figure_workspace(tmp_path)
    english = (ws.output / "images" / "p002-f01.png").read_bytes()
    seen: List[int] = []

    # CR-87: the orchestrator renders a whole document in one batch call, so the stubs
    # replace ``render_translated_figures`` and return one outcome per job.
    calls: List[int] = []

    def truncating(source_pdf: Path, jobs: Any, font: Any, **kwargs: Any) -> Any:
        calls.append(len(jobs))
        outcomes = []
        for job in jobs:
            seen.append(len(job.replacements))
            flags = tuple(index == 0 for index in range(len(job.replacements)))
            outcomes.append(Ok((PNG, figure_overlay.FigureRenderStats(
                scales=tuple(1.0 for _ in job.replacements), truncated=flags))))
        return Ok(outcomes)

    monkeypatch.setattr(figure_overlay, "render_translated_figures", truncating)
    outcome = unwrap(orch.export(formats=["md"]))
    assert seen == [15] and "figure_label_truncated:1:1" in outcome.warnings
    assert calls == [1]  # CR-87: the source is opened once for the document, not per figure
    assert "figure_labels_not_painted:1" in outcome.warnings
    markdown = (ws.output / "translated_book.md").read_text("utf-8")
    assert "images/p002-f01.tr.png" in markdown and "2D (what?) — TR:2D (what?)" in markdown
    assert (ws.output / "images" / "p002-f01.tr.png").read_bytes() == PNG
    assert (ws.output / "images" / "p002-f01.png").read_bytes() == english

    def clean(source_pdf: Path, jobs: Any, font: Any, **kwargs: Any) -> Any:
        return Ok([
            Ok((PNG, figure_overlay.FigureRenderStats(
                scales=tuple(1.0 for _ in job.replacements),
                truncated=tuple(False for _ in job.replacements))))
            for job in jobs
        ])

    monkeypatch.setattr(figure_overlay, "render_translated_figures", clean)
    outcome = unwrap(orch.export(formats=["md"]))
    assert not any(w.startswith("figure_label") for w in outcome.warnings)
    markdown = (ws.output / "translated_book.md").read_text("utf-8")
    assert "images/p002-f01.tr.png" in markdown and "<!-- legend:" not in markdown

    def failing(source_pdf: Path, jobs: Any, font: Any, **kwargs: Any) -> Any:
        # a per-job failure: the batch itself is fine, this one figure is not
        return Ok([
            err(ErrorCode.EXPORT_FAILED, "scripted failure", ErrorScope.JOB_FATAL)
            for _ in jobs
        ])

    monkeypatch.setattr(figure_overlay, "render_translated_figures", failing)
    outcome = unwrap(orch.export(formats=["md"]))
    assert "figure_translate_failed:p002-f01.png:scripted failure" in outcome.warnings
    assert "figure_labels_not_painted:1" in outcome.warnings
    markdown = (ws.output / "translated_book.md").read_text("utf-8")
    assert "2D (what?) — TR:2D (what?)" in markdown
    # the English original again, not the .tr.png of the earlier export
    assert "images/p002-f01.png" in markdown and "p002-f01.tr.png" not in markdown


# -- CR-63 / CR-64: USER errors of the export stage --------------------------- #


class UserErrorPdfExporter(CapturingPdfExporter):
    optional = True

    def export(self, document: Any, target_dir: Path, options: ExportOptions) -> Any:
        return err(ErrorCode.EXPORT_FAILED, "unknown PDF page size 'Foo'", ErrorScope.USER)


class BrokenPdfExporter(CapturingPdfExporter):
    optional = True

    def export(self, document: Any, target_dir: Path, options: ExportOptions) -> Any:
        return err(ErrorCode.EXPORT_FAILED, "layout made no progress", ErrorScope.JOB_FATAL)


async def test_a_user_error_of_an_optional_exporter_is_exit_1_not_2(tmp_path: Path) -> None:
    md, chunks = paragraphs(2)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1, 2])

    def factory(exporter: Any) -> Any:
        return lambda name: Ok(exporter) if name == "pdf" else get_exporter(name)

    refused = build(ws, FakeTranslator(), exporter_factory=factory(UserErrorPdfExporter()))[
        0
    ].export(formats=["pdf"])
    assert isinstance(refused, Err) and exit_code_for(refused.error) == EXIT_FAILURE
    assert "Foo" in refused.error.message

    downgraded = unwrap(
        build(ws, FakeTranslator(), run_id="run000000002",
              exporter_factory=factory(BrokenPdfExporter()))[0].export(formats=["md", "pdf"])
    )
    assert downgraded.exit_code == EXIT_PARTIAL and list(downgraded.outputs) == ["markdown"]
    assert any(w.startswith("export_failed:pdf:") for w in downgraded.warnings)

    # the real page-design check runs before anything is written
    (ws.output / "translated_book.md").unlink()
    bad_page = build(ws, FakeTranslator(), run_id="run000000003")[0].export(
        formats=["md", "pdf"], options=ExportOptions(page_size="Foo")
    )
    assert isinstance(bad_page, Err) and exit_code_for(bad_page.error) == EXIT_FAILURE
    assert bad_page.error.scope is ErrorScope.USER and output_files(ws) == []


async def test_a_failed_format_keeps_the_formats_already_on_disk(tmp_path: Path) -> None:
    """CR-91: the loop finishes; a format that failed never deletes the report of the ones
    that were written. Exit 1 (the USER error dominates, CR-63/D13) with the outputs listed."""
    md, chunks = paragraphs(2)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks, completed=[1, 2])
    (ws.output / "summary.json").write_text(
        json.dumps({"job_id": job_row(ws)["id"], "outputs": {}}), encoding="utf-8"
    )

    def factory(exporter: Any) -> Any:
        return lambda name: Ok(exporter) if name == "pdf" else get_exporter(name)

    outcome = unwrap(
        build(ws, FakeTranslator(), exporter_factory=factory(UserErrorPdfExporter()))[0].export(
            formats=["pdf", "md"]  # the failing format runs first
        )
    )
    assert outcome.exit_code == EXIT_FAILURE  # a USER error is exit 1, not 2 (CR-63)
    assert list(outcome.outputs) == ["markdown"]
    assert (ws.output / "translated_book.md").is_file()
    assert any(w.startswith("export_error:pdf:") and "Foo" in w for w in outcome.warnings)
    assert not any(w.startswith("export_failed:pdf:") for w in outcome.warnings)
    written = json.loads((ws.output / "summary.json").read_text("utf-8"))
    assert list(written["outputs"]) == ["markdown"]  # CR-51 merge still happens
    assert run_rows(ws)[-1] == ("export", "FAILED", EXIT_FAILURE)

    # nothing on disk: the error itself is the answer, exactly as before the fix
    alone = build(ws, FakeTranslator(), run_id="run000000002",
                  exporter_factory=factory(UserErrorPdfExporter()))[0].export(formats=["pdf"])
    assert isinstance(alone, Err) and exit_code_for(alone.error) == EXIT_FAILURE


async def test_pdf_font_is_validated_before_any_artefact_of_either_group(
    tmp_path: Path, fake_renderer: type
) -> None:
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator(), settings=make_settings(chunk_min=1000, chunk_max=1500))
    unwrap(orch.extract(ws.input_pdf, min_chars_per_page=0))
    unwrap(await orch.translate(ws.input_pdf))
    unwrap(
        await build(ws, FakeTranslator(), run_id="run000000002")[0].translate(
            ws.input_pdf, mode=MODE_OVERLAY
        )
    )
    bad_font = tmp_path / "bad.ttf"
    bad_font.write_bytes(b"this is not a font file")
    refused = build(ws, FakeTranslator(), run_id="run000000003")[0].export(
        formats=["md", "pdf-overlay"], options=ExportOptions(font_path=bad_font)
    )
    assert isinstance(refused, Err) and exit_code_for(refused.error) == EXIT_FAILURE
    assert output_files(ws) == [] and fake_renderer.calls == []
    assert not list((ws.output / "images").glob("*.tr.png"))


# -- CR-48: observability ----------------------------------------------------- #


async def test_overlay_log_events_name_their_own_unit(
    tmp_path: Path, fake_renderer: type
) -> None:
    fake_renderer.review = (
        (1, "p001-b004", 4, "shrunk_below_threshold", 0.61),
        (3, "p003-b002", 33, "residual_glyphs", None),
    )
    ws = pdf_workspace(tmp_path)
    orch, _ = build(ws, FakeTranslator())
    with collect_events() as events:
        unwrap(await orch.run(ws.input_pdf, mode=MODE_OVERLAY, min_chars_per_page=0))

    def named(event: str) -> List[logging.LogRecord]:
        return [r for r in events.records if getattr(r, "event", None) == event]

    # (a) every completion event carries the id of its own unit, not of the batch
    ids = [getattr(record, "chunk_id", None) for record in named("chunk_completed")]
    assert sorted(i for i in ids if i is not None) == list(range(1, 44)), ids
    # (d) one overlay_extracted per extraction
    assert len(named("overlay_extracted")) == 1 and len(named("overlay_units_stored")) == 1
    # (b) the placement outcome reaches the log; never the text
    assert len(named("overlay_block_placed")) == 1
    (review,) = named("overlay_block_review")
    assert getattr(review, "chunk_id", None) == 4
    assert "p001-b004" in review.getMessage() and "0.61" in review.getMessage()
    (residual,) = named("overlay_residual")
    assert "residual_glyphs" in residual.getMessage()
    assert not any("SECRET" in r.getMessage() or "GIZLI" in r.getMessage()
                   for r in events.records)
    # (c) the renderer got a progress callback and the last page is reported
    assert fake_renderer.calls[-1]["progress"] is True
    assert [r.getMessage() for r in named("overlay_page_progress")] == [
        "overlay export: page 3/3"
    ]


# -- CR-49: the lease stays alive during long synchronous stages --------------- #


def _stolen(ws: Workspace, stale_after_s: int) -> bool:
    """Could another process take the lease right now?"""
    db = with_db(ws)
    try:
        (job_id,) = raw_rows(db, "SELECT id FROM jobs")[0]
        thief = LeaseHolder(pid=os.getpid() + 1, host="other-host", run_id="thief0000001")
        return unwrap(LeaseRepository(db).acquire(job_id, thief, stale_after_s))
    finally:
        close_database(db)


class SlowExtractor(FakeExtractor):
    def __init__(self, markdown: str, ws: Workspace, seconds: float) -> None:
        super().__init__(markdown)
        self.ws = ws
        self.seconds = seconds
        self.stolen: Optional[bool] = None

    def extract(self, pdf_path: Path, options: ExtractOptions, progress: Any = None,
                figure_sink: Any = None) -> Result[ExtractedDocument]:
        time.sleep(self.seconds)
        self.stolen = _stolen(self.ws, 1)
        return super().extract(pdf_path, options, progress, figure_sink)


def test_the_lease_is_kept_alive_while_a_slow_extraction_runs(tmp_path: Path) -> None:
    ws = make_workspace(tmp_path, None)
    seed_job_only(ws)
    extractor = SlowExtractor(f"# Part 1\n\n{ENGLISH * 2}\n", ws, seconds=1.6)
    orch, _ = build(
        ws, FakeTranslator(), extractor_factory=lambda name: Ok(extractor),
        heartbeat_interval_s=0.1,
    )
    unwrap(orch.extract(ws.input_pdf))
    assert extractor.stolen is False  # 1.6 s of work; a 1 s staleness window finds it fresh
    assert not [t for t in threading.enumerate() if t.name == "bt-lease-keepalive"]

    lazy = SlowExtractor(f"# Part 1\n\n{ENGLISH * 2}\n", ws, seconds=1.6)
    orch, _ = build(
        ws, FakeTranslator(), run_id="run000000002",
        extractor_factory=lambda name: Ok(lazy), heartbeat_interval_s=30.0,
    )
    orch.extract(ws.input_pdf)
    assert lazy.stolen is True  # control: without beats the lease would have gone stale


async def test_the_lease_is_kept_alive_during_the_overlay_pre_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = pdf_workspace(tmp_path)
    seed_job_only(ws)
    real = orchestrator_module.extract_overlay
    observed: List[bool] = []

    def slow_extract_overlay(*args: Any, **kwargs: Any) -> Any:
        time.sleep(1.6)
        observed.append(_stolen(ws, 1))
        return real(*args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "extract_overlay", slow_extract_overlay)
    orch, _ = build(ws, FakeTranslator(), heartbeat_interval_s=0.1)
    summary = unwrap(await orch.translate(ws.input_pdf, mode=MODE_OVERLAY, dry_run=True))
    assert observed == [False] and summary.counts.total == 43


# -- CR-70: the duplicated overlay constants must not drift -------------------- #


def test_overlay_constants_of_domain_and_database_are_identical() -> None:
    from typing import get_args

    from book_translator.database import models as db_models
    from book_translator.domain import overlay as domain_overlay

    assert tuple(db_models.OVERLAY_KEEP_REASONS) == tuple(domain_overlay.OVERLAY_KEEP_REASONS)
    assert tuple(db_models.OVERLAY_PAGE_SKIP_REASONS) == tuple(
        domain_overlay.OVERLAY_PAGE_SKIP_REASONS
    )
    assert tuple(db_models.OVERLAY_JOB_STATUSES) == tuple(domain_overlay.OVERLAY_JOB_STATUSES)
    assert set(db_models.OVERLAY_BLOCK_KINDS) == {k.value for k in domain_overlay.OverlayBlockKind}
    assert set(db_models.OVERLAY_ALIGNMENTS) == {
        a.value for a in domain_overlay.OverlayAlignment
    }
    assert set(db_models.OVERLAY_FAMILIES) == set(get_args(domain_overlay.OverlayFamily))
    # the literals the orchestrator writes are members of the job status list
    for status in ("OVERLAY_EXTRACTED", "OVERLAY_TRANSLATING", "OVERLAY_PAUSED",
                   "OVERLAY_TRANSLATED", "OVERLAY_EXPORTED", "NONE"):
        assert status in db_models.OVERLAY_JOB_STATUSES


# --------------------------------------------------------------------------- #
# Automatic provider fallback on an exhausted quota (--fallback-provider)
# --------------------------------------------------------------------------- #


def pair_factory(
    providers: Dict[str, BaseTranslator]
) -> Callable[[str, Any], Result[BaseTranslator]]:
    """``get_translator``-compatible factory over a name -> translator table."""

    def factory(name: str, settings: Any) -> Result[BaseTranslator]:
        found = providers.get(name)
        if found is None:
            return err(
                ErrorCode.PROVIDER_CONFIG, f"unknown provider {name!r}", ErrorScope.USER
            )
        return Ok(found)

    return factory


def build_pair(
    ws: Workspace,
    providers: Dict[str, BaseTranslator],
    **kwargs: Any,
) -> Orchestrator:
    clock = FakeClock()
    return Orchestrator(
        kwargs.pop("settings", None) or make_settings(concurrency=1),
        ws.output,
        tool_version=TOOL_VERSION,
        run_id=kwargs.pop("run_id", "run000000001"),
        translator_factory=pair_factory(providers),
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )


def named(translator: FakeTranslator, name: str) -> FakeTranslator:
    translator.name = name
    return translator


def provider_column(ws: Workspace) -> Dict[int, Optional[str]]:
    db = with_db(ws)
    try:
        rows = raw_rows(db, "SELECT id, provider FROM chunks ORDER BY id")
        return {int(chunk_id): provider for chunk_id, provider in rows}
    finally:
        close_database(db)


async def test_quota_falls_back_and_records_the_real_provider(tmp_path: Path) -> None:
    """The user decision: do not stop mid-book on a quota, switch and leave a warning."""
    md, chunks = paragraphs(5)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    primary = named(FakeTranslator({3: ["err_456"]}, strategy=GlossaryStrategy.NATIVE), "deepl")
    secondary = named(FakeTranslator(), "google")
    orch = build_pair(ws, {"deepl": primary, "google": secondary})

    summary = unwrap(
        await orch.translate(ws.input_pdf, provider="deepl", fallback_provider="google")
    )

    assert summary.exit_code == EXIT_SUCCESS
    assert summary.counts.completed == 5
    # the job identity is still the primary: arming a fallback is not a provider switch
    assert summary.provider == "deepl" and job_row(ws)["provider"] == "deepl"
    # chunks 1-2 were paid for at DeepL, 3-5 came from Google
    assert provider_column(ws) == {1: "deepl", 2: "deepl", 3: "google", 4: "google", 5: "google"}
    assert summary.provider_units == {"deepl": 2, "google": 3}
    rows = chunk_rows(ws)
    assert rows[2].warnings == ()
    assert set(rows[3].warnings) == {
        "provider_fallback:deepl->google",
        "glossary_unavailable:google",
    }
    assert set(rows[5].warnings) == set(rows[3].warnings)
    assert all(c.status is ChunkStatus.COMPLETED for c in rows.values())
    # the primary was asked exactly once more than it could serve, and never again
    assert [c[0] for c in primary.calls] == [1, 2, 3]
    assert [c[0] for c in secondary.calls] == [3, 4, 5]
    assert json.loads(ws.output.joinpath("summary.json").read_text("utf-8"))[
        "provider_units"
    ] == {"deepl": 2, "google": 3}


async def test_fallback_units_survive_into_status(tmp_path: Path) -> None:
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    primary = named(FakeTranslator({2: ["err_456"]}), "deepl")
    orch = build_pair(ws, {"deepl": primary, "google": named(FakeTranslator(), "google")})

    unwrap(await orch.translate(ws.input_pdf, provider="deepl", fallback_provider="google"))
    report = unwrap(orch.status())

    assert report.provider_units == {"deepl": 1, "google": 2}


@pytest.mark.parametrize("outcome", ["err_403", "err_400", "err_5xx"])
async def test_non_quota_errors_never_trigger_the_fallback(
    tmp_path: Path, outcome: str
) -> None:
    """Auth, bad request and network errors keep their existing meaning."""
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    primary = named(FakeTranslator({1: [outcome] * 8}), "deepl")
    secondary = named(FakeTranslator(), "google")
    orch = build_pair(
        ws,
        {"deepl": primary, "google": secondary},
        settings=make_settings(concurrency=1, max_retries=1),
    )

    summary = unwrap(
        await orch.translate(ws.input_pdf, provider="deepl", fallback_provider="google")
    )

    assert summary.exit_code in (EXIT_PAUSED, EXIT_PARTIAL)
    assert not secondary.calls
    assert all(row == "deepl" or row is None for row in provider_column(ws).values())


async def test_fallback_provider_is_built_before_the_run_starts(tmp_path: Path) -> None:
    """Finding out the fallback is unusable at the moment the quota dies is too late."""
    md, chunks = paragraphs(2)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    primary = named(FakeTranslator(), "deepl")
    orch = build_pair(ws, {"deepl": primary})

    result = await orch.translate(ws.input_pdf, provider="deepl", fallback_provider="google")

    assert isinstance(result, Err)
    assert result.error.scope is ErrorScope.USER
    assert "--fallback-provider google" in result.error.message
    assert not primary.calls  # nothing was sent


async def test_fallback_is_off_unless_it_is_asked_for(tmp_path: Path) -> None:
    md, chunks = paragraphs(3)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    primary = named(FakeTranslator({1: ["err_456"]}), "deepl")
    secondary = named(FakeTranslator(), "google")
    orch = build_pair(ws, {"deepl": primary, "google": secondary})

    summary = unwrap(await orch.translate(ws.input_pdf, provider="deepl"))

    assert summary.exit_code == EXIT_PAUSED  # v1 behaviour, unchanged
    assert not secondary.calls
    assert summary.provider_units == {}


async def test_a_fallback_run_resumes_on_the_primary(tmp_path: Path) -> None:
    """The switch lives in the run, not in the state: a later run tries DeepL again
    (which is the point - the user has topped the quota up by then)."""
    md, chunks = paragraphs(4)
    ws = make_workspace(tmp_path, md)
    seed(ws, chunks)
    first = named(FakeTranslator({2: ["err_456"]}), "deepl")
    orch = build_pair(ws, {"deepl": first, "google": named(FakeTranslator(), "google")})
    unwrap(await orch.translate(ws.input_pdf, provider="deepl", fallback_provider="google"))

    ws.source.write_text(md, encoding="utf-8")
    second = named(FakeTranslator(), "deepl")
    again = build_pair(
        ws,
        {"deepl": second, "google": named(FakeTranslator(), "google")},
        run_id="run000000002",
    )
    summary = unwrap(
        await again.translate(
            ws.input_pdf, provider="deepl", fallback_provider="google", retry_failed=True
        )
    )

    assert not second.calls  # everything was already completed, nothing re-sent
    assert summary.provider_units == {"deepl": 1, "google": 3}  # read back from the column
