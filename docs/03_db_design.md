# DB Design: `translation_state.db` (book-translator, schema v1)

Scope: one SQLite file per output directory, accessed through SQLAlchemy 2.x sync ORM/Core. No stored procedures exist in SQLite; Section 5 maps every repository method to the exact SQL it executes and its transaction mode. All naming is `snake_case`, plural table names (`jobs`, `chunks`, `runs`), single-row tables singular (`lease`, `schema_meta`).

Key decisions (binding for the DB Developer):

| Topic | Decision |
|---|---|
| Timestamps | `TEXT`, UTC, fixed 27-char `YYYY-MM-DDTHH:MM:SS.ffffffZ`; always produced by Python, never by `datetime('now')` |
| Booleans | `INTEGER` 0/1 with `CHECK (col IN (0,1))` |
| Enums | `TEXT` + `CHECK (col IN (...))`; not SQLAlchemy `Enum` (it stores enum *names* and hides the constraint) |
| JSON | `TEXT`, validated in Python by a `TypeDecorator`; no `json_valid()` in CHECKs (JSON1 is a compile option before SQLite 3.38) |
| Chunk identity | `chunks.id INTEGER PRIMARY KEY` = rowid alias = `chunk_id` = `order` (1-based, dense per job) |
| One job per DB | Enforced by a `singleton` column on `jobs` (`--fresh` archives the whole file; the lease is therefore also single-row) |
| Conditional transitions | Core `UPDATE ... WHERE <precondition>` + `rowcount` check. Never ORM attribute assignment + `commit()` (no precondition possible) |
| Transaction modes | Every write method runs under `BEGIN IMMEDIATE`; read-only methods under `BEGIN` (DEFERRED). pysqlite's implicit-BEGIN behaviour is disabled (Section 6) |
| SQLite floor | Hard floor 3.8.0 (partial indexes); tested floor 3.31 (Ubuntu 20.04 lib). `RETURNING` (3.35) and `UPSERT` (3.24) are deliberately **not** required — see 5.3 |

---

## 1. Schema Overview

```mermaid
erDiagram
    SCHEMA_META {
        INTEGER id PK "always 1"
        INTEGER schema_version
        TEXT created_by_tool_version
        TEXT created_at
        TEXT upgraded_by_tool_version
        TEXT upgraded_at
    }
    JOBS {
        TEXT id PK "uuid4, 36 chars"
        INTEGER singleton UK "always 1"
        TEXT input_path
        TEXT input_sha256
        TEXT source_md_sha256
        TEXT status "CREATED..EXPORTED"
        TEXT tool_version
        TEXT extractor_name
        INTEGER fallback_used
        TEXT detected_language
        INTEGER chunk_min_chars
        INTEGER chunk_max_chars
        TEXT provider
        TEXT glossary_strategy
        TEXT provider_glossary_id
        TEXT glossary_hash
        TEXT warnings "JSON array"
        TEXT created_at
        TEXT updated_at
    }
    CHUNKS {
        INTEGER id PK "== chunk_id == order"
        TEXT job_id FK
        TEXT kind
        INTEGER translatable
        TEXT source_text
        TEXT content_hash
        INTEGER char_count
        INTEGER parent_block_id
        INTEGER sub_index
        INTEGER sub_count
        TEXT heading_path "JSON array"
        TEXT status "PENDING..FAILED"
        INTEGER retry_count
        TEXT next_attempt_at
        TEXT last_error_code
        TEXT last_error
        TEXT translated_text
        INTEGER review_flag
        TEXT warnings "JSON array"
        TEXT provider
        TEXT glossary_strategy
        TEXT glossary_hash
        INTEGER chars_sent
        INTEGER chars_billed
        INTEGER latency_ms
        INTEGER attempts
        TEXT last_run_id "soft ref"
        TEXT claimed_at
        TEXT completed_at
        TEXT created_at
        TEXT updated_at
    }
    RUNS {
        TEXT id PK "12 hex"
        TEXT job_id FK
        TEXT command
        TEXT tool_version
        TEXT provider
        INTEGER provider_switch_allowed
        TEXT started_at
        TEXT finished_at
        TEXT outcome
        INTEGER exit_code
        INTEGER chunks_completed
        INTEGER chunks_failed
        INTEGER chars_sent
        INTEGER provider_calls
        INTEGER rate_limited_count
    }
    LEASE {
        TEXT job_id PK_FK
        INTEGER holder_pid
        TEXT holder_host
        TEXT run_id
        TEXT acquired_at
        TEXT heartbeat_at
    }
    JOBS ||--o{ CHUNKS : "1..5000"
    JOBS ||--o{ RUNS : "0..100"
    JOBS ||--o| LEASE : "0..1"
    RUNS |o..o{ CHUNKS : "last_run_id (no FK)"
```

No additional tables. Rejected: `provider_calls` (12.3 item 12 — JSONL log covers NFR-11), `chunk_warnings` (JSON array on the row suffices; never queried by value), `glossary_entries` (the glossary lives in `glossary.json`; the DB only needs its hash).

---

## 2. Table Design

Conventions used in every table below:

- **Storage / SA type** column shows the SQLite storage class and the SQLAlchemy type the developer must use. `UtcTimestamp` and `JsonList` are `TypeDecorator`s defined in `models.py` (Section 2.6).
- Every timestamp column carries `CHECK (col IS NULL OR length(col) = 27)`. It does not validate the full format, but it catches the two common mistakes (`datetime.isoformat()` → 32 chars with `+00:00`, or 20/26 chars when microseconds are zero/omitted), both of which would silently break string ordering.
- Timestamps have **no SQL defaults**. `strftime('%Y-%m-%dT%H:%M:%fZ','now')` would give 3 fractional digits, `datetime('now')` gives a different format entirely; either would corrupt comparisons. The repository always binds Python-produced values from one clock helper `utcnow()`.

### 2.1 `schema_meta`

Purpose: single-row schema version record, read before any other statement.

| Column | Storage / SA type | Null | Default | Description |
|---|---|---|---|---|
| id | INTEGER / `Integer` | NO | | PK; `CHECK (id = 1)` |
| schema_version | INTEGER / `Integer` | NO | | `CHECK (schema_version >= 1)` |
| created_by_tool_version | TEXT / `String(40)` | NO | | Tool version that created the file |
| created_at | TEXT / `UtcTimestamp` | NO | | |
| upgraded_by_tool_version | TEXT / `String(40)` | YES | | Set by the last migration |
| upgraded_at | TEXT / `UtcTimestamp` | YES | | Set by the last migration |

Constraints: `PK (id)`, `CHECK (id = 1)` (single row).

### 2.2 `jobs`

Purpose: the one job this DB belongs to (one per output directory).

| Column | Storage / SA type | Null | Default | Description |
|---|---|---|---|---|
| id | TEXT / `String(36)` | NO | | PK; `str(uuid4())`; `CHECK (length(id) = 36)` |
| singleton | INTEGER / `Integer` | NO | 1 | `UNIQUE`, `CHECK (singleton = 1)` → at most one row |
| input_path | TEXT / `Text` | NO | | Absolute path as given at creation (display only) |
| input_sha256 | TEXT / `String(64)` | NO | | `CHECK (length(input_sha256) = 64)`; FR-18 binding |
| source_md_sha256 | TEXT / `String(64)` | YES | | Set after extract; re-checked at translate (4.5); `CHECK (... IS NULL OR length = 64)` |
| status | TEXT / `String(12)` | NO | 'CREATED' | `CHECK IN ('CREATED','EXTRACTED','CHUNKED','TRANSLATING','PAUSED','TRANSLATED','EXPORTED')` |
| tool_version | TEXT / `String(40)` | NO | | Version that created the job |
| extractor_name | TEXT / `String(20)` | YES | | 'pymupdf' / 'marker' (for JobSummary when `export` runs alone) |
| fallback_used | INTEGER / `Boolean(create_constraint=True)` | NO | 0 | `CHECK IN (0,1)` |
| detected_language | TEXT / `String(8)` | YES | | From langdetect; `--allow-non-english` warning goes to `warnings` |
| chunk_min_chars | INTEGER / `Integer` | YES | | Set at chunk stage; `CHECK (NULL OR > 0)` |
| chunk_max_chars | INTEGER / `Integer` | YES | | `CHECK (NULL OR chunk_max_chars >= chunk_min_chars)` |
| provider | TEXT / `String(40)` | YES | | Provider the job is bound to ('deepl', 'local', 'llm:<vendor>') |
| glossary_strategy | TEXT / `String(12)` | YES | | `CHECK (NULL OR IN ('native','prompt','post_replace','none'))` — `GlossaryStrategy.value` |
| provider_glossary_id | TEXT / `String(120)` | YES | | DeepL glossary id (not a secret, 12.3/8) |
| glossary_hash | TEXT / `String(64)` | YES | | Hash of the effective glossary last bound |
| warnings | TEXT / `JsonList` | NO | '[]' | JSON array of strings |
| created_at | TEXT / `UtcTimestamp` | NO | | |
| updated_at | TEXT / `UtcTimestamp` | NO | | Touched by every `update_job` |

Constraints: `PK (id)`, `UNIQUE (singleton)`, CHECKs above. Job status transitions are business rules (3.2) and are enforced by the orchestrator, not by the DB; the DB only enforces the enum.

### 2.3 `chunks`

Purpose: one row per chunk; static chunker output plus mutable runtime state.

| Column | Storage / SA type | Null | Default | Description |
|---|---|---|---|---|
| id | INTEGER / `Integer` | NO | | `INTEGER PRIMARY KEY` (rowid alias) = `chunk_id` = `order`; `CHECK (id >= 1)`; explicit values, no AUTOINCREMENT |
| job_id | TEXT / `String(36)` | NO | | FK → `jobs.id` ON DELETE CASCADE |
| kind | TEXT / `String(10)` | NO | | `CHECK IN ('TEXT','HEADING','LIST','TABLE','CODE','IMAGE','HR','FOOTNOTE')` |
| translatable | INTEGER / `Boolean(create_constraint=True)` | NO | | `CHECK IN (0,1)`; `CHECK (kind NOT IN ('CODE','IMAGE','HR') OR translatable = 0)` |
| source_text | TEXT / `Text` | NO | | Exact substring of `source_book.md` (incl. trailing blank lines) |
| content_hash | TEXT / `String(64)` | NO | | sha256(source_text); `CHECK (length = 64)` |
| char_count | INTEGER / `Integer` | NO | | `len(source_text)` in Python; `CHECK (char_count >= 0)` (denormalized, see 3) |
| parent_block_id | INTEGER / `Integer` | YES | | Set only for sub-splits (4.4) |
| sub_index | INTEGER / `Integer` | YES | | 0-based within parent |
| sub_count | INTEGER / `Integer` | YES | | Pieces in the parent |
| heading_path | TEXT / `JsonList` | NO | '[]' | JSON array of heading titles (display/EPUB mapping only) |
| status | TEXT / `String(10)` | NO | 'PENDING' | `CHECK IN ('PENDING','PROCESSING','COMPLETED','FAILED')` |
| retry_count | INTEGER / `Integer` | NO | 0 | Retryable failures so far; `CHECK (retry_count >= 0)` |
| next_attempt_at | TEXT / `UtcTimestamp` | YES | | Backoff gate; `CHECK (status = 'PENDING' OR next_attempt_at IS NULL)` |
| last_error_code | TEXT / `String(40)` | YES | | `ErrorCode.value` of the last failure |
| last_error | TEXT / `String(2000)` | YES | | Message, truncated by the repository; `CHECK (last_error IS NULL OR length(last_error) <= 2000)` |
| translated_text | TEXT / `Text` | YES | | Written only by `complete()`; `CHECK ((status = 'COMPLETED') = (translated_text IS NOT NULL))` |
| review_flag | INTEGER / `Boolean(create_constraint=True)` | NO | 0 | E-17 / glossary post-check |
| warnings | TEXT / `JsonList` | NO | '[]' | Chunker + translation warnings |
| provider | TEXT / `String(40)` | YES | | Provider that produced `translated_text` (per-chunk provenance; the job may switch providers) |
| glossary_strategy | TEXT / `String(12)` | YES | | `CHECK (NULL OR IN ('native','prompt','post_replace','none'))` |
| glossary_hash | TEXT / `String(64)` | YES | | Glossary the chunk was translated under → "N chunks translated with previous glossary" warning (5.1 step 6) |
| chars_sent | INTEGER / `Integer` | NO | 0 | Payload chars of the successful attempt; `CHECK (>= 0)` |
| chars_billed | INTEGER / `Integer` | NO | 0 | Provider-reported; `CHECK (>= 0)` |
| latency_ms | INTEGER / `Integer` | YES | | Successful attempt; `CHECK (NULL OR >= 0)` |
| attempts | INTEGER / `Integer` | NO | 0 | Provider calls made in total; `CHECK (>= 0)` |
| last_run_id | TEXT / `String(12)` | YES | | Run that last claimed the chunk. **No FK** (see 3). For a COMPLETED row it *is* `completed_by_run_id` (claim and complete always happen in the same run) |
| claimed_at | TEXT / `UtcTimestamp` | YES | | Set on claim, cleared when leaving PROCESSING |
| completed_at | TEXT / `UtcTimestamp` | YES | | Set by `complete()` |
| created_at | TEXT / `UtcTimestamp` | NO | | |
| updated_at | TEXT / `UtcTimestamp` | NO | | Every transition |

Sub-split CHECK (one constraint, named `ck_chunks_subsplit`):

```sql
CHECK (
  ((parent_block_id IS NULL) = (sub_index IS NULL))
  AND ((sub_index IS NULL) = (sub_count IS NULL))
  AND (sub_index IS NULL OR (sub_index >= 0 AND sub_count >= 1 AND sub_index < sub_count))
)
```

`sub_count >= 1` rather than `>= 2` on purpose: a block that exceeds the limit but has no split point (e.g. one 2 000-char URL-like token) may legitimately come out as one piece; a stricter CHECK would turn a chunker corner case into a job-fatal insert failure.

Constraints summary: `PK (id)`, `FK (job_id) → jobs(id) ON DELETE CASCADE`, all CHECKs above. Dense numbering (`1..n` without gaps) is verified by `replace_all` in Python before insert, not by the DB.

### 2.4 `runs`

Purpose: one row per CLI invocation that writes state (`run`, `extract`, `glossary`, `translate`, `export`). `status` and the listing commands create no run row.

| Column | Storage / SA type | Null | Default | Description |
|---|---|---|---|---|
| id | TEXT / `String(12)` | NO | | PK; the 12-hex `run_id` from logging (log file correlation) |
| job_id | TEXT / `String(36)` | NO | | FK → `jobs.id` ON DELETE CASCADE |
| command | TEXT / `String(10)` | NO | | `CHECK IN ('run','extract','glossary','translate','export')` |
| tool_version | TEXT / `String(40)` | NO | | |
| provider | TEXT / `String(40)` | YES | | Provider used in this run (NULL for extract/glossary/export) |
| provider_switch_allowed | INTEGER / `Boolean(create_constraint=True)` | NO | 0 | `--allow-provider-switch` was passed (5.1 step 5) |
| started_at | TEXT / `UtcTimestamp` | NO | | |
| finished_at | TEXT / `UtcTimestamp` | YES | | NULL = still running or crashed |
| outcome | TEXT / `String(12)` | YES | | `CHECK (NULL OR IN ('SUCCESS','PARTIAL','PAUSED','FAILED','INTERRUPTED'))`; `CHECK ((finished_at IS NULL) = (outcome IS NULL))` |
| exit_code | INTEGER / `Integer` | YES | | 0/1/2/3/130 per 9.3 |
| chunks_completed | INTEGER / `Integer` | NO | 0 | Written by `finish_run` (see 3) |
| chunks_failed | INTEGER / `Integer` | NO | 0 | |
| chars_sent | INTEGER / `Integer` | NO | 0 | Cumulative over **all** attempts in this run, incl. failed ones |
| provider_calls | INTEGER / `Integer` | NO | 0 | |
| rate_limited_count | INTEGER / `Integer` | NO | 0 | 429 count |

Constraints: `PK (id)`, FK, CHECKs; all counters `CHECK (>= 0)`.

### 2.5 `lease`

Purpose: single-process lock (E-20, 5.6).

| Column | Storage / SA type | Null | Default | Description |
|---|---|---|---|---|
| job_id | TEXT / `String(36)` | NO | | PK; FK → `jobs.id` ON DELETE CASCADE |
| holder_pid | INTEGER / `Integer` | NO | | `os.getpid()` |
| holder_host | TEXT / `String(255)` | NO | | `socket.gethostname()` |
| run_id | TEXT / `String(12)` | NO | | Holder's run id (log correlation). Not an FK: the lease is acquired *before* `start_run` (5.1 step 3 vs 8) |
| acquired_at | TEXT / `UtcTimestamp` | NO | | |
| heartbeat_at | TEXT / `UtcTimestamp` | NO | | Staleness reference |

Constraints: `PK (job_id)`. Single row follows from `jobs.singleton`.

### 2.6 Type decorators (`models.py`)

- `UtcTimestamp(TypeDecorator)`, `impl = String(27)`, `cache_ok = True`.
  Bind: require an aware `datetime`; convert to UTC; `dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"`. A naive datetime raises `ValueError` (programming error, caught at the repository boundary as `INTERNAL`). Result: `datetime.strptime(v, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)`.
  Why TEXT and not INTEGER epoch: (a) the format is fixed-width and zero-padded, so byte-wise string comparison equals chronological comparison, which is all `next_attempt_at <= :now` and `heartbeat_at <= :cutoff` need; (b) it is readable in the `sqlite3` shell and in bug reports without conversion; (c) microsecond precision without float rounding; (d) at ≤ 5 000 rows the 19-byte overhead per value is irrelevant. The one cost — no arithmetic in SQL — is never needed: all `now`/`cutoff` values are computed in Python.
- `JsonList(TypeDecorator)`, `impl = Text`. Bind: must be a `list`/`tuple` of `str`, else `ValueError`; `json.dumps(list(v), ensure_ascii=False, separators=(",", ":"))`. Result: `json.loads`; a non-list or decode error raises and surfaces as `STATE_CORRUPT`. Validation is Python-side only (no JSON1 dependency).

---

## 3. Normalization

**3NF check.** Candidate keys: `jobs.id` (and `singleton`), `chunks.id`, `runs.id`, `lease.job_id`, `schema_meta.id`. Every non-key attribute is a fact about its row's key only; there are no transitive dependencies:

- `chunks.provider` / `glossary_strategy` / `glossary_hash` are facts about *that chunk's translation*, not copies of `jobs.provider` etc. The job columns hold the *current binding*, the chunk columns hold *provenance*; they legitimately diverge after `--allow-provider-switch` or a glossary edit, and the divergence is exactly what the resume warnings need.
- `runs.chars_sent`, `provider_calls`, `rate_limited_count` are run-level facts (they include failed attempts) and are **not** derivable from `chunks`, so they are not denormalizations.
- `jobs.status` is a workflow position (e.g. `EXTRACTED` exists before any chunk row), not an aggregate of chunk statuses.

**Deliberate denormalizations / 1NF relaxations:**

| Item | Why |
|---|---|
| `chunks.char_count` = `len(source_text)` | `totals()` and `--dry-run` reporting must sum sizes without loading ~5 000 × 10 KB of text. Set once at insert; a test asserts equality. Not enforced with `CHECK (char_count = length(source_text))` because SQLite's `length()` stops at the first NUL byte, and a stray NUL from PDF extraction would then make the whole insert fail. |
| `runs.chunks_completed` / `chunks_failed` | Derivable via `COUNT(*) ... WHERE last_run_id = :run AND status = ...`, but the summary and `status` want them without a scan. Written once by `finish_run`. If a run crashed (`outcome IS NULL`), `status` recomputes them from `chunks` — the derivable path is the fallback. Per-completion increments inside `complete()` were considered and rejected: one extra `runs` write per chunk for a value that is only read at the end. |
| `chunks.last_run_id` without FK | The run row always exists when a claim happens (`start_run` precedes the scheduler), but an FK would force every repository test to create a run first and would couple `chunks` to a future run-pruning policy. It is correlation metadata for log lookup, not referential data. |
| `heading_path`, `warnings` as JSON arrays | Multi-valued attributes on the row. They are write-once display data, never filtered or joined on; a child table would add two joins to the streaming reader for nothing. |
| `chunks.id` doubles as `order` | The design fixes `chunk_id == order` (12.3 item 2). Storing `order` separately would be a stored functional dependency on the key (`order = id`), i.e. redundancy with an update anomaly. The domain `Chunk.order` is populated from `id` by the repository. `order` is also an SQL keyword. |
| `jobs.singleton` | Not a data attribute but a constraint carrier; it turns "one job per DB" from a convention into a DB invariant, and makes `get_or_create_job` race-free by construction (second insert fails on UNIQUE). A v2 migration can drop it if multi-job files are ever wanted. |

---

## 4. Index Plan

Volumes: ≤ 5 000 chunks/job (row ≈ 3–10 KB because of the two text columns), ≤ 100 runs, 1 job, 1 lease. Implication: any *table* scan over `chunks` touches up to ~50 MB of pages, while an *index* scan touches ~200 KB. Indexes therefore exist only to keep hot queries off the table; nothing else is justified.

```sql
-- I1  primary key (rowid). Serves: single-row lookups in complete/reschedule/fail,
--     ordered streaming (table is physically ordered by id), keyset pagination `id > :last`.
--     (implicit: INTEGER PRIMARY KEY)

-- I2  counts per status, FAILED/PROCESSING id lists, requeue_processing, reset_failed, FK support.
CREATE INDEX ix_chunks_job_status ON chunks (job_id, status);
--     Equal (job_id,status) entries are stored in rowid order, so
--     `WHERE job_id=? AND status='FAILED' ORDER BY id` needs no sort step.

-- I3  earliest_next_attempt and the "waiting for backoff" count (partial: only PENDING rows are stored).
CREATE INDEX ix_chunks_pending_backoff ON chunks (job_id, next_attempt_at) WHERE status = 'PENDING';
--     earliest: MIN(next_attempt_at) is an O(log n) index seek (NULLs sort first and are skipped by MIN).
--     claim: NOT served by I3 (verified with EXPLAIN QUERY PLAN on SQLite 3.45, CR-17). With the
--            mandatory `ORDER BY id LIMIT n` the planner picks I2 (`job_id=? AND status=?`): its entries
--            are already in rowid order, so the scan stops after n eligible rows with no sort step,
--            whereas I3 would need a TEMP B-TREE sort of every pending entry on each claim. I3 is only
--            chosen when the ORDER BY is dropped, and ascending ids are part of the claim contract.
--            Acceptance check 4 therefore asserts I2 (and no temp B-tree) for the claim SELECT.

-- I4  review id list (partial; stores only flagged rows, typically < 1 % of chunks).
CREATE INDEX ix_chunks_review ON chunks (job_id) WHERE review_flag = 1;
```

Partial-index trap (must be honoured in `repository.py` and verified by a test): SQLite uses a partial index only when the query's WHERE clause *provably implies* the index WHERE. `status = 'PENDING'` as a **literal** does; `status = :param` (bound parameter) does **not**. All status/flag predicates in claim, earliest, counts-by-status shortcuts and review listing are therefore written with literals (`literal_column("'PENDING'")` / `text()`), and `tests/test_repository.py` asserts via `EXPLAIN QUERY PLAN` that `earliest_next_attempt` uses `ix_chunks_pending_backoff` and that the claim SELECT uses exactly `ix_chunks_job_status` (see the I3 note above: the `ORDER BY id` makes I2 the better plan).

Intentionally **not** created (and why):

| Candidate | Reason for omission |
|---|---|
| `chunks(content_hash)` | Integrity check compares hash per row by id; never a lookup by hash. |
| `chunks(parent_block_id)` | The assembler groups sub-chunks while streaming in id order; sub-chunks of one parent are adjacent by construction (4.4). |
| `chunks(last_run_id)` | Used once per crashed run for counter recovery; a single scan is acceptable. |
| `chunks(glossary_hash)`, `chunks(provider)` | One `COUNT(*)` per run start (Section 5, A3); a range on I2 (`status='COMPLETED'`) plus row fetches once per run is fine. |
| Covering index for `totals()` (`chars_sent, chars_billed, char_count, ...`) | Would be read twice per run (start/end) and by `status`; a ≤ 50 MB scan from the OS page cache costs tens of ms. Not worth maintaining on every transition. |
| Any index including `source_text`/`translated_text` | Would double the file size. |
| `runs(job_id)`, `runs(started_at)` | ≤ 100 rows; `get_last_run` scans 100 rows. FK deletes never happen in v1 (jobs are never deleted; `--fresh` archives the file). |
| `chunks(job_id, id, next_attempt_at)` variant of I3 | Early termination on the ordered scan would save a sort of at most a few thousand 20-byte entries. Rejected for redundancy with the rowid. |
| WITHOUT ROWID for `chunks` | Recommended by SQLite only for rows well under 1/20 page; chunk rows are 3–10 KB. |
| `ANALYZE` / `PRAGMA optimize` | Index selection here is decided by constraint matching, not statistics. Not needed. |

Composite width never exceeds 2 columns; no random PKs anywhere (`jobs.id` is a UUID but has one row; `runs.id` has ≤ 100).

---

## 5. Repository Method → SQL Mapping

General contract for `repository.py`:

- One session, one transaction per call; `expire_on_commit=False`; records returned as frozen dataclasses (`JobRecord`, `RunRecord`, `LeaseRecord`, domain `Chunk`), never live ORM instances.
- Mode **W** = `BEGIN IMMEDIATE` (write), mode **R** = `BEGIN` (deferred, read-only). How the mode is emitted is in Section 6.
- Transitions use SQLAlchemy Core `update(Chunk).where(...)` and check `result.rowcount`. Never `obj.status = ...; session.commit()`.
- `IN (:ids)` lists are chunked at 500 ids per statement (SQLite `SQLITE_MAX_VARIABLE_NUMBER` is 999 on builds before 3.32).
- Every public method is wrapped by one boundary decorator mapping exceptions to `Err`: `sqlite3.OperationalError` with "database is locked" → `STATE_LOCKED` (JOB_FATAL); other `OperationalError`/`DatabaseError` → `STATE_CORRUPT` (JOB_FATAL); `IntegrityError` → `INTERNAL` (JOB_FATAL, message includes the constraint name); `ValueError` from type decorators → `INTERNAL`. Message text never includes row contents (may contain book text; `last_error` may echo provider messages, which are already redacted upstream).
- `:now` is always passed by the caller or taken from one `utcnow()` helper, formatted by `UtcTimestamp`; never mixed with SQLite time functions.

### 5.1 JobRepository

| Method | Mode | SQL | Precondition / result |
|---|---|---|---|
| `schema_version()` | R | `SELECT schema_version FROM schema_meta WHERE id = 1` | No table / no row → `Err(STATE_CORRUPT)`. (Normally called by `session.py` at open, see 7.) |
| `get_or_create_job(spec)` | W | `SELECT <cols> FROM jobs LIMIT 2` → if 1 row: return it; if 0 rows: `INSERT INTO jobs (id, singleton, input_path, input_sha256, status, tool_version, warnings, created_at, updated_at) VALUES (:uuid, 1, :path, :sha, 'CREATED', :ver, '[]', :now, :now)` → return; if 2 rows: `Err(STATE_CORRUPT)` | The repository never compares `spec.input_sha256` with the existing row — hash binding is the orchestrator's pre-flight step 4. A concurrent second creator fails on `UNIQUE(singleton)` → `IntegrityError` → retry the SELECT once inside the same method. |
| `get_job(job_id)` | R | `SELECT <cols> FROM jobs WHERE id = :job` | `Ok(None)` when absent. |
| `update_job(job_id, patch)` | W | `UPDATE jobs SET <only fields present in patch>, updated_at = :now WHERE id = :job` | `rowcount = 0` → `Err(STATE_NOT_FOUND)`. Empty patch → `Ok(None)` without a transaction. Patchable: `status, source_md_sha256, chunk_min_chars, chunk_max_chars, provider, glossary_strategy, provider_glossary_id, glossary_hash, extractor_name, fallback_used, detected_language, warnings`. |
| `start_run(job_id, run_id, command)` (+ `tool_version`, `provider`, `provider_switch_allowed` — see D1) | W | `INSERT INTO runs (id, job_id, command, tool_version, provider, provider_switch_allowed, started_at, chunks_completed, chunks_failed, chars_sent, provider_calls, rate_limited_count) VALUES (:run, :job, :cmd, :ver, :prov, :switch, :now, 0, 0, 0, 0, 0)` | Duplicate `run_id` → `IntegrityError` → `Err(INTERNAL)`. |
| `finish_run(job_id, run_id, outcome)` | W | `UPDATE runs SET finished_at = :now, outcome = :outcome, exit_code = :code, chunks_completed = :c, chunks_failed = :f, chars_sent = :cs, provider_calls = :pc, rate_limited_count = :rl WHERE id = :run AND job_id = :job AND finished_at IS NULL` | `rowcount = 0` → `Err(STATE_NOT_FOUND)` ("run missing or already finished"). |
| `get_last_run(job_id)` **(A1)** | R | `SELECT <cols> FROM runs WHERE job_id = :job ORDER BY started_at DESC, id DESC LIMIT 1` | For `status` ("last run outcome"). |

### 5.2 ChunkRepository

| Method | Mode | SQL | Precondition / result |
|---|---|---|---|
| `replace_all(job_id, chunks)` | W | 1. `SELECT COUNT(*) FROM chunks WHERE job_id = :job AND status = 'COMPLETED'` → must be 0.<br>2. `DELETE FROM chunks WHERE job_id = :job`<br>3. `INSERT INTO chunks (id, job_id, kind, translatable, source_text, content_hash, char_count, parent_block_id, sub_index, sub_count, heading_path, status, retry_count, review_flag, warnings, chars_sent, chars_billed, attempts, created_at, updated_at) VALUES (...)` as `executemany`, batches of 500 | Python pre-checks before any SQL: ids are exactly `1..n` in order, `char_count == len(source_text)`, `content_hash` matches → else `Err(CHUNK_INTEGRITY, JOB_FATAL)`. Step 1 failing → `Err(INTERNAL, JOB_FATAL, "COMPLETED chunks present")` — the orchestrator must never call this with completed rows (it archives on `--fresh` instead). Returns `Ok(n)`. Ordering with `update_job(source_md_sha256, chunk_min/max)`: orchestrator calls `update_job` **first**, then `replace_all`. Either crash order is safe: hash set + no chunks → re-chunk; chunks present + old hash → hash mismatch with `completed == 0` → re-chunk (O1 default). |
| `requeue_processing(job_id)` | W | `UPDATE chunks SET status = 'PENDING', claimed_at = NULL, updated_at = :now WHERE job_id = :job AND status = 'PROCESSING'` | Unconditional (3.2). `retry_count`, `last_run_id` untouched (forensics: which run orphaned it); `next_attempt_at` is already NULL for PROCESSING rows (CHECK). Returns `Ok(rowcount)`. |
| `claim_pending(job_id, limit, now, run_id)` | W | 1. `SELECT id FROM chunks WHERE job_id = :job AND status = 'PENDING' AND (next_attempt_at IS NULL OR next_attempt_at <= :now) ORDER BY id LIMIT :limit`<br>2. `UPDATE chunks SET status = 'PROCESSING', next_attempt_at = NULL, last_run_id = :run, claimed_at = :now, updated_at = :now WHERE job_id = :job AND id IN (:ids) AND status = 'PENDING'`<br>3. `SELECT <all cols> FROM chunks WHERE id IN (:ids) ORDER BY id` | `limit <= 0` → `Ok([])` with no transaction. Step 1 empty → `Ok([])`. Step 2 `rowcount != len(ids)` → rollback, `Err(INTERNAL, JOB_FATAL)` (impossible under the lease; the check is the tripwire). Atomicity: see 5.3. `'PENDING'` must be a literal (partial index). |
| `complete(job_id, chunk_id, result)` | W | `UPDATE chunks SET status = 'COMPLETED', translated_text = :text, provider = :prov, glossary_strategy = :gs, glossary_hash = :gh, chars_sent = :cs, chars_billed = :cb, latency_ms = :lat, attempts = :att, review_flag = :rf, warnings = :warn, last_error = NULL, last_error_code = NULL, next_attempt_at = NULL, claimed_at = NULL, completed_at = :now, updated_at = :now WHERE job_id = :job AND id = :id AND status = 'PROCESSING'` | Returns `Ok(rowcount == 1)`. `False` = precondition failed (chunk was released by a cancellation race) — never overwrite (3.2). Text and status are in **one** statement; there is no observable state with one but not the other. `last_run_id` is not touched: it was set by the claim of the same run, so it already identifies the completing run. `retry_count` is kept as history; `last_error` is cleared so `status` does not show a stale error on a completed chunk. |
| `reschedule(job_id, chunk_id, error, next_attempt_at)` | W | `UPDATE chunks SET status = 'PENDING', retry_count = retry_count + 1, next_attempt_at = :next, last_error_code = :code, last_error = :msg, claimed_at = NULL, updated_at = :now WHERE job_id = :job AND id = :id AND status = 'PROCESSING'` | `:msg` truncated to 2 000 chars (suffix `…`) before bind. `rowcount = 0` → see D3. Max-retries is **not** checked here (orchestrator decides). `:next` must be strictly formatted; it is compared as a string by `claim_pending`. |
| `fail(job_id, chunk_id, error)` | W | `UPDATE chunks SET status = 'FAILED', last_error_code = :code, last_error = :msg, next_attempt_at = NULL, claimed_at = NULL, updated_at = :now WHERE job_id = :job AND id = :id AND status = 'PROCESSING'` | `retry_count` unchanged (it records how many retryable errors preceded). `last_run_id` kept → the operator can open `logs/run-<last_run_id>.jsonl` for the failure. `rowcount = 0` → D3. |
| `release(job_id, chunk_ids)` | W | `UPDATE chunks SET status = 'PENDING', claimed_at = NULL, updated_at = :now WHERE job_id = :job AND id IN (:ids) AND status = 'PROCESSING'` (batches of 500, one transaction) | Returns `Ok(rowcount)`; ids that are no longer PROCESSING (completed during the grace period) are skipped silently — expected. `retry_count`, `next_attempt_at` (NULL) unchanged. Empty list → `Ok(0)`, no transaction. |
| `reset_failed(job_id)` | W | `UPDATE chunks SET status = 'PENDING', retry_count = 0, next_attempt_at = NULL, last_error = NULL, last_error_code = NULL, updated_at = :now WHERE job_id = :job AND status = 'FAILED'` | Returns `Ok(rowcount)`. Touches nothing else (translated_text is NULL on FAILED rows by CHECK). |
| `counts(job_id)` | R | `SELECT status, COUNT(*) FROM chunks WHERE job_id = :job GROUP BY status` plus `SELECT COUNT(*) FROM chunks WHERE job_id = :job AND status = 'PENDING' AND next_attempt_at IS NOT NULL AND next_attempt_at > :now` | Both served by I2/I3 (index-only). Missing statuses → 0. Second query feeds the "M waiting for backoff" figure (D2). |
| `earliest_next_attempt(job_id)` | R | `SELECT MIN(next_attempt_at) FROM chunks WHERE job_id = :job AND status = 'PENDING' AND next_attempt_at IS NOT NULL` | `Ok(None)` when nothing is waiting. Called only when `claim_pending` returned `[]` with nothing in flight, so every PENDING row has a future `next_attempt_at`; the `IS NOT NULL` guard is defensive. |
| `iter_ordered(job_id, batch_size=200)` | R (one transaction spanning the whole iteration) | Keyset loop: `SELECT <all cols> FROM chunks WHERE job_id = :job AND id > :last_id ORDER BY id LIMIT :batch` until fewer than `batch` rows come back | Rowid order = physical order → sequential page reads; no `OFFSET` (O(n²)). One deferred read transaction gives a consistent snapshot; WAL readers never block, and no writer exists during assembly (lease held, translate finished). Memory: at most `batch` rows alive (E-19). Exception boundary: D4. |
| `totals(job_id)` | R | `SELECT COUNT(*), COALESCE(SUM(chars_sent),0), COALESCE(SUM(chars_billed),0), COALESCE(SUM(CASE WHEN status='COMPLETED' THEN char_count ELSE 0 END),0), COALESCE(SUM(CASE WHEN translatable=1 THEN 1 ELSE 0 END),0), COALESCE(SUM(char_count),0) FROM chunks WHERE job_id = :job` | Table scan by design (Section 4). `SUM(CASE…)` instead of `FILTER` (3.30+) to stay on the version floor. |
| `failed_ids(job_id)` **(A2)** | R | `SELECT id FROM chunks WHERE job_id = :job AND status = 'FAILED' ORDER BY id` | I2, no sort. For `JobSummary.failed_chunk_ids`, the export refusal message, and the final summary (AC US-7/3). |
| `review_ids(job_id)` **(A2)** | R | `SELECT id FROM chunks WHERE job_id = :job AND review_flag = 1 ORDER BY id` | I4 (`review_flag = 1` literal). |
| `count_completed_with_other_glossary(job_id, glossary_hash)` **(A3)** | R | `SELECT COUNT(*) FROM chunks WHERE job_id = :job AND status = 'COMPLETED' AND translatable = 1 AND (glossary_hash IS NULL OR glossary_hash <> :hash)` | Resume warning "N chunks translated with previous glossary" (5.1 step 6, R5). |
| `count_completed_by_run(run_id)` **(A4)** | R | `SELECT SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END), SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) FROM chunks WHERE last_run_id = :run` | Counter recovery for a crashed run (`runs.outcome IS NULL`) in `status`. Scan, once. |

### 5.3 Atomic claim in SQLite

Three options were evaluated:

1. `UPDATE ... WHERE id IN (SELECT id ... ORDER BY id LIMIT n) RETURNING *` — one statement, but `RETURNING` needs SQLite ≥ 3.35.0 (2021-03). Python 3.10 on Ubuntu 20.04 (`libsqlite3` 3.31) and some conda/pyenv builds fall below that.
2. Same `UPDATE` without `RETURNING`, then re-select by `last_run_id = :run AND claimed_at = :now` — works everywhere but identifies the claimed set indirectly.
3. **Chosen:** two-step (SELECT ids → UPDATE by ids with `status = 'PENDING'` re-check → SELECT rows) inside one `BEGIN IMMEDIATE` transaction.

Why 3 is atomic: `BEGIN IMMEDIATE` takes SQLite's RESERVED lock at transaction start. Only one connection in the whole system can hold it; every other writer's `BEGIN IMMEDIATE` blocks (up to `busy_timeout`) and readers keep reading the pre-transaction snapshot. Nothing can change a `PENDING` row between step 1 and step 2. The `AND status = 'PENDING'` in step 2 plus the `rowcount == len(ids)` check are a belt-and-braces tripwire that would expose a broken transaction setup in tests rather than in production. Within one process the orchestrator's `asyncio.Lock` serializes calls anyway; across processes the lease prevents a second scheduler. The transaction-level guarantee therefore matters mostly for the lease acquire itself (5.4) and as defence in depth.

Version policy: **no `RETURNING`, no `UPSERT`** anywhere in v1. Hard floor SQLite 3.8.0 (partial indexes, `PRAGMA query_only`); tested floor 3.31.0. `session.py` checks `sqlite3.sqlite_version_info >= (3, 8, 0)` at open and returns a USER-scope error otherwise (error code: see Open Question Q5). If a later version wants the single-statement form, it is a drop-in change guarded by `sqlite_version_info >= (3, 35, 0)` with option 3 kept as the fallback — but two code paths are not worth it at these volumes.

### 5.4 LeaseRepository

| Method | Mode | SQL | Semantics |
|---|---|---|---|
| `acquire(job_id, holder, stale_after_s)` | W | 1. `SELECT holder_pid, holder_host, run_id, heartbeat_at FROM lease WHERE job_id = :job`<br>2a. no row → `INSERT INTO lease (job_id, holder_pid, holder_host, run_id, acquired_at, heartbeat_at) VALUES (:job, :pid, :host, :run, :now, :now)`<br>2b. row with `heartbeat_at <= :cutoff` **or** same `(holder_pid, holder_host)` → `UPDATE lease SET holder_pid = :pid, holder_host = :host, run_id = :run, acquired_at = :now, heartbeat_at = :now WHERE job_id = :job`<br>2c. otherwise → `ROLLBACK` | `cutoff = now - stale_after_s`, formatted in Python. Returns `Ok(True)` for 2a/2b, `Ok(False)` for 2c. Same-holder takeover is safe: two live processes cannot share a pid on one host; it covers a process that re-enters acquire after a failed start. `BEGIN IMMEDIATE` is *mandatory* here: with a deferred transaction two processes could both read "no row" under shared locks and one of them would get `SQLITE_BUSY` on lock upgrade without waiting (SQLite does not retry a deferred→write upgrade via `busy_timeout` in that pattern). |
| `get(job_id)` **(A5)** | R | `SELECT <cols> FROM lease WHERE job_id = :job` | For the `STATE_LOCKED` message (holder pid/host/time, 5.6) and for `status` ("lease holder"). `Ok(None)` when free. |
| `heartbeat(job_id, holder)` | W | `UPDATE lease SET heartbeat_at = :now WHERE job_id = :job AND holder_pid = :pid AND holder_host = :host AND run_id = :run` | `rowcount = 0` → `Err(STATE_LOCKED, JOB_FATAL, "lease lost")`: someone ran `--force-unlock` (or took over after a stall > 60 s). The orchestrator must treat this as cancel-with-release; continuing would risk a double-sender. |
| `release(job_id, holder)` | W | `DELETE FROM lease WHERE job_id = :job AND holder_pid = :pid AND holder_host = :host AND run_id = :run` | `rowcount = 0` → `Ok(None)` but logged at WARNING (lease already broken/taken). |
| `force_break(job_id)` | W | `DELETE FROM lease WHERE job_id = :job` | `--force-unlock`. |

Timing: heartbeat every 10 s, staleness 60 s (settings `BOOK_TRANSLATOR_LEASE_STALE_S`). The heartbeat task must go through the same `asyncio.Lock` as worker writes; a `replace_all` of 5 000 rows (~0.3 s) or a `totals()` scan can delay it, which is far inside the 60 s budget.

---

## 6. PRAGMA and Connection Settings (`session.py`)

Engine: `create_engine(f"sqlite:///{abs_path}", connect_args={"check_same_thread": False}, poolclass=QueuePool, pool_size=2, max_overflow=3)`. `check_same_thread=False` is required because repository calls run in `asyncio.to_thread` worker threads; sharing is safe because the orchestrator lock serializes them. Tests: `create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})` — one shared in-memory connection.

Per-connection **`connect` event** (fires for every pooled DBAPI connection, outside any transaction, which is what these PRAGMAs require):

```sql
PRAGMA journal_mode=WAL;      -- persistent in the file; harmless to repeat. On ':memory:' it answers 'memory' — assert 'wal' only for file DBs.
PRAGMA synchronous=NORMAL;    -- per connection; WAL + NORMAL: durable across process crash, may lose the last commits on power loss/OS crash (Section 9)
PRAGMA busy_timeout=5000;     -- per connection; only ever exercised by a second process (status/lease attempt)
PRAGMA foreign_keys=ON;       -- per connection; default OFF in SQLite
PRAGMA query_only=1;          -- ONLY for read-only opens (status command); any write then fails with an error → repository boundary → Err(INTERNAL)
```

The same hook also sets `dbapi_connection.isolation_level = None`. This disables pysqlite's implicit deferred `BEGIN` (which is emitted lazily before the first DML, *not* before a SELECT — so a SELECT-then-UPDATE would otherwise not share one transaction). Transactions are then started explicitly by an engine-level **`begin` event** that emits `BEGIN IMMEDIATE` when the connection's execution option `sqlite_txn_mode == "IMMEDIATE"` and plain `BEGIN` otherwise. The repository obtains a write session via `session.connection(execution_options={"sqlite_txn_mode": "IMMEDIATE"})` before its first statement (helper `write_session()` / `read_session()` context managers in `session.py`). `COMMIT`/`ROLLBACK` remain SQLAlchemy's. Acceptance test: capture emitted statements and assert every W-mode method's first statement is `BEGIN IMMEDIATE`, every R-mode method's is `BEGIN`. If the execution-option route proves awkward, the fallback is two engines (`read_engine`, `write_engine`) on the same file with a fixed `begin` listener each; for in-memory tests they must then share one DB via `file:memdb1?mode=memory&cache=shared&uri=true`.

Other settings: `sessionmaker(expire_on_commit=False, autoflush=False)`; page size default 4 096 (rows > 4 KB use overflow pages; acceptable); `PRAGMA wal_checkpoint(PASSIVE)` emitted by `close_database()` before `engine.dispose()` so the `-wal` file does not linger at its maximum size after a long run (the last connection closing also checkpoints and removes `-wal`/`-shm`).

`open_database(path: Path, *, read_only: bool, tool_version: str) -> Result[Database]`:

1. `read_only` and file missing → `Err(STATE_NOT_FOUND, USER)`. Not read-only and file missing or 0 bytes → create schema (7).
2. `sqlite3.sqlite_version_info` floor check.
3. Read `schema_meta` (7); on a missing table in an existing non-empty file → `Err(STATE_CORRUPT)`.
4. Return `Database(engine, read_session, write_session, schema_version)`.

---

## 7. Schema Versioning and Migrations

`schema_meta` holds one row (`id = 1`). `models.py` exports `CURRENT_SCHEMA_VERSION = 1`.

Open-time check in `session.py` (not in the repositories):

| Found | Action |
|---|---|
| new/empty file, not read-only | `Base.metadata.create_all()` + `INSERT INTO schema_meta (id, schema_version, created_by_tool_version, created_at) VALUES (1, 1, :ver, :now)` in **one** transaction (SQLite DDL is transactional). |
| `schema_version == CURRENT` | proceed |
| `schema_version < CURRENT`, not read-only | back up the file to `state-archive/<ts>-pre-migrate-v<n>/translation_state.db` (online backup API, 8.1), then run every step `n → n+1` in **one** transaction; the last statement of the transaction is `UPDATE schema_meta SET schema_version = :new, upgraded_by_tool_version = :ver, upgraded_at = :now WHERE id = 1`; commit; re-read and verify. Any exception → rollback → `Err(STATE_SCHEMA_INCOMPATIBLE)` with the backup path in the message. |
| `schema_version < CURRENT`, read-only (`status`) | `Err(STATE_SCHEMA_INCOMPATIBLE, USER, "run translate once to migrate")` — never migrate under `query_only`. |
| `schema_version > CURRENT` | `Err(STATE_SCHEMA_INCOMPATIBLE, USER, "state created by a newer tool version X")` (AC US-8/4). |
| table missing / row missing | `Err(STATE_CORRUPT, USER)` |

Migration function table (`MIGRATIONS: dict[int, Callable[[Connection], None]]`, key = *from* version):

| From → To | Function | Content |
|---|---|---|
| 0 → 1 | `_create_v1` | `create_all` + `schema_meta` row (the "new file" path above) |

Rules for future migrations (documented in `session.py` docstring): idempotent DDL (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`); column additions via `ALTER TABLE ... ADD COLUMN` with defaults; anything needing a table rebuild follows SQLite's 12-step procedure with `PRAGMA foreign_keys=OFF` for that connection *before* `BEGIN`; version bump is always the last statement; `CURRENT_SCHEMA_VERSION` is bumped in the same commit as the migration function. Alembic is deliberately not used (single-file DB, hand-written steps are shorter than the tooling).

`create_all` is acceptable as the v1 DDL source **only if** the models reproduce this document exactly: `CheckConstraint` objects with the names `ck_<table>_<what>`, `Index(..., sqlite_where=text("status = 'PENDING'"))` for partial indexes, `Boolean(create_constraint=True)`. A test dumps `sqlite_master` and asserts the expected table/index names and the partial-index `WHERE` texts.

---

## 8. Data Lifecycle

### 8.1 `--fresh`

1. `close_database()` (checkpoint + dispose) so no connection of this process is open.
2. Archive with the **online backup API** (`sqlite3.connect(src).backup(sqlite3.connect(dst))`) into `<output>/state-archive/<YYYYMMDDTHHMMSSZ>/translation_state.db`. Backup produces a consistent single file even if a stale `-wal` exists, which a plain `shutil.copy2` of the main file would silently drop. `-wal`/`-shm` are not archived.
3. Delete `translation_state.db`, `translation_state.db-wal`, `translation_state.db-shm` from the output directory.
4. `open_database()` creates a fresh v1 file and a new job (new `jobs.id`). Stage artifacts (`source_book.md`, `glossary.json`, `summary.json`) are archived alongside by the orchestrator (5.5); that is outside the DB layer.

Nothing is ever deleted without the archive step; archives are never pruned automatically (mention `state-archive/` in the README).

### 8.2 What each maintenance operation touches

| Operation | Rows | Columns |
|---|---|---|
| `reset_failed` (`--retry-failed`) | `status = 'FAILED'` only | `status, retry_count, next_attempt_at, last_error, last_error_code, updated_at` |
| `requeue_processing` (every run start) | `status = 'PROCESSING'` | `status, claimed_at, updated_at` |
| `release` (Ctrl+C) | given ids still PROCESSING | same as above |
| `force_break` | the lease row | deletes it |
| `replace_all` | all chunk rows | delete + insert (only while `completed == 0`) |

COMPLETED rows are modified by nothing except `replace_all` (which refuses when any exist). `translated_text` is written by exactly one statement in the codebase (`complete`).

### 8.3 Retention

- `runs`: kept forever (≤ 100 × ~300 B). No pruning in v1.
- `chunks`: kept until `--fresh`; the exported book is derived from them, so they are the audit trail for "what was sent and what came back".
- `lease`: deleted on clean exit; stale rows are taken over, never accumulated.

### 8.4 Size estimate

| Book | Chunks | Per row (source + translated ≈ +12 % for Turkish + ~350 B metadata) | Data | Indexes (I1–I4) | WAL peak (autocheckpoint 1 000 pages) | File on disk |
|---|---|---|---|---|---|---|
| 200 pages, 800 chunks × ~1.25 KB source | 800 | ~3 KB typical / 10 KB upper bound (12.3/9) | 2.4 MB typical, 8 MB bound | < 0.1 MB | ≤ 4 MB | **~3–8 MB** (+ up to 4 MB `-wal` while running) |
| 1 000 pages, ~4 000 chunks | 4 000 | same | 12 MB typical, 40 MB bound | < 0.5 MB | ≤ 4 MB | **~13–45 MB** |

Free-page churn from `reschedule` updates is negligible (updates rewrite small rows in place when the size is unchanged; text columns are not touched until `complete`). No `VACUUM` in v1.

---

## 9. Integrity and Concurrency Notes

- **Single writer by construction.** The lease guarantees one process; inside it the orchestrator's `asyncio.Lock` serializes every repository call — worker transitions, the 10 s heartbeat, and the scheduler's claim. SQLite's own locking is therefore a second line of defence, exercised only by `status` in another process (WAL readers never block and are never blocked) and by a competing `acquire` (blocks ≤ `busy_timeout`, then loses on the staleness rule).
- **Heartbeat vs worker writes.** Same lock, same connection pool, same `BEGIN IMMEDIATE` discipline; a heartbeat is one `UPDATE` (< 1 ms). Worst-case heartbeat delay = longest single repository call (`replace_all`, ~0.3 s at 5 000 rows), against a 60 s staleness window.
- **SIGKILL mid-transaction.** With WAL, a transaction is durable only once its commit frame is in the `-wal` file. A process killed before that leaves frames without a commit record; the next open ignores them (automatic rollback, no recovery step). Consequences: a chunk killed between `claim` and `complete` stays `PROCESSING` and is re-queued at the next start (E-09); a partially written `replace_all` vanishes entirely (chunk count 0 → re-chunk). No half-written `translated_text` can exist because text and `COMPLETED` are one statement, and a statement is atomic within its transaction.
- **`synchronous=NORMAL` trade-off.** In WAL mode NORMAL is fully durable against *application* crashes (the case NFR-02 names). Against OS crash / power loss, transactions committed after the last checkpoint may roll back — the only effect here would be a few COMPLETED chunks reverting to PENDING and being re-sent. That is the accepted cost of the design's PRAGMA choice; a `BOOK_TRANSLATOR_SQLITE_SYNCHRONOUS=FULL` override is a two-line addition if strict zero-resend under power loss is ever required (Open Question Q6).
- **Precondition writes are the state machine.** Every transition is `UPDATE ... WHERE status = '<from>'` and reports `rowcount`; illegal transitions are impossible at the DB level regardless of orchestrator bugs, and the CHECK constraints (`translated_text` ⇔ `COMPLETED`, `next_attempt_at` only on `PENDING`, enums, non-negative counters) reject inconsistent rows even from hand edits with the `sqlite3` shell.
- **`next_attempt_at` correctness depends on format discipline.** Comparison is textual; the 27-char fixed format and the `length = 27` CHECK are what make `<=` mean "earlier or equal". Never write timestamps with `datetime.isoformat()` or SQLite time functions.
- **Corruption detection.** `PRAGMA quick_check` is not run on every open (cost grows with the file). `status --verify` may run it (optional; a few hundred ms at 40 MB).

---

## 10. Hand-off to the DB Developer

Files to produce (all under the project root):

| File | Contents |
|---|---|
| `src/database/models.py` | `Base`; `UtcTimestamp`, `JsonList` type decorators; ORM classes `SchemaMeta`, `Job`, `Chunk`, `Run`, `Lease` exactly per Section 2 (named `CheckConstraint`s, `Boolean(create_constraint=True)`, FKs with `ondelete="CASCADE"`); the four `Index` objects of Section 4 with `sqlite_where`; string constants for every enum value list (`CHUNK_STATUSES`, `JOB_STATUSES`, `RUN_OUTCOMES`, `GLOSSARY_STRATEGIES`, `CHUNK_KINDS`, `RUN_COMMANDS`); `CURRENT_SCHEMA_VERSION = 1`. No business logic. |
| `src/database/session.py` | `make_engine(path_or_memory, read_only)`, the `connect` PRAGMA hook, the `begin` mode hook, `read_session()` / `write_session()` context managers, `open_database() -> Result[Database]` with version check + `MIGRATIONS` table, `close_database()`, `archive_database(src, dest_dir) -> Result[Path]` (backup API), `utcnow()`. |
| `src/database/repository.py` | `JobRepository`, `ChunkRepository`, `LeaseRepository` implementing Section 5 (incl. additions A1–A5 and deviations D1–D4 once accepted); record dataclasses `JobRecord`, `JobSpec`, `JobPatch`, `RunRecord`, `RunOutcome`, `LeaseHolder`, `LeaseRecord`, `StatusCounts`, `ChunkTotals`; the exception→`Err` boundary decorator; `last_error` truncation; IN-list batching. Core `update()/insert()/delete()` for every write; ORM only for reads and schema definition. |
| `tests/test_repository.py` | In-memory SQLite (`StaticPool`), one fixture that opens a DB, creates a job and inserts N synthetic chunks; the acceptance checks below. |

Acceptance checks (each a test):

1. Schema: `sqlite_master` contains the five tables and indexes `ix_chunks_job_status`, `ix_chunks_pending_backoff` (with `WHERE status = 'PENDING'`), `ix_chunks_review` (with `WHERE review_flag = 1`); `schema_meta` row is `(1, 1, tool_version, ts)`.
2. PRAGMAs: on a file DB `journal_mode` is `wal`, `foreign_keys` is 1, `busy_timeout` is 5000, `synchronous` is 1 (NORMAL); read-only open has `query_only` = 1 and a write attempt returns `Err`.
3. Transaction mode: statement capture shows `BEGIN IMMEDIATE` first for every W method and `BEGIN` for every R method.
4. `claim_pending`: returns ids in ascending order, ≤ limit, only PENDING with NULL or past `next_attempt_at`; claimed rows are PROCESSING with `next_attempt_at NULL`, `last_run_id`/`claimed_at` set; a second call returns disjoint ids; `limit = 0` returns `[]`; `EXPLAIN QUERY PLAN` of the SELECT uses exactly `ix_chunks_job_status` (no table scan, no TEMP B-TREE; the I3 note in Section 4 explains why the partial index is not the right plan for an `ORDER BY id LIMIT n` claim) and the `earliest_next_attempt` query uses `ix_chunks_pending_backoff`.
5. Claim atomicity across connections: with two engines on one temp file, start a W transaction on engine A that has executed step 1 of a claim but not committed; a claim on engine B blocks until A commits and then sees none of A's ids (use a thread + timeout ≤ busy_timeout).
6. `complete`: `True` from PROCESSING; `False` from PENDING/FAILED/COMPLETED with the row unchanged; after success `translated_text` set, `last_error` NULL, `completed_at` set; CHECK rejects a direct `UPDATE ... SET status='COMPLETED'` without text and `SET translated_text=...` on a PENDING row.
7. `reschedule`: precondition PROCESSING; `retry_count` +1; `next_attempt_at` stored as 27 chars and round-trips to the same aware datetime; a naive datetime raises before SQL.
8. `fail`: precondition PROCESSING; `retry_count` unchanged; `last_error` longer than 2 000 chars is truncated (and the DB CHECK rejects an untruncated one).
9. `release`: only PROCESSING ids change; returns the count; `retry_count` unchanged.
10. `requeue_processing` and `reset_failed`: touch exactly the listed columns; return counts; COMPLETED rows untouched (assert `translated_text` and `completed_at` identical before/after).
11. `next_attempt_at` honoured: a rescheduled chunk is not claimed with `now < next_attempt_at` and is claimed with `now >= next_attempt_at`; `earliest_next_attempt` returns the minimum and `None` when nothing waits.
12. `counts` / `totals` / `failed_ids` / `review_ids` / A3 / A4 return the expected numbers on a mixed fixture (incl. zero rows for absent statuses).
13. `iter_ordered` yields all rows in id order with `batch_size=3` on 10 rows and holds at most one batch in memory (spy on the SELECT count = 4).
14. `replace_all`: refuses when a COMPLETED row exists; rejects non-dense ids and wrong `char_count`/`content_hash` with `CHUNK_INTEGRITY`; inserts 5 000 rows in < 2 s; CHECK `ck_chunks_subsplit` rejects `sub_index >= sub_count`; `translatable = 1` with `kind = 'CODE'` is rejected.
15. Lease: `acquire` → `True` on empty; `False` for a different holder with a fresh heartbeat; `True` after `heartbeat_at` is older than `stale_after_s`; `True` for the same pid/host; `heartbeat` returns `Err(STATE_LOCKED)` after `force_break`; `release` by a non-holder is a no-op.
16. Job: `get_or_create_job` twice returns the same id; a manual second insert fails on `singleton`; `update_job` with an empty patch runs no SQL; `finish_run` twice → second is `Err(STATE_NOT_FOUND)`.
17. Versioning: a file with `schema_version = 99` → `STATE_SCHEMA_INCOMPATIBLE`; a file without `schema_meta` → `STATE_CORRUPT`; `archive_database` produces a file that opens and has the same row counts.
18. Boundary: monkeypatching `session.execute` to raise `sqlite3.OperationalError("database is locked")` yields `Err(STATE_LOCKED)`; an `IntegrityError` yields `Err(INTERNAL)` whose message names the constraint but contains no row text.

Performance targets (in-process, file DB on SSD, 5 000-chunk fixture):

| Operation | Target |
|---|---|
| `complete` / `reschedule` / `fail` / `heartbeat` | < 2 ms |
| `claim_pending(limit=4)` | < 5 ms |
| `counts`, `earliest_next_attempt`, `failed_ids` | < 10 ms |
| `totals` (table scan) | < 100 ms |
| `iter_ordered` full pass | < 1 s |
| `replace_all` 5 000 rows | < 2 s |

---

## 11. Deviations from Section 2.5 and Open Questions

Deviations proposed (need acceptance by the Solution Architect / user before the DB Developer codes them):

- **D1** `start_run` also takes `tool_version`, `provider`, `provider_switch_allowed` (facts listed in 3.1 but absent from the signature).
- **D2** `StatusCounts` gains `waiting_backoff: int` (design 3.2 wants "M of them waiting for backoff" in `status`).
- **D3** `reschedule`, `fail` return `Result[bool]` like `complete` (precondition outcome is data, not an error); the orchestrator logs `False` as an integrity warning. Alternative: keep `Result[None]` and return `Err(INTERNAL)` on `rowcount = 0`.
- **D4** `iter_ordered` returns a plain iterator that may raise on a broken DB; the assembler's outer `Result` boundary catches it. Alternative: `ordered_ids() -> Result[List[int]]` + `get_many(ids) -> Result[List[Chunk]]`, which keeps the "never raise across a boundary" rule strictly but costs one extra statement per batch.
- **D5** `complete` needs `run_id` and `glossary_hash`, neither in `TranslationResult`. Proposal: `ChunkRepository(session_factory, run_id, glossary_hash)` bound per run; `claim_pending`'s explicit `run_id` parameter stays for API symmetry.
- **A1–A5** additions: `get_last_run`, `failed_ids`/`review_ids`, `count_completed_with_other_glossary`, `count_completed_by_run`, `LeaseRepository.get`.

Open questions:

| # | Question | Default if unanswered |
|---|---|---|
| Q5 | Which `ErrorCode` for "SQLite library too old" at open? None fits. | Add `SQLITE_UNSUPPORTED` (USER scope); until then reuse `STATE_SCHEMA_INCOMPATIBLE` with a clear message. |
| Q6 | Is `synchronous=NORMAL`'s power-loss window acceptable, or add a `FULL` override setting? | Keep NORMAL; document. |
| Q7 | Should `status --verify` run `PRAGMA quick_check`? | Yes, opt-in flag only. |
| Q8 | `last_error` limit of 2 000 chars — enough for provider messages with request ids? | Yes; JSONL log holds the full text. |

---
✅ DB Architect tamamlandı.
➡️  Sonraki adım: DB Developer
