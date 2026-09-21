"""Acceptance checks 1-18 of the DB design (docs/03_db_design.md, section 10)."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from sqlalchemy import Connection, func, literal_column, select, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session

from book_translator.database import models as m
from book_translator.database.repository import (
    ChunkRepository,
    JobPatch,
    JobRecord,
    JobRepository,
    JobSpec,
    LeaseHolder,
    LeaseRepository,
    RunOutcome,
    claim_select,
)
from book_translator.database.session import (
    Database,
    archive_database,
    close_database,
    open_database,
    utcnow,
)
from book_translator.domain.models import (
    ChunkKind,
    ChunkStatus,
    GlossaryStrategy,
    TranslationResult,
)
from book_translator.domain.result import AppError, Err, ErrorCode, ErrorScope, Ok, unwrap
from tests.conftest import (
    GLOSSARY_HASH,
    RUN_ID,
    TOOL_VERSION,
    capture_statements,
    make_chunk,
    make_chunks,
    raw_row_dict,
    raw_rows,
)


def _result(chunk_id: int, text: str = "Çeviri metni.\n\n", **overrides: Any) -> TranslationResult:
    base: Dict[str, Any] = dict(
        chunk_id=chunk_id,
        translated_text=text,
        provider="deepl",
        chars_sent=len(text),
        chars_billed=len(text) + 1,
        latency_ms=120,
        attempts=1,
        glossary_strategy=GlossaryStrategy.NATIVE,
        warnings=(),
        review_flag=False,
    )
    base.update(overrides)
    return TranslationResult(**base)


def _retryable(message: str = "429 too many requests") -> AppError:
    return AppError(
        code=ErrorCode.PROVIDER_RATE_LIMITED, message=message, scope=ErrorScope.CHUNK_RETRYABLE
    )


def _fatal(message: str = "400 bad request") -> AppError:
    return AppError(
        code=ErrorCode.PROVIDER_BAD_REQUEST, message=message, scope=ErrorScope.CHUNK_FATAL
    )


def _claim(chunks: ChunkRepository, job_id: str, limit: int) -> List[int]:
    return [
        c.chunk_id for c in unwrap(chunks.claim_pending(job_id, limit, utcnow(), chunks.run_id))
    ]


def _tick() -> None:
    """Windows' datetime.now() has ~15 ms granularity; ensure the next utcnow() differs."""
    time.sleep(0.02)


def _status_map(db: Database) -> Dict[int, str]:
    return {int(i): str(s) for i, s in raw_rows(db, "SELECT id, status FROM chunks ORDER BY id")}


# --------------------------------------------------------------------------- #
# 1. Schema
# --------------------------------------------------------------------------- #


def test_01_schema_objects(mem_db: Database) -> None:
    rows = raw_rows(mem_db, "SELECT type, name, sql FROM sqlite_master")
    tables = {name for kind, name, _ in rows if kind == "table"}
    assert tables == set(m.TABLE_NAMES)
    indexes = {name: sql for kind, name, sql in rows if kind == "index" and sql}
    assert set(m.INDEX_NAMES) <= set(indexes)
    assert "WHERE status = 'PENDING'" in indexes["ix_chunks_pending_backoff"]
    assert "WHERE review_flag = 1" in indexes["ix_chunks_review"]
    assert "(job_id, status)" in indexes["ix_chunks_job_status"]

    meta = raw_rows(
        mem_db, "SELECT id, schema_version, created_by_tool_version, created_at FROM schema_meta"
    )
    assert len(meta) == 1
    row_id, version, tool, created = meta[0]
    assert (row_id, version, tool) == (1, m.CURRENT_SCHEMA_VERSION, TOOL_VERSION)  # v2 (doc 07)
    assert len(created) == 27 and created.endswith("Z")
    assert unwrap(JobRepository(mem_db).schema_version()) == m.CURRENT_SCHEMA_VERSION

    chunk_sql = next(sql for kind, name, sql in rows if kind == "table" and name == "chunks")
    for name in (
        "ck_chunks_subsplit",
        "ck_chunks_translated_text_completed",
        "ck_chunks_translatable",
    ):
        assert name in chunk_sql


# --------------------------------------------------------------------------- #
# 2. PRAGMAs
# --------------------------------------------------------------------------- #


def test_02_pragmas_file_db_and_read_only(db_path: Path) -> None:
    missing = open_database(db_path, read_only=True, tool_version=TOOL_VERSION)
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.STATE_NOT_FOUND

    db = unwrap(open_database(db_path, tool_version=TOOL_VERSION))
    try:
        job = unwrap(JobRepository(db).get_or_create_job(JobSpec("in.pdf", "b" * 64, TOOL_VERSION)))
        pragmas = {
            name: raw_rows(db, f"PRAGMA {name}")[0][0]
            for name in (
                "journal_mode",
                "foreign_keys",
                "busy_timeout",
                "synchronous",
                "query_only",
            )
        }
        assert pragmas == {
            "journal_mode": "wal",
            "foreign_keys": 1,
            "busy_timeout": 5000,
            "synchronous": 1,
            "query_only": 0,
        }
    finally:
        close_database(db)

    ro = unwrap(open_database(db_path, read_only=True, tool_version=TOOL_VERSION))
    try:
        assert raw_rows(ro, "PRAGMA query_only")[0][0] == 1
        assert unwrap(JobRepository(ro).get_job(job.id)) is not None
        attempt = JobRepository(ro).update_job(job.id, JobPatch(status="EXTRACTED"))
        assert isinstance(attempt, Err)
        assert attempt.error.code is ErrorCode.INTERNAL
    finally:
        close_database(ro)


# --------------------------------------------------------------------------- #
# 3. Transaction modes
# --------------------------------------------------------------------------- #


def test_03_transaction_modes(mem_db: Database, seeded: JobRecord, chunks: ChunkRepository) -> None:
    jobs = JobRepository(mem_db)
    leases = LeaseRepository(mem_db)
    holder = LeaseHolder(pid=10, host="host-a", run_id=RUN_ID)
    unwrap(jobs.start_run(seeded.id, RUN_ID, "translate", TOOL_VERSION, "deepl", False))
    unwrap(leases.acquire(seeded.id, holder, 60))
    ids = _claim(chunks, seeded.id, 3)
    unwrap(chunks.complete(seeded.id, ids[0], _result(ids[0])))

    writes = {
        "get_or_create_job": lambda: jobs.get_or_create_job(JobSpec("x", "a" * 64, TOOL_VERSION)),
        "update_job": lambda: jobs.update_job(seeded.id, JobPatch(status="TRANSLATING")),
        "start_run": lambda: jobs.start_run(seeded.id, "ffffffffffff", "export", TOOL_VERSION),
        "finish_run": lambda: jobs.finish_run(
            seeded.id, "ffffffffffff", RunOutcome("SUCCESS", 0, 0, 0, 0, 0, 0)
        ),
        "requeue_processing": lambda: chunks.requeue_processing(seeded.id),
        "claim_pending": lambda: chunks.claim_pending(seeded.id, 2, utcnow(), RUN_ID),
        "complete": lambda: chunks.complete(seeded.id, 4, _result(4)),
        "reschedule": lambda: chunks.reschedule(seeded.id, 5, _retryable(), utcnow()),
        "fail": lambda: chunks.fail(seeded.id, 6, _fatal()),
        "release": lambda: chunks.release(seeded.id, [7]),
        "reset_failed": lambda: chunks.reset_failed(seeded.id),
        "replace_all": lambda: chunks.replace_all(seeded.id, make_chunks(3)),
        "lease.acquire": lambda: leases.acquire(seeded.id, holder, 60),
        "lease.heartbeat": lambda: leases.heartbeat(seeded.id, holder),
        "lease.release": lambda: leases.release(seeded.id, holder),
        "lease.force_break": lambda: leases.force_break(seeded.id),
    }
    reads = {
        "schema_version": jobs.schema_version,
        "get_job": lambda: jobs.get_job(seeded.id),
        "get_last_run": lambda: jobs.get_last_run(seeded.id),
        "counts": lambda: chunks.counts(seeded.id),
        "earliest_next_attempt": lambda: chunks.earliest_next_attempt(seeded.id),
        "iter_ordered": lambda: list(chunks.iter_ordered(seeded.id)),
        "totals": lambda: chunks.totals(seeded.id),
        "failed_ids": lambda: chunks.failed_ids(seeded.id),
        "review_ids": lambda: chunks.review_ids(seeded.id),
        "count_completed_with_other_glossary": lambda: chunks.count_completed_with_other_glossary(
            seeded.id, "x"
        ),
        "count_completed_by_run": lambda: chunks.count_completed_by_run(RUN_ID),
        "lease.get": lambda: leases.get(seeded.id),
    }
    for name, call in reads.items():
        with capture_statements(mem_db) as statements:
            call()
        assert statements and statements[0] == "BEGIN", name
        assert "BEGIN IMMEDIATE" not in statements, name
    for name, call in writes.items():
        with capture_statements(mem_db) as statements:
            outcome = call()
        assert statements and statements[0] == "BEGIN IMMEDIATE", (name, statements[:2], outcome)
        assert not any("RETURNING" in s for s in statements), name  # SQLite floor 3.31 (5.3)
        # replace_all was called last on purpose: it refuses (COMPLETED rows exist) but must
        # still have opened an IMMEDIATE transaction first.


# --------------------------------------------------------------------------- #
# 4. claim_pending
# --------------------------------------------------------------------------- #


def test_04_claim_pending(mem_db: Database, seeded: JobRecord, chunks: ChunkRepository) -> None:
    now = utcnow()
    future = now + timedelta(minutes=5)
    past = now - timedelta(minutes=5)
    # chunk 2 waits for backoff, chunk 3 has a past next_attempt_at
    with mem_db.engine.begin() as conn:
        conn.execute(update(m.Chunk).where(m.Chunk.id == 2).values(next_attempt_at=future))
        conn.execute(update(m.Chunk).where(m.Chunk.id == 3).values(next_attempt_at=past))

    assert unwrap(chunks.claim_pending(seeded.id, 0, now, RUN_ID)) == []
    first = unwrap(chunks.claim_pending(seeded.id, 4, now, RUN_ID))
    assert [c.chunk_id for c in first] == [1, 3, 4, 5]
    assert all(c.status is ChunkStatus.PROCESSING and c.next_attempt_at is None for c in first)
    for chunk_id in (1, 3, 4, 5):
        row = raw_row_dict(mem_db, "chunks", "id = ?", (chunk_id,))
        assert row["status"] == "PROCESSING"
        assert row["next_attempt_at"] is None
        assert row["last_run_id"] == RUN_ID
        assert row["claimed_at"] == m.format_timestamp(now)
    second = [c.chunk_id for c in unwrap(chunks.claim_pending(seeded.id, 100, now, RUN_ID))]
    assert second == [6, 7, 8, 9, 10]
    assert not set(second) & {c.chunk_id for c in first}
    assert (
        unwrap(chunks.claim_pending(seeded.id, 5, now, RUN_ID)) == []
    )  # only chunk 2 left, waiting
    assert unwrap(chunks.claim_pending(seeded.id, 5, future, RUN_ID))[0].chunk_id == 2

    sql = str(
        claim_select(seeded.id, now, 4).compile(
            mem_db.engine, compile_kwargs={"literal_binds": True}
        )
    )
    plan = [str(row[3]) for row in raw_rows(mem_db, "EXPLAIN QUERY PLAN " + sql)]
    # DB design section 4 (I3 note, CR-17): with `ORDER BY id LIMIT n` SQLite prefers I2, whose
    # entries are already in rowid order (early termination, no sort); the partial index would
    # need a TEMP B-TREE sort of every pending entry per claim.
    assert any("USING INDEX ix_chunks_job_status" in step for step in plan), plan
    assert not any("ix_chunks_pending_backoff" in step for step in plan), plan
    assert not any("TEMP B-TREE" in step for step in plan), plan
    assert not any(step.startswith("SCAN chunks") for step in plan), plan
    min_sql = str(
        select(func.min(m.Chunk.next_attempt_at))
        .where(
            m.Chunk.job_id == seeded.id,
            m.Chunk.status == literal_column("'PENDING'"),
            m.Chunk.next_attempt_at.is_not(None),
        )
        .compile(mem_db.engine, compile_kwargs={"literal_binds": True})
    )
    min_plan = [str(row[3]) for row in raw_rows(mem_db, "EXPLAIN QUERY PLAN " + min_sql)]
    assert any("ix_chunks_pending_backoff" in step for step in min_plan), min_plan


# --------------------------------------------------------------------------- #
# 5. Claim atomicity across connections
# --------------------------------------------------------------------------- #


def test_05_claim_atomic_across_connections(db_path: Path) -> None:
    db_a = unwrap(open_database(db_path, tool_version=TOOL_VERSION))
    db_b = unwrap(open_database(db_path, tool_version=TOOL_VERSION))
    try:
        job = unwrap(
            JobRepository(db_a).get_or_create_job(JobSpec("in.pdf", "c" * 64, TOOL_VERSION))
        )
        unwrap(ChunkRepository(db_a, "aaaaaaaaaaaa", None).replace_all(job.id, make_chunks(4)))
        repo_b = ChunkRepository(db_b, "bbbbbbbbbbbb", None)
        now = utcnow()
        outcome: Dict[str, Any] = {}

        def claim_on_b() -> None:
            started = time.perf_counter()
            outcome["result"] = repo_b.claim_pending(job.id, 2, now, "bbbbbbbbbbbb")
            outcome["elapsed"] = time.perf_counter() - started

        with db_a.write_session() as session:
            a_ids = list(session.execute(claim_select(job.id, now, 2)).scalars().all())
            assert a_ids == [1, 2]
            worker = threading.Thread(target=claim_on_b)
            worker.start()
            worker.join(0.5)
            assert worker.is_alive(), (
                "engine B must block on BEGIN IMMEDIATE while A holds the lock"
            )
            session.execute(
                update(m.Chunk)
                .where(m.Chunk.id.in_(a_ids), m.Chunk.status == literal_column("'PENDING'"))
                .values(
                    status="PROCESSING", last_run_id="aaaaaaaaaaaa", claimed_at=now, updated_at=now
                )
            )
        worker.join(6)
        assert not worker.is_alive()
        b_ids = [c.chunk_id for c in unwrap(outcome["result"])]
        assert b_ids == [3, 4]
        assert outcome["elapsed"] >= 0.4
    finally:
        close_database(db_a)
        close_database(db_b)


# --------------------------------------------------------------------------- #
# 6. complete
# --------------------------------------------------------------------------- #


def test_06_complete(mem_db: Database, seeded: JobRecord, chunks: ChunkRepository) -> None:
    ids = _claim(chunks, seeded.id, 2)
    assert ids == [1, 2]
    unwrap(chunks.fail(seeded.id, 2, _fatal()))

    assert (
        unwrap(chunks.complete(seeded.id, 1, _result(1, review_flag=True, warnings=("w1",))))
        is True
    )
    row = raw_row_dict(mem_db, "chunks", "id = ?", (1,))
    assert row["status"] == "COMPLETED"
    assert row["translated_text"] == "Çeviri metni.\n\n"
    assert row["last_error"] is None and row["last_error_code"] is None
    assert row["claimed_at"] is None and len(row["completed_at"]) == 27
    assert row["glossary_hash"] == GLOSSARY_HASH and row["provider"] == "deepl"
    assert row["glossary_strategy"] == "native" and row["review_flag"] == 1
    assert row["warnings"] == '["w1"]' and row["last_run_id"] == RUN_ID

    for chunk_id in (1, 2, 3):  # COMPLETED, FAILED, PENDING
        before = raw_row_dict(mem_db, "chunks", "id = ?", (chunk_id,))
        assert unwrap(chunks.complete(seeded.id, chunk_id, _result(chunk_id, "other"))) is False
        assert raw_row_dict(mem_db, "chunks", "id = ?", (chunk_id,)) == before

    with pytest.raises(sa_exc.IntegrityError, match="ck_chunks_translated_text_completed"):
        with mem_db.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE chunks SET status = 'COMPLETED' WHERE id = 3")
    with pytest.raises(sa_exc.IntegrityError, match="ck_chunks_translated_text_completed"):
        with mem_db.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE chunks SET translated_text = 'x' WHERE id = 3")


# --------------------------------------------------------------------------- #
# 7. reschedule
# --------------------------------------------------------------------------- #


def test_07_reschedule(mem_db: Database, seeded: JobRecord, chunks: ChunkRepository) -> None:
    assert unwrap(chunks.reschedule(seeded.id, 1, _retryable(), utcnow())) is False  # PENDING
    _claim(chunks, seeded.id, 1)
    when = datetime(2030, 1, 2, 3, 4, 5, 678901, tzinfo=timezone(timedelta(hours=3)))
    assert unwrap(chunks.reschedule(seeded.id, 1, _retryable("rate limited"), when)) is True
    row = raw_row_dict(mem_db, "chunks", "id = ?", (1,))
    assert row["status"] == "PENDING" and row["retry_count"] == 1 and row["claimed_at"] is None
    assert row["last_error_code"] == "provider_rate_limited" and row["last_error"] == "rate limited"
    assert row["next_attempt_at"] == "2030-01-02T00:04:05.678901Z"
    assert len(row["next_attempt_at"]) == 27
    stored = unwrap(chunks.earliest_next_attempt(seeded.id))
    assert stored == when and stored is not None and stored.tzinfo is not None
    assert stored.utcoffset() == timedelta(0)

    _claim(chunks, seeded.id, 1)  # claims chunk 2
    naive = chunks.reschedule(seeded.id, 2, _retryable(), datetime(2030, 1, 1))
    assert isinstance(naive, Err) and naive.error.code is ErrorCode.INTERNAL
    assert raw_row_dict(mem_db, "chunks", "id = ?", (2,))["status"] == "PROCESSING"
    with pytest.raises(ValueError):
        m.format_timestamp(datetime(2030, 1, 1))


# --------------------------------------------------------------------------- #
# 8. fail
# --------------------------------------------------------------------------- #


def test_08_fail(mem_db: Database, seeded: JobRecord, chunks: ChunkRepository) -> None:
    assert unwrap(chunks.fail(seeded.id, 1, _fatal())) is False
    _claim(chunks, seeded.id, 2)
    unwrap(chunks.reschedule(seeded.id, 1, _retryable(), utcnow()))  # retry_count -> 1
    assert _claim(chunks, seeded.id, 1) == [1]
    long_message = "x" * 5000
    assert unwrap(chunks.fail(seeded.id, 1, _fatal(long_message))) is True
    row = raw_row_dict(mem_db, "chunks", "id = ?", (1,))
    assert row["status"] == "FAILED" and row["retry_count"] == 1
    assert row["next_attempt_at"] is None and row["claimed_at"] is None
    assert len(row["last_error"]) == 2000 and row["last_error"].endswith("\u2026")
    assert row["last_error_code"] == "provider_bad_request"
    assert row["last_run_id"] == RUN_ID
    with pytest.raises(sa_exc.IntegrityError, match="ck_chunks_last_error_len"):
        with mem_db.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE chunks SET last_error = ? WHERE id = 2", ("y" * 2001,))


# --------------------------------------------------------------------------- #
# 9. release
# --------------------------------------------------------------------------- #


def test_09_release(mem_db: Database, seeded: JobRecord, chunks: ChunkRepository) -> None:
    _claim(chunks, seeded.id, 4)  # 1..4 PROCESSING
    unwrap(chunks.reschedule(seeded.id, 1, _retryable(), utcnow()))  # 1 -> PENDING, retry 1
    _claim(chunks, seeded.id, 1)  # 1 -> PROCESSING again (retry_count 1)
    unwrap(chunks.complete(seeded.id, 2, _result(2)))
    before = {i: raw_row_dict(mem_db, "chunks", "id = ?", (i,)) for i in range(1, 11)}
    assert unwrap(chunks.release(seeded.id, [])) == 0
    assert unwrap(chunks.release(seeded.id, [1, 2, 3, 5, 99])) == 2  # 1 and 3 were PROCESSING
    statuses = _status_map(mem_db)
    assert statuses[1] == "PENDING" and statuses[3] == "PENDING" and statuses[4] == "PROCESSING"
    assert statuses[2] == "COMPLETED" and statuses[5] == "PENDING"
    after = raw_row_dict(mem_db, "chunks", "id = ?", (1,))
    assert (
        after["retry_count"] == 1
        and after["next_attempt_at"] is None
        and after["claimed_at"] is None
    )
    assert raw_row_dict(mem_db, "chunks", "id = ?", (2,)) == before[2]
    assert raw_row_dict(mem_db, "chunks", "id = ?", (5,)) == before[5]


# --------------------------------------------------------------------------- #
# 10. requeue_processing / reset_failed
# --------------------------------------------------------------------------- #


def test_10_requeue_and_reset_touch_listed_columns_only(
    mem_db: Database, seeded: JobRecord, chunks: ChunkRepository
) -> None:
    _claim(chunks, seeded.id, 3)  # 1,2,3 PROCESSING
    unwrap(chunks.complete(seeded.id, 1, _result(1)))
    unwrap(chunks.reschedule(seeded.id, 2, _retryable(), utcnow()))
    _claim(chunks, seeded.id, 1)  # 2 again
    unwrap(chunks.fail(seeded.id, 2, _fatal()))  # 2 FAILED (retry_count 1), 3 PROCESSING
    before = {i: raw_row_dict(mem_db, "chunks", "id = ?", (i,)) for i in range(1, 11)}

    _tick()
    assert unwrap(chunks.requeue_processing(seeded.id)) == 1
    after = {i: raw_row_dict(mem_db, "chunks", "id = ?", (i,)) for i in range(1, 11)}
    changed = {k for k in before[3] if before[3][k] != after[3][k]}
    assert changed == {"status", "claimed_at", "updated_at"}
    assert after[3]["status"] == "PENDING" and after[3]["claimed_at"] is None
    assert after[3]["last_run_id"] == RUN_ID
    for i in (1, 2, 4):
        assert before[i] == after[i]

    _tick()
    assert unwrap(chunks.reset_failed(seeded.id)) == 1
    final = {i: raw_row_dict(mem_db, "chunks", "id = ?", (i,)) for i in range(1, 11)}
    changed = {k for k in after[2] if after[2][k] != final[2][k]}
    assert changed == {"status", "retry_count", "last_error", "last_error_code", "updated_at"}
    assert final[2]["status"] == "PENDING" and final[2]["retry_count"] == 0
    assert final[2]["next_attempt_at"] is None and final[2]["last_error"] is None
    assert final[1] == after[1] == before[1]  # COMPLETED untouched throughout
    assert final[1]["translated_text"] == before[1]["translated_text"]
    assert final[1]["completed_at"] == before[1]["completed_at"]


# --------------------------------------------------------------------------- #
# 11. next_attempt_at honoured
# --------------------------------------------------------------------------- #


def test_11_backoff_gate(mem_db: Database, seeded: JobRecord, chunks: ChunkRepository) -> None:
    assert unwrap(chunks.earliest_next_attempt(seeded.id)) is None
    now = utcnow()
    _claim(chunks, seeded.id, 10)
    t1 = now + timedelta(seconds=30)
    t2 = now + timedelta(seconds=10)
    unwrap(chunks.reschedule(seeded.id, 1, _retryable(), t1))
    unwrap(chunks.reschedule(seeded.id, 2, _retryable(), t2))
    assert unwrap(chunks.earliest_next_attempt(seeded.id)) == t2
    assert unwrap(chunks.claim_pending(seeded.id, 5, now, RUN_ID)) == []
    assert unwrap(chunks.claim_pending(seeded.id, 5, t2 - timedelta(microseconds=1), RUN_ID)) == []
    claimed = unwrap(chunks.claim_pending(seeded.id, 5, t2, RUN_ID))
    assert [c.chunk_id for c in claimed] == [2]
    assert unwrap(chunks.earliest_next_attempt(seeded.id)) == t1
    assert [
        c.chunk_id
        for c in unwrap(chunks.claim_pending(seeded.id, 5, t1 + timedelta(seconds=1), RUN_ID))
    ] == [1]
    assert unwrap(chunks.earliest_next_attempt(seeded.id)) is None


# --------------------------------------------------------------------------- #
# 12. counts / totals / failed_ids / review_ids / A3 / A4
# --------------------------------------------------------------------------- #


def test_12_aggregates(mem_db: Database, job: JobRecord, chunks: ChunkRepository) -> None:
    fixture = make_chunks(8)
    fixture[5] = make_chunk(6, "```\ncode\n```\n\n", kind=ChunkKind.CODE, translatable=False)
    unwrap(chunks.replace_all(job.id, fixture))
    now = utcnow()
    assert unwrap(chunks.counts(job.id, now)).failed == 0
    _claim(chunks, job.id, 6)  # 1..6
    unwrap(chunks.complete(job.id, 1, _result(1, chars_sent=10, chars_billed=12)))
    unwrap(chunks.complete(job.id, 2, _result(2, chars_sent=20, chars_billed=22, review_flag=True)))
    unwrap(
        chunks.complete(job.id, 6, _result(6, "```\ncode\n```\n\n", chars_sent=0, chars_billed=0))
    )
    unwrap(chunks.fail(job.id, 3, _fatal()))
    unwrap(chunks.reschedule(job.id, 4, _retryable(), now + timedelta(minutes=1)))
    unwrap(chunks.reschedule(job.id, 5, _retryable(), now - timedelta(minutes=1)))
    # 1,2,6 COMPLETED; 3 FAILED; 4 PENDING (waiting); 5 PENDING (due); 7,8 PENDING
    other = ChunkRepository(mem_db, "other0000000", "h" * 64)
    _claim(other, job.id, 1)  # claims 5 under the other run
    unwrap(other.complete(job.id, 5, _result(5, chars_sent=5, chars_billed=5)))
    unwrap(other.fail(job.id, 5, _fatal()))  # no-op: already COMPLETED

    counts = unwrap(chunks.counts(job.id, now))
    assert counts.pending == 3 and counts.processing == 0 and counts.completed == 4
    assert counts.failed == 1 and counts.total == 8 and counts.waiting_backoff == 1

    totals = unwrap(chunks.totals(job.id))
    assert totals.total_chunks == 8 and totals.translatable_count == 7
    assert totals.chars_sent == 35 and totals.chars_billed == 39
    expected_completed_chars = sum(c.char_count for c in fixture if c.chunk_id in (1, 2, 5, 6))
    assert totals.completed_chars == expected_completed_chars
    assert totals.total_chars == sum(c.char_count for c in fixture)

    assert unwrap(chunks.failed_ids(job.id)) == [3]
    assert unwrap(chunks.review_ids(job.id)) == [2]
    assert unwrap(chunks.count_completed_with_other_glossary(job.id, GLOSSARY_HASH)) == 1  # chunk 5
    assert (
        unwrap(chunks.count_completed_with_other_glossary(job.id, "h" * 64)) == 2
    )  # 1, 2 (6 is not translatable)
    assert unwrap(chunks.count_completed_by_run(RUN_ID)) == (3, 1)
    assert unwrap(chunks.count_completed_by_run("other0000000")) == (1, 0)
    assert unwrap(chunks.count_completed_by_run("nope")) == (0, 0)

    empty_job = "0" * 36
    assert unwrap(chunks.counts(empty_job, now)).total == 0
    assert unwrap(chunks.totals(empty_job)).total_chunks == 0
    assert unwrap(chunks.failed_ids(empty_job)) == []


# --------------------------------------------------------------------------- #
# 13. iter_ordered
# --------------------------------------------------------------------------- #


def test_13_iter_ordered(mem_db: Database, seeded: JobRecord, chunks: ChunkRepository) -> None:
    with capture_statements(mem_db) as statements:
        ids = [c.chunk_id for c in chunks.iter_ordered(seeded.id, batch_size=3)]
    assert ids == list(range(1, 11))
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 4
    assert statements.count("BEGIN") == 1
    assert list(chunks.iter_ordered("0" * 36)) == []
    with pytest.raises(ValueError):
        next(chunks.iter_ordered(seeded.id, batch_size=0))


# --------------------------------------------------------------------------- #
# 14. replace_all
# --------------------------------------------------------------------------- #


def test_14_replace_all(mem_db: Database, job: JobRecord, chunks: ChunkRepository) -> None:
    bad_ids = chunks.replace_all(job.id, [make_chunk(1), make_chunk(3)])
    assert isinstance(bad_ids, Err) and bad_ids.error.code is ErrorCode.CHUNK_INTEGRITY
    bad_count = chunks.replace_all(job.id, [make_chunk(1, char_count=3)])
    assert isinstance(bad_count, Err) and bad_count.error.code is ErrorCode.CHUNK_INTEGRITY
    bad_hash = chunks.replace_all(job.id, [make_chunk(1, content_hash="0" * 64)])
    assert isinstance(bad_hash, Err) and bad_hash.error.code is ErrorCode.CHUNK_INTEGRITY
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM chunks")[0][0] == 0

    sub = [
        make_chunk(1, parent_block_id=7, sub_index=1, sub_count=1),
    ]
    subsplit = chunks.replace_all(job.id, sub)
    assert isinstance(subsplit, Err) and subsplit.error.code is ErrorCode.INTERNAL
    assert "ck_chunks_subsplit" in subsplit.error.message
    code = chunks.replace_all(job.id, [make_chunk(1, kind=ChunkKind.CODE, translatable=True)])
    assert isinstance(code, Err) and "ck_chunks_nontranslatable_kinds" in code.error.message

    big = make_chunks(5000)
    started = time.perf_counter()
    assert unwrap(chunks.replace_all(job.id, big)) == 5000
    assert time.perf_counter() - started < 2.0
    ids, count = raw_rows(mem_db, "SELECT MAX(id), COUNT(*) FROM chunks")[0]
    assert (ids, count) == (5000, 5000)
    assert (
        raw_rows(mem_db, "SELECT COUNT(*) FROM chunks WHERE char_count <> length(source_text)")[0][
            0
        ]
        == 0
    )

    # valid sub-split and replacement of pending rows
    good = [
        make_chunk(1),
        make_chunk(2, parent_block_id=9, sub_index=0, sub_count=2),
        make_chunk(3, parent_block_id=9, sub_index=1, sub_count=2),
    ]
    assert unwrap(chunks.replace_all(job.id, good)) == 3
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM chunks")[0][0] == 3

    _claim(chunks, job.id, 1)
    unwrap(chunks.complete(job.id, 1, _result(1)))
    refused = chunks.replace_all(job.id, make_chunks(2))
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.INTERNAL
    assert "COMPLETED" in refused.error.message
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM chunks")[0][0] == 3


# --------------------------------------------------------------------------- #
# 15. Lease
# --------------------------------------------------------------------------- #


def test_15_lease(mem_db: Database, job: JobRecord, leases: LeaseRepository) -> None:
    me = LeaseHolder(pid=100, host="alpha", run_id="aaaaaaaaaaaa")
    other = LeaseHolder(pid=200, host="beta", run_id="bbbbbbbbbbbb")
    assert unwrap(leases.get(job.id)) is None
    assert unwrap(leases.acquire(job.id, me, 60)) is True
    record = unwrap(leases.get(job.id))
    assert record is not None and record.holder_pid == 100 and record.run_id == "aaaaaaaaaaaa"
    assert unwrap(leases.acquire(job.id, other, 60)) is False
    assert unwrap(leases.get(job.id)) == record
    assert unwrap(leases.acquire(job.id, LeaseHolder(100, "alpha", "cccccccccccc"), 60)) is True
    assert unwrap(leases.get(job.id)).run_id == "cccccccccccc"  # type: ignore[union-attr]

    stale = utcnow() - timedelta(seconds=120)
    with mem_db.engine.begin() as conn:
        conn.execute(update(m.Lease).where(m.Lease.job_id == job.id).values(heartbeat_at=stale))
    assert unwrap(leases.acquire(job.id, other, 60)) is True
    assert unwrap(leases.get(job.id)).holder_host == "beta"  # type: ignore[union-attr]
    unwrap(leases.heartbeat(job.id, other))
    assert unwrap(leases.get(job.id)).heartbeat_at > stale  # type: ignore[union-attr]

    lost = leases.heartbeat(job.id, me)
    assert isinstance(lost, Err) and lost.error.code is ErrorCode.STATE_LOCKED
    assert unwrap(leases.release(job.id, me)) is None  # non-holder: no-op
    assert unwrap(leases.get(job.id)) is not None
    unwrap(leases.force_break(job.id))
    assert unwrap(leases.get(job.id)) is None
    broken = leases.heartbeat(job.id, other)
    assert isinstance(broken, Err) and broken.error.code is ErrorCode.STATE_LOCKED
    assert unwrap(leases.acquire(job.id, me, 60)) is True
    unwrap(leases.release(job.id, me))
    assert unwrap(leases.get(job.id)) is None


# --------------------------------------------------------------------------- #
# 16. Job and runs
# --------------------------------------------------------------------------- #


def test_16_job_and_runs(mem_db: Database, jobs: JobRepository) -> None:
    spec = JobSpec(
        input_path="C:/books/input.pdf", input_sha256="a" * 64, tool_version=TOOL_VERSION
    )
    first = unwrap(jobs.get_or_create_job(spec))
    second = unwrap(jobs.get_or_create_job(JobSpec("other.pdf", "f" * 64, "9.9.9")))
    assert first.id == second.id and second.input_sha256 == "a" * 64
    assert first.status == "CREATED" and first.warnings == () and first.fallback_used is False
    assert raw_rows(mem_db, "SELECT COUNT(*) FROM jobs")[0][0] == 1

    with pytest.raises(sa_exc.IntegrityError, match="jobs.singleton"):
        with mem_db.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO jobs (id, singleton, input_path, input_sha256, status, tool_version, "
                "warnings, created_at, updated_at) "
                "VALUES (?, 1, 'x', ?, 'CREATED', 'v', '[]', ?, ?)",
                ("1" * 36, "b" * 64, m.format_timestamp(utcnow()), m.format_timestamp(utcnow())),
            )

    with capture_statements(mem_db) as statements:
        assert unwrap(jobs.update_job(first.id, JobPatch())) is None
    assert statements == []

    _tick()
    unwrap(
        jobs.update_job(
            first.id,
            JobPatch(
                status="CHUNKED",
                source_md_sha256="d" * 64,
                chunk_min_chars=1000,
                chunk_max_chars=1500,
                provider="deepl",
                glossary_strategy="native",
                warnings=["non-english source"],
                fallback_used=True,
                extractor_name="pymupdf",
                detected_language="en",
            ),
        )
    )
    updated = unwrap(jobs.get_job(first.id))
    assert updated is not None
    assert updated.status == "CHUNKED" and updated.chunk_max_chars == 1500
    assert updated.warnings == ("non-english source",) and updated.fallback_used is True
    assert updated.updated_at > first.updated_at
    missing = jobs.update_job("0" * 36, JobPatch(status="PAUSED"))
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.STATE_NOT_FOUND
    bad_enum = jobs.update_job(first.id, JobPatch(status="BOGUS"))
    assert isinstance(bad_enum, Err) and bad_enum.error.code is ErrorCode.INTERNAL
    assert unwrap(jobs.get_job("0" * 36)) is None

    assert unwrap(jobs.get_last_run(first.id)) is None
    unwrap(jobs.start_run(first.id, "run000000001", "extract", TOOL_VERSION))
    unwrap(jobs.start_run(first.id, "run000000002", "translate", TOOL_VERSION, "deepl", True))
    duplicate = jobs.start_run(first.id, "run000000002", "translate", TOOL_VERSION)
    assert isinstance(duplicate, Err) and duplicate.error.code is ErrorCode.INTERNAL
    last = unwrap(jobs.get_last_run(first.id))
    assert last is not None and last.id == "run000000002" and last.provider_switch_allowed is True
    assert last.finished_at is None and last.outcome is None
    outcome = RunOutcome("PARTIAL", 2, 5, 1, 1234, 6, 1)
    unwrap(jobs.finish_run(first.id, "run000000002", outcome))
    again = jobs.finish_run(first.id, "run000000002", outcome)
    assert isinstance(again, Err) and again.error.code is ErrorCode.STATE_NOT_FOUND
    finished = unwrap(jobs.get_last_run(first.id))
    assert finished is not None and finished.outcome == "PARTIAL" and finished.chars_sent == 1234
    assert finished.finished_at is not None and finished.exit_code == 2


# --------------------------------------------------------------------------- #
# 17. Versioning and archive
# --------------------------------------------------------------------------- #


def test_17_versioning_and_archive(db_path: Path, tmp_path: Path) -> None:
    db = unwrap(open_database(db_path, tool_version=TOOL_VERSION))
    job = unwrap(JobRepository(db).get_or_create_job(JobSpec("in.pdf", "e" * 64, TOOL_VERSION)))
    unwrap(ChunkRepository(db, RUN_ID, None).replace_all(job.id, make_chunks(7)))
    close_database(db)

    archived = unwrap(archive_database(db_path, tmp_path / "state-archive" / "20260101T000000Z"))
    assert archived.exists() and archived.name == "translation_state.db"
    copy = unwrap(open_database(archived, tool_version=TOOL_VERSION))
    try:
        assert raw_rows(copy, "SELECT COUNT(*) FROM chunks")[0][0] == 7
        assert raw_rows(copy, "SELECT COUNT(*) FROM jobs")[0][0] == 1
    finally:
        close_database(copy)
    missing = archive_database(tmp_path / "nope.db", tmp_path / "x")
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.STATE_NOT_FOUND

    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE schema_meta SET schema_version = 99 WHERE id = 1")
    newer = open_database(db_path, tool_version=TOOL_VERSION)
    assert isinstance(newer, Err) and newer.error.code is ErrorCode.STATE_SCHEMA_INCOMPATIBLE
    assert "newer" in newer.error.message

    corrupt_path = tmp_path / "corrupt.db"
    with sqlite3.connect(corrupt_path) as raw:
        raw.execute("CREATE TABLE something_else (x INTEGER)")
    corrupt = open_database(corrupt_path, tool_version=TOOL_VERSION)
    assert isinstance(corrupt, Err) and corrupt.error.code is ErrorCode.STATE_CORRUPT
    corrupt_ro = open_database(corrupt_path, read_only=True, tool_version=TOOL_VERSION)
    assert isinstance(corrupt_ro, Err) and corrupt_ro.error.code is ErrorCode.STATE_CORRUPT


# --------------------------------------------------------------------------- #
# 18. Boundary
# --------------------------------------------------------------------------- #


def test_18_boundary(
    mem_db: Database, seeded: JobRecord, chunks: ChunkRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = JobRepository(mem_db)
    unwrap(jobs.start_run(seeded.id, RUN_ID, "translate", TOOL_VERSION))
    duplicate = jobs.start_run(seeded.id, RUN_ID, "translate", TOOL_VERSION)
    assert isinstance(duplicate, Err)
    assert (
        duplicate.error.code is ErrorCode.INTERNAL and duplicate.error.scope is ErrorScope.JOB_FATAL
    )
    assert "runs.id" in duplicate.error.message
    assert TOOL_VERSION not in duplicate.error.message and seeded.id not in duplicate.error.message

    marker = "UNIQUE-BOOK-TEXT-MARKER"
    bad = ChunkRepository(mem_db, RUN_ID, None).replace_all(
        "0" * 36, [make_chunk(1, marker, kind=ChunkKind.IMAGE, translatable=True)]
    )
    assert isinstance(bad, Err) and bad.error.code is ErrorCode.INTERNAL
    assert "ck_chunks_nontranslatable_kinds" in bad.error.message
    assert marker not in bad.error.message and marker not in (bad.error.cause or "")

    def locked(self: Session, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Session, "execute", locked)
    outcome = chunks.counts(seeded.id)
    assert isinstance(outcome, Err)
    assert (
        outcome.error.code is ErrorCode.STATE_LOCKED and outcome.error.scope is ErrorScope.JOB_FATAL
    )
    monkeypatch.undo()

    def locked_conn(self: Connection, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Connection, "execute", locked_conn)  # writes run on the Core connection
    write = chunks.requeue_processing(seeded.id)
    assert isinstance(write, Err) and write.error.code is ErrorCode.STATE_LOCKED
    monkeypatch.undo()
    assert isinstance(chunks.counts(seeded.id), Ok)

    def corrupt(self: Session, *args: Any, **kwargs: Any) -> Any:
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(Session, "execute", corrupt)
    broken = chunks.totals(seeded.id)
    assert isinstance(broken, Err) and broken.error.code is ErrorCode.STATE_CORRUPT
    monkeypatch.undo()

    with mem_db.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE chunks SET warnings = 'not json' WHERE id = 1")
    decoded = chunks.claim_pending(seeded.id, 1, utcnow(), RUN_ID)
    assert isinstance(decoded, Err) and decoded.error.code is ErrorCode.STATE_CORRUPT
    assert raw_row_dict(mem_db, "chunks", "id = ?", (1,))["status"] == "PENDING"  # rolled back


# --------------------------------------------------------------------------- #
# 19. get_single_job (CR-15)
# --------------------------------------------------------------------------- #


def test_19_get_single_job(mem_db: Database, jobs: JobRepository) -> None:
    """The orchestrator's single-job lookup lives in the repository (design 1.3/1.4)."""
    assert unwrap(jobs.get_single_job()) is None
    created = unwrap(
        jobs.get_or_create_job(JobSpec("C:/books/input.pdf", "a" * 64, TOOL_VERSION))
    )
    with capture_statements(mem_db) as statements:
        assert unwrap(jobs.get_single_job()) == created
    assert statements[0] == "BEGIN"  # read-only (R method)
    # A second row is impossible through the repository (UNIQUE + CHECK on singleton);
    # bypass the CHECK to reach the corruption branch.
    with mem_db.engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        conn.exec_driver_sql(
            "INSERT INTO jobs (id, singleton, input_path, input_sha256, status, tool_version, "
            "warnings, created_at, updated_at) "
            "SELECT 'ffffffff-ffff-4fff-8fff-ffffffffffff', 2, input_path, "
            "input_sha256, status, tool_version, warnings, created_at, updated_at FROM jobs"
        )
        conn.commit()
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
        conn.commit()
    corrupt = jobs.get_single_job()
    assert isinstance(corrupt, Err) and corrupt.error.code is ErrorCode.STATE_CORRUPT
