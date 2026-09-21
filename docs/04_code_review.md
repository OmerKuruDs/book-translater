# Code Review: `book-translator` (Faz A + Faz B)

**Modül:** book-translator (Python 3.10+ CLI)
**İnceleme Tarihi:** 2026-09-18
**Reviewer:** code-reviewer (Claude)
**İncelenen girdi:** `docs/01_business_analysis.md`, `docs/02_solution_design.md`, `docs/03_db_design.md`, `docs/durum.md`, `src/` (44 modül), `tests/` (295 geçti, 1 atlandı)

## Verdict: **CHANGES REQUIRED**

---

## Türkçe Özet (kullanıcı için)

Kod genel olarak tasarıma sadık ve dikkatli yazılmış: `Result` deseni her katmanda uygulanmış, DB'ye yazan her geçiş `UPDATE ... WHERE status='...'` ile korunmuş, COM/PDF/dosya kaynakları her yolda kapatılıyor, gerçek CLI denemesinde başlık/altbilgi temizliği, tire birleştirme, başlık seviyeleri ve kod bloğu doğru çıktı; anahtar (`DEEPL_API_KEY`) log/DB/summary'ye sızmıyor.

Yine de merge öncesi düzeltilmesi gereken sorunlar var:

- 1 **Blocker**: `DEEPL_API_KEY` içinde boşluk varsa (örn. `.env`'e yapıştırırken sonuna bir şey gelmişse) hata mesajı anahtarın tamamını ekrana ve `--json` çıktısına basıyor (pydantic `input_value=...`). Log filtresi burada devrede değil.
- 5 **Critical**: (1) hiç chunk yokken `export` 0 baytlık `translated_book.md` yazıp EPUB'da çöküyor; (2) çeviri sırasında DB yazma hatası (disk dolu vb.) sessizce yutuluyor, chunk `PROCESSING` kalıyor, çıkış kodu 0 olabiliyor ve çeviri tekrar ücretlendiriliyor; (3) paragraf satırının hemen altındaki ``` kod bloğu (arada boş satır yoksa) paragrafa yapışıyor ve kod sağlayıcıya gönderiliyor; (4) kitap metnindeki `<` ve `&` EPUB XHTML'ine kaçışsız giriyor (teknik kitaplarda bölüm bozuluyor); (5) `run`/`extract` elle düzenlenmiş `source_book.md`'yi arşivlemeden eziyor.
- 17 **Warning**, 12 **Suggestion**: ayrıntılar aşağıda. `docs/durum.md`'deki sapmaların çoğu kabul edildi; reddedilenler: orchestrator'ın repository'yi atlayıp doğrudan SQL çalıştırması (`get_single_job()` eklenmeli) ve claim sorgusunun tasarımdaki kısmi indeksi kullanmaması (test gevşetilmiş).

Sonraki adım: Blocker + Critical'lar düzeltilip testler eklendikten sonra QA'ya geçilmeli. QA için risk listesi en altta.

---

## Scope and method

- Toolchain re-verified first: `pytest -q` (295 passed, 1 skipped), `mypy src` (clean, strict), `ruff check src tests` (clean).
- Every finding below was verified by reading the code at the cited lines and, where marked, by a probe script or the real CLI run (6-page generated PDF: running header, page numbers, 18/13 pt headings, 10 pt body, hyphen split across pages, numbered list, Courier block, image-only page). Probe scripts live in the session scratchpad, no project file was modified.
- Severity follows the Xeneff table: Blocker = data loss / security / crash in production; Critical = business-rule or architecture-contract violation; Warning = performance / maintainability / robustness risk; Suggestion = optional improvement.

---

## Findings

### Blocker

#### [CR-01] `DEEPL_API_KEY` is echoed in the error message when it contains internal whitespace — `src/config.py:89-95`, `src/cli.py:317-331`

**Code:**
```python
# config.py
except ValueError as exc:  # pydantic ValidationError is a ValueError subclass
    return err(ErrorCode.PROVIDER_CONFIG, f"invalid configuration: {exc}", ErrorScope.USER)
```

**Verified (probe):** `DEEPL_API_KEY="0123abcd-1111-2222-3333-444455556666:fx extra"` → `load_settings()` returns `Err` whose message contains
`input_value='0123abcd-1111-2222-3333-444455556666:fx extra', input_type=str`. `cli._fail` prints `error.message` on the console (`console.print`, not the logging path) and puts it verbatim into the `--json` payload; the `RedactionFilter` only covers `logging` records, so nothing masks it. A key with a stray trailing token still contains the real secret.

**Why it matters:** NFR-10 ("API keys ... never logged"), FR-26. Console output and `--json` are routinely captured in CI logs / tickets.

**Fix:** do not interpolate `str(exc)`. Build the message from `exc.errors(include_input=False)` (pydantic 2.x), e.g. `"; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in ...)`, and additionally run `RedactionFilter(secrets).redact_text()` over every message printed by `_fail` (defence in depth). Add a test asserting the key value is absent from stdout/stderr for a whitespace key.

### Critical

#### [CR-02] `export` on a job with zero chunks writes a 0-byte artifact and crashes the EPUB build — `src/pipeline/orchestrator.py:1997-2058`, `src/pipeline/assembler.py:160-239`, `src/exporters/epub_exporter.py:216-218, 268-277`

**Verified (real CLI):** after `extract` only (job `EXTRACTED`, 0 chunk rows) `export --output out` printed `INFO wrote translated_book.md (0 bytes)`, then `error EPUB build failed: ParserError`, followed by an `Exception ignored in ZipFile.__del__ ... I/O operation on closed file` traceback on stderr, exit 1; the empty `translated_book.md` stayed in the output directory (and was later archived by `--fresh`). Same with `--allow-partial`.

**Cause:** `assemble()` has no "no chunks" guard (empty iterator → `Ok(AssembledDocument(markdown=""))`); `_export_bound` never checks `counts.total`; the EPUB exporter falls back to a single empty chapter (216-218) and ebooklib raises inside `write_epub` (the unclosed `ZipFile` produces the `__del__` noise).

**Why it matters:** FR-22 / AC US-10/5 semantics ("run translate first"), E-18 (no half artifacts), and a raw traceback on stderr contradicts 2.1 ("a raw exception reaching the CLI is a bug").

**Fix:** in `_export_bound` (before any exporter runs) read `chunks.counts(job.id)` and return `Err(EXPORT_REFUSED_PARTIAL, "no chunks in state; run translate first", USER)` when `total == 0`; make `assemble()` return the same error for an empty stream; guard `EpubExporter.export` with `if not document.markdown.strip(): return err(EXPORT_FAILED, "empty document", JOB_FATAL)`. Add an orchestrator test for the chunk-less export.

#### [CR-03] JOB_FATAL repository errors inside worker bookkeeping are swallowed; the run can end with exit 0 while chunks are stuck `PROCESSING` — `src/pipeline/orchestrator.py:1790-1795, 1823-1826, 1855-1857, 1252-1267`

**Code:**
```python
done = await self._db(ctx.lock, ctx.chunks.complete, ctx.bound.job.id, chunk.chunk_id, result)
if isinstance(done, Err):
    log.error("complete failed: %s", done.error.message, extra={"event": "complete_failed"})
    return
```
The same pattern exists in `_fail` and for `reschedule`. The repository boundary classifies every SQLite failure (disk full, `STATE_LOCKED`, `STATE_CORRUPT`) as `JOB_FATAL`, but the worker only logs it: `ctx.db_error` is never set, the scheduler keeps claiming, and the final exit-code chain (1252-1267) falls through to `EXIT_SUCCESS` when no chunk is `FAILED` — even with `fc.processing > 0`. The paid translation is discarded; the chunk is re-queued at the next run and re-sent.

**Why it matters:** NFR-02 (state consistent after failures), NFR-03 (no duplicate sends), 2.1 (scope routing: JOB_FATAL must stop the job), 9.3 (exit 0 means full success).

**Fix:** on `Err` from `complete`/`fail`/`reschedule`/`release` with `scope is JOB_FATAL`, set `ctx.db_error = error` and `self.cancel.set()` (same path as `lease_lost`); in the exit-code chain never return `EXIT_SUCCESS` when `fc.processing > 0` or `fc.pending > 0` unless `--limit` was used. Add a test that monkeypatches `complete` to return `Err(STATE_CORRUPT)` and asserts exit 1 and no further claims.

#### [CR-04] A fenced code block directly after a paragraph line is absorbed into the paragraph and its code is sent to the provider — `src/pipeline/chunker.py:73-83, 130-141`

**Verified (probe):**
```
md = "Intro line.\n```\nsecret_code()\n```\n\nNext para.\n"
parse_blocks -> PARAGRAPH 'Intro line.\n```\nsecret_code()\n```\n\n'
segments sent to provider: ['Intro line.', '```', 'secret_code()', '```']
```
`_continues(PARAGRAPH, line)` only asks `_classify(line)`, which has no fence rule; the fence test (`_FENCE_OPEN`) runs only after `close()`. Design 4.2 fixes the precedence as `fence-state > heading > hr > ...` for every line.

**Why it matters:** E-05 / FR-12 / AC US-6/2 ("the entire fence is inside exactly one chunk and flagged do-not-translate"). Extractor output is unaffected (blocks are always separated by a blank line), but `source_book.md` is explicitly user-editable (R4, O1) and the marker normalizer path is not covered by this reviewer's probe. The hypothesis property test (`tests/test_chunker.py:404-463`) generates exactly this shape (separator `"\n"`) but only asserts losslessness, so it cannot catch it.

**Fix:** in `_continues`, return `False` for PARAGRAPH (and BLOCKQUOTE/LIST if desired) when `_FENCE_OPEN.match(line)`; add a unit test for "paragraph line + fence without blank line" asserting a `CODE` block with `translatable=False`.

#### [CR-05] Raw `<` and `&` in book/translated text reach the EPUB XHTML unescaped — `src/exporters/base.py:181-191`, `src/extractors/pymupdf_extractor.py:570-611`

**Verified (fork probe):** `markdown_to_html("Use the <div> element and <b>bold</b>.\n\nnext <script>alert(1)</script> para\n")` renders `<p>Use the </p><div> element and <b>bold</b>.<p>next <script>alert(1)</script> para</p></div>` — paragraph structure broken, script tag kept. The serializer never escapes; python-markdown passes inline/block HTML through by default (`md_in_html` is not the cause); `read_epub` smoke check does not detect it.

**Why it matters:** AC US-10/2 ("opens in a standard reader"), NFR-06 (structure parity). Any technical book that mentions `<div>`, `<br>`, `a < b`, `AT&T` produces malformed chapter XHTML. Reader behaviour was not tested; the structural corruption was.

**Fix:** before `markdown_lib.markdown(...)`, `html.escape(line, quote=False)` every line outside fenced code except the two internal markers (figure placeholder `<p class="figure-placeholder">` and the `<div class="untranslated" markdown="1">` / `</div>` wrapper); or escape in the extractor serializer so the markdown itself is clean. Add an exporter test with `<div>` and `&` in prose.

#### [CR-06] `extract` / `run` overwrite an existing `source_book.md` without archiving it — `src/pipeline/orchestrator.py:789`

`atomic_write_bytes(self.source_path, ...)` is unconditional. The design explicitly allows hand-editing `source_book.md` before `translate` (R4, O1). A user who edits the file and then re-runs `run` (US-8: "re-run the same command") loses the edits silently: with `completed == 0` the changed file is re-chunked without notice; with `completed > 0` the run stops with `STATE_HASH_MISMATCH`, but the edits are already gone. `--fresh` archives artifacts, plain re-runs do not.

**Why it matters:** E-12 spirit ("archived, not silently overwritten"), user data loss.

**Fix:** if `source_path` exists and its content differs from the new extraction, move the old file to `state-archive/<ts>/source_book.md` (reuse `archive_stamp`) and add a warning; when the job already has `source_md_sha256` and `completed > 0`, refuse unless `--fresh`. Add a test.

### Warning

#### [CR-07] Heartbeat is cancelled before the grace drain, grace is uncapped, and a lost lease still waits out the grace period — `src/pipeline/orchestrator.py:1533-1537, 1539-1585`, `src/cli.py:223-230`, `src/config.py:53`

`_run_job`'s `finally` cancels the heartbeat task first and only then calls `_drain`, which may wait `grace_s` seconds with no heartbeat. `--grace-seconds` has `min=0` and no maximum, `lease_stale_s` defaults to 60: `--grace-seconds 120` lets another process take the lease and re-claim the in-flight chunks while this process is still completing them (double send, NFR-03). Separately, when the heartbeat reports `lease lost` (1592-1596) the drain still honours the grace period although design 5.4 says "treat as cancel-with-release".

**Fix:** cancel the heartbeat after `_drain`; on `ctx.lease_lost` set `self.abort` before draining; validate `grace_s < lease_stale_s` in `Settings`.

#### [CR-08] Retry-After is never available from the real DeepL SDK — `src/translators/deepl_translator.py:60-69`, `tests/test_translators.py:375-379`

`deepl 1.32.0`: `TooManyRequestsException` (`deepl/exceptions.py:43`) carries no `retry_after`; `getattr(exc, "retry_after", None)` is always `None`, so AC US-7/2 ("respecting Retry-After when present") is satisfied only by the unit test that sets the attribute artificially. Backoff still works (full jitter, 2–60 s), so this is a documented limitation, not a defect — but it must be stated in the README/`durum.md` and QA must observe real 429 behaviour. Consider a floor (e.g. 5 s) for `PROVIDER_RATE_LIMITED` delays.

#### [CR-09] `tag_handling="xml"` is used without XML-escaping the segment text — `src/translators/deepl_translator.py:272-281`, `src/translators/protect.py:55-67`

Segments are sent verbatim with `tag_handling="xml"`, `ignore_tags=["x"]`. Bare `<`, `>` and `&` in prose (`if x < 10`, `AT&T`) are then part of an XML document. Whether DeepL drops/mangles such text or returns entities (`&lt;`) was **not verified** (no live key); what was verified is that the code neither escapes before sending nor unescapes after. **QA must test this live**; if confirmed, wrap with `xml.sax.saxutils.escape` / `unescape` around the placeholders.

#### [CR-10] `--allow-non-english` still sends `source_lang="EN"` — `src/translators/base.py:48-56`, `src/pipeline/orchestrator.py:1685-1691`

Design 6.3: with the override, DeepL `source_lang` is left to auto-detect. `TranslationRequest.source_lang` is a fixed `"EN"`, so a German source accepted with the flag is translated as if it were English. Listed as an open question in `durum.md`; it is a functional gap of FR-07. **Fix:** make `source_lang: Optional[str]`, pass `None` when `job.detected_language != "en"` and no native glossary is bound (DeepL requires an explicit source language only with a glossary).

#### [CR-11] Redaction regex masks every UUID-shaped string, corrupting logged paths and job ids — `src/logging_setup.py:57, 127-130`

**Verified (real CLI):** the archive path `...\0257a31a-aacb-4363-9ad6-991c53564a72\scratchpad\...\state-archive\...` was logged as `...\***\scratchpad\...` on the console and in the JSONL file, so the "previous state archived to ..." message points to a wrong path. Job ids in messages are masked the same way. DeepL keys are UUID-shaped, so the design's pattern (9.5) cannot distinguish them; the safe combination is: exact-secret replacement (already done) + pattern restricted to `\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:fx\b` for Free keys + never putting the job id into message text (it is a structured `job_id` field already). Also anchor the pattern so it cannot clip the first 36 chars of a sha256 string.

#### [CR-12] rich markup silently deletes bracketed text from error messages and listings — `src/cli.py:330`, `src/cli.py:903-908`

**Verified (probe + real CLI):** `console.print(f"[red]error[/red] [{code}] {message}", markup=True)` renders `install the extra with pip install "book-translator[local]"` as `... "book-translator"`; `[marker]` (`extractors/base.py:107`, `marker_extractor.py:54`) and any path segment in brackets disappear too; the `providers` table showed `pip install "book-translator"` without `[local]`. **Fix:** `rich.markup.escape(error.message)` in `_fail`, and escape table cell values in `_print_listing`.

#### [CR-13] Command-level failures never reach the JSONL log — `src/cli.py:317-331`

**Verified (real CLI):** `error chunk 1 is PENDING; run translate first` and `EPUB build failed: ParserError` were printed but the corresponding `logs/run-*.jsonl` contained only `lease_acquired`/`exported` lines. NFR-11 expects the run log to explain the outcome. **Fix:** `log.error(error.message, extra={"event": "command_failed", "code": error.code.value})` inside `_fail` before printing (the redaction filter then applies).

#### [CR-14] Ctrl+C during the synchronous stages exits 1 with click's "Aborted!" instead of 130 — `src/cli.py:651-691, 694-725, 792-832, 980-985`

`extract`, `glossary`, `export` run outside `_run_async`; a `KeyboardInterrupt` propagates into click, which catches it (`click/core.py:1563, 1594`), prints `Aborted!` and exits 1; the `main()` handler in `cli.py:980-985` is unreachable in standalone mode. The orchestrator's `finally` still releases the lease and records the run as `FAILED` rather than `INTERRUPTED`. **Fix:** wrap the sync stage calls in `try/except KeyboardInterrupt` → `_fail(common, err(INTERRUPTED, ...))`, and let `_end_stage` receive the interrupted outcome.

#### [CR-15] Orchestrator bypasses the repository and imports ORM models — `src/pipeline/orchestrator.py:44, 47, 477-497`

```python
from sqlalchemy import select
from ..database import models as db_models
...
ids = list(session.execute(select(db_models.Job.id).limit(2)).scalars().all())
```
Design 1.3 forbids the orchestrator to "build SQL"; 1.4 allows `pipeline → database.repository` only. `durum.md` documents this and proposes `JobRepository.get_single_job()`. **Rejected as a permanent deviation**: add `get_single_job() -> Result[Optional[JobRecord]]` (read session, `LIMIT 2`, `STATE_CORRUPT` on 2 rows) and drop the `select`/`models` imports. `pipeline → database.session` (`open_database`, `close_database`, `archive_database`, `utcnow`) is acceptable; add it to the allowed list in 1.4.

#### [CR-16] Layering is not enforced — `pyproject.toml:39` (dependency only), no contract, no AST test

`import-linter>=2` is in `[dev]` but there is no `[tool.importlinter]` contract and no test walking imports (design 1.4 / §11 "Layering"). The grep in this review found no violation besides CR-15, but nothing prevents regressions. **Fix:** add a contract (layers: `cli` > `pipeline` > `{extractors, glossary, translators, exporters}` > `database` > `domain`; forbidden `cli -> database`, service -> service) or a small AST test.

#### [CR-17] The claim query does not use the partial index the DB design requires; the acceptance test was loosened — `tests/test_repository.py:271-281`, `src/database/repository.py:412-423`, DB design §4 (I3) and acceptance check 4

**Verified (probe, file DB, 300 rows):** `EXPLAIN QUERY PLAN` → `SEARCH chunks USING INDEX ix_chunks_job_status (job_id=? AND status=?)`. The test accepts `ix_chunks_job_status` *or* `ix_chunks_pending_backoff`, so it passes without the partial index. Functionally harmless at ≤ 5 000 rows (index range + rowid order, no sort), and `earliest_next_attempt` does use I3 (asserted). **Fix:** either update DB design §4 to state that I3 serves `earliest_next_attempt` only and tighten the test to `ix_chunks_job_status`, or keep the intent and find a form the planner accepts; do not leave a test that asserts "either".

#### [CR-18] No heartbeat during extraction — `src/pipeline/orchestrator.py:759-771`

When state exists, `_extract` acquires the lease and then runs the extractor synchronously; a marker/1000-page extraction easily exceeds `lease_stale_s` (60 s), after which a second process may take the lease (E-20). Documented as open in `durum.md`. **Fix:** a small sync heartbeat thread (`threading.Timer`/`Event.wait(interval)`) around long synchronous stages, or heartbeat from the extractor progress callback.

#### [CR-19] Test gaps versus design §11

Not covered anywhere (checked test names and bodies): non-TTY progress line format/throttling (9.6); signal wiring and second-Ctrl+C abort (5.7); grace period in which in-flight chunks *complete* (tests use `grace_s=0`); `extract --fresh` / `run --fresh` artifact archiving (only `translate --fresh`); EPUB through `Orchestrator.export` / the `export` command; `status` with a live lease and an unfinished last run (counter recovery); `assemble()` on an empty stream (CR-02); fence-after-paragraph (CR-04); pymupdf image placeholder emission and caption (the only image fixture is an image-only page, `tests/test_extractor_cleanup.py:452-455`); extractor mid-document exception → `Err(EXTRACTION_FAILED)`; non-ASCII term discovery (CR-20). `tests/test_exporters.py:401` contains a no-op assertion (`assert isinstance(Ok(1), Ok)`).

#### [CR-20] Glossary discovery is ASCII-only — `src/glossary/discovery.py:45-48`

**Verified (fork probe):** `Müller`, `José`, `Zoë` are missed or truncated (`Jos`, `Zo`); truncated sources become wrong DeepL glossary keys and wrong post-check keys (FR-08, NFR-05). **Fix:** tokenize with `[^\W\d_][^\W\d_’'\-]*` and decide capitalization with `str.isupper()` / `s[0].isupper() and s[1:].islower()`; add a test.

#### [CR-21] `page.find_tables()` runs unconditionally on every page — `src/extractors/pymupdf_extractor.py:161-186`

The table finder is PyMuPDF's most expensive per-page call; on a 1 000-page book (NFR-01) it dominates extraction and cannot be disabled (`ExtractOptions` has no `detect_tables`). Not measured. **Fix:** `ExtractOptions.detect_tables: bool = True` + CLI flag, and/or skip pages with an empty `page.get_drawings()`.

#### [CR-22] After task cancellation the DB lock is released while the `to_thread` operation is still running — `src/pipeline/orchestrator.py:1431-1433, 1572-1576`

`async with lock: await asyncio.to_thread(fn)` exits the lock on `CancelledError` even though the thread continues; `_drain` then runs `release()` under the lock concurrently with a worker's in-thread `complete()`. SQLite serializes the two (`BEGIN IMMEDIATE` + `busy_timeout`), so state stays consistent, but if `release` wins, a finished translation is discarded and re-billed. Low probability (needs cancellation exactly during the DB write). **Fix:** wrap DB calls in `asyncio.shield` and await the shielded future in `_drain` before releasing, or track in-thread futures and wait for them.

#### [CR-23] `run --fresh` archives the user-curated `glossary.json`, and the following translate runs with 0 terms without a prominent hint — `src/pipeline/orchestrator.py:279, 591-604`

**Verified (real CLI):** after `extract --fresh`, `glossary.json` was gone from the output directory and the next `translate --dry-run` reported `glossary: none (0 terms)`. Design 8.1 says stage artifacts are archived alongside the DB, so this is by design, but the glossary is the user's manual work (A-6). **Fix (choose one):** keep `glossary.json` in place on `--fresh` (it is input, not derived state) or print an explicit warning with the archived path.

### Suggestion

#### [CR-24] `translate` requires `--input` — `src/cli.py:730`
Design 9.2 lists `--input` under `run`/`extract` only; the smoke test hit `Missing option '--input'` (exit 2). The hash check (5.1 step 4) needs the file, but `job.input_path` is stored: make `--input` optional for `translate` and fall back to the recorded path (warn if it no longer exists).

#### [CR-25] `runs.provider` is recorded for `extract` and `export` runs — `src/pipeline/orchestrator.py:768, 799, 1946`
DB design §2.4: NULL for extract/glossary/export. Pass `provider=None` for those commands.

#### [CR-26] `--limit` runs exit 0 with `pending > 0` — `src/pipeline/orchestrator.py:1261-1267`
Exit 0 means "full success" (9.3). Either document that `--limit` returns 0 with a `limit_reached` warning, or return a distinct message in the summary.

#### [CR-27] Heading regex strips a trailing `#` that is part of the title — `src/pipeline/segmenter.py:22`, `src/pipeline/chunker.py:22`, `src/pipeline/assembler.py:32`
**Verified (probe):** `## Programming in C#` → provider receives `Programming in C`, the `#` is re-attached after the translation. CommonMark requires a space before a closing sequence: use `(?:[ \t]+#+)?[ \t]*$`.

#### [CR-28] Marker count check flags arithmetic asterisks — `src/pipeline/glossary_check.py:41-49`
**Verified:** `"Compute 5 * 3"` → `"5 x 3"` yields `structure_mismatch:*` and a review flag. Ignore `*` surrounded by spaces (never emphasis) to reduce review noise.

#### [CR-29] `AppError.cause` is never emitted and tracebacks bypass redaction — `src/logging_setup.py:118-149, 178-179`
`.cause` is read nowhere in `src/` (grep), so provider/DB exception details are lost for diagnosis (NFR-11); the JSONL `exception` field is written without `redact_text`. Log `cause` at DEBUG via `extra` (already redacted) and redact `formatException` output.

#### [CR-30] Duplicated Markdown scanners — `src/pipeline/chunker.py:21-28`, `src/pipeline/segmenter.py:22-27`, `src/pipeline/assembler.py:32-34`, `src/exporters/base.py:153-178`
Heading/fence/list regexes and fence-state loops are copied four times (and already diverge: CR-27, `_FENCE` accepts `[ \t]{0,3}` in one place and ` {0,3}` in another). Move them to one module (`pipeline/markdown_syntax.py` or `domain`).

#### [CR-31] Second Ctrl+C cannot interrupt in-flight HTTP calls — `src/pipeline/orchestrator.py:1572-1575`, `src/cli.py:432-472`
`task.cancel()` does not stop a `to_thread` worker; `asyncio.run` waits for the default executor on shutdown, so "abort" still blocks until the DeepL SDK timeout. Document it ("abort waits for the current requests to time out") and consider `Translator(... timeout=...)` via `deepl.http_client` defaults.

#### [CR-32] `extract --fresh` on a directory without a DB creates the DB before extraction — `src/pipeline/orchestrator.py:763-767`
A `NO_TEXT_LAYER` result then leaves an empty state file behind (E-01 says no state must be created). Check `self.db_path.exists()` separately from `fresh`.

#### [CR-33] Smaller items from the service-layer review
- `pymupdf_extractor.py:706`: `min_pages=max(2, len(raw_pages) if len(raw_pages) < 2 else 2)` always equals 2 — write `2` and document that a band line repeating on just two pages is removed.
- `cleanup.py:324, 328`, `normalizer.py:156`: warnings embed book words (`ambiguous hyphen kept: Anglo-Saxon`) that end up in `jobs.warnings`, `summary.json` and the log; log positions instead, or keep words at DEBUG.
- `glossary/schema.py:281-288`: a failed `glossary.json` write is reported as `INTERNAL`; use an I/O error code.
- `exporters/base.py:194-201`: unreadable `--css` silently falls back to the bundled stylesheet; emit a warning.
- `exporters/base.py:187`, `markdown_exporter.py:24`: image tokens are replaced inside fenced code too.
- `glossary/discovery.py:158-183`: the joiner branch cannot produce `A B of C` (3-term with joiner).
- `extractors/langdetect.py:62-63`: ratios computed twice in `check_english`; very short text reports "detected: unknown" — say "too little text".
- Real CLI: `lease_acquired` logged twice for `--fresh` (archive step + bind); `images: 0` although the image-only page held an image (count skipped pages' images in `PageInfo.had_images`); `dry_run` message counts translatable chunks (4) while `counts.pending` says 5 — align wording.

---

## Verified OK (no finding)

- Chunker lossless invariant: join verified in `chunk()` (`chunker.py:516-523`), dense ids, tiling of blocks; property test with Unicode/Turkish text. Packing follows 4.3 (fits → flush at min, deviation choice, long choice); heading carry works for the prose path; table isolation; oversize split with `parent_block_id`/`sub_index`/`sub_count` and exact concatenation.
- Segmenter round trip by construction (`prefix + text + suffix` + `tail`), verified in the property test; markers never leave the process.
- Backoff formula (`backoff.py:20-39`) = design 5.4; `AdaptiveLimiter` AIMD with cooldown, cancellation-safe `acquire` (permit not leaked, tested).
- Pre-flight order (settings → DB → lease → input hash → provider → glossary → chunk → requeue → TRANSLATING) matches 5.1; hash mismatch and provider mismatch return exit 5; `--fresh` uses the online backup API and removes `-wal`/`-shm`.
- Claim loop / worker scopes: `CHUNK_RETRYABLE` → reschedule with persisted `next_attempt_at` (survives restart, tested); exhausted → `fail`; `CHUNK_FATAL` → `fail`; `JOB_FATAL`/`USER` → release + `PAUSED` + cancel, exit 3; worker exceptions → `INTERNAL/CHUNK_FATAL`; `CancelledError` re-raised everywhere (`orchestrator.py:1012-1013, 1628-1629, 1644-1645`), never converted to `Err`.
- All repository calls from the event loop go through `_db` (single `asyncio.Lock` + `to_thread`), including heartbeat and drain release; calls before/after `_run_job` are sequential.
- Repository SQL: every transition is `UPDATE ... WHERE status='<from>'` with `rowcount` checks; `claim_pending` two-step under `BEGIN IMMEDIATE` with the tripwire; status literals via `literal_column`; `complete` writes text + `COMPLETED` in one statement; lease `acquire` rolls back on refusal and the write session skips the commit; `heartbeat` → `STATE_LOCKED` on lost lease; `read_only` opens set `query_only=1` and `status` performs no writes.
- Secrets: `SecretStr`, never persisted; the real CLI run found no key fragment in DB, archive DB, logs or `summary.json`; `RedactionFilter` covers message/args/event/cause for logging records.
- Resources: pymupdf documents closed on every path (`_extract` try/finally), DB engines disposed in every stage `finally`/`_unbind`, `atomic_write_bytes` (mkstemp in target dir + fsync + `os.replace`, temp removed on failure, no open handle on Windows), EPUB built in memory then written atomically, smoke-checked, unlinked on failure.
- Memory: `iter_ordered` keyset pagination (200 rows), assembler streams, dry-run streams, `in_flight` bounded by permits.
- Encoding: all file I/O `utf-8`, stdout/stderr reconfigured on Windows, `legacy_windows=False`; the real non-TTY run contained no ESC bytes.
- Import direction: `cli` imports only registries + `pipeline` + `config` + `logging_setup`; services import only `domain` (+ `config` for translators); `database` imports only `domain`. Only CR-15 deviates.
- Optional extras: `marker`, `ctranslate2`, `weasyprint` are probed with `importlib`, modules import without them (295 tests pass in an environment without them).
- Type hygiene: five `type: ignore`s, all narrow and justified (`_pdf.py` confines the untyped pymupdf calls); no `Any` leaking through public signatures except the intentional client/engine factories.

---

## Deviations from design (`docs/durum.md`) — accepted / rejected

### Faz B

| Deviation | Decision |
|---|---|
| `status()`/`export` read the single job via `database.models.Job` directly; proposal `JobRepository.get_single_job()` | **Rejected as permanent** → CR-15: implement `get_single_job()` and remove the ORM/`select` imports from the orchestrator. |
| `pipeline/orchestrator.py` imports `logging_setup` (`chunk_context`) | **Accepted** (leaf module, no cycle). Add `pipeline → logging_setup` to 1.4. |
| `glossary` stage writes no `runs` row | **Accepted**: the stage touches no DB state; opening the DB only to record a run adds a lease/lock for nothing. Update DB design §2.4 wording. |
| Empty/truncated response rule: 1st reschedule, 2nd lenient completion + review flag, provider-level `PROVIDER_EMPTY_RESPONSE` 3rd → FAILED; counter restart-safe via `last_error` prefix | **Accepted**: matches 5.3 ("second occurrence flags review, third fails") closely; tested. Note the persisted counter restarts at 1 after a crash (one extra cycle), acceptable. |
| `--fresh`: `extract`/`run` archive DB + all artifacts; `translate --fresh` archives DB only; changed `source_book.md` re-chunked while `completed == 0` (O1) | **Accepted**, with CR-23 (glossary.json handling) and CR-06 (archive before overwrite on plain re-runs). |
| Heartbeat uses real `asyncio.sleep` (interval injectable) | **Accepted**; tested via `heartbeat_interval_s=0.01`. See CR-07 for ordering. |
| Provider binding compared via `translator.name`; `--cleanup-remote` accepted but "not implemented"; no `<output>/.env` fallback | **Accepted** (`--cleanup-remote` is a nice-to-have in 7.4; document the `.env` rule in README). |
| Open question: redaction masks job UUIDs | → CR-11 (Warning, fix proposed). |
| Open question: no heartbeat during long `extract` | → CR-18 (Warning). |
| Open question: `--allow-non-english` still sends `source_lang="EN"` | → CR-10 (Warning, `TranslationRequest.source_lang: Optional[str]`). |

### Faz A

| Deviation | Decision |
|---|---|
| Image-only pages skipped, no placeholder (E-04 literal); `EXTRACTION_FAILED` JOB_FATAL; marker→pymupdf fallback in the orchestrator; language decision via `check_english()` in the orchestrator; typed `_pdf.py` wrapper; lettered list markers → `-` | **Accepted**. Count the skipped page's images in `PageInfo.had_images` / summary (CR-33). |
| Chunker: heading before CODE/IMAGE/HR/TABLE/oversize stays in the closing chunk with `heading_stranded:*` | **Accepted**: DeepL translates each text independently, so a stranded heading has no quality impact; the warning makes it visible. (Optional: emit a `HEADING` chunk instead, the DB enum already allows it.) |
| Block parser is a superset of BT-Markdown (soft-wrap paragraphs, `+` lists, `***` hr) | **Accepted**, but CR-04 must be fixed (fence precedence). |
| `provider_limit < min_chars` lowers min with a warning | **Accepted**. |
| Segmenter `Segmented.tail`; delimiter row in next segment's prefix | **Accepted** (round trip verified). |
| DeepL: `deepl.http_client.max_network_retries = 0` process-wide; 429 without Retry-After; unclassified → `INTERNAL/CHUNK_FATAL` | **Accepted** as SDK constraints; CR-08 requires documentation + live QA. |
| Local NMT `apply_post_replace(text, glossary, source_text=None)`; always `POST_REPLACE`; misses reported by the 7.6 post-check | **Accepted**. |
| LLM base `[n] text` line protocol | **Accepted** (no vendor ships in v1). |
| Glossary: stricter stop-list; `--force` keeps manual entries; empty glossary hash = `sha256("")`, orchestrator writes `None` | **Accepted** (orchestrator verified to write `None`, `orchestrator.py:1111`). |
| Exporter: no bundled DejaVu font (CSS font stack when `font_path` absent); `md_in_html`; in-memory EPUB + atomic write; per-chapter footnotes; metadata keys | **Accepted for v1** (PDF exporter is optional and untestable here); E-15 still requires a Turkish-capable embedded font when the `[pdf]` extra is delivered — track as a ticket. `md_in_html` is not the cause of CR-05. |
| Assembler copies non-translatable chunks verbatim regardless of status | **Accepted**. |

Undocumented deviations found during review: `translate` requires `--input` (CR-24); `runs.provider` set for extract/export (CR-25); claim query index (CR-17).

---

## Risk points for QA

1. **Live DeepL**: 429 behaviour without Retry-After (CR-08) — watch `limiter_changed`/`chunk_rescheduled` events and that no chunk is sent twice (`runs.provider_calls` vs completed); 456 and 401/403 → job `PAUSED`, exit 3, chunk back to `PENDING`, resume works; glossary creation/reuse by name `book-translator:<hash16>`; `tag_handling="xml"` with prose containing `<`, `>`, `&` (CR-09) and inline code/URLs (placeholder round trip); `detected_source_lang` when `--allow-non-english` (CR-10).
2. **Large PDFs** (≥ 500 pages): extraction time with `find_tables` (CR-21), memory during `translate`/`export` (streaming), lease staleness during extraction (CR-18, run `status` from a second shell mid-extraction and observe `stale`).
3. **Two-column layouts / unusual typography**: reading order, header/footer heuristic on chapter-title headers, heading tiers; hand-edit `source_book.md` afterwards and re-run `translate` (must re-chunk while nothing is completed) and `run` (CR-06).
4. **Ctrl+C timing**: first Ctrl+C during a request (grace, completions written, exit 130, `runs.outcome = INTERRUPTED`), second Ctrl+C (release to `PENDING`, note the wait for the HTTP timeout, CR-31), Ctrl+C during `extract`/`export` (CR-14), `--grace-seconds` larger than 60 (CR-07).
5. **Resume after crash**: kill the process (`taskkill /F`) mid-run; on restart expect `re-queued N chunks left in PROCESSING`, no COMPLETED chunk re-sent (compare `fake`/live call counts), `status` shows "last run did not finish cleanly" with recovered counters; disk-full simulation during `complete` (CR-03).
6. **Glossary edits between runs**: change a target after some chunks completed → warning "consistency risk: N chunks"; empty the glossary → no warning path; user file `--glossary` precedence; non-ASCII names (CR-20); DeepL rejecting entries (newline, identical source/target).
7. **Non-TTY output**: pipe to a file / CI: no ESC bytes, one `progress completed=…` line per second or every 10th chunk, `--json` on stdout only, human text on stderr; `--quiet`.
8. **Windows console encoding**: Turkish characters in headings/summary on cmd.exe and PowerShell 5.1 (code page 1252/850), `--json` round trip, EPUB opened in Calibre/Thorium with `çğıİöşü`; paths with spaces/brackets in `--output` (CR-12).
9. **Export edge cases**: export before translate (CR-02), `--allow-partial` marker blocks in EPUB, `--chapter-level 2`, book with < 2 H1, `--css` unreadable, `--format pdf` without weasyprint (exit 2, md+epub still produced).
10. **State safety**: two shells on the same output dir (exit 4 with holder pid), `--force-unlock`, `--fresh` archive contents, schema-version bump simulation (`UPDATE schema_meta SET schema_version=99` → exit 5 without crash).

---

## Onay Koşulu

- CR-01 (Blocker) and CR-02…CR-06 (Critical) fixed with tests before merge.
- Warnings CR-07, CR-11, CR-12, CR-13, CR-15, CR-17 fixed in this PR or linked to tickets; CR-08/CR-09/CR-10 need the live DeepL QA result to decide.
- Suggestions are optional; CR-24 and CR-25 are cheap and recommended.

---

## Second pass (re-review after fix round)

**İnceleme Tarihi:** 2026-09-18 (second pass)
**Reviewer:** code-reviewer (Claude)
**Scope:** the files touched in the fix round (`src/config.py`, `src/logging_setup.py`, `src/cli.py`, `src/pipeline/{orchestrator,chunker,assembler}.py`, `src/exporters/{base,epub_exporter}.py`, `src/database/repository.py`, `docs/03_db_design.md`, the changed tests, new `tests/test_layering.py` / `tests/test_logging_setup.py`), plus the live-run outputs in `docs/test_doc_output/` (read only; no provider call was made).
**Baseline re-verified:** `pytest -q` → 322 passed, 1 skipped; `mypy src` → clean (44 files); `ruff check src tests` → clean.

### Verdict: **APPROVED WITH CHANGES**

All 15 findings of the fix round are resolved with tests (evidence table below); no regression was found in the diff scope. One new **Critical** (CR-34) came out of the live run: it is not a regression of the fix round, but it corrupts the EPUB structure for a very common Turkish sentence shape, so it must be fixed before release. CR-35…CR-37 are optional. QA can start now; CR-34 is listed as a known risk with a reproducible case.

### Türkçe özet (kullanıcı için)

Düzeltme turundaki 15 bulgunun hepsi gerçekten kapanmış; her biri için kodu ve testi okudum, birkaçını ayrıca küçük deneme betikleriyle doğruladım (EPUB'da `<`/`&` içeren başlıklar, boş chunk ile export, Ctrl+C, redaction). Yeni ve önemli tek konu: canlı testteki "3. Bölüm, …" satırı sadece yanlış alarm değil — EPUB'da gerçekten numaralı liste maddesine dönüşmüş (`<ol start="3">`). Yani uyarı doğruydu, asıl düzeltme çıktıyı onarmak: kaynakta numaralı liste yokken çeviride satır başındaki `3.` ifadesini `3\.` olarak kaçışlamak (Markdown'da düz paragraf kalır; doğrulandı). Bunun dışında kalanlar isteğe bağlı iyileştirmeler.

### Status of the fixed findings

| CR | Severity | Status | Evidence (file:line) | Notes |
|---|---|---|---|---|
| CR-01 | Blocker | **resolved** | `src/config.py:90-95, 99-108` (`describe_validation_error`, `exc.errors(include_input=False, include_url=False)`); `src/cli.py:332` (`_fail` redacts the message once more); `tests/test_cli.py:325-346` | Human and `--json` output checked for a whitespace key. `error.context` is printed unredacted in `--json`, but no context key carries credentials (grep of every `context={...}` in `translators/`, `config.py`, `orchestrator.py`: chunk ids, http status, paths, counts only). |
| CR-02 | Critical | **resolved** | `src/pipeline/orchestrator.py:2106-2114` (`counts.total == 0` → `EXPORT_REFUSED_PARTIAL`, USER, before any exporter is resolved); `src/pipeline/assembler.py:218-223` (empty stream → same error); `src/exporters/epub_exporter.py:210-215` (empty document → `EXPORT_FAILED`, no file); `tests/test_orchestrator.py:988-1012`, `tests/test_cli.py:349-370`, `tests/test_exporters.py:444-459` | Exit 2, no artifact, no traceback, exporter factory never called. |
| CR-03 | Critical | **resolved** | `src/pipeline/orchestrator.py:1688-1693` (`_state_failure`: first error kept, `cancel.set()`); used at `1663-1664` (release), `1901-1903` (complete), `1932-1934` (fail), `1963-1964` (reschedule); exit chain `1329-1343` (`db_error` → exit 1 before the cancel branch; `fc.processing > 0` → exit 1, never 0); `tests/test_orchestrator.py:1015-1054` | Cancel path checked: `_drain` (`1626-1630`) treats `db_error` as immediate (no grace) and releases the remaining in-flight chunks; the chunk whose write failed stays PROCESSING and is re-queued on the next run (visible in the summary as `state_error:`). The scheduler loop also breaks on `db_error` (`1573-1579`). |
| CR-04 | Critical | **resolved** | `src/pipeline/chunker.py:81-83` (`_continues(PARAGRAPH)` returns False on `_FENCE_OPEN`); `tests/test_chunker.py:581-592` | Lossless join still asserted. LIST/BLOCKQUOTE continuation unchanged (an indented fence under a list item stays in the list — CommonMark-consistent, pre-existing). |
| CR-05 | Critical | **resolved** | `src/exporters/base.py:38-43, 188-234, 244` (`escape_prose`: inline code spans, fences, image token lines and the blockquote prefix are preserved; existing entities are not double-escaped); `tests/test_exporters.py:408-441` | Probe (scratchpad): heading `# Part A <b> & C` → `nav.xhtml`/`toc.ncx` carry `&lt;b&gt; &amp;`, `read_epub` OK; tables, footnote definitions and `> quoted <b>` render correctly; the untranslated `<div>` wrapper is added after escaping so `md_in_html` still works. Two side effects, both outside BT-Markdown: autolinks `<https://…>` become literal text (the extractor never emits them) and 4-space indented lines are double-escaped (`&amp;lt;`) because python-markdown treats them as indented code → CR-35. |
| CR-06 | Critical | **resolved** | `src/pipeline/orchestrator.py:802-805, 862-914` (`_preserve_previous_source`: identical → no-op; differing + `completed > 0` → `STATE_HASH_MISMATCH`, exit 5, message names `--fresh`; otherwise moved to `state-archive/<stamp>/source_book.md` with a warning and a `source_archived` event); `tests/test_orchestrator.py:1057-1101` | Archive path comes from `_archive_dest()` (`581-588`, suffix on collision). With no DB yet (`bound is None`) a differing file is archived unconditionally — correct, nothing can be completed. `--fresh` moves the file earlier (`564-575`), so the two archive steps never collide. |
| CR-07 | Warning | **resolved** | `src/pipeline/orchestrator.py:1613-1620` (`_drain` runs before the heartbeat task is cancelled); `1626-1630` (`lease_lost` / `db_error` / `abort` → grace 0); `1673-1686` (`_effective_grace` = min(`grace_s`, `lease_stale_s − 10`), logged as `grace_capped`); `tests/test_orchestrator.py:1104-1165` | The cap is applied at runtime instead of a `Settings` validator — acceptable: the user sees the warning and the lease can no longer expire during the drain. With `lease_stale_s = 5` (tests only) the cap is 0, i.e. no grace. |
| CR-11 | Warning | **resolved** | `src/logging_setup.py:60-64` (`<uuid>:fx` with boundaries only), `141-144`; `src/cli.py:309-319` (env key registered at start-up), `360-364` (`.env` key appended after `load_settings`), `372-374` (file handler receives the settings key); `tests/test_logging_setup.py:15-42` | Job ids, archive paths and sha256 strings survive; the Free-key shape and the configured Pro key are masked; `{FREE_KEY}extra` is not clipped. |
| CR-12 | Warning | **resolved** | `src/cli.py:29, 350-351` (`escape()` in `_fail`), `382` (log-file warning), `947-951` (table title / columns / cells); `tests/test_cli.py:394-415` | All three rich `.print(` sites in `cli.py` are covered (grep). |
| CR-13 | Warning | **resolved** | `src/cli.py:333-339` (`command_failed`, ERROR, `file_only=True` so the console does not print it twice); `tests/test_cli.py:418-438` | The line has `job_id: null` (developer's own note) → CR-37. |
| CR-14 | Warning | **resolved** | `src/cli.py:503-510` (`_run_sync` catches **only** `KeyboardInterrupt`; every other exception propagates unchanged), call sites `709, 752, 856`; `src/pipeline/orchestrator.py:854-856, 2062-2063` (`result = _interrupted(); raise` → the `finally` records the run as `INTERRUPTED` and releases the lease); `tests/test_cli.py:441-460` (extract / glossary / export → 130, no "Aborted") | Checked that the `Err`-returning stub path (`test_error_messages_keep_bracketed_text`) still takes the normal failure route, so nothing else is swallowed. |
| CR-15 | Warning | **resolved** | `src/database/repository.py:522-533` (`@boundary`, read session, `LIMIT 2`, `STATE_CORRUPT` on two rows); `src/pipeline/orchestrator.py:482, 536, 2212` use it; no `sqlalchemy` / `database.models` import remains in the orchestrator (grep); `tests/test_repository.py:858-880` (incl. the corruption branch via `ignore_check_constraints`) | — |
| CR-16 | Warning | **resolved** | `tests/test_layering.py:102-111` (AST walk over all 44 modules, relative imports resolved), `129-142` (`test_checker_recognises_the_forbidden_edges` proves the checker is not vacuous: `pipeline → database.models`, `cli → database`, `cli → translators.deepl_translator`, service → service, `domain → config` are all reported) | Minor: the `pipeline → database.*` allow-list matches the `repository.` / `session.` prefixes only, so a module-level `from ..database import repository` would be flagged (false positive in the safe direction) → CR-36. Design doc 02 §1.4 still lacks `pipeline → database.session` / `logging_setup` (doc-only). |
| CR-17 | Warning | **resolved** | `tests/test_repository.py:277-283` (claim plan must use exactly `ix_chunks_job_status`, never `ix_chunks_pending_backoff`, no `TEMP B-TREE`, no `SCAN`; `earliest_next_attempt` must use I3, `293-294`); `docs/03_db_design.md:308-322, 535` (I3 note: I3 serves `earliest_next_attempt` / the backoff count only; acceptance check 4 updated) | Test and DB doc now say the same thing. |
| CR-23 | Warning | **resolved** | `src/pipeline/orchestrator.py:276-278` (`glossary.json` removed from the archived artifact list), `590-617` (`_keep_glossary`: copy into the archive, keep in place, WARNING with the number of approved entries, INFO otherwise); `tests/test_orchestrator.py:1168-1194` | — |

Deferred by decision (unchanged, still open): CR-08, CR-09, CR-10 (decided by the live DeepL QA result), CR-18, CR-19, CR-20, CR-21, CR-22, and Suggestions CR-24…CR-33.

### New findings

#### [CR-34] ⚠️ Critical — A translated paragraph that starts with a Turkish ordinal ("3. Bölüm, …") is rendered as an ordered list in the EPUB; the `structure_mismatch:list_items` warning is a true positive — `src/pipeline/assembler.py:35, 77-78, 144-149`, `src/exporters/base.py:36, 247`

**Verified (live output, no provider call):** `docs/test_doc_output/source_book.md:59` "Chapter 3 covers image processing, …" became `translated_book.md:59` "3. Bölüm, neredeyse tüm …" (same for "4. Bölüm" and "5. Bölüm" at lines 61 and 63). In `translated_book.epub` → `EPUB/ch003.xhtml` that paragraph is `<ol start="3"><li><p>Bölüm, neredeyse tüm bilgisayar görme uygulamalarında …`. Probe: `markdown_to_html("Intro.\n\n3. Bölüm, yöntemleri anlatır.\n\n4. Bölüm, devam eder.\n")` → `<ol start="3"><li><p>Bölüm, …</p></li><li><p>Bölüm, …</p></li></ol>`; with a backslash escape `3\. Bölüm` → `<p>3. Bölüm, …</p>`. Mid-paragraph occurrences (no blank line before) are harmless in HTML (`<p>Bu kitap.\n3. Bölüm…</p>`) but are still counted by `scan_structure`.

**Why it matters:** NFR-06 (structure parity) and AC US-10/2: every "Chapter N …" sentence at the start of a paragraph — the standard shape of a book overview — becomes a list item with the number stripped from its text. The warning currently does its job (review flag set), but the artifact itself is wrong, so silencing the check (the option discussed in `durum.md`) would hide a real defect. `glossary_check.py` is not involved: it counts `**`, `*`, `` ` ``, `[^` only (`glossary_check.py:23`); the list count lives in the assembler.

**Fix (concrete, two parts, both in the assembler so the `.md` and the EPUB agree):**
1. *Repair* in `assemble()` when appending a translated chunk whose source group has **no ordered list items** (`scan_structure(source).ordered_items == 0`): for every translated line (outside fences) matching `^([ \t]{0,3})(\d{1,9})([.)])([ \t]+\S)` that would start a list — previous line blank / start of chunk, or the number is `1` (CommonMark: only a list starting with 1 may interrupt a paragraph) — rewrite it as `\1\2\\\3\4` (`3\. Bölüm`). CommonMark and python-markdown both treat `\.` / `\)` as a literal, so the Markdown export stays valid and the EPUB renders a paragraph. Log `ordinal_escaped:<chunk>` at DEBUG.
2. *Check* in `scan_structure`: split `list_items` into `bullet_items` and `ordered_items`; count an ordered marker only under the CommonMark interruption rule (previous line blank, or previous line a list item / continuation, or number `1`); in `flush()` compare `ordered_items` only when the source group has any, and compare `bullet_items` always (`-` / `*` / `+` never arise from prose). After step 1 the check is exact for the ordinal case and still catches dropped or added list items.
3. Tests: (a) the exact live pair (source "Chapter 3 covers …", translation "3. Bölüm, …") → no warning, `translated_book.md` contains `3\. Bölüm`, `markdown_to_html` yields `<p>3. Bölüm`; (b) a real 3-item ordered list translated with 2 items → warning still raised; (c) a source ordered list translated as `1. … 2. … 3. …` → no warning; (d) mid-paragraph "3. Bölümde" → untouched.

#### [CR-35] 📘 Suggestion — 4-space indented lines are double-escaped by `escape_prose` — `src/exporters/base.py:208-234`

Probe: `"    indented <tag> line"` → `<pre><code>indented &amp;lt;tag&amp;gt; line`. python-markdown treats the indented line as a code block and escapes it again. BT-Markdown has no indented code (the chunker parses such lines as paragraphs, so they are translated anyway) and the extractor strips leading whitespace, so this can only appear in a hand-edited `source_book.md`. Either document "indented code blocks are not BT-Markdown; use fences" in the README, or skip escaping for a line that starts with 4 spaces / a tab after a blank line outside a list. The `<https://…>` autolink form is likewise turned into literal text — acceptable (never produced); mention it in the same README note.

#### [CR-36] 📘 Suggestion — Layering checker prefix rule and design doc 1.4 — `tests/test_layering.py:27, 93-96`, `docs/02_solution_design.md` §1.4

`any(target.startswith(m + ".") for m in PIPELINE_DATABASE)` rejects the module-level form `from ..database import repository` (target `book_translator.database.repository`, no trailing dot) although it is the allowed dependency. Use `target == m or target.startswith(m + ".")`. Also add `pipeline → database.session` and `pipeline → logging_setup` to design doc 02 §1.4 (already noted by the developer; doc-only).

#### [CR-37] 📘 Suggestion — `command_failed` log line has `job_id: null` — `src/cli.py:333-339`

The stage has already unbound the job context when `_fail` runs, so the JSONL line that explains the outcome cannot be joined to the job by `job_id`. Cheap fix: keep the last bound job id in `_Common` (set from the stage result or the `Orchestrator` before `_unbind`) and pass it as `extra={"job_id": ...}`; `ContextFilter` gives explicit `extra` values precedence (`logging_setup.py:116-119`).

### Notes from the live outputs (not review findings; already under "extractor" in `durum.md`)

- The scrambled numbered list in `translated_book.md:31-43` (2, 4, 3, 5, 7, 6, …) is already scrambled in `source_book.md:31-43` — it is the pymupdf reading order of the "2D (what?)" figure, not a segmenter / reassembly defect (chunk join order is enforced by the assembler's `order` check and the source digest).
- Ligatures (`ﬁ`, `ﬂ`) and figure labels parsed as `##` headings remain extractor items for the QA list (CR-19 / CR-33 scope).

### Risk points for QA (updated)

Removed as resolved: export-before-translate crash (CR-02), disk-full during `complete` exiting 0 (CR-03), bracketed text in paths (CR-12), `--grace-seconds` > 60 (CR-07), Ctrl+C in `extract` / `export` exiting 1 (CR-14), hand-edited `source_book.md` overwritten by `run` (CR-06). Regression checks for these are now in the test suite (see the evidence table); QA should still exercise items 4 and 5 below manually once.

1. **Live DeepL (deferred CR-08/09/10)**: 429 behaviour without Retry-After (watch `limiter_changed` / `chunk_rescheduled`; `runs.provider_calls` vs completed → no chunk sent twice); 456 / 401 / 403 → job `PAUSED`, exit 3, chunk back to `PENDING`, resume works; glossary creation / reuse `book-translator:<hash16>`; prose with `<`, `>`, `&` and inline code / URLs in `tag_handling="xml"` mode (CR-09: the 3-page live run had no such characters — use a technical page); `--allow-non-english` with a non-English PDF (CR-10: DeepL still receives `source_lang="EN"`).
2. **CR-34 (new)**: any book whose overview says "Chapter N …" — after export open `translated_book.epub` in Calibre / Thorium and check that "N. Bölüm …" paragraphs are paragraphs, not numbered lists; compare `summary.json` `structure_mismatch:list_items` warnings with the actual EPUB. Until CR-34 is fixed, expect one review flag per such paragraph.
3. **Large PDFs** (≥ 500 pages): extraction time with `find_tables` (CR-21), memory during `translate` / `export` (streaming), lease staleness during extraction (CR-18: run `status` from a second shell mid-extraction and observe `stale`).
4. **Ctrl+C timing**: first Ctrl+C during a request → grace (capped at `lease_stale_s − 10`, `grace_capped` warning when `--grace-seconds` is larger), in-flight completions written, exit 130, `runs.outcome = INTERRUPTED`; second Ctrl+C → release to `PENDING` (note the wait for the HTTP timeout, CR-31); Ctrl+C during `extract` / `glossary` / `export` → exit 130, lease released, run row `INTERRUPTED`.
5. **Resume after crash**: `taskkill /F` mid-run → on restart `re-queued N chunks left in PROCESSING`, no COMPLETED chunk re-sent; simulate a state write failure (read-only DB file or full disk) during `translate` → exit 1, `state_error:` warning in the summary, never exit 0; the chunk left in PROCESSING is re-queued on the next run (one extra paid send, by design).
6. **Glossary edits between runs**: change a target after some chunks completed → "consistency risk: N chunks"; `--fresh` keeps `glossary.json` and logs `glossary_kept` with the approved count (the archive holds a copy); non-ASCII names (CR-20); DeepL rejecting entries (newline, identical source / target).
7. **Non-TTY output**: pipe to a file / CI → no ESC bytes, one `progress completed=…` line per second or every 10th chunk, `--json` on stdout only, human text on stderr; `--quiet`; every failure must also appear as a `command_failed` line in `logs/run-*.jsonl` (with `job_id: null` for now, CR-37).
8. **Windows console encoding**: Turkish characters in headings / summary on cmd.exe and PowerShell 5.1, `--json` round trip, EPUB opened in Calibre / Thorium with `çğıİöşü`; paths with spaces / brackets in `--output`.
9. **Export edge cases**: export right after `extract` → exit 2 "run translate first", no artifact; `--allow-partial` marker blocks in the EPUB (highlighted `.untranslated` div, message text with `<` / `&` escaped); `--chapter-level 2`; book with < 2 H1; headings containing `<`, `&`, inline code; `--css` unreadable (silent fallback, CR-33); `--format pdf` without weasyprint (exit 2, md+epub still produced); hand-edited `source_book.md` with an indented code block (CR-35).
10. **State safety**: two shells on the same output dir (exit 4 with holder pid), `--force-unlock`, `--fresh` archive contents (`translation_state.db`, `source_book.md`, `summary.json`, `translated_book.*`, a *copy* of `glossary.json`), re-running `extract` after hand-editing `source_book.md` (archived to `state-archive/<stamp>/source_book.md`, warning printed; with completed chunks → exit 5 pointing to `--fresh`), schema-version bump simulation (`UPDATE schema_meta SET schema_version=99` → exit 5 without crash).
11. **Redaction**: run with a Free key and with a Pro key (env and `.env`), grep `logs/*.jsonl`, `summary.json`, console capture and `translation_state.db` for the key; confirm job ids and archive paths are *not* masked any more.

### Onay koşulu (second pass)

- CR-34 fixed with the four tests above before release (small, assembler-only change); QA may start in parallel using risk item 2.
- CR-35…CR-37 optional; CR-36's doc update is recommended in the same PR.
- Deferred CR-08/09/10 are decided by the live DeepL QA result; CR-18…CR-22 and the Suggestions stay tracked.

---
✅ Code Reviewer (yeniden inceleme) tamamlandı.
➡️  Sonraki adım: QA Engineer

---

# v1.1 Review (F1 + F3 + schema v2 + F2)

**Modül:** book-translator v1.1 delta (step 0, F1 figures, F3 native PDF, schema v2, F2 overlay, Addendum A)
**İnceleme Tarihi:** 2026-09-18
**Reviewer:** code-reviewer (Claude)
**Girdi:** `docs/05_change_request_v1_1_ba.md`, `docs/06_solution_design_v1_1.md` (+ Addendum A), `docs/07_db_design_v1_1.md`, `docs/durum.md`, `src/` (60 modules), `tests/`, live state dirs `bt_f1` and `bt_overlay2` (copies only).
**Baseline (re-run by the reviewer):** `pytest -q` → 610 passed / 1 skipped (69 s); `mypy src` clean (60 files); `ruff check src tests` clean.

## Verdict: **CHANGES REQUIRED**

| Severity | Count |
|---|---|
| Blocker | 1 (CR-38) |
| Critical | 3 (CR-39 … CR-41) |
| Warning | 27 (CR-42 … CR-68) |
| Suggestion | 14 (CR-69 … CR-82) |

## Türkçe Özet (kullanıcı için)

v1.1 kodu genel olarak özenli: katman kuralı bozulmamış (`pymupdf`'i yalnızca `pdfkit/api.py` import ediyor), şema v2 geçişi gerçekten atomik ve geri alınabilir, overlay çıktısında resimler ve çizimler piksel piksel korunuyor (150 DPI maskeli fark = 0), kaynak PDF hiçbir yolda değiştirilmiyor, loglarda anahtar ya da kitap metni yok, çeviri adımında aynı birim iki kez gönderilmiyor. Test dokümanındaki sonuç gerçekten iyi.

Ama test dokümanının dışına çıkınca dört ciddi sorun var; bunlar düzelmeden QA'ya geçilmemeli:

1. **Blocker — metin kaybı (reflow).** Şekil dedektörü, bir kümeyle %80 örtüşen her satırı "şekil etiketi" sayıyor. Arka planında tam sayfa resim olan PDF'lerde (OCR'lı tarama, kâğıt dokulu sayfa) sayfanın bütün metni, gölgeli not kutularında kutunun bütün paragrafı şekle yutuluyor. Addendum A'dan sonra lejant listesi varsayılan olarak çıktıdan silindiği için bu metin md/epub/pdf'te hiç görünmüyor, sadece PNG'nin içinde İngilizce kalıyor; çıkış kodu 0. Probe: 3 sayfa / 15 paragraf → şekiller açıkken 0 paragraf.
2. **Critical — `export` kaynak PDF'i doğrulamıyor.** `translate` dosya hash'ini kontrol ediyor ama `export` etmiyor. PDF değişir ya da yer değiştirirse DB'deki kutular başka dosyanın üstüne yazılıyor, çıkış kodu 0. Yol da göreli saklanıyor (`docs\test_doc.pdf`); başka klasörden `export` çalışmıyor ya da şekil etiketleri sessizce İngilizce kalıyor.
3. **Critical — sayfanın üst/alt %12'sindeki tek satırlık gövde metni üst bilgi sanılıyor**, çevrilmiyor ve inceleme listesine de girmiyor.
4. **Critical — madde işaretli / numaralı listeler overlay'de tek paragrafa çöküyor**; "aynı özelliklerle geri yaz" şartını bozuyor.

Bunların dışında 27 Warning var; en önemlileri: 429 hatasında batch'in tek tek isteklere parçalanması, anahat (bookmark) hedeflerinin bozulması, `summary.json`'da yerleştirme sayılarının hep 0 olması, sığmayan kutunun boş kalması, reddedilen `extract`'in PNG'leri çoktan ezmiş olması, kullanıcının kendi PNG'sinin silinmesi, kod bloklarındaki boş satırların `strip_legends` ile yutulması.

**Punto farkı (bilinen kozmetik sorun):** somut tasarım önerisi aşağıda ("Shared per-page scale") — iki geçişli yerleştirme: önce ölç, sonra aynı stildeki gövde birimlerine ortak ölçek uygula; aykırı kutular kendi ölçeğinde kalıp inceleme listesine girer. Maliyet birim başına ~4 ms.

`durum.md` sapmalarının çoğu kabul edildi; reddedilenler: PNG'nin yerinde ezilmesi (A.2), "orphan uyarısı var" iddiası (kodda yok), "exporter'lar fence-aware" (Markdown exporter değil), sayfa boyutu hatasının exit 2 olması, `extract --figure-legend` yardım metni.

## Scope and method

Five parallel review slices (overlay extraction + pass; placement engine; F1 figures; schema v2; F3 + cross-cutting), each reading its files completely and verifying every finding with a probe script (synthetic PyMuPDF documents, fake translators from `tests/conftest.py`, copies of the live state directories). The reviewer then re-verified the Blocker, every Critical and the key Warnings independently (re-ran the text-loss probes, the swapped-PDF export through the real CLI, the list / header-band extraction dumps, and read the cited lines). The provider was never called; nothing under `src/` or `tests/` was modified. Probe scripts: `%TEMP%\claude\…\scratchpad\fork1 … fork5`, `parent`.

---

## Findings

### Blocker

#### [CR-38] The figure detector swallows page prose; with the default options the text disappears from every reflow output — `src/extractors/figures.py:336-338, 366-367, 481-493`, `src/extractors/pymupdf_extractor.py:681-685`, `src/pipeline/orchestrator.py:2777-2778`

**What is wrong.** `_absorb_inner` claims every kept line whose box overlaps a cluster union by ≥ 80 % — without the `is_label_like` test that step 7 of design §4.1 applies to labels *outside* the union. The background rule (`_is_background`, `figures.py:148-156`) covers vector drawings only: a raster image ≥ 1 % of the page is always a candidate and any cluster containing one is accepted. Absorbed lines are removed from the paragraph flow, heading detection and glossary discovery and survive only as legend items. Since Addendum A.2 the legend list is stripped by default (`strip_legends`), so the text reaches no output; beyond `legend_limit` (40) it is not even sent to the provider.

**Evidence (re-run by the reviewer).**
- `fork3/probe_bgimage.py`: 3 pages with a full-page raster under the text (OCR "sandwich" / paper texture): figures on → `figures: 3 | prose paragraphs: 0 | legend items: 15`; `--no-figures` → 15 paragraphs.
- `fork3/probe_sidebar.py`: a shaded note box (1 fill + 4 border lines = 5 paths) around a 12-line paragraph → `<!-- image:1 … -->` plus one legend item holding the whole paragraph.
- `fork3/probe_frame2.py`: tinted frame around a page body → 5 paragraphs in the legend; after `strip_legends` none of them is present.

**Why it matters.** Silent loss of book content with exit 0 on common inputs (scanned+OCR books, technical books with note boxes); violates E-28 ("pure-text box … not a figure — text stays in the flow"), the intent of FR-33, NFR-18 (0 false figures) and regresses v1.

**Fix.** (a) treat a raster covering ≥ ~60 % of the page as background (mirror `_is_background`); (b) after `_absorb_inner`, regroup absorbed lines by block; if any block is not label-like (> 2 lines, > 80 chars, or wide) reject the cluster (`figure_candidate_rejected:text_heavy`) or hand those lines back to the flow; (c) page-level safety net: absorbed chars > ~40 % of the page's chars → drop the figure + warning; (d) fixtures: OCR-sandwich page, shaded sidebar, framed page; (e) see CR-57: never strip a legend whose labels were not painted into the PNG.

### Critical

#### [CR-39] `export` never verifies the source PDF: stored rectangles are written into whatever file sits at `job.input_path` (exit 0), and the path is stored as typed — `src/pipeline/orchestrator.py:2542` (`_bind(None, …)`), `:758-800` (hash compared only when an input is passed), `:776` (`JobSpec(str(input_pdf), …)`), `:2904-2922` (`_translate_figures`), `:2966-2972, 3011` (`_export_overlay`)

**Evidence (reviewer, real CLI).** Copy of `bt_overlay2`, a different 3-page PDF placed at `docs/test_doc.pdf` under a new cwd, `book-translator export --output out --format pdf-overlay` → `overlay written … placed 34, shrunk 0, could_not_fit 0`, `exit code: 0`; page 1 of the output reads `FOREIGN  0. FOREIG page 0. FO DOCUME … 2010 yılında piyasaya sürülen Microsoft Kinect …` — the foreign page with holes punched at the stored rects and Turkish paragraphs written into them. Three slices reproduced this independently. Same exposure in the reflow path: `_translate_figures` renders label boxes onto the unchecked file and overwrites `images/pNNN-fKK.png`. The live DB holds `input_path = 'docs\test_doc.pdf'`: `export` from another cwd → exit 1 `input_not_found` (overlay) or exit 0 with only `figure_translate_failed:input_missing:test_doc.pdf` (reflow; the labels stay English *and* the legend is stripped, so the paid label translations appear nowhere).

**Why it matters.** The core requirement is "write the translation back onto the same page". Addendum A-D4 (hash recomputed from stored units instead of re-extracting) is sound for `translate` — there `input_sha256` is checked before any provider contact (verified: swapped PDF → exit 5, 0 provider calls) — but the recomputed hash is a DB self-consistency check only; the step that actually uses the geometry has no binding to the file (FR-43, NFR-21, NFR-32).

**Fix.** Store `str(input_pdf.resolve())`; in `_export_bound`, when `pdf-overlay` or the figure step will run, `sha256_file(Path(job.input_path))` and compare with `job.input_sha256` → overlay: `STATE_HASH_MISMATCH` (exit 5); figures: warning `figure_translate_failed:input_changed` and keep the legend (CR-57). Add `export --input` for moved files (goes through the same check). Tests: swapped file, different cwd.

#### [CR-40] Non-repeating short body lines inside the top/bottom 12 % band become HEADER/FOOTER: never translated and never listed — `src/extractors/overlay_extractor.py:344-352`

**What is wrong.** `short = len(unit.lines) == 1 and is_short_band_candidate(...)`; `if running or short:` → HEADER/FOOTER, `translate=False`, `keep_reason="header_footer"`. The v1 extractor requires a repeating pattern before dropping a band line (`pymupdf_extractor.py:363-370`); the overlay extractor does not. The band is 101 pt on A4 and reaches into the normal text area.

**Evidence (re-run by the reviewer, `fork1/dump.py misc.pdf`).** `p001-b003 header tr=0 keep=header_footer … 'Key idea'` — a line that occurs once in a 4-page document at y = 79. `cols.pdf`: six different single-line column-bottom orphans (y = 748 of 842) all `footer, tr=0`.

**Why it matters.** Per §4.5 `header_footer` keeps are counted but not listed, so FR-45 does not surface them: content silently stays English (FR-46 says "v1 heuristic"; Q21 exit 0).

**Fix.** Accept `short` only when the unit shares its text row with a PAGE_NUMBER unit or a running-pattern line (still covers "Book overview" on p2 of the test document); otherwise keep it translatable. At minimum list pattern-less keeps in `overlay_review.json` as `kept_original` / note `header_guess`. Fixture: one-off short line in the band; two-column orphan lines.

#### [CR-41] List items collapse into one overlay paragraph unit — `src/extractors/cleanup.py:722-766` (`split_paragraphs`)

**What is wrong.** `parse_list_marker` is used only for the `hanging` exemption; a line that *starts* with a list marker does not open a new paragraph (v1 `same_paragraph`, `cleanup.py:398`, does).

**Evidence (re-run by the reviewer, `fork1/dump.py list.pdf`).** Intro sentence + 3 "•" lines + "1." / "2." lines → `p001-b001 body … lines=6 … 'The following steps are required for calibration of the camera: • …'` — one BODY unit.

**Why it matters.** The translation is laid out as one flowing paragraph inside the union rect: list structure lost (FR-43 "matching alignment and style", user core requirement), the oversized unit shrinks more, and the provider sees bullets as inline characters. Lists are ubiquitous in technical books.

**Fix.** In `split_paragraphs`: `starts_new = True` when `parse_list_marker(line.text)` matches (keep the hanging-continuation rule for wrapped items). Fixture with bullets, ordinals and wrapped items; assert one unit per item and that the marker survives placement.

### Warning

#### [CR-42] `runs.mode` is `'reflow'` for overlay runs of `run` and `export`; `status` recovers counters of an unfinished overlay run from `chunks` — `src/pipeline/orchestrator.py:820-842, 922, 1004, 2550, 3175-3178`
`_start_run` inserts the row once; the extract stage calls it without `mode` (default `MODE_REFLOW`), so the later `mode=mode` call at 1453 is a no-op; the export stage never passes it. **Evidence (reviewer):** live `bt_overlay2` DB (43 overlay units, 0 chunks): `runs = [('run','reflow','SUCCESS')]` while `summary.json` says `"mode": "overlay"`; after `export --format pdf-overlay` on a copy → `('export','reflow',…)`. `status` calls `chunks.count_completed_by_run` unconditionally: an overlay run killed after 5 completed units shows `chunks_completed: 0` (`OverlayBlockRepository.count_completed_by_run`, `repository.py:1904`, has no caller). Doc 07 B14 / doc 06 §5.6. **Fix:** pass the mode to every `_start_run`; `export` → `'overlay'` when only `pdf-overlay` is requested; route the counter recovery by `run.mode` (guarded by `schema_version >= 2`); test `SELECT mode FROM runs` after `run --mode overlay`.

#### [CR-43] A batch-level provider error is handled per unit: the limiter collapses, counters inflate and the batch fragments into single-unit requests — `src/pipeline/orchestrator.py:2283-2286, 2394, 2427-2442`
`for unit in todo: await self._handle_error(...)` → `limiter.on_rate_limited()` and `stats.rate_limited += 1` per unit, a separate jittered `next_attempt_at` per unit; `on_success()` per unit, not per request. **Evidence (`fork1/probe_429.py`, concurrency 8, one scripted 429 on a 10-unit batch):** 12 provider calls instead of 3 (10 single-text retries), limiter `8→4→2→1`, `rate_limited_count = 10` for one 429. §3.6.2 "one batch = one permit = one call", NFR-23 (requests ≤ 2× reflow) — and the burst happens exactly while the provider throttles. **Fix:** for a batch-level `Err` signal limiter/stats once, compute one delay from `max(retry_count)` and reschedule all units with the same `next_attempt_at`; count `on_success` once per request; test.

#### [CR-44] Per-unit completion commits (~49 ms each under the global lock) widen the re-billing window; CR-22 now costs "the rest of the batch" — `src/pipeline/orchestrator.py:2285-2288`, `src/database/repository.py:1644`
Live log: 30 `chunk_completed` events of one batch span 1.46 s (≈ 2.4 s for a 50-unit batch). A kill, second Ctrl+C, grace expiry or lease loss in that loop releases the already-paid remainder (`_drain` releases every id of the batch, `:2189-2194`); other workers wait on the lock. The design accepted a one-batch window, not this cost. **Fix:** `complete_many(job_id, results)` in one `BEGIN IMMEDIATE`; shield it and await it in `_drain` before releasing (closes CR-22 for both passes).

#### [CR-45] A crash between `replace_all` and the binding patch locks the pass at exit 5 until `--fresh` — `src/pipeline/orchestrator.py:1802-1828, 1853-1871`
Two transactions; with rows present and `overlay_sha256 IS NULL` the code falls through to the signature comparison → `state_hash_mismatch: … (state None…, now 6127af0f4974…); start over with --fresh` (probed by two slices). `--fresh` archives the whole DB including a completed, paid reflow pass (D7). Doc 07 §3.3 specifies "NONE with rows present → re-extract and replace". **Fix:** when `overlay_status == 'NONE'` / hash NULL and no unit is COMPLETED → re-extract + `replace_all` (it already refuses when COMPLETED units exist), or write both in one transaction.

#### [CR-46] `translate --mode overlay --fresh` leaves the old `translated_book.overlay.pdf` / `overlay_review.json`, and `status` attributes them to the new pass — `src/pipeline/orchestrator.py:1358-1360, 3074-3106`
`fork1/probe_fresh.py`: status shows `OVERLAY_EXTRACTED` together with `review_counts {'placed': 34, …}` and the old `output_path`. **Fix:** archive both files in `translate(fresh)` or report them only when `overlay_status == "OVERLAY_EXPORTED"`. (v1 behaviour, noted: `--dry-run --fresh` archives real paid state.)

#### [CR-47] Fragment tagging misses the continuation half and tags non-translatable units — `src/extractors/overlay_extractor.py:552-559`
`test_doc`: p003-b004 "open-ended research problems…" (lowercase start, continues p001-b012) has `fragment=0`; the rotated unit in `misc.pdf` has `fragment=1`. E-42 / FR-45 expect both halves in the review list. **Fix:** tag the first BODY unit of a page when it starts lowercase; exclude `translate=False` units from `body_indexes`. `tests/test_overlay_extractor.py:155` (`assert isinstance(last_body.fragment, bool)`) is vacuous — replace.

#### [CR-48] Observability gaps in overlay mode (NFR-30) — `src/pipeline/orchestrator.py:2253, 2754, 2810, 3018`; `src/exporters/{pdf_overlay_exporter,_placement,figure_overlay,pdf_native,pdf_exporter}.py` (no logger)
(a) `chunk_context(first_id, attempt)` wraps the whole batch: the 43 live `chunk_completed` events carry only `chunk_id` 1 and 14 — a failed unit cannot be found from the log. (b) `grep` finds no emitter for `overlay_block_placed`, `overlay_block_review`, `overlay_residual`; `pdf_engine_selected` is emitted as `pdf_engine`, `font_validated` as `pdf_font_validated`; residual/collateral redaction leaves no trace in the JSONL log. (c) `progress=None` is passed to the renderer: a 300-page export is silent. (d) `overlay_extracted` is logged twice (`overlay_extractor.py:736`, `orchestrator.py:1833`). **Fix:** per-unit `chunk_context`; per-page INFO with counts, WARNING per review entry with `block_id`, `reason`, `scale` — never the text; pass the progress callback; align event names with §5.6 or update the doc.

#### [CR-49] CR-18 and CR-21 are worse in overlay mode: `find_tables` runs 2–3× per page under a lease with no heartbeat — `src/extractors/pymupdf_extractor.py:276`, `src/extractors/overlay_extractor.py:243`, `src/pipeline/orchestrator.py:1508, 2100`
`read_raw_page → _read_tables` runs unconditionally, `page_table_cells` runs it again; `OverlayOptions.detect_tables` disables only the second call and is wired to nothing; `run --mode overlay` also runs the full reflow `extract` first. `fork1/probe_tables.py`: 6 calls for 3 pages, 0.68 s of 0.74 s. The heartbeat starts only in `_run_job`; `_prepare_overlay_units` runs before it — at ~0.23 s/page a book over ~260 pages exceeds `lease_stale_s = 60`. **Fix:** read tables once per page and reuse the cells; wire `detect_tables` to a setting/flag; heartbeat from the extraction progress callback (closes CR-18 for both passes).

#### [CR-50] Retitling one bookmark rebuilds the whole outline and degrades every entry — `src/exporters/pdf_overlay_exporter.py:463-484`, `src/pdfkit/api.py:84-85, 202-203`
`doc.get_toc()` (simple) → `set_toc(rows)`. `fork2/p_toc.py`: all destinations moved from `Point(72,400)` / `(72,555)` to `(72,36)`, bold/colour dropped, a URI bookmark became `kind 0, page -1`. FR-47 / AC US-21/2. **Fix:** iterate `get_toc(simple=False)` and `doc.set_toc_item(idx, title=translated)` (probed: destination, colour, URI kept); wrapper `doc_set_toc_item`; test with a mid-page destination and a URI entry.

#### [CR-51] `summary.json` never carries the placement result — `src/pipeline/orchestrator.py:1931-1963, 1995, 3037, 3087`
**Evidence (reviewer):** live `summary.json` → `"placed": 0, "kept_original": 0, "review_path": null`, `status: OVERLAY_TRANSLATED`; `overlay_review.json` of the same run → `placed 34, kept_original 9, entries 2`. A standalone `export` does not rewrite `summary.json`. FR-45 ("file + summary count"), §5.6. **Fix:** return the `OverlayArtifact` counts from `_export_overlay` into `ExportOutcome` and `_overlay_summary(placed=…, review_path=…)`; print the review count on the console line.

#### [CR-52] A unit that cannot take even the ellipsis ends up blank; a single long word becomes a bare "…"; truncated figure labels are silent — `src/exporters/_placement.py:190-220`, `src/exporters/pdf_overlay_exporter.py:343`, `src/pipeline/orchestrator.py:2921`
Redaction runs before any fit is known. `fork2/p_ell.py`: tiny rect → `placed=False`, source text already removed, nothing written; truncation is word-based only. Reported as `could_not_fit` / exit 2 (Q13-conformant), but keeping the English text is strictly better than an empty box. In the figure path `render_translated_figure` drops `FigureRenderStats` (`…_with_stats` is used by tests only): `truncated=(False, True)` → no warning, the English label is erased and the PNG shows "…". **Fix:** measure before redacting (free with the shared-scale pass below); units measuring −1 whose ellipsis probe fails → `kept_original` note `could_not_fit`, not redacted, still exit 2; character-level fallback for one-word texts; call the `_with_stats` variant and warn `figure_label_truncated:<file>:<n>`.

#### [CR-53] Pages stored with `/Rotate ≠ 0` are never translated — `src/extractors/overlay_extractor.py:504-508`
`fork2/p_rot2.py` (upright-looking text on a `/Rotate 90` page): every line has `dir=(0,-1)` in MuPDF's unrotated space → one unit per line, `translate=False, keep_reason="rotated"`; the page stays English with N review entries. Safe, but landscape-stored pages (wide tables/figures) are common. No test (`tests/test_overlay_export.py:222` hard-codes `rotation=0`). Cropbox-offset pages work (probed). **Fix:** judge direction relative to `page.rotation` and place with `rotate=page.rotation`; until then one page-level warning `overlay_page_rotated:N` and a documented limit.

#### [CR-54] E-50 is half kept: pre-existing redaction annotations are re-created without their properties, and the figure path applies them — `src/exporters/pdf_overlay_exporter.py:336-345`, `src/pdfkit/api.py:401-417`, `src/exporters/figure_overlay.py:102`
Overlay PDF: the re-added annot lost `fill [0,0,0]` and its overlay text (new xref; rect and underlying text kept). Figure PNG: `_placement.redact` runs on a page that still holds the source's redaction annots → `fork2/p_fig.py`: text under an existing annot ("KEEPME") disappears from the PNG. **Fix:** make detach/re-add (with fill, text, colours) part of `_placement.redact` so both callers share it.

#### [CR-55] A refused or failed re-`extract` has already overwritten `images/*.png` — `src/pipeline/orchestrator.py:929-935` (sink runs inside `_run_extractor`) vs `:940-943, 956-961`
Code order verified by the reviewer: the language check and `_preserve_previous_source` (which can refuse with exit 5) run *after* the sink wrote the PNGs. Probe on a copy of `bt_f1`: edited `source_book.md`, `extract --figure-dpi 100` → exit 5 `state_hash_mismatch`, but `images/p002-f01.png` went 131 374 → 56 605 bytes while `figures.json` still says dpi 200. Breaks CR-06 "nothing written on refusal" and E-57; silently reverts an in-place translated PNG. **Fix:** sink into `images/.staging-<run>`; promote after all checks; remove on every `Err` path.

#### [CR-56] Stale-image cleanup deletes files the tool did not create, without archiving — `src/pipeline/orchestrator.py:1172-1187` (pinned by `tests/test_orchestrator.py:1266-1278`)
`glob("*.png")` minus the new inventory → `images/my-cover.png` deleted, no archive entry, no warning. E-33/O1 allow hand-edited image lines pointing at the user's own PNG. **Fix:** delete only `^p\d{3}-f\d{2}\.png$` names that were in the *old* inventory, or move them into the archive stamp.

#### [CR-57] The translated figure overwrites the English PNG that `source_book.md` and `figures.json` also use; the legend is stripped even when in-place translation did not happen — `src/pipeline/orchestrator.py:2777-2778, 2920-2936`
After `export`: PNG sha `69f3…` → `58c4…`, 131 374 → 118 488 bytes, `figures.json` still reports 131 374 (status/summary totals wrong); previewing `source_book.md` shows the Turkish figure; a later `extract` silently reverts `translated_book.md`'s image to English until the next export. When the figure step fails or is skipped (`input_missing`, font, `legend_mismatch`, labels without `label_boxes`) `strip_legends` still removes the list → the paid label translations appear nowhere. **Fix:** write `images/pNNN-fKK.tr.png` and rewrite `src` in the assembled document only (or record `translated: true` + bytes in the inventory); strip a legend only for figures whose replacement set was fully painted.

#### [CR-58] `strip_legends` alters fenced code — `src/pipeline/figure_labels.py:101-110`
The blank-line collapse and `replace("\n\n\n", "\n\n")` run over the whole document (code read by the reviewer): `a = 1\n\n\nb = 2` inside a fence → `a = 1\n\nb = 2` (`fork3/probe_strip.py`). Triggers whenever the book has ≥ 1 legend, i.e. on the default path; breaks the verbatim-code guarantee. **Fix:** collapse only around each removed block; skip fenced lines.

#### [CR-59] `resolve_figure` can be bypassed with backslashes / drive paths on Windows; `FigureEntry.file` is an unvalidated write target — `src/exporters/base.py:210-223`, `src/exporters/epub_exporter.py:293-301`, `src/extractors/figure_inventory.py:97`, `src/pipeline/orchestrator.py:2920`
The `..` test uses `PurePosixPath` parts but `base_dir.joinpath` interprets `\`: `'..\\outside\\secret.png'`, `'images\\..\\..\\x.png'`, `C:\…` all escape the output directory (`fork3/probe_trav.py`) and the EPUB exporter would embed the file. Low risk (user-controlled file) but the docstring's guarantee is false. **Fix:** reject `\` and `:` in `src`; require `path.resolve().is_relative_to(base_dir.resolve())`; validate inventory `file` against `^images/p\d{3}-f\d{2}\.png$` on load.

#### [CR-60] Figure detection is quadratic in the number of paths — `src/extractors/figures.py:188-195, 343-347`
One page: 500 drawings 0.08 s, 2 000 → 1.13 s, 5 000 → 11.4 s (`fork3/probe_perf.py`); scatter plots/maps reach 10⁴–5·10⁴ paths. NFR-20 (≤ 1 s/page). **Fix:** grid bucketing with cell = `merge_gap_pt` (or sweep line); pre-merge above N paths.

#### [CR-61] The source PDF is fully re-read and re-opened for every figure at export — `src/exporters/figure_overlay.py:72-82`, loop at `src/pipeline/orchestrator.py:2913-2923`
`read_bytes()` + `open(stream=…)` per figure: N figures × file size (read from the code, not timed). **Fix:** open once per export, group records by page.

#### [CR-62] `figure_orphan` (design §5.4, E-33) is not implemented — no match for `figure_orphan` under `src/` (reviewer grep)
`durum.md` states "`figure_missing`/orphan uyarısı orchestrator'da"; `figure_missing` comes from the exporters (`base.py:276`, `markdown_exporter.py:62`), the orphan warning exists nowhere. **Fix:** implement (compare `image_lines()` with `images/*.png` at export) or correct doc 06 + `durum.md`.

#### [CR-63] USER errors from the PDF exporter exit 2 with no artefact instead of exit 1 — `src/pipeline/orchestrator.py:2813-2817`, `src/exporters/pdf_exporter.py:151`
Any `Err` of an `optional` exporter becomes a warning (code verified by the reviewer): `--pdf-page-size Foo` → exit 2, `outputs: []`, `export_failed:pdf:unknown PDF page size 'Foo'…`; `--pdf-margin 200mm` likewise; a stale `translated_book.pdf` stays. D13 maps USER errors to 1; 2 means "partial output". **Fix:** downgrade only `EXPORTER_UNAVAILABLE`; better, call `resolve_page_design` next to the font check before assembly; CLI test.

#### [CR-64] `--pdf-font` for `pdf-overlay` is validated after the reflow artefacts were written — `src/pipeline/orchestrator.py:2747` (`"pdf" in exporter_names` only), `:2665-2689`, `:2973`
Verified by reading the code order (no dual-pass directory was available to probe): with `-f md -f pdf-overlay --pdf-font bad.ttf` md/epub are produced (and PNGs possibly rewritten) before the exit-1 refusal. FR-53 "before any rendering", FR-49 "nothing written on any refusal". **Fix:** hoist `resolve_font` into `_export_bound` before either group whenever `font_path` is set and `pdf` or `pdf-overlay` is requested.

#### [CR-65] `source_profile.json` values are not range-checked — `src/extractors/source_profile.py:94-105`, `src/exporters/pdf_native.py:195, 211-212`
`margins_pt: [-50,…]` → exit 0 with text x from −5.3 to 600.7 on a 595 pt page; page 1e9 × 1e9 accepted; body 500 pt → exit 2 "layout made no progress". The tool writes sane values, but the file is the documented place to adjust the page design. **Fix:** in `load_source_profile` require finite values, 72 ≤ w,h ≤ 14 400, margins ≥ 0 leaving ≥ `MIN_COLUMN_PT`, 4 ≤ body ≤ 72; otherwise `Err` → the existing `pdf_page_design:` warning + fallback.

#### [CR-66] Source-derived page design is incomplete against Addendum A.3 — `src/extractors/cleanup.py:870-878, 890`, `src/exporters/fonts.py:130-135`, `src/exporters/base.py:75`, `src/exporters/pdf_native.py:211-214`, `src/pipeline/orchestrator.py:2842-2848`
(a) `SourceProfile.serif` is computed and persisted but never read: a sans-serif source reflows in Charis SIL. (b) Margins are measured over *all* text lines, so the body box extends into the source's header/footer zone (43.7 pt top on the test document). (c) The hard-coded 18/15/20/15 mm survive in `ExportOptions` and are applied without `"margins"` in `assumed` (`tests/test_pdf_native.py:532, 604` pin this) although A.3 says they are gone. (d) No missing-profile warning for `--pdf-margin source` with an explicit page size. **Fix:** `ExportOptions.body_family` from the profile; measure kept body lines; record `"margins"` in `assumed` with a proportional fallback; warn in case (d).

#### [CR-67] A DB exception from `iter_page` escapes `OverlayRenderer.render` — `src/exporters/pdf_overlay_exporter.py:247`, `src/pipeline/orchestrator.py:3008-3018`
`blocks = list(blocks_by_page(info.page))` sits outside the page `try`; probe with a raising callback → `ESCAPED render(): OperationalError`. Corrupt rows are caught earlier by the outline pass, so only a transient lock/I/O error mid-export is uncovered — but it breaks the "no exception crosses a layer" rule. **Fix:** move the call inside the `try` or wrap `iter_page` in the orchestrator closure → `Err(STATE_LOCKED/STATE_CORRUPT)`.

#### [CR-68] Test quality and gaps in the v1.1 suites
Vacuous: `tests/test_pdf_native.py:537` and `tests/test_exporters.py:531` (`assert isinstance(Ok(1), Ok)`, the CR-19 pattern again); `tests/test_orchestrator.py:1825-1833` (`exit_code in (SUCCESS, PARTIAL)`, assertion under `if`, `mode in ("overlay","reflow")`), `:1376-1377` (NFR-31: PNG bytes can never appear in a string payload — assert payload set == caption + labels), `:1810-1817` (`after != before` only); `tests/test_overlay_extractor.py:155`. Timing: `tests/test_repository_overlay.py:965` wall-clock `< 6.0 s` with ~2× headroom; `test_05` real 0.5 s wait. External artefact: `tests/test_extractor_cleanup.py:798-803` pins byte identity against `docs/test_doc_output/source_book.md`, which `durum.md` says gets refreshed — move to `tests/fixtures/`. Silent skips: `tests/test_orchestrator.py:1809, 1820` `skipif(not HAS_…)`. Leaks: four `page_infos(pymupdf.open(...))` handles in `tests/test_overlay_export.py`. Missing: positive `RESIDUAL_GLYPHS` / `COLLATERAL_REDACTION` tests (only absence asserted, `:619-620`); Q21 exit codes through orchestrator/CLI; overlay `--allow-partial` export; rotated page; outline destinations; every Blocker/Critical above; `runs.mode`; constant parity (CR-70); an AST test that only `pdfkit` imports `pymupdf`/`fitz` (clean today by grep, not pinned).

### Suggestion

#### [CR-69] Duplicate constraint name `ck_overlay_blocks_keep_reason` — `src/database/models.py:768, 769-771`
The enum check and the `(translate = 1) = (keep_reason IS NULL)` group check share one name (fresh schema: 107 `CONSTRAINT` clauses, 106 unique names). SQLite accepts it, but an `IntegrityError` cannot tell the two apart and `test_19` / `test_01` (name sets / dict) do not notice. Rename the enum check (`…_keep_reason_enum`) before v2 ships — renaming later needs a table rebuild.

#### [CR-70] Duplicated constants with no parity test — `src/domain/overlay.py:45-54, 148-155`, `src/database/models.py:68-99`
`OVERLAY_KEEP_REASONS`, `OVERLAY_PAGE_SKIP_REASONS`, `OVERLAY_JOB_STATUSES` exist twice (the domain copies are unreferenced); `OVERLAY_BLOCK_KINDS` / `OVERLAY_ALIGNMENTS` / `OVERLAY_FAMILIES` duplicate the domain enums/Literal; literals are repeated in `overlay_extractor.py:391-420`, `pdf_overlay_exporter.py:50, 437, 444`, `orchestrator.py:1565, 1597, 1642, 1819, 2705`. All six pairs are equal today (probe); drift would first show as a CHECK failure inside `replace_all`. **Fix:** derive the `models.py` lists from the domain enums (`database → domain` is an allowed edge) and add one parity test.

#### [CR-71] `complete()` ignores `result.chunk_id` — `src/database/repository.py:1644-1675`
`complete(unit_id=7, result.chunk_id=6)` → `Ok(True)`. Overlay results are paired with units by position (`zip(todo, outcome.value, strict=True)`, `orchestrator.py:2286`); a future adapter ordering bug would silently put a translation into the wrong box. One-line guard: `if result.chunk_id != unit_id: return Err(INTERNAL, JOB_FATAL)` (adapters always set it: `units.py:231, 402, 540`).

#### [CR-72] `overlay_unit_count` and `overlay_tool_version` are written but never read — `src/pipeline/orchestrator.py:1823-1824`
The doc 07 §3.3 tripwire (`COUNT(*) == overlay_unit_count`, else `STATE_CORRUPT`) is not implemented; the A-D4 recomputed hash covers the case functionally. Use the columns (cheap pre-check; `tool_version differs` warning on resume) or mark them informational in doc 07.

#### [CR-73] `open_database` catches a narrow exception tuple in the migration path — `src/database/session.py:410`
A `RuntimeError` inside a migration step escapes and the engine is not disposed (open file handle on Windows). Catch `Exception` there. A backup failure returns `STATE_CORRUPT` ("could not archive…") although the state is intact — `STATE_LOCKED` or an I/O code is more accurate.

#### [CR-74] The smoke re-open runs after `os.replace`; the whole output is held in memory — `src/exporters/pdf_overlay_exporter.py:269-275`, `src/pdfkit/api.py:227, 344`
A PDF failing the smoke test has already replaced the previous good file (letter of §4.5, but avoidable): smoke-test `open_document_bytes(data)` before `atomic_write_bytes`. `doc_to_bytes` keeps the output next to the document in memory (`doc_save` exists, unused; D11 said "save(tmp)").

#### [CR-75] Literal entity text is decoded twice — `src/exporters/_placement.py` (HTML build)
Translated text `&amp; &lt;b&gt;` written literally in a book renders as `& <b>` (MuPDF decodes `&amp;amp;` twice; probed with raw `insert_htmlbox`). Real `<`, `&`, quotes render correctly. Only books quoting HTML entities are affected: document, or emit `&#38;` inside entity-looking runs.

#### [CR-76] Line height is a constant 1.15 — `src/exporters/_placement.py:27`
The source pitch on the test document is ~1.26 em; every paragraph is set tighter than the source and leaves 4–10 pt spare. Use the source leading (from `line_boxes`, clamped 1.15–1.4) and give it up *before* shrinking the font — see the shared-scale design.

#### [CR-77] Leftovers from parallel development
- Near-copy / dead wrappers in `src/pdfkit/api.py`: `doc_is_encrypted` (264) vs `doc_needs_password` (331); `doc_permissions` + `permission_modify` (268/273) vs `doc_allows_modify` (335); `page_image_boxes` (197, tests only) vs `page_raster_boxes` (310, unused); `page_rect` (167) vs `page_size` (282); `doc_to_bytes` (227) vs `doc_save` (344, unused); `doc_page` (321) vs raw `doc[index]` at `pdf_native.py:437`; `page_annot_count` (420) unused; `page_fonts`, `page_words`, `doc_language` tests only.
- The two permission checks differ: `check_modifiable` refuses any `is_encrypted` document (`overlay_extractor.py:156`), the renderer only `needs_pass` (`pdf_overlay_exporter.py:226`) → one `doc_modifiable()`.
- `orchestrator.py:2917-2919` identity copy `figure_overlay.LabelReplacement(**dataclasses.asdict(r))` — `figure_overlay.py:15` imports the domain class; the "mirror" of `durum.md` no longer exists. `:2892-2903, 2956-2965` `ImportError` guards for modules that always ship; `:2854-2864` `getattr` probing of fields that exist; `cli.py:1390-1397` `find_spec` availability for `pdf-overlay` that can never be false.
- Unreferenced: `cleanup.caption_pattern`, `pipeline/units.iter_units`, `domain/overlay.UnitStatus`, `pdf_native.page_geometry` (tests only); the `PlacementResult` list returned by `_build_page`. Unused arguments: `cleanup.is_label_like(body_size)` `:475`, `orchestrator._export_reflow(bound)` `:2721`, `units.build_request(job_id)` `:351`.
- The outline rule "within 3 pt of the destination point" (§3.6.4) is not implemented; page + equal title is stricter in practice — accept, fix the doc.

#### [CR-78] Help text and docs drift
- `extract --figure-legend` (`src/cli.py:208-214`, used at `:991`) has no effect (`figure_options_from_settings` hard-codes `legend=True`, `extractors/base.py:52`) while `--help` says "Also render the label legend list … in md/epub/pdf". Hide it on `extract` or mark it deprecated.
- `--pdf-engine` help renders as "weasyprint ( extra)": rich markup eats `[pdf]` (`cli.py:385`) — the CR-12 class; escape as `\[pdf]`.
- JUSTIFY is described three ways: doc 06 §4.4 "≥ 2 lines", A-D3 "≥ 4", `durum.md` "≥ 3"; the code has `_JUSTIFY_MIN_LINES = 3` (`cleanup.py:526`). Fix A-D3 and §4.4.
- doc 06 D12 / §7.1 still list A5, 18 mm margins and legend "on" with no "superseded by Addendum A" marker; §3.6.2 shows the old `UnitAdapter` signature (A-D6); doc 07 says "21 runtime columns" (O-1) / "23" (ER) / booleans as `INTEGER` — code: 20 columns, `BOOLEAN`.
- `README.md` does not mention overlay, figures, native PDF or `source_profile.json`.

#### [CR-79] The Markdown exporter's fence tracking is not the matched-fence logic of `base.py` — `src/exporters/markdown_exporter.py:44-50`
It toggles on any ```` ``` ```` or `~~~` line: a `~~~` line inside a ```` ``` ```` block flips the state, the image token inside the code block is rewritten to `*[Şekil 1 — …]*` and the real `<!-- image:2 -->` after it stays raw (`fork3/probe_misc.py`); `markdown_to_html` handles the same input correctly. Reuse `base._fence_toggle`.

#### [CR-80] An oversize legend loses its caption context — `src/pipeline/chunker.py:465-468` (before the legend branch at `:474`)
40 labels × ~47 chars > `max_chars` (1500): the block is split, the caption lands in its own chunk, no `legend:N` warning, `Segmented.context` is `None`; pairing still works (40/40). The `durum.md` claim "tek chunk (`oversize:legend`)" holds only when the legend alone fits. No provider limit at risk. Check `legend_image_id` before the oversize branch, or document.

#### [CR-81] Smaller overlay items
- `claim_pending_batch` ignores `max_chars_per_request` (LLM 6 000, local NMT 4 000; those providers do not split internally — DeepL does).
- Units overlapping a widget are sent and billed, then kept original at export (`pdf_overlay_exporter.py:442-444`); the extractor never sets `keep_reason="widget_overlap"` (design §3.6.1).
- E-52 "provider returned the same text" is never detected (only empty/truncated).
- A centred single-line heading outside a drawing rect gets `alignment=left` (per design §4.4 step 7) — centring is lost whenever the translation is shorter than the source; consider centring against the page's text column.

#### [CR-82] Hygiene
Five `# type: ignore[type-arg]` on bare `dict` in `pdfkit/api.py:136, 206, 214, 382, 388` disappear with `Dict[str, Any]`; eight `noqa: BLE001` without reason text (e.g. `pdf_native.py:550, 575`); `_layout` returns/raises without `writer_close` (`pdf_native.py:474-498`, harmless with BytesIO); `_weasyprint_page_css` swallows a USER page-design `Err` into defaults (`pdf_exporter.py:81-87`); `sqlite3.connect()` used as a context manager and never closed in `tests/test_migration.py` (harmless under `tmp_path`).

---

## Re-assessment of the open v1 findings

| Id | Status in v1.1 |
|---|---|
| CR-08 / CR-09 / CR-10 | Unchanged, still decided by live QA. CR-09 now also applies to overlay units (same `tag_handling="xml"` path). |
| CR-18 (no heartbeat during extraction) | **Worse** — overlay extraction also runs before the heartbeat starts (`orchestrator.py:1508` vs `:2100`) and is ~3× slower per page because of CR-21; ~260 pages reach `lease_stale_s`. Fix with CR-49. |
| CR-19 (test gaps) | Partly closed by the v1.1 suites; the no-op assertion pattern spread (CR-68). |
| CR-20 (ASCII-only discovery) | **Still open**, regex unchanged (`src/glossary/discovery.py:46-47`, read by the reviewer); probe: "Müller met José and Zoë" → entries `Jos`, `Zo`, Müller missing. Matters more now: truncated keys also feed the overlay per-unit glossary check. Legend blocks are correctly excluded from discovery (`discovery.py:90-99`). |
| CR-21 (`find_tables` on every page) | **Worse** — 2 calls per page in overlay extraction, 3 with `run --mode overlay` (CR-49). |
| CR-22 (lock released while the thread still writes) | **Worse** — the exposure is now the rest of a batch (up to 50 paid units), CR-44. |
| CR-35 | Unchanged. |
| CR-24 … CR-33 | Unchanged; CR-32 (`extract --fresh` creates the DB before extraction) interacts with CR-55 (PNG written before the refusal checks). |

---

## Shared per-page scale — design proposal for the known cosmetic issue

**Assessment.** Visible on `docs/test_doc_output/v1_1_overlay/ov_p1.png` (paragraphs 4, 7, 8) and more on `ov_p2.png`. Natural scales: page 1 BODY 1.0 / 0.955 / 1.0 / 0.908 / 1.0 / 1.0 / 0.900 / 0.921; page 3 BODY 0.869–1.0; page 2 FIGURE_LABEL 0.681–0.923.

**Dry run.** `insert_htmlbox` (PyMuPDF 1.28.2) has no dry-run flag and `Story.fit_scale` returns different numbers — do not use it. Measuring on a scratch page (the existing `_fits_on_scratch` pattern) is exact and cheap: 0.116 s for 34 units (~4 ms per unit).

**Design.**
1. `_placement.measure_scale(scratch_doc, spec, font, floor, archive) -> float` (−1 when the text does not fit at the floor).
2. `_build_page` becomes measure → group → place:
   - **Measure** every unit to place *before* redaction (this also fixes CR-52: a unit measuring −1 whose ellipsis probe fails is not redacted).
   - **Group** by `(page, kind, round(font_size * 2) / 2, family)`. Per group `bound = max(review_threshold, median(natural) − 0.10)`; `shared = min(s for s in natural if s >= bound)`. Units below `bound` are outliers: they keep their own scale and their `shrunk_below_threshold` / `could_not_fit` reason, so one bad box never drags a page down (page 2 labels: 0.761 instead of 0.681, one outlier). Single-unit groups are unchanged.
   - **Place** with `font_size * shared * 0.99` and `scale_low = floor` as the safety net (probe: 2 of 30 units needed a further 0.2–8 % because font scaling ≠ layout scaling); record the effective scale `shared × returned`. The ellipsis fallback is unchanged and applies only to units measuring −1.
   - Before shrinking the font, relax the line height from the source pitch down to 1.15 (CR-76).
3. `OverlayRenderOptions.uniform_scale: bool = True`, CLI `--overlay-uniform-scale / --no-overlay-uniform-scale`; `overlay_review.json` entries gain `shared_scale`. `figure_overlay` uses the same helper for labels of one figure.
4. Cost ≈ 2× insert calls (+4 ms per unit); NFR-27 unaffected.

**Tests.** Three equal-style paragraphs with translations of different length → identical span sizes; an outlier keeps its own scale and gets a review entry; single-unit group identical to today; `uniform_scale=False` byte-compatible with the current output; a unit measuring −1 is not redacted; figure labels share a scale.

---

## Verified OK (no finding)

- **Layering:** `tests/test_layering.py` 3 passed; only `src/pdfkit/api.py:13` imports pymupdf (grep); no `exporters → extractors` or `pipeline → database.models` edge; function-level imports are caught by `ast.walk`.
- **Input binding at translate:** `input_sha256` is checked on every `translate` / `run` bind before any provider contact (`orchestrator.py:761-800`); swapped PDF → exit 5, 0 provider calls, 0 `prepare` calls. Option flips and DB tampering → mismatch (tests). `overlay_signature` (`overlay_extractor.py:612-628`) formats bboxes `.3f`; stable across the DB round trip and repeated extraction; the signature recomputed from the live DB rows equals `jobs.overlay_sha256`.
- **`check_modifiable`** (`overlay_extractor.py:124-165`): user password → refused; owner-encrypted without the modify bit → refused; owner-encrypted with all permissions → Ok; plain → Ok; runs before the translator factory and before `prepare`.
- **No duplicate sends:** resume re-queues PROCESSING only; kept units complete without a provider call (`orchestrator.py:2254-2266`); `--limit` counts units; `reset_failed` / `requeue_processing` are scoped to the pass repository; quota errors release every unit of the batch and set `OVERLAY_PAUSED`; the empty-response counter works per unit and survives a restart; `apportion` sums exactly; `chars_sent` excludes the context; the context is never persisted or logged (length only).
- **Extraction details:** hyphen rejoin ("recon-/struction" rejoined, "well-known" kept, ambiguous join logged as a tag without words); superscripts → "¹"/"⁹" and `<sup>` on write-back; rotated text `keep=rotated`; table cells one unit per cell ("35", "2.8" → `no_letters`); footnotes; right-aligned block; justify and `text-indent` write-back (first line x0 63 vs 51).
- **Refusals / defaults / exit codes:** `default_formats` and FR-49 refusals match Addendum A.1 (`orchestrator.py:382-392, 2624-2660, 3268-3279`); exit 2 on `could_not_fit`, skipped pages or partial export (`:3024`); `--overlay-floor-scale > --overlay-min-scale` → exit 1; `--fresh` archive set includes `overlay_review.json`, `source_profile.json`, `images/`.
- **Placement (NFR-21):** fill-less redaction, one `apply_redactions(images=NONE, graphics=LINE_ART_NONE, text=REMOVE)` per page (`_placement.py:138-144`, `pdfkit/api.py:348-362`); live output: images 0/0, drawings 1/39/1 before and after, masked 150-DPI pixel diff 0 on all three pages; text over a raster keeps the pixels and gets `over_image`; residual and collateral checks are enforced and listed (`pdf_overlay_exporter.py:351-362, 394-401`); never enlarged, nothing spills below the rect; ellipsis binary search correct and terminating; `<b>`, `&`, quotes render literally; URI/GOTO/NAMED/GOTOR links re-inserted; widget overlap → `kept_original`; highlight annotations survive; metadata + `language=tr`, author preserved; fonts subset (86 KB vs 112 KB source).
- **Write safety:** PDF before review JSON, both temp + `os.replace`; target locked on Windows → `Err EXPORT_FAILED`, no temp leftover, old file intact; documents closed on every path; the source PDF's sha256 identical before and after every probe; `figure_overlay` works on an in-memory copy and re-renders from the source PDF on every export (idempotent). NFR-27: 3 pages in 0.3 s.
- **Schema v2:** runtime-group parity via `RuntimeStateMixin` (`models.py:296-433`): identical `table_info` for the 20 columns, all 16 CHECK expressions equal apart from the table prefix; fresh-v2 `chunks` DDL + indexes textually identical to `tests/fixtures/v1_schema.sql`; fresh v2 vs migrated v1: same objects, equal constraint names and expressions, ALTER-added CHECKs fire. Migration (`session.py:260-307, 391-424`): transactional DDL under explicit `BEGIN IMMEDIATE` (failure injected after all seven `ADD COLUMN`s → version 1, no overlay tables, no new columns), idempotent re-run, online-backup API (WAL-safe) into `state-archive/<ts>-pre-migrate-v1/`, backup failure aborts before DDL, v99 → `STATE_SCHEMA_INCOMPATIBLE` without backup. Read-only open of a v1 file: no migration, no file created, column-based reads fall back to defaults (`repository.py:373-481`), orchestrator checks `schema_version < 2` before building the overlay repository. `claim_pending_batch` (`repository.py:1586-1641`): select + guarded update + fetch in one `BEGIN IMMEDIATE`, ordered by id, backoff-eligible, tripwire cannot fire falsely, `SEARCH … USING INDEX ix_overlay_blocks_job_status` on 30 000 rows, `page_progress` covered by `ix_overlay_blocks_job_page_status` (7 ms / 1 000 pages). `replace_all` refuses with COMPLETED units. Live DB copy: `integrity_check` ok, `foreign_key_check` empty.
- **F1:** determinism (two extracts → byte-identical PNG, `source_book.md`, `figures.json` minus `generated_at`); E-23 (region starts at y0 = 116.4, no header in the PNG; `_clip_to_band`, `_blockers`, `_final_bbox`); all six decorative rules present; table precedence, caption below/above, sub-figure merge, full-page figure; DPI ladder strictly decreasing to the floor with `size_limit_exceeded`; single render failure → bare token + warning; sink failure → `FIGURE_SINK_FAILED`, no DB created (`orchestrator.py:917, 998`); page-range parsing; legend chunk isolation (C3) and the lossless property test including image-with-src and legend blocks; `legend_mismatch:N`; `--allow-partial` with a FAILED legend chunk keeps the English PNG and the marker block; `--no-figures` byte identical to v1; legend blocks excluded from discovery; in-place labels 15/15 on the live copy.
- **F3:** precedence explicit > profile > Letter/10.5 pt fallback + warning (`pdf_native.py:187-230`, `orchestrator.py:2836-2872`); `parse_pdf_margin` (`config.py:192-224`); font validation (`fonts.py:64-87`): missing file, `.txt`, fake `.ttf`, Wingdings → exit 1 before any file is written; user CSS → one `css_unsupported:…` warning, no network fetch for `url()` / `@import`; outline with Turkish titles, language set, 174 KB; atomic write + smoke re-open; weasyprint missing → exit 2 with md still produced.
- **Privacy / paths:** live logs of `bt_f1` and `bt_overlay2` contain no DeepL key (exact and `:fx` shape), no book text, no string field over 160 chars; `overlay_review.json` / `figures.json` hold book text by design and are never logged; `summary.json` clean; `last_error` holds the truncated provider message only. Output directory `çıktı [test]` works for md/epub/pdf; image references stay POSIX.

---

## Deviations from design — accepted / rejected

### `docs/durum.md` — F2 deviations and Addendum A.4

| Deviation | Decision | Reason |
|---|---|---|
| A-D1 a line may join any open group | accepted | needs vertical succession to the group's last line; the E6 label columns come out right |
| A-D2 run-in join ("Figure 1.12" + caption, "1.3" + heading) | accepted | single CAPTION/HEADING units; the band guard keeps the running header as three units |
| A-D3 JUSTIFY threshold | accepted (code), **fix the docs** | code and `durum.md`: 3 lines; Addendum says 4, §4.4 says 2 (CR-78) |
| A-D4 resume compares a hash recomputed from stored units | accepted **with condition** | safe for `translate` (input hash checked first); it is a DB self-check only → requires CR-39 (hash at export) and CR-45 |
| A-D5 overlay runs the F1 detector when `figures.json` is missing | accepted | signature equality asserted; note the extra `find_tables` cost (CR-49) and that CR-38's false figures turn prose into FIGURE_LABEL units (harmless in overlay: they are still translated in place unless `--overlay-keep-figure-text`) |
| A-D6 adapters own their repository; per-unit `Err` | accepted | but the batch-level `Err` path must be fixed (CR-43) |
| A-D7 permission check ignores the sign of the word | accepted | probed with four encryption variants; unify with the renderer's check (CR-77) |
| A-D8 `status` overlay section as a dict | accepted | needs the stale-file guard (CR-46) and the right `mode` (CR-42) |
| A-D9 apportioned `chars_billed` | accepted | sum is exact |
| Legend block always extracted; `--figure-legend` only toggles the list | accepted with changes | cost documented; the list must survive when in-place painting did not happen (CR-57); `extract` help text rejected (CR-78) |
| `OverlayRenderOptions` field order | accepted | dataclass default rule; all uses are keyword |
| One review entry per reason | accepted | sorted, `counts.entries` consistent |
| Ellipsis at the largest fitting scale | accepted | see CR-52 for the unplaceable / one-word cases |
| Renderer re-applies FR-46 | accepted | defence in depth, consistent with `job.overlay_translate_headers` |
| pdfkit A/B near-copy wrappers | **rejected as a permanent state** | consolidate (CR-77); no behaviour risk except the diverging permission checks |
| `figure_overlay.py` own `LabelReplacement` mirror | moot | no mirror exists any more (`figure_overlay.py:15` imports the domain class); remove the identity copy and the stale note |
| `pdf-overlay` not registered in `exporters/base` | accepted | design C12 says so; drop the `find_spec` stub |
| Overlay quality fix: paragraph split (indent/block rule) | accepted with change | works on the test document; list markers must split too (CR-41) |
| Overlay quality fix: justify ≥ 3 lines, `text-indent` write-back, `<sup>` markers | accepted | verified by probe |
| A.1 `pdf-overlay` default / `default_formats` | accepted | matches the table; refusals probed |
| A.2 PNG overwritten in place | **rejected as implemented** | CR-57, CR-55, CR-39 |
| A.2 legend list default off | accepted only after CR-38 and CR-57 | otherwise it hides lost text and paid translations |
| A.3 `source_profile.json` + precedence | accepted with changes | CR-65, CR-66 |

### `docs/durum.md` — F3 / DB deviations

| Deviation | Decision | Reason |
|---|---|---|
| Image size from PNG xres, not `figures.json` | accepted | keeps exporters independent of extractors; both PNG writers store 200 dpi; falls back to 96 |
| `table_overflow:N` instead of scaling | accepted | MuPDF Story cannot scale tables; warning emitted; E-56 only partly met — QA to look at a wide table |
| Code lines soft-broken by column (real `\n`) | accepted with note | prevents clipping, reports `code_lines_wrapped:N`; copy-paste of a long token changes — document |
| Footer digits in base-14 Helvetica (not embedded) | accepted | digits only; NFR-25 unaffected |
| Page-size validation in the exporter | **rejected as implemented** | validation is right, but the optional-exporter downgrade turns it into exit 2 (CR-63) |
| Runtime group = 20 columns (doc 07 says 21/23) | accepted | counting doc 07 §3.2 gives 20; fix the doc (CR-78) |
| Boolean columns typed `BOOLEAN` | accepted | same as v1 (`chunks.translatable BOOLEAN` in the fixture); CHECKs present |
| Column-based `JobRepository` reads | accepted | required by D18; verified on a read-only v1 file; any future `chunks`/`lease` column needs the same care |
| `_validate_blocks` checks `block_id` | accepted | enforces B3 (derived id) cheaply |
| Domain types in `domain/overlay.py` | accepted | keeps `database` free of `extractors` |
| Constants duplicated (`OVERLAY_KEEP_REASONS` …) | accepted with change | equal today, unpinned (CR-70) |
| `complete()` treats `unit_id` as authoritative | accepted | parity with `ChunkRepository`; add the equality guard (CR-71) |
| `test_19_get_single_job` fixed id | accepted | deterministic id, no behaviour change (`tests/test_repository.py:858-880`) |
| DB Architect notes: `block_id` derived, placement result not persisted (O5) | accepted | but the summary must then carry the placement counts (CR-51) |

### `docs/durum.md` — F1 deviations and live observations

| Deviation | Decision | Reason |
|---|---|---|
| Raster clusters accepted from 1 % | accepted **with the CR-38 background guard** | AC US-14/1 needs it; unguarded it swallows pages |
| `BandInfo.band_ratio` | accepted | |
| Single render failure → bare token + warning | accepted | labels stay excluded; placeholder visible |
| `figures.json` written with 0 figures | accepted | deterministic `status` |
| `FigureKind` in `domain/models` | accepted | |
| Caption + legend one chunk although oversize (`oversize:legend`) | accepted with caveat | a legend that alone exceeds `max_chars` is split and loses its context (CR-80) |
| `figure_missing` only on lines with `src` | accepted | |
| "exporters fence-aware" | **partly rejected** | `markdown_to_html` yes, Markdown exporter no (CR-79) |
| `IMAGE_TOKEN` removed; `markdown_to_html` returns `HtmlRender` | accepted | |
| Live: label order = pymupdf reading order | accepted | cosmetic (quantised by line height, `figures.py:757`) |
| Live: "`figure_missing`/orphan warning in orchestrator" | **rejected** | the orphan warning does not exist (CR-62) |
| Live: `--no-figures` byte identical to v1 | accepted | verified; move the pinned file to `tests/fixtures/` (CR-68) |

---

## Risk points for QA (consolidated, v1 + v1.1)

**A. Provider / live DeepL**
1. (v1, deferred CR-08/09/10) 429 without Retry-After (`limiter_changed` / `chunk_rescheduled`; `runs.provider_calls` vs completed → nothing sent twice); 456 / 401 / 403 → `PAUSED`, exit 3, resume; glossary reuse `book-translator:<hash16>`; prose with `<`, `>`, `&`, inline code, URLs in `tag_handling="xml"` mode — now also for overlay units; `--allow-non-english` still sends `source_lang="EN"`.
2. (CR-43) one live 429 during an overlay pass: watch `provider_calls`, `rate_limited_count` and the limiter cascade; expect single-text retries today.
3. (NFR-23) chars and request count of an overlay pass vs the reflow pass of the same book; labels are always billed in reflow mode (A.2).

**B. Text loss / untranslated content (highest priority)**
4. (CR-38) run with default options: a scanned+OCR PDF (full-page image under the text), a book with shaded note/sidebar boxes, pages with a tinted or framed body, a text-heavy chart, a figure with > 40 labels. Compare paragraph and character counts of `source_book.md` with `--no-figures`; grep the final md/epub for sidebar text; look for `legend_truncated`.
5. (CR-40) pages whose first/last short line falls in the 12 % band (two-column papers, one-line paragraphs, body-size headings at the page top): compare English leftovers in the overlay PDF with `overlay_review.json` (they are not listed today).
6. (CR-41, CR-47, CR-81) bulleted and numbered lists, wrapped list items, centred title pages, a sentence crossing a page (second half fragment tag).
7. (CR-53) landscape `/Rotate 90` pages and cropbox-offset pages in overlay mode.

**C. Binding to the source file**
8. (CR-39) between `translate` and `export`: replace the PDF, edit it, move it, run `export` from another cwd — for `pdf-overlay` *and* for reflow (figure PNGs). Expect a refusal or a warning; today: wrong overlay with exit 0, or English figures with a stripped legend.
9. Swap the PDF before `translate --mode overlay` (must be exit 5, 0 provider calls); flip `--overlay-translate-headers` / `--overlay-keep-figure-text` on a started pass (exit 5).

**D. Resume, crash, cancellation**
10. (v1) `taskkill /F` mid-run → `re-queued N`, no COMPLETED unit re-sent; state write failure during `translate` → exit 1, never 0.
11. (CR-44) `taskkill /F` or a second Ctrl+C while an overlay batch is completing; compare billed characters on resume (bounded by one batch).
12. (CR-45) kill right after "overlay units extracted" (or `UPDATE jobs SET overlay_status='NONE', overlay_sha256=NULL`) → today exit 5 demanding `--fresh`; check what `--fresh` archives when a completed reflow pass shares the directory.
13. (CR-42) after `run --mode overlay` and after `export --format pdf-overlay`: `status --json` `mode` / `last_run.mode`, `SELECT command, mode FROM runs`; after a kill mid-overlay `status` counters (0/0 today).
14. (CR-46) `translate --mode overlay --fresh` then `status`: stale review counts / output path.
15. (v1) Ctrl+C timing: first → grace (capped), exit 130, `INTERRUPTED`; second → release; Ctrl+C during `extract` / `glossary` / `export`; (CR-31) wait for the HTTP timeout.
16. (CR-18/49) PDF ≥ 300 pages: extraction time in both modes, `status` from a second shell after 60 s (lease stale), a second `translate` started meanwhile.

**E. Overlay output quality**
17. Visual: mixed font sizes per page (shared-scale proposal), line spacing tighter than the source (CR-76), justify, first-line indent, superscripts, table cells, footnotes, text over images (`over_image`).
18. (CR-52) very small boxes and single long words → blank or "…" boxes, exit 2; long Turkish labels inside figure PNGs (no warning today — visual check).
19. (CR-50) PDF with bookmarks that have mid-page destinations and URI bookmarks: destinations after retitling.
20. (CR-54) PDF that already contains redaction annotations: overlay PDF and figure PNG.
21. (CR-51) `summary.json` overlay counts vs `overlay_review.json`; (Q21) exit 0 with a non-empty review, exit 2 with `could_not_fit` / no-text-layer pages / `--allow-partial`.
22. Links, form fields (units over a widget are billed, then kept), highlight annotations; owner-encrypted PDF with all permissions (extractor refuses, renderer would allow — CR-77); user-password PDF (exit 1 before any provider contact).
23. NFR-21 on a real book: image/drawing counts per page and masked pixel diff; NFR-24 file size; NFR-27 time and memory of a 300-page export (no progress output today, CR-48).
24. `--pdf-font` with full and with partial Turkish coverage (`glyph_missing`); `--pdf-font bad.ttf` with `-f md -f pdf-overlay` on a directory holding both passes (CR-64: md/epub written before the refusal?).
25. Export while the target PDF is open in a viewer on Windows: clean error, old file intact.

**F. Figures (reflow)**
26. (CR-55) re-run `extract --figure-dpi 100` while chunks are translated and `source_book.md` was edited: expect exit 5; compare sha256/size/mtime of the PNGs before and after the refused run.
27. (CR-56) put your own PNG into `images/`, reference it from `source_book.md`, re-run `extract`.
28. (CR-57) after `export`: `status --json` figure byte totals vs the real files; preview `source_book.md` (English or Turkish figure?); `extract` again and re-open `translated_book.md`.
29. (CR-58) a book with a code block containing ≥ 2 consecutive blank lines and ≥ 1 figure: diff the code in `translated_book.md` against the source.
30. (CR-59) hand-edited image lines on Windows: `..\`, `C:\…`, `images/../x.png`; inspect the EPUB contents. (CR-79) `~~~` inside a ```` ``` ```` block with an image token.
31. (CR-60/61) heavy vector pages (> 5 000 paths): `extract` time per page; `export` time on a large book with many figures.
32. E-33: delete/move an image line by hand → `figure_missing:N` placeholder; orphan files are *not* reported (CR-62). Marker extractor with figures enabled → `figures_unsupported:marker`, bare tokens, no `source_profile.json`, reflow PDF warns about the missing page design.
33. NFR-17: two `extract` runs → byte-identical PNGs and inventory; `--no-figures` byte identity with v1; `--figure-exclude-pages "2,5-7"` and bad input (exit 1).

**G. Native PDF (F3)**
34. (CR-63) `--pdf-page-size Foo`, huge margins → today exit 2, no PDF, stale file kept.
35. (CR-65/66) hand-edited or corrupt `source_profile.json`; sans-serif source (comes out serif); mixed page sizes (most common wins); landscape source; explicit page size with `--pdf-margin source` and no profile.
36. Wide table (`table_overflow`), 200-character code token (copy-paste changes), image taller than the page, PNG without DPI metadata, footer position with a bottom margin < 32 pt, user `--css` with `@page` / `@import` (one warning, no network).
37. `--pdf-engine weasyprint` without weasyprint → exit 2, md + epub still produced.

**H. State, schema, safety (v1 items still valid)**
38. Migration of a real v1 directory: first write command creates `state-archive/<ts>-pre-migrate-v1/translation_state.db` (opens as v1, same row counts); `status` with the v1.1 tool on an unmigrated copy works and creates nothing; the v1.0 tool refuses a migrated file cleanly. Failure paths: read-only output directory, `state-archive` existing as a file, full disk → error names the problem, file stays v1. `UPDATE schema_meta SET schema_version=99` → exit 5 on read-only and write opens, no backup directory.
39. Hand-damaged state + `export --format pdf-overlay`: bad JSON in `overlay_blocks.line_boxes` → exit 1 `state_corrupt`, run row `FAILED`, lease released, no partial PDF; deleted unit row → exit 5 on `translate`. Second writer holding `BEGIN IMMEDIATE` during overlay export → no raw traceback (CR-67).
40. Two shells on one output dir (exit 4 with holder pid), `--force-unlock`, `--fresh` archive contents (DB with both passes, `source_book.md`, `summary.json`, `figures.json`, `source_profile.json`, `translated_book.*` incl. `.overlay.pdf`, `overlay_review.json`, `images/` moved, a *copy* of `glossary.json`); E-51 both passes in one directory, `status` shows both, default formats per Addendum A.1; `run --mode overlay --format md` without a reflow pass → exit 1 before extraction.
41. Large book (≥ 500 pages) in overlay mode: `replace_all` time, DB size (doc 07 estimate 35–115 MB with both passes), `status` latency, WAL size.
42. Glossary: edits between runs ("consistency risk: N chunks"), non-ASCII names (CR-20) in both passes, provider switch between the reflow and overlay pass (`jobs.provider` binding is shared).
43. (v1) Non-TTY output (no ESC bytes, throttled progress, `--json` on stdout only), Windows console encoding with `çğıİöşü`, paths with spaces / brackets / non-ASCII (`çıktı [test]` verified), every failure also as `command_failed` in the JSONL log.
44. Redaction/privacy: grep `logs/*.jsonl`, `summary.json`, console capture and the DB for the DeepL key (Free and Pro shape); no book text at INFO or above after a run that produces review entries; `overlay_review.json` contains text by design — confirm it is never echoed to the console or log.
45. (v1) Export edge cases: export right after `extract` → exit 2; `--allow-partial` marker blocks in EPUB; `--chapter-level 2`; headings with `<`, `&`; indented code block (CR-35); "N. Bölüm" paragraphs stay paragraphs in the EPUB (CR-34 regression).

---

## Onay Koşulu (v1.1)

- **CR-38 (Blocker)** fixed with the three fixtures (OCR sandwich, sidebar, framed page) before anything else.
- **CR-39, CR-40, CR-41 (Critical)** fixed in the same round, each with the tests named in the finding.
- Warnings to fix in the same round because they are small and touch data safety or paid work: CR-42, CR-43, CR-45, CR-51, CR-52, CR-55, CR-56, CR-57, CR-58, CR-63, CR-64, CR-67. The remaining Warnings may be ticketed (CR-44, CR-46 … CR-50, CR-53, CR-54, CR-59 … CR-62, CR-65, CR-66, CR-68), but CR-49 must be done before any book over ~250 pages is run in overlay mode.
- Doc updates (CR-78) in the same PR: Addendum A-D3, doc 06 D12 / §7.1 "superseded" markers, doc 07 column counts, `durum.md` (orphan warning, `LabelReplacement` mirror, fence-aware claim), README.
- The shared per-page scale is a product decision for the user; it is not a condition of approval.
- After the fix round a short re-review of CR-38 … CR-41 (probe scripts are kept in the scratchpad and can be re-run unchanged); QA starts after that, using the risk list above.

---
✅ Code Reviewer (v1.1) tamamlandı.
➡️  Sonraki adım: ilgili Developer (Blocker CR-38 + Critical CR-39/40/41 düzeltme turu) → yeniden inceleme → QA Engineer

---

# Third pass — Bölüm A (paralel inceleme, YR numaralandırması)

> **Not (konsolidasyon):** Bu bölüm, üçüncü turun **paralel olarak yürütülen ilk kolu**dur ve kendi `YR-*` numaralandırmasını kullanır. Aynı turun ikinci kolu ve **birleştirilmiş nihai karar** aşağıdaki **"Third pass (v1.1 fix round re-review) — konsolide"** bölümündedir; orada `YR-* ↔ CR-*` eşleme tablosu ve iki kolun birleşik bulgu listesi var. **Onay koşulu olarak aşağıdaki konsolide bölüm geçerlidir**; bu bölümdeki "Karar"/"Onay koşulları" kısmı konsolide bölüm tarafından kapsanmıştır. Bu bölümün kendine özgü katkıları: çalıştırılmış test/lint kanıtı, YR-1 için etiket sayısı taraması (20 → 30 eşiği), YR-3'ün `prefix_style` teşhisi, ve YR-9/YR-11/YR-12.

**İnceleme tarihi:** 2026-09-21
**Kapsam:** dar — CR-38..41 + CR-52 + ortak sayfa ölçeği doğrulaması; 27 Warning'de örnekleme; `docs/durum.md` "Bilinen açıklar" listesinin doğruluğu. Kod değiştirilmedi.

## Özet

| Severity | Adet |
|---|---|
| 🛑 Blocker | 0 |
| ⚠️ Critical | 1 (YR-1) |
| 💡 Warning | 6 (YR-2 … YR-7) |
| 📘 Suggestion | 5 (YR-8 … YR-12) |

**Genel Durum: APPROVED WITH CHANGES**

---

## 0. Doğrulama komutları (çalıştırıldı)

| Komut | Sonuç | Beklenen |
|---|---|---|
| `.venv/Scripts/python.exe -m pytest -q` | **700 passed, 1 skipped, 149.77 s** | ✅ birebir |
| `.venv/Scripts/python.exe -m mypy src` | **Success: no issues found in 60 source files** | ✅ birebir |
| `.venv/Scripts/python.exe -m ruff check src tests` | **All checks passed!** | ✅ birebir |

Sapma yok. Canlı çıktı (`docs/test_doc_output/v1_1_final/`) incelendi; yeniden çeviri çalıştırılmadı, DeepL çağrılmadı.

---

## A. Blocker / Critical doğrulaması

### CR-38 (Blocker) — **KAPANDI**

Üç düzeltmenin üçü de kodda:

* **(a) arka plan rasteri** — `src/extractors/figures.py:671-689` `_is_background_image`: sayfanın ≥ %60'ını kaplayan raster + üstünde ≥ 3 metin satırı → aday değil (`_BACKGROUND_IMAGE_AREA_RATIO = 0.60`, `figures.py:47-48`). Metinsiz tam sayfa plaka hâlâ şekil (`tests/test_figures.py:560`).
* **(b) yalnız etiket-benzeri metin emilir** — `_absorb_inner` (`figures.py:719-741`) artık her kaynak bloğunu `_is_label_text` ile sınıyor; prose blok akışta kalıyor. Ayrıca küme seviyesinde `_is_prose_cluster` (`figures.py:743-780`) gölgeli not kutusu / çerçeveli sayfayı `figure_candidate_rejected:prose` ile reddediyor (`figures.py:554-573`).
* **(c) sayfa güvenlik ağı %40** — `figures.py:610-633`, `_PAGE_ABSORB_MAX_RATIO = 0.40`, `_PAGE_ABSORB_MIN_CHARS = 400`; not `figure_page_dropped:<page>:text_heavy` olarak `ctx.warnings`'e çıkıyor (`src/extractors/pymupdf_extractor.py:467-473`).
* **(d) fixture'lar** — `tests/test_extractor_cleanup.py:1123` (OCR sandwich), `:1141` (gölgeli sidebar), `:1159` (çerçeveli sayfa) + gerçek PDF üzerinden `--no-figures` ile paragraf-paragraf karşılaştırma (`:1181-1197`), ve `:1200` test_doc sayfa 2'nin hâlâ 1 vektör şekil / 15 etiket olduğunu sabitliyor. Birim testleri `tests/test_figures.py:547-625`.

Metin kaybı senaryosu kapandı. **Ama yeni bir yanlış-negatif sınıfı doğdu → YR-1.**

### CR-39 (Critical) — **KAPANDI**

* `_verify_source` (`src/pipeline/orchestrator.py:2968-3025`) export yolunda kaynak PDF'i **yazmadan önce** hash'liyor; `_export_bound:3136-3145` `wants_overlay or figure_step or input_pdf is not None` koşuluyla çağırıyor.
* Hash uyuşmazlığı → `STATE_HASH_MISMATCH` (exit 5) — **overlay ve reflow ayrımı olmadan**, yani "başka bir PDF koyma" senaryosu her iki yolda da kesin durduruluyor (`:3011-3021`).
* Mutlak yol: `orchestrator.py:886-892` `JobSpec` artık çözümlenmiş yolu saklıyor; eski (göreli) kayıtlar `_remember_input_path` ile tazeleniyor (`:944`, `:3023-3025`).
* `export --input` var ve yardımda tam açıklanmış; `tests/test_cli.py:845-868`.

### CR-40 (Critical) — **KAPANDI**

`src/extractors/overlay_extractor.py:405-420` (`_classify`): band satırı artık ancak `running` (tekrar eden desen) **veya** `row_anchor and _is_short_band_unit` ise HEADER/FOOTER. `_row_anchors` (`:468-481`) birimin sayfa numarası / running-pattern birimiyle aynı metin satırında olup olmadığına bakıyor — yani "1.3 Book overview 19" hâlâ üst bilgi, tek seferlik "Key idea" gövde. Sınırda kalanlar `HEADER_GUESS_WARNING` ile işaretleniyor (`:97, 663-664`) ve `extraction.warnings`'e çıkıyor (`tests/test_overlay_extractor.py:555-556`). İki sütunlu dip satırları uyarı almıyor (`:572`).

### CR-41 (Critical) — **KAPANDI**

`src/extractors/cleanup.py:746-752`: `parse_list_marker(line.text) is not None and (prev_closed or in_item)` → `starts_new = True`; asılı (wrapped) satırlar `hanging` kuralıyla maddede kalıyor (`:754-757`).

Canlı probe (sentetik PDF, `extract_overlay`): giriş cümlesi + 3 bullet + 2 numaralı madde → **6 ayrı birim**, sarılan satırlar kendi maddesinde. v1.1 öncesi bu 1 birimdi.

### CR-52 — **KAPANDI**

`src/exporters/pdf_overlay_exporter.py:389-423`: ölç → grupla → **sonra** redakte et; `plan.placeable is False` olan birim redakte edilmiyor, `kept_original` + `unplaceable` notuyla kaynak metni kalıyor, `could_not_fit` sayılmaya devam ediyor. Tek uzun kelime karakter bazında kesiliyor (`_placement.ellipsis_text:322-365`). Figür yolunda `render_translated_figure_with_stats` kullanılıyor ve `figure_label_truncated:<id>:<n>` uyarısı veriliyor (`orchestrator.py:3481-3488`). Testler: `tests/test_overlay_export.py:1307, 1333, 1351`.

### Ortak sayfa ölçeği (SHARED_SPREAD 0.10→0.15, varsayılan açık) — **TASARIMA UYGUN, ama parametreler belgesiz**

* Tasarım kontratı güncellenmiş: `docs/06_solution_design_v1_1.md:810` `--overlay-uniform-scale / --no-overlay-uniform-scale`, **varsayılan on**, "fix round" kaynaklı olarak tabloda; `:52` D12 satırı "Superseded by Addendum A" ile işaretli. README `:33`'te de anlatılmış. Addendum A'daki "A5/12pt varsayılanı yok / sayfa özellikleri kaynaktan türetilir" ilkesiyle çelişmiyor — ölçek kaynağın kendi punto ve satır yapısından türetiliyor, sabit bir punto dayatılmıyor.
* Uygulama incelemenin önerdiği algoritmayla aynı: `_placement.plan_placements:370-415` + `_share_scales:418-461`; grup anahtarı `(kind, round(font_size,1), family)` (`pdf_overlay_exporter.py:394-397`); aykırı birimler kendi ölçeğinde kalıyor; `shared_scale` review kaydına giriyor. Testler `tests/test_overlay_export.py:1207-1360` önerilen test listesiyle birebir.
* **Kanıt (canlı):** `overlay_review.json` → sayfa 1 gövdesi `shared_scale 0.9051`, sayfa 3 gövdesi `0.8679`, `placed 34 / shrunk 0 / could_not_fit 0`.
* Eksik: 0.15 değeri, grup anahtarı ve `LINE_HEIGHT_TIGHT = 1.05` hiçbir tasarım dokümanında yok (yalnız `durum.md:34`'te "0.10→0.15" notu). → YR-7, YR-8.

---

## B. Yeni bulgular (düzeltme turunun getirdiği risk)

### ⚠️ Critical

#### [YR-1] CR-38(c) sayfa güvenlik ağı, meşru yoğun-etiketli şekilleri de düşürüyor — `src/extractors/figures.py:53-55, 610-633`

**Sorun.** Kural, kümelerin emdiği karakterler `>= 400` **ve** sayfa karakterlerinin `> %40`'ı olduğunda o sayfanın **tüm** şekillerini düşürüyor. Tam sayfa bir diyagram sayfasında (sayfadaki tek metin diyagram etiketleri + alt yazı) bu oran doğal olarak ~1.0'dır. 20 kadar kısa etiket 400 karakter eşiğini geçmeye yetiyor.

**Kanıt (inceleyicinin probe'u, `tests/test_figures.py` yardımcılarıyla, diyagram + alt yazı, gövde metni yok):**

```
labels=  6 figures=1 notes=[]
labels= 12 figures=1 notes=[]
labels= 20 figures=1 notes=[]
labels= 30 figures=0 notes=['figure_page_dropped:1:text_heavy']   (540 of 594 characters)
labels= 40 figures=0 notes=['figure_page_dropped:1:text_heavy']   (720 of 774 characters)
```

**Neden önemli.** Tasarım kontratını doğrudan çiğniyor: `docs/06_solution_design_v1_1.md:635` — "**Full-page figure** (E-27, C8): `_process_page` returns `skipped=False` when the page has a figure even with no prose". Yani "gövde metni olmayan tam sayfa şekil" açıkça desteklenen bir durum; güvenlik ağı tam da o durumu vuruyor. Sonuç: sayfa görseli hiç çıkarılmıyor, etiketler reflow markdown'ına düz metin olarak dökülüyor, EPUB/PDF'te şekil yok, **exit 0**. Hedef kitle teknik kitaplar olduğu için bu sınıf girdi nadir değil.

Hafifletici: kayıp sessiz değil — `figure_page_dropped:<page>:text_heavy` uyarı listesine düşüyor, ve **metin kaybolmuyor** (akışa geri veriliyor). Bu yüzden Blocker değil Critical.

**Beklenen düzeltme.** Güvenlik ağını daraltmak, örn. şunlardan biri:
* yalnız **etiket-benzeri olmayan** (prose) emilen karakterleri say — zaten (b) kuralı prose'u ayırt ediyor, sayaç ondan sonra kurulabilir; veya
* küme birleşimi sayfanın büyük bir bölümünü (ör. ≥ %35) kaplıyorsa kuralı uygulama (gerçek tam sayfa şekil muafiyeti); veya
* sayfayı düşürmek yerine yalnız **prose blokları** akışa geri verip şekli koru.

Fixture: alt yazı + 30 kısa etiket + gövde metni olmayan tam sayfa diyagram → 1 şekil, not yok. (Mevcut `tests/test_figures.py:610` yalnız kuralın **ateşlenmesini** sabitliyor, ateşlenmemesi gereken durumu değil; `tests/conftest.py:536` `full_page_figure` fixture'ı sadece alt yazı içerdiği için eşiğin altında kalıyor.)

Not: uygulama, incelemedeki (c) önerisini **harfiyen** uyguladı; hatalı olan önerinin kendisiydi. Bu bir developer hatası değil, eşiğin fazla geniş tutulmuş olması.

---

### 💡 Warning

#### [YR-2] CR-61 davranışsal olarak kapanmadı; ortaya ölü kod ve yanlış docstring çıktı — `src/pipeline/orchestrator.py:3453-3468`, `src/exporters/figure_overlay.py:11, 163-193, 212`

Batch API (`render_translated_figures`) yazıldı ve testlendi, ama **üretim yolu kullanmıyor**: `_translate_figures` hâlâ `for record in records:` içinde `render_translated_figure_with_stats(...)` çağırıyor; o da tek elemanlı batch'e düşüp her figür için `source_pdf.read_bytes()` + `open_document_bytes` + `font_archive` yapıyor. 200 figürlü 50 MB'lık bir kitapta export sırasında ~10 GB gereksiz I/O. `grep` ile doğrulandı: `render_translated_figures` yalnız kendi sarmalayıcısından ve testlerden çağrılıyor.

Ayrıca `figure_overlay.py:11` docstring'i "reads and opens the source once for a whole batch" diyor — üretim yolu için **yanlış**.

**Düzeltme:** `_translate_figures` içinde `FigureJob` listesi kurup tek `render_translated_figures` çağrısı yap; `uniform_scale`'i de oradan geçir (bkz. YR-5).

#### [YR-3] `durum.md` açık (1) yanlış teşhis edilmiş; gerçek kusur `prefix_style`'ın `figures.json` sınırında düşmesi — `src/extractors/figure_inventory.py:53-77`, `src/domain/overlay.py:129-145`, `src/exporters/figure_overlay.py:74-84`

`durum.md:38` "(1) `.tr.png` içinde etiketler kutuda sola yaslı ve '2.' kalın öneki yok (overlay PDF'te ortalı+kalın) → `label_boxes` hizalama/prefix stili figür yolunda uygulanmıyor" diyor. İki iddianın biri yanlış, biri doğru ama dar:

* **Hizalama iddiası yanlış.** İki yol da aynı `detect_alignment` + aynı `_smallest_drawing_around` yardımcısını kullanıyor ve **aynı** sonucu üretiyor. Kanıt (ikisi de çalıştırıldı): overlay yolu `'2. Image formation' → center`, `'4. Model fitting and optimization' → left`; `figures.json` `label_boxes` aynısını kaydediyor. `build_replacements` (`src/pipeline/figure_labels.py:204`) hizalamayı taşıyor, `build_html` (`_placement.py:213`) `text-align` olarak uyguluyor. Canlı `.tr.png` görsel olarak da incelendi: kutu içi hizalama kaynakla uyumlu, çıktı temiz.
* **Kalın önek iddiası doğru ama dar.** `OverlayStyle.prefix_style` var, hesaplanıyor (`cleanup.py:567-580`), overlay DB'sinde saklanıyor (`repository.py:1266, 1444`) ve overlay PDF'inde uygulanıyor (`pdf_overlay_exporter.py:205` → `_placement.py:194-199`). Kanıt: overlay yolu `'2. Image formation'` için `prefix_style = ('2.', True, False)` üretiyor. Ama `LabelBoxEntry`/`LabelBox` ve `LabelReplacement` **bu alanı hiç taşımıyor**, `figure_overlay._spec` de `PlacementSpec`'e geçirmiyor → figür PNG'sinde E-46 / Q22 gereği kaybediliyor.

**Etki:** kozmetik değil, belgelenmiş bir gereksinimin (E-46/Q22) tek yolda uygulanmaması. Düzeltme küçük: `LabelBox` + `LabelBoxEntry` + `LabelReplacement`'a `prefix_style` ekle, `_spec`'te geçir. `durum.md` maddesi de düzeltilmeli.

#### [YR-4] CR-48 kısmi kaldı, ama `durum.md:34` onu koşulsuz "yapıldı" listesine koyuyor — `src/pipeline/orchestrator.py:3130, 3336` vs `docs/06_solution_design_v1_1.md:754`

Yapılanlar: birim başına `chunk_context` (`orchestrator.py:2594`), `overlay_block_placed/review/residual` yayınları (`:3617-3641`), progress callback (`:3566-3581`), çift `overlay_extracted` logu giderildi (`:2100-2106`).

Yapılmayanlar: **event adı hizalaması hiç yapılmamış** — kod `pdf_engine` / `pdf_font_validated` yayıyor, doc 06 `:754` hâlâ `pdf_engine_selected` / `font_validated` diyor; incelemenin "adları hizala **ya da** dokümanı güncelle" şıkkının **ikisi de** atlanmış. `src/exporters/*` altında hâlâ tek bir `getLogger` yok; gözlemlenebilirlik orkestratöre taşınarak dolaylı sağlanıyor. `tests/test_orchestrator.py:2651` event adı sapmasını kontrol etmiyor.

CR-48 zaten ticket'lanabilir Warning'di; sorun kapanmaması değil, **durum kaydının yanlış** olması.

#### [YR-5] Ortak ölçek varsayılan açık ama görünürlüğü yok; `--no-overlay-uniform-scale` figür yoluna ulaşmıyor — `src/exporters/pdf_overlay_exporter.py:160-190`, `src/exporters/figure_overlay.py:212`

* Bir stil grubu, grubun **en dar sığan** birimine göre küçültülüyor. Ölçek `review_threshold` (0.65) üzerinde kaldığı sürece ne bir `overlay_review.json` girdisi ne de bir log satırı oluşuyor — `_entry(..., shared_scale=...)` yalnız başka bir inceleme sebebi zaten varsa yazılıyor. Canlı örnekte sayfa 1 gövdesi 10.6 pt → 9.5 pt'ye indi; kullanıcı bunu yalnız PDF'e bakarak fark edebilir. En kötü durumda sayfanın tüm gövdesi sessizce %65'e iner.
* `render_translated_figure_with_stats` (`figure_overlay.py:212`) batch'i `uniform_scale` parametresi geçirmeden çağırıyor, yani hep varsayılan (`True`). Kullanıcı `--no-overlay-uniform-scale` dese bile figür etiketleri ortak ölçekte kalır.

**Düzeltme:** sayfa başına bir INFO log (`grup, shared_scale, birim sayısı`) ve/veya `summary.json`'a ortak ölçek özeti; `uniform_scale`'i figür yoluna da bağla.

#### [YR-6] `docs/durum.md`'de bayatlamış / yanlış satırlar

* `:38` madde (1) — YR-3'e göre yanlış teşhis.
* `:34` — CR-48 koşulsuz "yapıldı" listesinde (YR-4).
* `:41` (F2 sapmaları) — "`figure_overlay.py` kendi `LabelReplacement` aynası" ve "`pdfkit/api.py`'de A/B yakın-kopya sarmalayıcılar" satırları artık **yanlış**; her ikisi de bu turda temizlendi (`figure_overlay.py:21` domain sınıfını import ediyor; `api.py`'deki `doc_is_encrypted`/`doc_permissions`/`page_rect` vb. kopyalar kaldırıldı). Aynı satırdaki "`pdf-overlay` exporters/base'de kayıtlı değil" hâlâ doğru.
* `docs/04_code_review.md` düzeltme turu için güncellenmemişti (bu bölüm o boşluğu dolduruyor).

#### [YR-7] CR-38'in yeni kuralları hiçbir tasarım dokümanında yok

`docs/06_solution_design_v1_1.md` §4.1 adım listesi (`:630-635`) arka plan rasteri, prose-küme reddi ve sayfa güvenlik ağından söz etmiyor. `figure_page_dropped:<page>:text_heavy` uyarısı README, doc 06 ve durum.md'nin **hiçbirinde** geçmiyor — kullanıcı bu uyarıyı gördüğünde ne anlama geldiğini bulamaz. Aynı şekilde `SHARED_SPREAD = 0.15`, grup anahtarı ve `LINE_HEIGHT_TIGHT = 1.05` de belgesiz. CR-78 doküman turu bu parçaları kapsamamış.

---

### 📘 Suggestion

* **[YR-8]** `LINE_HEIGHT_TIGHT = 1.05` (`src/exporters/_placement.py:43-44`) puntoyu küçültmeden önce satır aralığını **kaynak aralığın altına** sıkıştırıyor; incelemenin önerisi tersiydi ("kaynak pitch'ten 1.15'e gevşet"). CR-76 (satır yüksekliği sabit) hâlâ açık, şimdi iki sabitle. Addendum A'nın "sayfa özellikleri kaynaktan türetilir" ilkesiyle gerilimde.
* **[YR-9]** `tests/test_orchestrator.py:2462` koşullu assert (`if "uniform_scale" in dc.fields(...)`) — alan kaldırılırsa test sessizce vakumlaşır. CR-47'de aynı sınıf vakum assert temizlenmişti; bu yenisi aynı desene düşüyor.
* **[YR-10]** `_placement.place` (`:460-487`) `font_factor == 1.0` iken gerçek sayfada sığmazsa kutu redakte edilmiş olarak boş kalır. Risk dar — `tests/test_overlay_export.py:1351` scratch/gerçek sayfa ölçüm eşitliğini sabitliyor — ama `factor == 1.0` için ikinci deneme yok.
* **[YR-11]** `src/pdfkit/api.py:92-100` `doc_set_toc_title`, `get_outline_xrefs()` sırasının `doc_toc()` sırasıyla birebir örtüştüğünü varsayıyor; bozuk/iç içe outline'da index kayması **sessizce yanlış başlık** yazar. `pdf_overlay_exporter.py:541` docstring'i hâlâ "set_toc_item" diyor.
* **[YR-12]** `_promote_staged_images` (`orchestrator.py:1120`) `source_book.md`'nin atomik yazımından (`:1124`) önce çalışıyor; son I/O hatasında `images/` yeni, `source_book.md` eski kalır. Tüm ret kontrolleri promote'tan önce olduğu için CR-55'in garantisi bozulmuyor, ama tam atomik değil.

---

## C. `docs/durum.md` "Bilinen açıklar" listesinin doğruluğu

| # | İddia | Doğrulama |
|---|---|---|
| (1) | `.tr.png` hizalama + kalın önek, `label_boxes` stili figür yolunda uygulanmıyor | ⚠️ **Yanlış teşhis** — YR-3. Hizalama iki yolda da doğru; yalnız `prefix_style` düşüyor. "Kozmetik" değil, E-46/Q22 gereksiniminin tek yolda uygulanmaması. |
| (2) | CR-39 sapması: yalnız reflow + kaynak PDF yok → exit 0 + uyarı | ✅ Doğru, ve **anlatıldığından daha güvenli**. `_verify_source` (`orchestrator.py:2996-3025`) hash uyuşmazlığında her iki yolda da exit 5 veriyor; yalnız **dosya yoksa** reflow tarafı hoşgörülü. O durumda `painted` boş kaldığı için `strip_legends` hiçbir lejantı silmiyor (`figure_labels.py:120-145`) ve `figure_labels_not_painted:<id>` uyarısı düşüyor — yani **ödenmiş çeviri hiçbir yerde kaybolmuyor**, Türkçe etiketler lejant listesi olarak belgede duruyor. Tutarsızlık kabul edilebilir; `export --input FILE` çıkış yolu da yardımda açıkça anlatılmış. |
| (3) | CR-66(b) kenar boşluğu ölçümü üst bilgiyi de sayıyor | ✅ Doğru, hâlâ açık — `src/extractors/cleanup.py:877-882` kenar boşluklarını sayfadaki **tüm** metin satırlarının zarfından ölçüyor; `BandInfo.header_bottom/footer_top` mevcut ama kullanılmıyor. Bu maddeye ait test yok. |
| (4) | CR-44 `complete_many` yok | ✅ Doğru — `repository.py`'de `complete_many` yok. Yerine `_shielded` (`orchestrator.py:2564-2577`) eklenmiş: ödenmiş batch'in kayıt işi iptal edilse bile tamamlanıyor. Pencere daraldı, birim başına commit maliyeti duruyor. |
| (5) | CR-49 `find_tables` tekrarı sürüyor (lease kalp atışı çözüldü) | ✅ Doğru. |
| (6) | Ertelenenler: CR-08/09/10, CR-20, CR-69..76, CR-79..82 | ✅ Hiçbiri bu turda ciddileşmedi. İki not: **CR-76** artık iki sabitle (YR-8); **CR-22** maruziyeti `_shielded` ile daraldı (kötüleşmedi). |

**Listede eksik olanlar** (bu tura ait, "bilinen açık" olarak kaydedilmeli): YR-1, YR-2, YR-3, YR-4, YR-7.

**Listede saklanan daha ciddi bir şey bulunmadı.** Zorunlu listedeki 16 bulgunun (CR-38..41, 42, 43, 45, 51, 52, 55, 56, 57, 58, 63, 64, 67) tamamı gerçekten kapanmış ve her biri anlamlı — vakum olmayan — testle sabitlenmiş. Ticket'lanabilir olanlardan CR-46, 47, 50, 53, 54, 59, 60, 62, 65, 66(a/c), 77 (ana maddeleri), 78 de beklenenin ötesinde bu turda kapatılmış. Düzeltme turunda **regresyon bulunmadı** (YR-1 hariç, o da düzeltmenin kendi kapsam genişliğinden).

---

## Karar (Bölüm A — konsolide bölüm tarafından kapsanmıştır)

### APPROVED WITH CHANGES

> Aşağıdaki onay koşulları konsolide bölümün "Onay Koşulu (third pass)" listesine dâhil edildi; **bağlayıcı olan o listedir.**

**Onay koşulları:**

1. **YR-1 (Critical)** aynı PR'da düzeltilir; QA bu düzeltmeyi kapsayacak şekilde başlar. Fixture: gövde metinsiz, 30 kısa etiketli tam sayfa diyagram → 1 şekil, `figure_page_dropped` notu yok.
2. **YR-3, YR-4, YR-6, YR-7** aynı PR'da (üçü doküman/durum kaydı düzeltmesi, YR-3 küçük bir alan eklemesi).
3. **YR-2, YR-5** ticket'lanabilir; YR-2 100'den fazla figürlü bir kitap export edilmeden önce yapılmalı.
4. **YR-8 … YR-12** isteğe bağlı.

## QA'ya devredilecek risk noktaları

1. **Yoğun etiketli / tam sayfa diyagram sayfaları** (YR-1): şekil düşüyor mu, `figure_page_dropped:...:text_heavy` uyarısı çıkıyor mu, düzeltmeden sonra düşmüyor mu.
2. **Taranmış + OCR'lı PDF** (CR-38a): 3'ten az OCR satırı olan sayfa (bölüm başlığı sayfası) — raster hâlâ aday olur, metin şekle emilebilir; regresyon testi yok.
3. **Ortak ölçeğin görünürlüğü** (YR-5): yoğun bir sayfada tüm gövdenin %65'e inip inmediği, hiçbir inceleme girdisi/log olmadan; `--no-overlay-uniform-scale` ile karşılaştırma. Figür etiketleri bayrağa uymuyor.
4. **`export` bağlama matrisi** (CR-39): (a) kaynak taşınmış + `--input` doğru dosya → başarı; (b) `--input` başka dosya → exit 5; (c) dosya yok + yalnız md/epub → exit 0 + `figure_translate_failed:input_missing` + lejant listesi belgede duruyor; (d) dosya yok + `pdf-overlay` → exit 1; (e) başka çalışma dizininden `export`.
5. **Band kenarı** (CR-40): iki sütunlu kitapta sütun dibi tek satırlar, tek seferlik kısa band satırları; `header_guess` uyarı sayısı makul mü (uzun kitapta uyarı seli).
6. **Listeler** (CR-41): iç içe / harfli (`a)`) maddeler, sarılan maddeler, rakamla başlayan asılı devam satırı (yanlış bölünme riski) — overlay PDF'te madde yapısı korunuyor mu.
7. **Döndürülmüş sayfalar** (CR-53): 90/180/270 `/Rotate` ile gerçek bir kitap sayfası.
8. **Figür yolu performansı** (YR-2): 50+ figürlü bir belgede `export` süresi ve bellek.
9. **`.tr.png` kalın önek** (YR-3): "2. Görüntü oluşumu" gibi numaralı etiketlerde önek kalınlığı overlay PDF ile aynı mı.
10. **v1 regresyonu**: `--no-figures` çıktısının v1 ile bayt-aynı kaldığı ve reflow-only akışın (md/epub/pdf) bozulmadığı.

---
✅ Code Reviewer (Third pass — Bölüm A) tamamlandı.
➡️  Devamı: aşağıdaki konsolide bölüm.

---

# Third pass (v1.1 fix round re-review) — konsolide

**Modül:** book-translator v1.1 düzeltme turu (P kolu: extractors/exporters, Q kolu: orchestrator/CLI/docs)
**İnceleme Tarihi:** 2026-09-21
**Reviewer:** code-reviewer (Claude)
**Kapsam:** dar yeniden inceleme — (A) kapandığı iddia edilen bulguların doğrulaması, (B) `durum.md`'deki 6 "bilinen açık"ın değerlendirmesi, (C) turun dokunduğu 16 dosyada regresyon taraması, (D) `docs/test_doc_output/v1_1_final/` canlı çıktısının iddialarla karşılaştırılması. Tam baştan inceleme değildir; testler bu turda çalıştırılmadı (statik okuma + canlı artefakt ölçümü).
**Yöntem:** dört paralel dilim (yerleştirme/exporter, extractor, orchestrator, CLI/pdfkit/repo/doc) her dosyayı baştan sona okudu; her Blocker/Critical ve yüksek Warning iddiası reviewer tarafından ayrıca kodda doğrulandı, canlı PDF/PNG/JSON çıktıları PyMuPDF ile ölçüldü. Bu bölüm ayrıca yukarıdaki **Bölüm A**'nın (paralel kol, `YR-*`) bulgularını içine alır.

**Test/lint kanıtı (Bölüm A tarafından çalıştırıldı, bu kolda tekrarlanmadı):** `pytest -q` → **700 passed / 1 skipped** (149,77 s); `mypy src` → **60 dosya temiz**; `ruff check src tests` → **temiz**. `durum.md`'nin sayıları birebir tutuyor.

## Verdict: **APPROVED WITH CHANGES**

| Severity | Count |
|---|---|
| Blocker | 0 |
| Critical | 1 (CR-83 = YR-1) |
| Warning | 15 (CR-84 … CR-98) |
| Suggestion | 15 (CR-99 … CR-113) |

Önceki turun **Blocker CR-38 ve üç Critical'ı (CR-39/40/41) gerçekten kapandı**. Tek yeni Critical, CR-38 düzeltmesinin kendi yan etkisidir ve iki kol tarafından **bağımsız olarak** bulundu.

### YR ↔ CR eşlemesi (Bölüm A ile konsolidasyon)

| Bölüm A | Bu bölüm | Not |
|---|---|---|
| YR-1 (Critical) | **CR-83** | Aynı bulgu, iki bağımsız kanıt: Bölüm A'nın etiket taraması (20 etiket → şekil durur, 30 etiket → düşer) ve bu kolun canlı ölçümü (test dokümanı s.2: oran 0,373 / sınır 0,40; 335 karakter / taban 400). Bölüm A ayrıca tasarım kontratı ihlalini belgeliyor: doc 06 `:635` "full-page figure … even with no prose" açıkça destekleniyor. |
| YR-2 | **CR-87** | Aynı. Bölüm A ek olarak `figure_overlay.py:11` docstring'inin de yanlış olduğunu tespit ediyor. |
| YR-3 | **CR-97** | Aynı sonuç, **Bölüm A'nın teşhisi daha isabetli**: kusur `detect_style`'ın stil birleştirmesi değil, `prefix_style` alanının `figures.json` sınırında düşmesi. CR-97 aşağıda bu teşhise göre düzeltildi. |
| YR-4 | CR-48 (PARTIAL) + **CR-103** | Bölüm A haklı: asıl sorun kapanmaması değil, `durum.md`'nin CR-48'i koşulsuz "yapıldı" listesine koyması. |
| YR-5 | **CR-89** + **CR-95** | Aynı iki yarı: figür yoluna ulaşmayan bayrak, ve ortak ölçeğin görünürsüzlüğü. |
| YR-6 | **CR-103** + Onay koşulu 7 | `durum.md:41`'deki "LabelReplacement aynası" ve "pdfkit A/B kopyaları" satırları artık yanlış — temizlendiler; `durum.md` düzeltilmeli. |
| YR-7 | **CR-103** (genişletildi) | CR-38'in yeni kuralları (arka plan rasteri, prose-küme reddi, sayfa güvenlik ağı, `figure_page_dropped` uyarısı) ve `SHARED_SPREAD` / grup anahtarı / `LINE_HEIGHT_TIGHT` hiçbir tasarım dokümanında yok. |
| YR-8 | **CR-96** | Aynı. |
| YR-9 | **CR-111** | Bu kolda ayrıca bulunmuştu (CR-105'in test tarafı); ayrı numara verildi. |
| YR-10 | **CR-90** | Aynı. |
| YR-11 | **CR-112** | Yalnız Bölüm A buldu. |
| YR-12 | **CR-113** | Yalnız Bölüm A buldu. |

**Yalnız bu kolun bulduğu, Bölüm A'da olmayanlar:** CR-84, CR-85, CR-86 (CR-38 ailesinin diğer iki freni + overlay yolunda kaybolan uyarı), CR-91 (CR-63 düzeltmesinin yazılmış çıktıları düşürmesi), CR-92 (değişmiş PDF'in reflow export'unu kilitlemesi — Bölüm A CR-39'u koşulsuz "KAPANDI" saymıştı), CR-93 (çift kalp atışı), CR-94, CR-98, CR-99 … CR-110.

## Türkçe Özet (kullanıcı için)

Düzeltme turu büyük ölçüde gerçek: iddia edilen bulguların çoğu kodda tam kapanmış, bir kısmı kısmen, ikisi hiç yapılmamış. Metin kaybı (CR-38), `export`'un yanlış PDF'e yazması (CR-39), üst bilgi sanılan gövde satırları (CR-40) ve listelerin tek paragrafa çökmesi (CR-41) artık düzelmiş — hepsini kodda satır satır doğruladım ve canlı çıktı da iddiaları tutuyor (3 sayfa overlay, 1. sayfa gövdesi tek punto 9,5 pt, sığmayan/küçülen kutu yok, İngilizce PNG duruyor, Türkçe olan ayrı `.tr.png` dosyasında).

Ama CR-38'i düzeltmek için konan üç yeni güvenlik freni fazla sıkı ayarlanmış. En ciddisi şu: "bir sayfanın karakterlerinin %40'ından fazlası şekle giderse o sayfadaki şekilleri tamamen at" kuralı. Projenin **kendi test dokümanının 2. sayfası bu sınırın hemen dibinde**: oran 0,373 (sınır 0,40) ve 335 karakter (sınır 400). Yani etiketi biraz daha çok olan sıradan bir "tam sayfa şema" sayfasında şekil sessizce kaybolur. Bu kural, "şekil sayfanın yazısını yuttu" durumu ile "sayfa zaten bir şekil" durumunu ayırt edemiyor. QA'ya geçmeden bunun düzeltilmesi gerekiyor.

İkinci sırada iki tane daha var: bir formatın hatası yüzünden **zaten diske yazılmış md/epub dosyaları rapordan düşüyor** (kullanıcı dosyaların var olduğunu göremiyor), ve kaynak PDF değiştiyse sadece md isteyen bir `export` bile komple duruyor (çıkışı yok).

Küçük bir düzeltme: `durum.md`'deki "`.tr.png` içinde etiketler sola yaslı" tespiti **yanlış** — ölçtüm, ortalı etiketler gerçekten ortalı geliyor. Doğru olan kısmı şu: kalın "2." öneki kayboluyor, çünkü şekil envanteri her etiketi tek bir stile indiriyor.

## D. Canlı çıktı doğrulaması (`docs/test_doc_output/v1_1_final/`)

| `durum.md` iddiası | Sonuç | Kanıt |
|---|---|---|
| overlay 3 → 3 sayfa | ✅ doğru | `translated_book.overlay.pdf` `page_count == 3` |
| 86 KB | ✅ doğru | 85 651 bayt |
| 1. sayfa gövdesi tek punto 9,5 pt (kaynak 10,6) | ✅ doğru | sayfa 1 span dökümü: 9.5 pt → 3 643 karakter; diğerleri 8.5 (dipnot), 10.62 (çevrilmeyen üst bilgi). Sayfa 3 de tek punto (9.11). `source_profile.json` `body_font_size_pt = 10.5` |
| 0 shrunk / 0 could_not_fit | ✅ doğru | `overlay_review.json` `counts`: `placed 34, shrunk_below_threshold 0, could_not_fit 0, kept_original 9, entries 3` (üçü de `fragment`) |
| `shared_scale` inceleme dosyasında | ✅ doğru | entry anahtarları: `block_id, note, page, reason, scale, shared_scale, source_text, translated_text, unit_id` |
| reflow PDF 4 sayfa, kaynak sayfa boyutunda | ✅ doğru | `translated_book.pdf` 4 sayfa, `Rect(0,0,595.28,790.87)` = `source_profile.json` ile aynı |
| `images/p002-f01.tr.png` Türkçe, İngilizce orijinal duruyor | ✅ doğru | iki dosya da mevcut (131 374 / 110 152 bayt); `source_book.md:27` → `.png`, `translated_book.md:27` → `.tr.png` (CR-57 kanıtı) |
| — | ⚠️ eksik | `v1_1_final/` içinde **`summary.json` yok**, dolayısıyla CR-51'in (yerleştirme sayılarının summary'ye yazılması) canlı kanıtı yok; yalnız kodda doğrulandı. QA bunu canlı çalıştırmada görmeli. |

## A. Status of the claimed fixes

Legend: **CLOSED** = kodda tam; **PARTIAL** = kısmen; **NOT DONE** = yok.

### P kolu (extractors / exporters)

| Bulgu | İddia | Doğrulama | Kanıt |
|---|---|---|---|
| CR-38 (Blocker) | arka plan raster + etiket-benzerlik + %40 sayfa ağı + 3 fixture | **CLOSED** (üç repro da düzelmiş) — ama yeni CR-83/84/85/86 | `figures.py:47-48` (`_BACKGROUND_IMAGE_AREA_RATIO=0.60`), `:679-687`, `:703-716` + `cleanup.py:472-489`, `:743-780` (`_is_prose_cluster`), `:612-633` (sayfa ağı); fixture'lar `tests/test_figures.py:547/564/578`, uçtan uca `tests/test_extractor_cleanup.py:1123/1141/1159`, parametrize `:1180` |
| CR-40 (Critical) | tekrar etmeyen band satırı çevrilir + `header_guess` | **CLOSED** | `overlay_extractor.py:418-425` (`running or (row_anchor and _is_short_band_unit(...))`), `:484-501` `_is_header_guess`, `:663-664`, `:693-694`; test `tests/test_overlay_extractor.py:548-572` |
| CR-41 (Critical) | liste işareti yeni paragraf açar, asılı girinti korunur | **CLOSED**, işaret metinde kalıyor | `cleanup.py:748-750`, asılı girinti `:753-757`, `same_paragraph` `:402-403`; `tests/test_overlay_extractor.py:611-614` (`texts[4].startswith("1. First")`) |
| CR-52 | önce ölç, sığmayan blok orijinal kalır, tek kelime karakter fallback, `figure_label_truncated` | **CLOSED** (artık CR-90 var) | `pdf_overlay_exporter.py:399-407` (ölç) → `:433` (redact); `:411-423` `UNPLACEABLE_NOTE`; `_placement.py:354-365` karakter araması; `orchestrator.py:3464` `..._with_stats`, `:3484` uyarı |
| Ortak sayfa ölçeği (`SHARED_SPREAD` 0,15) | ölç→grupla→yerleştir, `uniform_scale=True` varsayılan | **CLOSED** | `_placement.py:295-312` `measure_scale`, `:420-459` `_share_scales`, `:46` `SHARED_SPREAD = 0.15`, `:49` `SHARED_MARGIN = 0.99`; `pdf_overlay_exporter.py:116, 396, 406`. **Not:** `durum.md` "orkestratör tarafından 0.10→0.15" diyor; değer aslında `_placement.py` modül sabiti, orkestratörün ve CLI'nin bu değere kancası yok |
| CR-47 | lowercase başlangıç etiketlenir, `translate=False` hariç | **CLOSED** | `overlay_extractor.py:669-673`, `:689`; `tests/test_overlay_extractor.py:155` artık gerçek assert, ayrıca `:646-657` |
| CR-50 | anahat başlığı tek tek değiştirilir | **CLOSED**, istenenden iyi | `pdfkit/api.py:92-99` `doc_set_toc_title` (`xref_set_key`; `set_toc_item` stil bayraklarını siliyor), `pdf_overlay_exporter.py:544, 557`; `set_toc()` bu yolda artık çağrılmıyor |
| CR-53 | en azından `overlay_page_rotated:N` uyarısı | **CLOSED**, tam destek yapılmış (uyarıya gerek kalmadı) | `overlay_extractor.py:251-292` `_to_visible`/`_rotate_dir`, `:833-838`, `:875-876`; `pdf_overlay_exporter.py:364-371`; `tests/test_overlay_extractor.py:679-698` (90/180/270) |
| CR-54 | önceki redaksiyon annot'ları `_placement.redact` içinde korunur | **CLOSED** | `_placement.py:228-236` (park → `Square` → apply → `Redact`), `pdfkit/api.py:434-442` (yalnız `/Subtype` yazılır); iki çağıran da paylaşıyor: `pdf_overlay_exporter.py:433`, `figure_overlay.py:139-141` |
| CR-59 | ters bölü / sürücü harfi reddi + `is_relative_to` | **CLOSED** | `exporters/base.py:229-239`; envanter tarafı `figure_inventory.py:49` + `:114-124` `field_validator` |
| CR-60 | karesel şekil tespiti | **CLOSED** (grid bucketing) | `figures.py:181-268` `_Grid`, `:448-453`, `:484`; varsayılan `merge_gap_pt=12` ile O(n·k); eşdeğerlik testi `tests/test_figures.py:~648`. Kalan uç durum → CR-107 |
| CR-61 | kaynak PDF export başına bir kez açılır | **NOT DONE (üretim yolunda)** → CR-87 | API var (`figure_overlay.py:176-184`) ama tek üretim çağıranı `orchestrator.py:3455-3466` döngüsü, tek-işlik sarmalayıcı `figure_overlay.py:212-214` → `_open` `:87-99` her şekilde `read_bytes()` |
| CR-63 | USER hatası exit 1 | **CLOSED** (ama CR-91 regresyonu) | `pdf_exporter.py:176-181`, `orchestrator.py:3342-3345`, ön kontrol `:3036-3046` / `:3133-3138` |
| CR-65 | `source_profile.json` aralık kontrolleri | **PARTIAL**: tüketici tarafı kapandı, yükleyici açık | `pdf_native.py:207-224` (`_valid_page`/`_valid_margins`/`_finite`), `:242-243, :270-276, :290-296`, uyarı `pdf_page_design_invalid:<field>`. Ama `source_profile.py:101-104` hâlâ sadece `<= 0` bakıyor → `NaN` yüklenir |
| CR-66 | (a) sans, (b) gövde satırı ölçümü, (c) `assumed`, (d) uyarı | (a) **CLOSED** `orchestrator.py:3398-3400` + `fonts.py:132`; (b) **NOT DONE** `cleanup.py:878`; (c) **CLOSED** `exporters/base.py:78`, `pdf_native.py:283-284`; (d) **PARTIAL** `orchestrator.py:3375-3381` | (b): canlı `margins_pt` üst değeri 43,67 — ilk gövde satırı y≈116; ölçüm üst bilgiyi sayıyor (`durum.md`'nin kabulü doğru) |
| CR-67 | `iter_page` istisnası katman geçmez | **PARTIAL** | Kapsama bir kat aşağıda: `pdf_overlay_exporter.py:287-294`. Orkestratör kapanışı hâlâ çıplak: `orchestrator.py:3563-3564`. Sonuç `EXPORT_FAILED/JOB_FATAL` → **exit 1**, istenen `STATE_LOCKED`(4)/`STATE_CORRUPT`(5) değil |
| CR-68 | test kalitesi | **PARTIAL** | Adı geçen iki boş assert kapandı (`grep "isinstance(Ok("` → 0 sonuç; `tests/test_pdf_native.py:534-545`, `tests/test_exporters.py:527-535`). Listenin geri kalanı bu turda denetlenmedi |
| CR-77 | pdfkit yakın-kopyalar, izin kontrolü birleşmesi | **PARTIAL** | Kaldırıldı: `doc_is_encrypted`, `doc_permissions`, `permission_modify`, `doc_allows_modify`, `page_raster_boxes`, `page_rect`, `doc_save`, `page_annot_count`. Birleşti: `pdfkit/api.py:280-286` `doc_modifiable`, `pdf_overlay_exporter.py:265` artık `overlay_extractor.py:158-172` ile aynı. Kalan → CR-105/CR-109 |

### Q kolu (orchestrator / CLI / docs)

| Bulgu | İddia | Doğrulama | Kanıt |
|---|---|---|---|
| CR-39 (Critical) | mutlak yol + export'ta hash + `export --input` + figür dalında uyarı | **PARTIAL (3/4)** | Mutlak yol `orchestrator.py:892`; export hash `:3139-3148` → `_verify_source`; overlay exit 5 `:3011-3023`; `export --input` `cli.py:153-162, 1199, 1247`. **Eksik:** `figure_translate_failed:input_changed` dalı yok (`grep input_changed` → 0 sonuç) → CR-92 |
| CR-42 | `runs.mode` doğru | **CLOSED** | `orchestrator.py:985, 1706, 2901, 2945-2966, 3838-3843`; `repository.py:1907` sayaç artık çağrılıyor |
| CR-43 | batch hatası tek sinyal | **CLOSED** | `orchestrator.py:2601-2626` (`on_rate_limited` bir kez, `max(retry_count)` tek gecikme, ortak `when`, `signal=False`), `on_success` `:2592-2593` |
| CR-44 | yalnız shield (minimal) | **PARTIAL — iddia edildiği gibi** | Shield var ve `_drain` önce gather ediyor: `:2558`, `:2564-2579`, `:2459-2460`; `complete_many` yok, `_complete:2709-2712` birim başına bir transaction (`session.py:154-165` `BEGIN IMMEDIATE` + commit). Açık kalan: SIGKILL penceresi ve `:2716-2723` kira kaybı dalında ödenmiş çevirinin atılması |
| CR-45 | bağlanmamış birimler yeniden çıkarılır | **CLOSED** | `orchestrator.py:2056-2066`, `:2080`, uyarı `overlay:unbound_units_replaced` |
| CR-46 | bayat overlay artefaktları | **CLOSED** | `OVERLAY_PASS_ARTIFACTS:371-374`, `:1607`, `_archive_state:819-827`, ayrıca `_stamp_review_file:3643-3656` / `_is_stale_output:3669-3689` |
| CR-48 | gözlemlenebilirlik | **PARTIAL** | Birim bazlı bağlam ✅ `:2594-2595, 2623`; emitter'lar ✅ `_log_placement:3617-3641`; ilerleme callback'i ✅ `:3566-3580`; çift log ✅ giderildi (`:2101-2108`). **Açık:** olay adları hâlâ `pdf_engine` (`:3336`) / `pdf_font_validated` (`:3130`), doc 06 §5.6 (`06:754`) hâlâ `pdf_engine_selected` / `font_validated` diyor |
| CR-49 | tek `find_tables` + kalp atışı | **Kalp atışı CLOSED, `find_tables` NOT DONE — iddia edildiği gibi** | `_LeaseKeepAlive:441-487`, `:936-937`, `:956-958` (thread; `extract`/ön-uçuş/export boyunca). `find_tables` hâlâ iki kez: `pymupdf_extractor.py:276/294/298-300` + `overlay_extractor.py:299-306` → `pdfkit/api.py:340-341`; `OverlayOptions.detect_tables` (`overlay_extractor.py:115`) hiçbir yere bağlı değil (`cli.py:778-779` yalnız iki alan doldurur, `orchestrator.py:1627` varsayılanı kullanır). Yeni yan etki → CR-93 |
| CR-51 | `summary.json` yerleştirme sayıları | **CLOSED** | `_OverlayExport:501-511`, `:3204-3211`, `:3220-3221`, tek başına export `:2919-2920` → `_merge_export_into_summary:3225-3245`. Not: yalnız `outputs` + `overlay` birleştiriliyor; `summary.json` yoksa hiçbir şey yazılmıyor (`:3229-3230`) |
| CR-55 | staging dizini | **CLOSED** | `:1084` `IMAGES_STAGING_PREFIX`, sink `:1333-1334`, promote yalnız kontrollerden sonra `:1114-1123`, `finally: rmtree` `:1208-1210`; test `tests/test_orchestrator.py:1288` |
| CR-56 | kullanıcı PNG'si silinmez | **CLOSED (pratikte)**, harfiyen PARTIAL | `:1371-1400`: araç adı deseni dışındaki her dosya korunur + `figure_orphan:<file>`. Kalan uç durum (kullanıcının dosyası tam olarak `pNNN-fKK.png` adında) → CR-108 |
| CR-57 | `.tr.png` + lejant yalnız tam boyandıysa silinir | **CLOSED**, canlı kanıtlı | `:3475-3477`, `figure_labels.py:148-151`, `:3295` `rewrite_image_sources`, `covered` `:3457`, `:3490-3491`, uyarılar `:3301` / `:3484`. Canlı: `source_book.md:27` → `.png`, `translated_book.md:27` → `.tr.png` |
| CR-58 | `strip_legends` fence'e dokunmaz | **CLOSED** | `figure_labels.py:132` erken çıkış, `:134-141` yalnız kaldırılan bloğun çevresinde daraltma, `:46` + `:66-71` eşleşen fence takibi. Kalan → CR-106 |
| CR-62 | `figure_orphan` | **CLOSED** | `_figure_orphans:1406-1430`, çağrı `:3357`; testler `tests/test_orchestrator.py:1287, 2510-2521` |
| CR-64 | `--pdf-font` her şeyden önce | **CLOSED** | `:3124-3131` (`"pdf" in reflow_names or wants_overlay`), `_apply_source_profile` ve `_verify_source`'tan da önce |
| `profile.serif=false → body_family="sans"` | orkestratör bağlaması | **CLOSED** | `orchestrator.py:3398-3400`; tüketim `pdf_native.py:611` → `fonts.py:132`; weasyprint yolu `pdf_exporter.py:70-72` |
| `--overlay-uniform-scale` | CLI → renderer | **CLOSED (overlay), NOT WIRED (figür)** | `cli.py:371-375` → `:933/1012`, `:1212/1248` → `orchestrator.py:2866, 3061, 3189, 3550-3559` → `pdf_overlay_exporter.py:406`. Figür etiketleri bayrağı almıyor → CR-89 |
| `export --input`, `export --figure-legend` | yeni CLI yüzeyi | **CLOSED** | `cli.py:153-162` / `:218-225` (`show_default="--no-figure-legend"`); `extract` artık bayrağı tanımlamıyor (`cli.py:1022-1044`) |
| README + doc 06/07 | doküman güncellemeleri | **CLOSED** (bir istisna) | README `:28-77` (overlay, figures, native PDF, `source_profile.json`, ağaç); doc 06 `:52, :72, :789, :795` `[Superseded by Addendum A]`; A-D3/§4.4 "≥ 3" (`06:667, :994`); doc 07 "20 columns" (`07:121, :629`). **Açık:** doc 06 `:533-539` hâlâ eski `UnitAdapter` imzasını basıyor (`:503-506`'daki NOT'a rağmen); doc 06 §5.6 olay adları (bkz. CR-48) |

## New findings (CR-83 …)

### 🛑 Blocker

Yok.

### ⚠️ Critical

#### [CR-83] CR-38'in %40 sayfa güvenlik ağı, sayfanın kendisi bir şekil olduğunda o sayfanın **bütün şekillerini** siliyor — `src/extractors/figures.py:612-633`, sabitler `:53-54`

**Kod:**
```python
page_chars = sum(len(ln.text) for ln in kept_lines)
absorbed_chars = sum(len(kept_lines[i].text) for c in clusters for i in set(c.inner))
if absorbed_chars >= _PAGE_ABSORB_MIN_CHARS and absorbed_chars > _PAGE_ABSORB_MAX_RATIO * page_chars:
    ...
    return []      # sayfadaki her şekil düşer
```

**Sorun.** Ölçüt, emilen karakterleri *sayfanın tüm tutulan karakterlerine* oranlıyor — emilmemiş **düzyazı** karakterlerine değil. Bu yüzden "şekil sayfanın yazısını yuttu" ile "sayfa zaten bir şekildir" ayırt edilemiyor. Etiket yoğun tam sayfa bir şema/harita/patlatılmış montaj çiziminde neredeyse her karakter şekle aittir, oran ≈ 1,0 olur; önünde yalnızca 400 karakterlik taban durur.

**Somut senaryo.** 40 etiketli tam sayfa şema (ortalama 12 karakter = 480) + 60 karakterlik kısa altyazı → `480 >= 400` ve `480 > 0.40 × 540 = 216` → `return []`. Sayfada **hiç şekil üretilmez**, 40 etiket paragraf akışına serbest parçalar olarak döner, `figures.json`'a hiçbir kayıt girmez.

**Kanıt — projenin kendi test dokümanı bu sınırın dibinde.** `docs/test_doc.pdf` sayfa 2 (tek gerçek şekil):

```
emilen etiket karakterleri : 335   (taban 400)
sayfanın toplam karakteri  : 899
oran                       : 0.373 (sınır 0.40)
```

İki bağımsız marj da ince: ~2 etiket daha ya da biraz daha kısa bir altyazı, referans sayfanın tek şeklini kaybettirir. Bu, kuralın gerçek kitaplarda ne sıklıkta yanlış ateşleyeceğinin doğrudan göstergesidir.

**Neden önemli.** F1'in çekirdek gereksinimini (US-14, FR-33) tersine çeviriyor: CR-38 metin kaybını kapatırken **şekil kaybı** açtı. `extract` yolunda hiç değilse `figure_page_dropped:<page>:text_heavy` notu çıkıyor; overlay yolunda o da yok (CR-86).

**Beklenen düzeltme.** Payda emilmemiş düzyazı olsun, sayfanın tamamı değil: ağ yalnız *akışta kalması gereken* metin kaybolduğunda ateşlemeli. Örneğin kümelerin dışında kalan `prose_chars` üzerinden "kalan düzyazı en az X karakter/oran" koşulu, ya da tek kümeli + altyazılı sayfaların (tam sayfa şekil kalıbı) muaf tutulması. Fixture: 40 etiketli tam sayfa şema (şekil korunmalı) + gerçek OCR sandviç (şekil düşmeli). Test dokümanının 2. sayfası bir regresyon testi olarak sabitlenmeli (oran assert'i ile).

### 💡 Warning

#### [CR-84] `_is_prose_cluster` karışık kuralı fiilen %15'lik bir alan kapısı: içinde çok satırlı bir not olan gerçek şekiller sessizce düşüyor — `src/extractors/figures.py:743-780`, sabitler `:51-52`

`return share >= _PROSE_AREA_MIXED_RATIO and prose_chars > label_chars` — etiketler tanım gereği kısa olduğundan `prose_chars > label_chars` az etiketli diyagramlarda kolayca sağlanır; geriye **%15 alan** kalır. Senaryo: 250×160 pt bir akış şeması, etiketleri "Kamera"/"Sensör" (12 karakter), içinde 150×48 pt dört satırlık bir açıklama kutusu → `share = 7200/40000 = 0,18 ≥ 0,15` ve `248 > 12` → küme tümden reddedilir (`:556-573`), şekil hiç üretilmez. Reddin izi yalnız INFO log (`figure_candidate_rejected:<page>:prose`); iş uyarılarına **not düşmüyor**. Projenin kendi pozitif testi 0,135'te — bir sarma satırı payı. **Fix:** `prose_chars > label_chars` yerine anlamlı bir eşik (ör. `prose_chars > 2 × label_chars` *ve* çok satırlı blok sayısı ≥ 2), ve reddi `notes`'a ekleyip kullanıcıya göster.

#### [CR-85] %60 raster arka plan kuralı, üzerinde ≥ 3 satır yazı olan büyük gerçek görselleri de atıyor — ve bunu hiç duyurmuyor — `src/extractors/figures.py:670-687`, `:47-48`

Kural yalnız alan oranına ve "üstündeki satır sayısı ≥ 3"e bakıyor; satırların düzyazı olup olmadığına ya da görsele yayılıp yayılmadığına bakmıyor. Senaryo: sayfanın tamamını kaplayan bir fotoğraf plakası, üzerine basılmış 2 satırlık başlık + 1 satırlık künye → 3 satır → `background_image`, plaka hiç çıkarılmaz. Düzyazı ve sayfa-ağı yollarının aksine burada `notes`'a **hiçbir şey eklenmiyor** (`:469-476` yalnız INFO), kayıp iş uyarılarında görünmez. **Fix:** üstteki satırların `is_label_like` olmamasını *ve* görsel alanına yayılmış olmasını da şart koş; her hâlükârda reddi `notes`'a çıkar.

#### [CR-86] `figure_page_dropped:<page>:text_heavy` notu iki tespit yolundan yalnız birinde kullanıcıya ulaşıyor — `src/extractors/pymupdf_extractor.py:506` (overlay) vs `:1231` (extract)

`detect_figure_regions` kendi `_Context`'ini `:506`'da kurup yalnız `regions` döndürüyor; `_detect_page_figures`'ın `:471-473`'te biriktirdiği `ctx.warnings` atılıyor. `extract` yolu notu koruyor (`:1231` → `:1274`). Senaryo: `figures.json` bulunmayan temiz bir dizinde `translate --mode overlay` — CR-83/CR-84 bir sayfanın şekillerini düşürür ve kullanıcıya **hiçbir şey söylenmez**. **Fix:** `detect_figure_regions` de notları döndürsün, `overlay_extractor.py:859-867` çağrısı bunları `OverlayExtraction.warnings`'e eklesin.

#### [CR-87] CR-61 üretim yolunda kapanmadı: kaynak PDF her şekil için baştan okunup yeniden açılıyor — `src/pipeline/orchestrator.py:3455-3466`, `src/exporters/figure_overlay.py:212-214`, `:87-99`

Toplu API (`render_translated_figures`, `:176-184`) gerçekten tek `_open` yapıyor, ama tek üretim çağıranı `for record in records:` döngüsünde tek-işlik `render_translated_figure_with_stats` çağırıyor; o da tek elemanlı bir batch kurup `source_pdf.read_bytes()` + `open_document_bytes` çalıştırıyor. Senaryo: 90 MB / 800 sayfa PDF, 250 çevrilmiş şekil → 250 tam dosya okuma ve 250 doküman açma (~22 GB I/O) — CR-61'in şikâyet ettiği maliyetin aynısı. İnceleme tablosunda "kapandı" görünmesi yanıltıcı. **Fix:** orkestratör kayıtları tek `render_translated_figures` çağrısında toplasın (önce CR-88).

#### [CR-88] Toplu şekil renderer'ı tek bir *değiştirilebilir* dokümanı bütün işler arasında paylaşıyor — `src/exporters/figure_overlay.py:179-184`, `:139-152`

`_render_job` canlı sayfa üzerinde redaksiyon yapıp metin yazıyor, sonra rasterize ediyor; aynı sayfadaki iki `FigureJob` artık bağımsız değil. Senaryo: 12. sayfada A ve B şekilleri var. A redaksiyonu uygular, Türkçe etiketlerini yazar; B sonra **aynı değiştirilmiş sayfadan** kendi bölgesini rasterize eder. A `redact`'ten sonra `place`/pixmap'ten önce hata verirse (`:152`'deki `except` yalnız A için `Err` döner), 12. sayfa A'nın dikdörtgenleri boşaltılmış hâlde kalır ve **başarılı raporlanan** B'nin PNG'si İngilizce metni silinmiş bir şekil taşır. Bugün gizli, çünkü tek üretim çağıranı iş başına bir çağrı yapıyor (CR-87) — yani CR-87 düzeltilir düzeltilmez ortaya çıkar. **Fix:** her iş kendi sayfa kopyası üzerinde çalışsın (doküman bir kez okunsun, sayfa iş başına klonlansın).

#### [CR-89] `--no-overlay-uniform-scale` figür etiketlerine ulaşmıyor — `src/exporters/figure_overlay.py:168`, `:212-214`, `src/pipeline/orchestrator.py:3464`

`render_translated_figures(..., uniform_scale: bool = True)` var, ama `render_translated_figure_with_stats` böyle bir parametre almıyor ve varsayılanla çağırıyor; orkestratörün geçirecek yolu yok. Senaryo: kullanıcı eski çıktıyla diff almak için `export --no-overlay-uniform-scale` çalıştırır; gövde metni itaat eder, `.tr.png` içindeki etiketler yine grup ölçeğiyle gelir. README `:33` ve doc 06 §7.1 bu istisnayı yazmıyor. **Fix:** parametreyi baştan sona geçir (CR-87 düzeltmesiyle aynı yerde).

#### [CR-90] Redaksiyondan sonra boş kutu hâlâ mümkün — `src/exporters/_placement.py:474-491`, `src/exporters/pdf_overlay_exporter.py:433`, `:461-467`

CR-52 *plan* aşamasında yerleştirilemeyeni koruyor, ama `place()` gerçek sayfada iki denemeyi de kaybederse `Placement(placed=False, truncated=True, text="")` döner — kutu **zaten redakte edilmiştir** (`:433`). `_build_page` bunu yalnız `COULD_NOT_FIT` + `"not placed"` olarak raporlar; `kept_original` / `UNPLACEABLE_NOTE` eklemez ve kaynak metin gitmiştir. Tetikleyici, ölçümün taslak sayfada (`_placement.py:264-292`: origin 5.0, `rotate=0`, boyutlar takas) gerçek sayfayla anlaşmazlığa düşmesi. Yani CR-52'nin semptomu dar bir pencerede yaşıyor. **Fix:** redaksiyon sonrası `placed=False`'ı ayrı ve gürültülü bir inceleme sebebi yap (ör. `blank_box`), ve mümkünse kaynak metni geri yaz.

#### [CR-91] CR-63 düzeltmesi, **zaten diske yazılmış** çıktıları rapordan düşürüyor — `src/pipeline/orchestrator.py:3339-3350`

Exporter döngüsü formatları sırayla yazıyor; indirgenemeyen bir `Err`'de `return artifact` diyor ve o ana kadar yazılan formatları taşıyan `ExportOutcome` atılıyor. `cli.py:732-733` `_fail`'e gidiyor: `output …` satırları basılmıyor, `summary.json` güncellenmiyor (`:2919`), iş `EXPORTED` işaretlenmiyor (`:3172-3175`). Senaryo: `export -f md -f epub -f pdf`, `pdf_native` `EXPORT_FAILED / ErrorScope.USER` veriyor (`pdf_native.py:563-570`; `pdf` `optional=True`). `translated_book.md` ve `.epub` diskte ve geçerli; kullanıcı yalnız hatayı ve exit 1'i görüyor, hiçbir çıktı listelenmiyor. Tur öncesi bu exit 2 + iki çıktı idi. Diskteki dosyalarla DB/summary durumu ayrışıyor. **Fix:** `Err`'i sakla, döngüyü bitir, `ExportOutcome`'u yazılanlarla birlikte döndür ve exit kodunu USER hatası için 1 yap (kısmi çıktıları yine listeleyerek).

#### [CR-92] Değişmiş kaynak PDF, yalnız reflow isteyen bir `export`'u tamamen kilitliyor; çıkış yolu yok — `src/pipeline/orchestrator.py:3141-3148`, `:3011-3023`

`figure_step = bool(reflow_names) and bool(self._paintable_figures())` doğruysa `_verify_source` çalışıyor ve hash farkında `required` bayrağına bakmadan `STATE_HASH_MISMATCH` dönüyor. Senaryo: etiket kutulu `figures.json`'u olan bir kitap; kullanıcı `translate`'ten sonra PDF'i yeniden kaydediyor / OCR'ını temizliyor, sonra `export --format md` çalıştırıyor → exit 5, hiçbir şey yazılmıyor — oysa md'nin PDF'e ihtiyacı yalnız etiket boyamak için. `export --input` de kurtarmıyor (hash yine farklı) ve export'ta figür adımını kapatan bir bayrak yok; tek çıkış `translate --fresh` (yeniden ödeme). CR-39 tam da burası için `figure_translate_failed:input_changed` + lejantın korunmasını istemişti; **eksik dosya** kardeş dalı (`:3002-3010`) bunu doğru yapıyor, **değişmiş dosya** dalı yapmıyor. README `:55-59` bu davranışı yazıyor, yani bilinçli; ama maliyeti orantısız. **Fix:** `required=False` iken hash farkını uyarıya indir (`figure_translate_failed:input_changed`), lejantı koru, exit 2; overlay için exit 5 kalsın.

#### [CR-93] İki ayrı kalp atışı aynı `leases` satırına yazıyor; biri modülün tek-yazıcı kilidini atlıyor — `src/pipeline/orchestrator.py:441-487`, `:936-937`, `:2493-2503`

Modül sözleşmesi (docstring `:10-12`): olay döngüsünden yapılan tüm repository çağrıları tek bir `asyncio.Lock` altında `asyncio.to_thread` ile gider, kalp atışı görevi de kilidi paylaşır. `_LeaseKeepAlive` `_bind`'de başlıyor ve `translate` boyunca da yaşıyor; kendi havuzlu bağlantısından (`session.py:96-103`) kilitsiz `BEGIN IMMEDIATE` atıyor, aynı anda in-loop `_heartbeat_loop` kilitle atıyor. İki yazıcı aynı satır için `busy_timeout=5000` (`session.py:115`) altında yarışıyor. Senaryo: keep-alive'ın beat'i bir worker'ın `complete` commit'iyle çakışır; worker beklemeye girer, zaman aşımına uğrarsa `Err` → `_state_failure:2486-2491` → `ctx.db_error` → `cancel.set()` → tüm koşu exit 1 ile durur, gerçekte bir sorun olmadığı hâlde. Pratik olasılık düşük (beat milisaniyelik), ama ilan edilen kilit değişmezi ihlal ediliyor. **Fix:** async kalp atışı işi devraldığında thread'i duraklat, ya da thread'in beat'ini de aynı kilitten geçir.

#### [CR-94] `_layout` iki hata çıkışında da `DocumentWriter`'ı kapatmıyor — `src/exporters/pdf_native.py:549-554`, `:563-570` (kapanış yalnız `:571`)

`writer` `:545`'te kuruluyor, iki `return err(...)` `try` içinden dönüyor; `writer_close` yalnız başarı yolunda. Senaryo: metin sütunundan geniş bir tablo `_EMPTY_PAGE_LIMIT` dalını tetikler (`:563`); `export` temiz bir USER `Err` döner ama MuPDF writer'ı ve yerel tampon nesneleri CPython finalizasyonuna kalır. `run` tek süreçte birkaç format ürettiğinden sızıntı birikir; Windows'ta alttaki tutamaç aşamadan sonra da tamponu canlı tutabilir. **Fix:** `try/finally` ile `writer_close`.

#### [CR-95] Ortak ölçek varsayılan açık olduğu için **sığan** metin de küçülüyor; düşüş sayfa genelinde inceleme eşiğine kadar inebilir — `src/exporters/_placement.py:437-441`

`bound = max(review_threshold, median(naturals) - SHARED_SPREAD)`, `shared = min(natural ≥ bound)`. Medyan referans alındığı için, bir sayfanın yarısı ağır taşan bloklardan oluştuğunda hafif bloklar da aşağı çekilir. Senaryo: doğal ölçekler `[1.0, 0.85, 0.80, 0.70, 0.66, …]` → medyan 0,80, `bound = 0,65`, `shared = 0,66` → 1,0'da rahat sığan paragraf da **%34 küçültülür**, ve bu birimlerin hiçbiri inceleme listesine girmez (girmesi için `scale < 0,65` olmalı). Canlı test dokümanında bile 1. sayfa gövdesi 10,5 pt kaynaktan 9,5 pt'ye iniyor. Bu, incelemenin "ürün kararı" dediği davranışın artık **varsayılan** olmasının bedeli. **Fix (öneri):** birim başına maksimum düşüşe de bir tavan koy (ör. doğal ölçeğinin %10'undan fazla küçülen birim gruptan çıkarılsın), ve sayfa ortak ölçeği `< 0,9` olduğunda `overlay_review.json`'a sayfa düzeyinde bilgi girdisi yaz. En azından README'de "tüm gövde tek puntoya iner ve bu punto sayfanın en sıkışık paragrafına göre belirlenir" cümlesi netleşsin.

#### [CR-96] CR-76 ters yönde uygulanmış: satır yüksekliği kaynaktan okunmuyor, 1,15'ten **daha sıkıya** (1,05) iniyor — `src/exporters/_placement.py:40-43`, `:399-404`

`LINE_HEIGHT = 1.15` hâlâ sabit ve `line_boxes` satır aralığı hiçbir yerde okunmuyor; eklenen `LINE_HEIGHT_TIGHT = 1.05` yalnız `uniform` modda, font küçültülmeden önce deneniyor. CR-76 "kaynak satır aralığını kullan (1,15–1,40 arası kırp) ve küçültmeden önce onu bırak" diyordu; kod bunun yerine kaynak ritminden (test dokümanında ~1,26 em) daha da uzaklaşıp 1,05'e sıkıştırıyor. Sonuç: `--overlay-uniform-scale` açıkken paragraflar CR-76'nın zaten "kaynaktan sıkı" dediği 1,15'ten de sıkı çıkabiliyor, ve iki mod arasındaki fark yalnız font ölçeği değil satır aralığı da oluyor. CR-76 ertelenmişti; ama tur bu satırlara dokunduğu için sapma kayda geçmeli. **Fix:** kaynak satır aralığını `line_boxes`'tan türet, 1,15–1,40 arasına kırp, küçültmeden önce bu bandın altına in — 1,05'e değil.

#### [CR-97] `prefix_style` figür yolunda `figures.json` sınırında düşüyor: kalın sıra numarası öneki `.tr.png` içinde kayboluyor (overlay PDF'te korunuyor) — `src/domain/models.py:91-106`, `src/extractors/pymupdf_extractor.py:606-644` (`:629` `detect_style(lines)`), `src/extractors/figure_inventory.py:53-77`, `src/domain/overlay.py:129-145`, `src/exporters/figure_overlay.py:74-84`

**(Teşhis Bölüm A / YR-3'ten alınmıştır ve doğrulandı — ilk teşhisim "stil birleştirmesi" idi, asıl sebep bu.)** Overlay yolu öneki **tam olarak taşıyor**: `cleanup.py:567` `detect_prefix_style` → `cleanup.py:612` `OverlayStyle.prefix_style` → `domain/overlay.py:74` → `repository.py:1266, 1444` → `pdf_overlay_exporter.py:205` → `_placement.py:77, 194`. Figür yolu bu zinciri hiç kurmuyor: `_label_boxes` yalnız `detect_style(lines)` çağırıyor (`pymupdf_extractor.py:629`), `LabelBox`'ta (`domain/models.py:91-106`), `LabelBoxEntry`'de (`figure_inventory.py:53-77`) ve `LabelReplacement`'ta (`domain/overlay.py:129-145`) `prefix_style` alanı **yok**, dolayısıyla `figure_overlay._spec` de `PlacementSpec`'e geçiremiyor. Yani bu kozmetik bir stil kaybı değil, belgelenmiş bir gereksinimin (E-46 / Q22) iki yoldan yalnız birinde uygulanması. Ölçtüm — kaynakta ve overlay PDF'te önek gerçekten kalın, figür PNG'sinde değil:

```
kaynak PDF s.2 : Times,Bold '2.'        + Times 'Image processing'
overlay PDF s.2: Charis SIL Bold '2.'   + Charis SIL Regular 'Görüntü işleme'
figures.json   : {"text": "2. Image formation", "bold": false, ...}
```

**`durum.md`'nin "kutuda sola yaslı" kısmı yanlış:** hizalama uygulanıyor. Ölçtüm — `alignment` `figures.json`'dan `figure_labels.py:196-207` üzerinden `figure_overlay.py:82`'ye geçiyor ve `.tr.png` içinde ortalı kutular gerçekten ortalı:

```
'6. Recognition'      align=center  kutu 219 px  TR mürekkep 50..170  (kenar boşlukları 50 / 49)
'3. Image processing' align=center  kutu 297 px  TR mürekkep 42..256  (42 / 41)
```

**Fix:** `LabelBox` + `LabelBoxEntry` + `LabelReplacement`'a `prefix_style` alanını ekle ve `_label_boxes`'ta `detect_prefix_style(lines)` ile doldurup `figure_overlay._spec`'te `PlacementSpec.prefix_style`'a geçir — overlay yolunda hazır olan altyapının aynısı. `durum.md`'nin ilgili maddesi de "hizalama doğru, kayıp olan `prefix_style`" olarak düzeltilmeli.

#### [CR-98] `open_database` geçiş yolu hâlâ dar bir istisna demeti yakalıyor: DB dışı bir hata `Result` sözleşmesini delip `engine.dispose()`'u atlıyor — `src/database/session.py:410`

`except (SQLAlchemyError, sqlite3.Error, KeyError, ValueError)`. Senaryo: bir v1→v2 geçiş adımı `OSError` (WAL büyürken disk dolar), `TypeError` veya `AttributeError` fırlatır → istisna `Result[Database]` döndüren fonksiyondan dışarı çıkar, `:411`'deki `engine.dispose()` çalışmaz (Windows'ta açık dosya tutamacı kalır) ve kullanıcı "yedek şurada" mesajı yerine ham traceback görür. CR-73 (Suggestion) olarak ertelenmişti; geçiş yolu artık canlıda kullanıldığı için (v1 dosya gerçekten migrate edildi) Warning'e yükseltiyorum. **Fix:** `except Exception`.

### 📘 Suggestion

- **[CR-99]** CR-71 kod yerine docstring ile "kapatıldı": `repository.py:1647` artık `result.chunk_id`'nin yok sayıldığını *belgeliyor* ama korumuyor. Bir değişmez açıkça yazılıp zorlanmadığında daha risklidir. Tek satır: `if result.chunk_id != unit_id: return Err(INTERNAL, JOB_FATAL)`. (Pratik risk düşük: `orchestrator.py:2594` `zip(..., strict=True)` uzunluk kaymasını zaten yakalar; bu yüzden Critical değil.)
- **[CR-100]** CR-69 yapılmadı: `database/models.py:768` ve `:770` hâlâ aynı `ck_overlay_blocks_keep_reason` adını üretiyor. v2 yayına çıkmadan yeniden adlandırılmalı; sonra tabloyu yeniden kurmak gerekir.
- **[CR-101]** Ortak ölçek rafinman döngüsü, korumaya çalıştığı birimi atlıyor: `_placement.py:454-455` `if measured >= 0` — adaydaki ölçekte hiç sığmayan birim `-1.0` döndürür, `factor`'ı aşağı çekmez ve `place()`'in ikinci denemesiyle sessizce kendi ölçeğine düşer (`shared_scale=None`). Ayrıca ölçüm maliyetinin sayfa başına tavanı yok: birim başına 1 + (küçülen için) 1 + `_REFINE_ROUNDS = 4` × |eligible| ekleme çağrısı; en kötü durumda incelemenin bütçelediği ~4 ms/birimin ~6 katı.
- **[CR-102]** `overlay_extractor.py:689` `unit.text[:1].islower()` koşulsuz `fragment` işaretliyor: "iPhone kameraları…", "von Neumann…", "x ile gösterilir…" gibi tam paragraflar her sayfada inceleme listesine düşüyor. Negatif durumu sabitleyen test yok.
- **[CR-103]** Doküman/kod tutarsızlıkları: `_placement.py:13` hâlâ `median - 0.10` yazıyor (kod 0,15); `durum.md:34` bu değeri "orkestratör tarafından" konmuş gibi anlatıyor (aslında `_placement.py:46` modül sabiti, hiçbir CLI/ayar kancası yok); doc 06 `:533-539` eski `UnitAdapter` imzasını basmaya devam ediyor; olay adları `pdf_engine` / `pdf_font_validated` ile doc 06 §5.6'daki `pdf_engine_selected` / `font_validated` hâlâ ayrı (CR-48).
- **[CR-104]** Durum/rapor hijyeni: `orchestrator.py:1894` `update_job(...)` sonucu incelenmiyor (başarısız `TRANSLATED` yazımı yine exit 0 verir, sonraki komut işi `TRANSLATING` takılı bulur — aynı fonksiyondaki `:1781`, `:1817` kontrol ediyor); `:3024-3026` `_remember_input_path`'in dönüşü atıldığı için `export --input` sonrası md/epub metadata'sı eski yolu yazıyor; `:3742-3749` overlay PDF'in bayatlığını inceleme dosyasının binding'inden karar veriyor, PDF'in kendi mtime'ına bakmıyor; `translate --fresh` yalnız overlay artefaktlarını taşıyor, eski `translated_book.md/.epub/.pdf` yeni state'in yanında kalıyor.
- **[CR-105]** CR-77 kalıntıları: her zaman gelen modüller için üç `except ImportError` (`orchestrator.py:3040-3042`, `:3439-3449`, `:3514-3522`) ve var olan alanların `getattr`/`dataclasses.fields` ile yoklanması (`:3387-3400`, `:3549-3550`). İkisinin de somut bedeli var: gerçek bir döngüsel import `EXPORTER_UNAVAILABLE` → exit 2 "kısmi başarı" olarak maskelenir; `uniform_scale` alanı yeniden adlandırılırsa CLI bayrağı sessizce no-op olur.
- **[CR-106]** `figure_labels.py:66-71` (ve `:165-172` kopyası) kapanış fence'inin **uzunluğunu** karşılaştırmıyor: dört backtick'li bir blok içindeki üç backtick'li örnek dış fence'i kapatır ve kalan satırlardaki legend işaretçisi ayrıştırılıp `strip_legends` tarafından silinir. Ayrıca `:143-145` sondaki `\n`'i yalnız bir şey silindiyse ekliyor → aynı belge, lejant olup olmamasına göre farklı bitiyor (gereksiz hash oynaklığı).
- **[CR-107]** Performans uç durumları: `_Grid` her kutuyu kapsadığı tüm hücrelere yazıyor (`figures.py:195-202`; kutu başına `_GRID_MAX_CELLS_PER_BOX = 4096`, sayfa başına iki grid) — `--figure-merge-gap 1` gibi küçük bir değerde hücre 4 pt olur ve 200×200 pt bir yol 2 500 hücre tutar; toplam giriş sayısına tavan yok. `overlay_extractor.py:475-481`, `:495-499`, `:680-681` birim sayısında karesel (yoğun bir dizin/İÇİNDEKİLER sayfasında ~1 500 birim).
- **[CR-108]** CR-56 kalıntısı: `orchestrator.py:1371-1400` araç deseniyle (`pNNN-fKK.png`) *eşleşen* dosyaları eski envantere bakmadan siliyor; kullanıcının tam bu adla koyduğu bir PNG hâlâ arşivlenmeden silinir. Pratik tehlike (CR-56'nın `my-cover.png` örneği) kapandı.
- **[CR-109]** `pdfkit/api.py:1-6` "pymupdf'i yalnız bu modül import eder" diyor ve `doc_page` sarmalayıcısı bunun için var, ama beş çağrı yeri hâlâ doğrudan indeksliyor (`marker_extractor.py:130`, `overlay_extractor.py:831`, `:878`, `pymupdf_extractor.py:1045`, `:1195`); `tests/test_layering.py` bunu yakalamıyor. Ayrıca `page_fonts` (`:194`), `page_words` (`:204`), `doc_language` (`:230`) hâlâ yalnız testlerden çağrılıyor.
- **[CR-110]** CR-82 kalıntıları: `noqa: BLE001`'ların 6'sı hâlâ gerekçesiz (`fonts.py:82`, `pdf_exporter.py:293`, `pdf_native.py:625`, `:650`, `pdf_overlay_exporter.py:309`, `:581`); CR-72 hâlâ açık — `overlay_unit_count` / `overlay_tool_version` yazılıyor (`orchestrator.py:2090-2091`), hiçbir yerde okunmuyor, yani "bu pass başka bir araç sürümüyle çıkarıldı" kontrolü fiilen yok.
- **[CR-111]** (= YR-9) `tests/test_orchestrator.py:2462` koşullu assert: `if "uniform_scale" in dc.fields(...)`. Alan yeniden adlandırılır veya kaldırılırsa test sessizce vakumlaşır — CR-47'de aynı sınıf vakum assert temizlenmişti, bu yenisi aynı desene düşüyor. CR-105'teki `dataclasses.fields` yoklamasının test tarafındaki ikizi; ikisi birlikte düzeltilmeli (düz keyword argüman + alan yoksa yüksek sesle hata).
- **[CR-112]** (= YR-11) `src/pdfkit/api.py:92-100` `doc_set_toc_title`, `get_outline_xrefs()` sırasının `doc_toc()` sırasıyla birebir örtüştüğünü varsayıyor; bozuk ya da iç içe bir outline'da index kayması **sessizce yanlış başlığı** yazar (hata vermez). `pdf_overlay_exporter.py:541` docstring'i hâlâ "set_toc_item" diyor. En azından uzunluk eşitliğini doğrula (`len(xrefs) == len(toc)`), eşit değilse anahat çevirisini atla + uyarı.
- **[CR-113]** (= YR-12) `_promote_staged_images` (`orchestrator.py:1120`) `source_book.md`'nin atomik yazımından (`:1124`) önce çalışıyor; son I/O hatasında `images/` yeni, `source_book.md` eski kalır. CR-55'in "reddedilirse hiçbir şey yazılmaz" garantisi bozulmuyor (tüm ret kontrolleri promote'tan önce), ama iki artefakt arasında tam atomiklik yok.

## B. Known gaps assessment (`durum.md` 6 maddesi)

| # | `durum.md` maddesi | Karar | Gerekçe |
|---|---|---|---|
| 1 | `.tr.png` etiketleri sola yaslı + "2." kalın öneki yok | **Kısmen yanlış → yeni bulgu CR-97** | Hizalama iddiası **çürütüldü**: `figures.json` → `figure_labels.py:196-207` → `figure_overlay.py:82` zinciri hizalamayı taşıyor ve ölçümde ortalı etiketler ortalı çıkıyor (`6. Recognition`: 219 px kutuda 50/49 kenar boşluğu). Kalın önek iddiası **doğru**; sebebi `detect_style()`'ın etiketi tek stile indirmesi. `durum.md` maddesi düzeltilmeli. |
| 2 | CR-39 sapması: yalnız reflow + kaynak yok → exit 0 + uyarı | **Kabul edilebilir sapma** | Doğruladım: ödenmiş çeviri kaybolmuyor. `orchestrator.py:3436` `painted=[]` döner, `strip_legends(markdown, [])` (`figure_labels.py:120, 130`) hiçbir lejantı silmez, `:3300-3306` her ayakta kalan lejant için `figure_labels_not_painted:N` yazar. Kullanıcı md'sini alır, etiketler İngilizce kalır, lejant listesi durur ve uyarı görünür — makul. **Ama kardeş dalı (dosya *değişmiş*) kabul edilemez → CR-92.** |
| 3 | CR-66(b) kenar boşluğu ölçümü üst bilgiyi sayıyor | **Açık kalsın, QA risk notu** (mevcut CR-66 kaydı yeterli, yeni numara yok) | `cleanup.py:878` `for b in page.blocks for ln in b.lines if ln.text` — her satır sayılıyor. Canlı kanıt: `margins_pt` üst değeri 43,67 iken ilk gövde satırı y≈116. Etkisi yalnız reflow PDF'in gövde kutusu; metin kaybı yok. Bilet açılsın. |
| 4 | CR-44 `complete_many` yok | **Kabul edilebilir**, QA risk notu | Shield gerçekten var ve `_drain` release'ten önce gather ediyor (`:2459-2460`), `release` yalnız `PROCESSING` satırlara dokunuyor — Ctrl+C penceresi kapandı. Açık kalan yalnız SIGKILL / güç kesintisi penceresi (bir batch) ve `:2716-2723` kira kaybı dalı. Bu, tasarımın baştan kabul ettiği "tek batch" riskiyle aynı mertebede. |
| 5 | CR-49 `find_tables` tekrarı sürüyor (kalp atışı çözüldü) | **Kabul edilebilir ama koşullu** | Kalp atışı gerçekten kapandı (`_LeaseKeepAlive`), yani CR-18'in ~260 sayfa sınırı kalktı. `find_tables` iki kez çalışmaya devam ediyor ve `detect_tables` hiçbir yere bağlı değil → sayfa başına ~0,23 s, 300 sayfa ≈ 70 s ekstra. Onay koşulundaki "~250 sayfadan büyük kitap overlay'de çalıştırılmadan önce yapılmalı" şartı **geçerliliğini koruyor**. Ayrıca yeni CR-93 bu düzeltmenin yan etkisi. |
| 6 | Ertelenenler: CR-08/09/10, CR-20, CR-69..76, CR-79..82 | **Çoğu savunulabilir; üç istisna** | Savunulabilir: CR-08/09/10 (canlı DeepL'e bağlı, QA kararı), CR-75, CR-79, CR-80, CR-81 ve CR-82'nin kalanı. **İstisna 1:** CR-61 "P kolunda düzeltildi" diye listelenmiş ama üretim yolunda değil → CR-87; erteleme değil açık bulgu. **İstisna 2:** CR-73 → CR-98; geçiş yolu artık canlıda kullanıldığı için Warning. **İstisna 3:** CR-76 ertelenmiş sayılıyor ama tur tam bu satırları değiştirdi ve ters yöne gitti → CR-96. Ayrıca **CR-20** (ASCII-only sözlük keşfi; `glossary/discovery.py:46-47` değişmemiş, `_WORD = [A-Za-z]…`) hâlâ açık ve overlay birim-bazlı sözlük kontrolünü de besliyor; ertelemesi savunulabilir ama QA'da ölçülmeli. |

## Risk points for QA (updated — devir listesi)

Bu liste v1.1 incelemesinin "Risk points for QA" bölümünün **yerini almaz, onu günceller**.

**Kapandı; artık regresyon testi olarak koşulmalı (eskiden hata bekleniyordu):** 4 (CR-38 — ama CR-83 için tersi de koşulmalı, R1), 5 (CR-40), 6'nın CR-41/CR-47 kısmı, 7 (CR-53), 8/9'un overlay kısmı (CR-39), 12 (CR-45), 13 (CR-42), 14 (CR-46), 19 (CR-50), 20 (CR-54), 21'in summary kısmı (CR-51), 26 (CR-55), 27 (CR-56), 28 (CR-57), 29 (CR-58), 30'un CR-59 kısmı, 34 (CR-63), 39'un CR-67 kısmı (artık exit 1, ham traceback yok).

**Yeni / değişen riskler:**

- **R1 (CR-83, en yüksek öncelik).** Etiket yoğun **tam sayfa şekil** sayfaları: 30+ etiketli şema, harita, patlatılmış montaj çizimi, anatomi levhası; altyazısı kısa olanlar özellikle. Beklenen: şekil korunur. Bugün: sayfadaki tüm şekiller düşebilir. `figures.json` `totals.count` ve `figure_page_dropped:*:text_heavy` uyarısını izleyin. Test dokümanının 2. sayfası 0,373/0,40'ta — sınırın hemen altında olduğunu doğrulayın.
- **R2 (CR-84/85).** İçinde çok satırlı açıklama kutusu olan gerçek diyagramlar; üzerine yazı basılmış tam sayfa fotoğraf plakaları. Beklenen: şekil korunur. Bugün: sessizce düşebilir ve iş uyarısına **not girmez** — `logs/*.jsonl` içinde `figure_candidate_rejected` arayın.
- **R3 (CR-86).** `figures.json` olmayan temiz bir dizinde `translate --mode overlay`: bir sayfanın şekilleri düşerse kullanıcıya hiç uyarı gitmiyor. Extract yolu ile karşılaştırın.
- **R4 (CR-91).** `export -f md -f epub -f pdf` ile `pdf` aşamasını USER hatasına düşürün (ör. sütuna sığmayan içerik). Beklenen: md/epub'ın diskte olduğu raporlanmalı. Bugün: exit 1, çıktı listesi boş, `summary.json` güncellenmiyor, iş `EXPORTED` olmuyor — sonra `status` çıktısını diskteki dosyalarla karşılaştırın.
- **R5 (CR-92).** `translate`'ten sonra kaynak PDF'i yeniden kaydedin (içerik aynı, baytlar farklı), sonra `export --format md`. Beklenen: uyarı + md. Bugün: exit 5, hiçbir şey yazılmaz, `--input` de kurtarmaz.
- **R6 (CR-90).** Çok küçük kutular ve tek uzun kelimeler: taslak ölçüm ile gerçek sayfa anlaşmazlığa düşerse kutu **boş** kalır ve yalnız `could_not_fit` olarak raporlanır. Overlay PDF'i gözle tarayın; `overlay_review.json`'daki her `could_not_fit` girdisinin kutusunda metin olup olmadığına bakın.
- **R7 (CR-95, ürün kararı).** Ortak ölçek artık **varsayılan açık**. Karışık uzunlukta paragrafları olan sayfalarda tüm gövde tek puntoya iner ve bu punto en sıkışık paragrafa göre belirlenir; 1,0'da sığan paragraf %34'e kadar küçülebilir ve inceleme listesine **girmez**. `--overlay-uniform-scale` ve `--no-overlay-uniform-scale` çıktılarını yan yana koyup kullanıcıya hangisinin tercih edildiğini sorun.
- **R8 (CR-96).** `uniform` modda satır aralığı 1,05'e kadar sıkışabiliyor (kaynak ~1,26). Yoğun sayfalarda okunabilirliği gözle değerlendirin.
- **R9 (CR-87/88).** Çok şekilli büyük kitap (ör. 100+ çevrilmiş şekil) ile `export` süresi ve belleği. Bugün her şekil için kaynak PDF baştan okunuyor.
- **R10 (CR-89).** `export --no-overlay-uniform-scale` sonrası `.tr.png` etiketlerinin hâlâ grup ölçeğinde olduğunu doğrulayın.
- **R11 (CR-93).** Uzun `extract` / 300 sayfalık overlay export sırasında ikinci bir kabuktan `status`; `logs/*.jsonl` içinde `lease_keepalive_failed` ve beklenmedik exit 1 arayın (iki kalp atışı aynı satıra yazıyor).
- **R12 (CR-97).** `.tr.png` içindeki "2." gibi sıra numarası öneklerinin kalın gelmediğini, overlay PDF'te kalın geldiğini görsel olarak teyit edin; kullanıcı bunu kabul ediyor mu?
- **R13 (CR-51 kanıt boşluğu).** `v1_1_final/` içinde `summary.json` yok. QA canlı bir `run --mode overlay` sonrası `summary.json` ile `overlay_review.json` sayılarının eşleştiğini **ilk kez** doğrulamalı; ayrıca `summary.json` yokken tek başına `export`'un hiçbir şey yazmadığını (`orchestrator.py:3229-3230`) not edin.
- **R14 (CR-49, değişmedi).** ~250 sayfadan büyük kitabı overlay modunda koşmadan önce `find_tables` tekrarı düzeltilmeli; kalp atışı kira süresini artık korusa da süre iki katına yakın.
- **R15 (CR-20, değişmedi).** Aksanlı özel adlar içeren kaynak: sözlük keşfi hâlâ ASCII-only (`glossary/discovery.py:46-47`); "Müller" düşer, "José" → "Jos". Overlay birim-bazlı sözlük kontrolü de bu kesik anahtarları kullanıyor.

Önceki listedeki **A (sağlayıcı / canlı DeepL)**, **D (resume, çökme, iptal)** ve **H (state, şema, güvenlik)** blokları ile 22-25, 31-33, 35-38, 40-45 maddeleri **olduğu gibi geçerlidir**.

## Onay Koşulu (third pass)

1. **CR-83 / YR-1 (Critical) QA'ya geçmeden düzeltilmeli.** Fixture'lar: (a) gövde metni olmayan, alt yazı + 30 kısa etiketli tam sayfa diyagram → **1 şekil, `figure_page_dropped` notu yok** (bugün 0 şekil; mevcut `tests/test_figures.py:610` yalnız kuralın *ateşlenmesini* sabitliyor, ateşlenmemesi gereken durumu değil, ve `tests/conftest.py:536` `full_page_figure` sadece alt yazı içerdiği için eşiğin altında kalıyor); (b) gerçek OCR sandviç → şekil düşer. Ayrıca `docs/test_doc.pdf` 2. sayfasının oranını (0,373 / sınır 0,40; 335 karakter / taban 400) sabitleyen bir regresyon testi — referans sayfanın sınıra ne kadar yakın olduğu görünür kalmalı.
2. **CR-91 ve CR-92 aynı turda düzeltilmeli** — ikisi de kullanıcının elindeki gerçek dosyalarla raporlanan durumu ayrıştırıyor ve ikisi de küçük değişiklikler.
3. **CR-84, CR-85, CR-86 aynı turda** — üçü de CR-38 düzeltmesinin aynı ailesinden ve üçü de "sessiz kayıp" sınıfında; en azından reddin `notes`'a düşmesi (CR-85/86) tek satırlık iş.
4. **CR-87 + CR-88 birlikte** yapılmalı; CR-88 düzeltilmeden CR-87 uygulanırsa bozuk PNG riski doğar. CR-89 aynı değişikliğin parçası.
5. **CR-90, CR-93, CR-94, CR-98** bu turda veya bilet açılarak; CR-93 uzun koşulardan önce.
6. CR-95 / CR-96 **kullanıcı kararı**: ortak ölçeğin varsayılan açık kalması ve 1,05 satır aralığı bir ürün tercihi — onaylanırsa README'de netleşsin, onaylanmazsa CR-76'nın özgün önerisine dönülsün.
7. **Doküman ve durum kaydı düzeltmeleri aynı PR'da** (CR-103, YR-6/YR-7): (a) `durum.md` "bilinen açık 1"in hizalama kısmı yanlış — `prefix_style` olarak düzeltilmeli; (b) `SHARED_SPREAD` orkestratörde değil `_placement.py:46`'da; (c) CR-61 "düzeltildi" listesinden çıkarılmalı; (d) CR-48 koşulsuz "yapıldı" listesinden çıkarılmalı (event adları hizalanmadı); (e) `durum.md:41`'deki "`LabelReplacement` aynası" ve "pdfkit A/B yakın-kopyalar" satırları artık yanlış (temizlendiler), "`pdf-overlay` exporters/base'de kayıtlı değil" doğru kalıyor; (f) **CR-38'in yeni kuralları hiçbir tasarım dokümanında yok** — doc 06 §4.1 adım listesine arka plan rasteri, prose-küme reddi ve sayfa güvenlik ağı eklenmeli, `figure_page_dropped:<page>:text_heavy` uyarısı README'de açıklanmalı (kullanıcı bu uyarıyı gördüğünde bugün ne demek olduğunu bulamaz); `SHARED_SPREAD`, grup anahtarı ve `LINE_HEIGHT_TIGHT` de belgelenmeli.
8. Suggestion'lar (CR-99 … CR-113) biletlenebilir; **CR-100** v2 yayına çıkmadan yapılmalı (sonra tabloyu yeniden kurmak gerekir).
9. Düzeltmeler sonrası **tam yeniden inceleme gerekmez**; CR-83/84/85/86 ve CR-91/92 için dar bir doğrulama yeterlidir. QA, R1-R6 dışındaki maddelerle paralel başlayabilir.

---
✅ CODE REVIEWER tamamlandı.
➡️  Sonraki adım: QA ENGINEER

---

# Third pass — fix round (2026-09-21)

Applied by the orchestrator in three parallel arms (P1 figure detection/render, P2 placement/label style, Q orchestrator/CLI) plus a consolidation pass. Verification after consolidation: **721 passed / 1 skipped**, `mypy src` clean (60 files), `ruff check src tests` clean.

## Closed

| Finding | Severity | What was done | Evidence |
|---|---|---|---|
| CR-83 | Critical | Page safety net re-based on absorbed **prose** (not all page characters) plus a second "text page" trigger on fill ratio; full-page label-dense figures survive | `src/extractors/figures.py` `_page_text_split`, `_is_label_line`, net in `detect_figures`; `tests/test_cr83_figures.py`, `tests/test_figures.py` (`..._keeps_a_label_dense_full_page_figure`, `..._still_drops_a_scanned_column...`) |
| CR-84 | Warning | `_is_prose_cluster` mixed rule now needs prose > 2× labels **and** ≥ 2 multi-line blocks | `src/extractors/figures.py`; `test_a_diagram_with_one_annotation_box_stays_a_figure`, `test_two_prose_blocks_inside_a_candidate_still_reject_it` |
| CR-85 | Warning | Rejections reach the job warnings (`figure_candidate_rejected:<page>:<reason>`), de-duplicated per page | `src/extractors/figures.py`; `test_background_image_rejection_reaches_the_caller` |
| CR-86 | Warning | `detect_figure_regions(..., notes)`; the overlay pass passes its own warning list, so the reason no longer dies with a throwaway context | `src/extractors/pymupdf_extractor.py`, `src/extractors/overlay_extractor.py` |
| CR-87 | Warning | Orchestrator builds one job list per document and makes a single `render_translated_figures` call — the source is opened once, not once per figure | `src/pipeline/orchestrator.py` `_translate_figures`; `test_a_truncated_or_failed_figure_keeps_its_legend` asserts `calls == [1]` |
| CR-88 | Warning | Each job renders on its own one-page copy (`pdfkit.doc_page_copy`), closed in `finally`; jobs on one page are independent | `src/exporters/figure_overlay.py` `_render_job`; `tests/test_cr88_pdfkit.py` |
| CR-89 | Warning | `uniform_scale` threaded CLI → `_export_reflow` → `_translate_figures` → batch → single-figure wrappers | `src/pipeline/orchestrator.py`, `src/exporters/figure_overlay.py` |
| CR-91 | Warning | Exporter loop no longer returns on the first irreducible error: `export_error:<format>:<msg>` warning, written outputs reported, `summary.json` updated, exit = the failure's own code (1). No output at all → bare `Err` as before (CR-63) | `src/pipeline/orchestrator.py` `_export_reflow`, `src/cli.py` `_warning_line`; `test_a_failed_format_keeps_the_formats_already_on_disk`, `test_a_failed_format_is_printed_as_an_error_next_to_the_files_written` |
| CR-92 | Warning | Hash-mismatch branch split: exit 5 only when the overlay format is requested; otherwise the source is not read, md/epub are written, figure step skipped with `figure_translate_failed:input_changed:<name>`, exit 2. `--input` at a wrong-but-existing file behaves the same | `src/pipeline/orchestrator.py` `_verify_source`/`_SourceCheck`; `test_reflow_..._degrades_on_a_changed_source_and_survives_a_missing_one`, `test_a_changed_source_pdf_still_exports_md_through_the_cli` |
| CR-95 | Warning | **User decision.** Per-unit cap `SHARED_MAX_DROP = 0.10`; group of fewer than two falls back to natural scales; page-level informational record (`PAGE_SHARED_SCALE`) when the shared factor < 0.9. **Plus** `SHARED_SPREAD` 0.15 → 0.10 after the cap alone made the reference page worse (5 body sizes); measured result: 2 sizes, cap never fires, `could_not_fit = 0`, `shrunk = 0` | `src/exporters/_placement.py`, `src/exporters/pdf_overlay_exporter.py`; `test_shared_scale_cap_keeps_a_comfortable_unit_at_its_own_scale`, `test_the_narrow_spread_keeps_the_crowded_units_out_of_the_group`, `test_low_shared_scale_is_recorded_at_page_level`, `test_test_doc_page_one_body_units_are_never_shrunk_past_the_cap` |
| CR-96 | Warning | **User decision.** `LINE_HEIGHT_TIGHT` (1.05) removed; line height derived from `line_boxes` and clipped to `[1.15, 1.40]`, never tightened below the band. Reference page derives 1.274 / 1.292 against a ~1.26 source rhythm | `src/exporters/_placement.py` `source_line_height`; `test_source_line_height_is_derived_from_the_line_boxes_and_clipped`, `test_a_block_is_never_tightened_below_the_line_height_band` |
| CR-97 | Warning | `prefix_style` carried end to end (`LabelBox` → `figures.json` → `LabelBoxEntry` → `LabelReplacement` → `PlacementSpec`); old inventories still load | `src/domain/models.py`, `src/domain/overlay.py`, `src/extractors/{pymupdf_extractor,figure_inventory}.py`, `src/exporters/figure_overlay.py`, `src/pipeline/figure_labels.py`; `test_translated_figure_png_keeps_the_bold_running_number`, `test_prefix_style_survives_the_figures_json_round_trip` |

## Corrections to the review's own findings

- **CR-97 / `durum.md` gap 1.** The claim that `.tr.png` labels are left-aligned was measured and **disproved** — centred labels are centred. Only the bold prefix was lost, and that is what was fixed.
- **CR-83 measurement.** The review recorded the reference page at 335 / 899 characters (ratio 0.373); the implementation's own measurement is 326 / 890 (0.366). The ~1 % difference does not change the finding (the page sat just under the old 0.40 threshold); the regression test pins the measured values.
- **CR-95.** The cap as specified was not sufficient on its own: it made the reference page worse (five body sizes instead of one). `SHARED_SPREAD` had to be narrowed with it. This is recorded because the review proposed the cap alone.

## Deliberately not done in this round

- **CR-85, rule half.** Only the *visibility* of the 60 % raster rule's rejection was addressed; the rule itself (whether the absorbed lines are label-like) is unchanged.
- **Still NOT DONE** from the third pass: CR-65 (loader side), CR-66(b), CR-69, CR-71, CR-72, CR-73, CR-98.
- **Still PARTIAL:** CR-44 (minimal shield, accepted), CR-48 (event names not aligned), CR-49 (`find_tables` repeat, accepted — revisit before a ~250-page book), CR-67 (covered, exit code wrong), CR-77, CR-82.
- **Suggestions CR-99…CR-113** were not taken up.

## Open risk carried to QA

1. The new "text page" trigger (`_PAGE_TEXT_FILL_RATIO`, fill ≥ 50 %) is a geometric heuristic: a schematic whose labels cover more than half its own area could be dropped. Measured fills — reference p.2: 0.146, 40-label schematic: 0.19, bordered index page: 0.755.
2. `_PAGE_LABEL_MAX_CHARS = 40`: a label longer than 40 characters counts as prose.
3. `overlay_review.json` now carries page-level informational entries, so `counts.entries` and `summary.json`'s `overlay_review_entries` rise on affected pages; the CLI still prints the raw count.
4. The two crowded paragraphs of the reference page are placed at 8.50 pt against the body's 9.11 pt — intended, but it is a visible two-size page.

## Live verification and the follow-up to CR-95

`docs/test_doc_output/v1_1_third/` — full live run on `docs/test_doc.pdf`: reflow pass exit 0 (10/10 chunks, 8991 characters), overlay pass exit 0 (43/43 units, 3/3 pages), `placed 34 / shrunk 0 / could_not_fit 0 / kept 9` — identical to the previous round. `figures.json` carries `prefix_style: ["2.", true, false]`, `images/p002-f01.tr.png` was written and the English original kept.

**The cap as specified regressed the real output.** Measured distinct font sizes per page of the overlay PDF against the previous round:

| Page | Previous round | Cap as specified | After the follow-up |
|---|---|---|---|
| 1 | 4 | 5 | 5 |
| 2 | 3 | **9** | 4 |
| 3 | 3 | **6** | 4 |

Two causes, both fixed:

1. **Figure labels were capped too.** A diagram's labels went from one shared size to seven (8.86 / 9.96 / 10.12 / 11.46 / 11.50 / 11.77 / 12.00 pt). Label boxes vary far more in size than paragraphs, so the cap ejected most of them. `_placement.LABEL_MAX_DROP = 1.0` turns the cap off for a group of labels; `max_drop` is now resolvable per group key, and both label paths use it — `pdf_overlay_exporter._label_max_drop` (group key carries `OverlayBlockKind.FIGURE_LABEL`) and `figure_overlay` for the `.tr.png`.
2. **Ejected units were never re-grouped.** Each one landed on its own natural scale, so a page of prose came out at 10.33 / 10.45 / 10.50 pt — nearly identical sizes that read as sloppy. `_share_scales` now shares a style group in passes: `_share_one` forms at most one group and returns the leftovers, which are offered the same treatment among themselves until no progress is possible.

Page 1 still shows five sizes, and that is the user's decision working as intended: 1635 characters of body text stay at the **source size** (10.5 pt) instead of being pulled down to the crowded group's 9.44 pt. In the previous round all of it was placed at 9.5 pt.

Side effect: a page can now carry more than one `page_shared_scale` record (one per shared group below 0.9), so `counts.entries` rises further — 3 entries before the round, 7 after.

Final verification: **722 passed / 1 skipped**, `mypy src` clean (60 files), `ruff check src tests` clean.

---
✅ CODE REVIEWER + fix round tamamlandı.
➡️  Sonraki adım: QA ENGINEER
