# QA Plan — `book-translator` v1 + v1.1

Step 8 of the 8-step workflow. Input: `docs/01_business_analysis.md` (13 US / 27 FR / 15 NFR /
22 edge cases), `docs/05_change_request_v1_1_ba.md` (US-14…23 / FR-28…53 / NFR-16…32 /
E-23…57), `docs/04_code_review.md` (three passes + fix round), `docs/06_solution_design_v1_1.md`
§7.3, `docs/durum.md`, `README.md`.

Author: QA Engineer · Date: 2026-09-21 · Build under test: working tree at
`docs/durum.md` "3. tur düzeltme turu + matematik/redaksiyon düzeltmeleri".

---

## Türkçe Özet (kullanıcı için)

Kodu okuyarak ve komutları gerçekten çalıştırarak test ettim. Sağlayıcıya (DeepL) **tek karakter
göndermedim** — sadece `extract`, `glossary`, `--dry-run`, `status`, `export`, `providers` gibi
ücretsiz komutları kullandım.

**Doğrulama tabanı gerçekten temiz:** 749 test geçti / 1 atlandı, `mypy src` 60 dosyada temiz,
`ruff check src tests` temiz. Bunları kendim koşturdum.

**Ama bir sürüm engelleyici (Blocker) buldum.** Eski bir çıktı klasörünü (şema v2 — yani
matematik düzeltmesinden önce üretilmiş her overlay klasörü) yeni sürümle açtığınızda, otomatik
şema göçü `redact_bbox` kolonuna **kolonun adını** yazıyor. Sonrasında `export --format
pdf-overlay` "state_corrupt" diyerek exit 1 veriyor. Yani 48 sayfalık kitabınızın klasörü
(`docs/test2_output/`, 1102 overlay birimi, hâlâ v2) bir kez yazma komutu çalıştırdığınız anda
kullanılamaz hâle gelecek. Ödenmiş çeviriler veritabanında duruyor (kaybolmuyor) ve
`state-archive/<zaman>-pre-migrate-v2/` yedeği de alınıyor — ama araç kendi dosyasını
okuyamıyor. Tek satırlık bir düzeltmesi var.

Üç küçük hata daha var: `status --json` overlay sayılarını ikiye katlıyor; en riskli alanın
(matematik bandı + redaksiyon kutusu) regresyon testleri depoda olmayan PDF'lere bağlı, yani
temiz bir kopyada 60 test düşüyor; şema göçü kullanıcıya hiç haber verilmeden yapılıyor.

**Kararım: KOŞULLU GEÇİŞ (CONDITIONAL GO)** — Blocker düzeltilip göç bir v2 dosyasıyla
doğrulandıktan sonra sürüm alınabilir. Ayrıntılar §7'de.

---

## 1. Scope and strategy

### 1.1 What this plan covers

One-off QA of **v1 and v1.1 together**, after Code Reviewer's third pass closed with
*APPROVED WITH CHANGES* and the fix round was applied. The tool is a local, single-user CLI, so
the test pyramid is: many unit tests (already 749), a thin integration layer driven through the
real CLI, and manual scenarios for everything that needs a real PDF, a real provider, or a human
eye on a page.

| Group | Scenarios | IDs | Automated today | Gap (→ §3) |
|---|---|---|---|---|
| A Smoke and environment | 3 | TC-01…03 | n/a | — |
| B Reflow pass, end to end | 9 | TC-04…12 | strong | small |
| C Overlay pass, end to end | 11 | TC-13…23 | strong | shared-scale visuals |
| D Two passes in one directory | 3 | TC-24…26 | good | — |
| E Resume, interrupt, crash, cost safety | 8 | TC-27…34 | medium | SIGKILL, real Ctrl+C |
| F Source-file binding | 3 | TC-35…37 | strong | — |
| G Dry run, status, JSON output | 6 | TC-38…43 | medium | count parity |
| H Glossary | 7 | TC-44…50 | strong | live provider behaviour |
| I Error paths and exit codes | 13 | TC-51…63 | strong | quota / auth / 429 |
| J Schema migration | 6 | TC-64…69 | **weak — a real defect slipped through** | v2-with-rows |
| K Non-functional spot checks | 6 | TC-70…75 | none | all manual |
| **Total** | **75** | | | |

The regression of the four real-world fixes is not a separate group: it is TC-16, TC-17, TC-66
and the R-1…R-5 checklist in §4.2.

### 1.2 What this plan does NOT cover, and why

* **Live DeepL behaviour under load** (429 cascade, 456 quota, 401 auth, glossary upload/reuse,
  `tag_handling="xml"` with `<`, `>`, `&`). The user's monthly quota is limited and this QA pass
  was explicitly forbidden from spending it. Every such scenario below is marked
  **[QUOTA]** and must be run once, deliberately, on a small PDF before release. Code review
  findings CR-08 / CR-09 / CR-10 have been deferred three times on exactly this basis; they
  cannot be closed from the desk.
* **Books over ~250 pages in overlay mode.** CR-49 (`find_tables` runs 2–3× per page) is still
  open and the review's approval condition says this must be fixed first. Scenarios TC-66/67
  describe the measurement to run after that fix.
* **Mathematics typeset in text fonts (`CMR`/`CMBX`).** Documented as a limitation in the README
  and accepted by the user; not a defect.
* **Non-English sources, language pairs other than EN→TR, EPUB/DOCX input, OCR** — out of scope
  per `docs/01_business_analysis.md` §8.
* **Multi-machine / concurrent-writer stress beyond two shells.** A-4 assumption.

### 1.3 Environment for every scenario unless stated otherwise

```
OS              Windows 11, PowerShell 5.1 and cmd.exe (both, for TC-60)
Python          .venv\Scripts\python.exe  (3.11.9)
Invocation      .venv\Scripts\python.exe -m book_translator.cli <command>
                (or the `book-translator` console script)
SQLite          3.45.1
Provider key    .env next to the working directory, DEEPL_API_KEY set
Fixtures        docs/test_doc.pdf       3 pages, prose + 1 vector figure with labels
                docs/test-2-math.pdf    4 pages, display mathematics
                docs/test-2.pdf        48 pages, LaTeX/Computer Modern academic text
```

`--dry-run` is safe: `Orchestrator._translate_bound` skips `translator.prepare()` when
`dry_run` is set (`src/pipeline/orchestrator.py:1747`) and returns before any send
(`:1824`), so no glossary is uploaded and no key is validated. **A green `--dry-run` therefore
does not prove the API key works** — use `providers` for that.

`export` never contacts the provider either: `_translate_figures`
(`src/pipeline/orchestrator.py:3490`) only *paints* labels that `translate` already paid for.

### 1.4 Verification baseline (run for this plan, 2026-09-21)

```
$ .venv/Scripts/python.exe -m pytest -q
749 passed, 1 skipped in 138.67s (0:02:18)

$ .venv/Scripts/python.exe -m mypy src
Success: no issues found in 60 source files

$ .venv/Scripts/python.exe -m ruff check src tests
All checks passed!
```

The single skip is the sample-DB test noted in `docs/durum.md`.

### 1.5 Exit code contract under test

From `src/pipeline/orchestrator.py:209-252` and doc 06 §7.3:

| Code | Meaning | `ErrorCode`s mapped |
|---|---|---|
| 0 | success (a non-empty review list is still 0) | — |
| 1 | error: bad input, bad option, unusable content, no output produced | everything unmapped |
| 2 | partial output | `EXPORT_REFUSED_PARTIAL`, `EXPORTER_UNAVAILABLE`, `could_not_fit`, skipped pages, `--allow-partial`, changed source in reflow |
| 3 | paused (quota / auth) | `PROVIDER_QUOTA`, `PROVIDER_AUTH`, `GLOSSARY_BIND_FAILED` |
| 4 | state locked by another process | `STATE_LOCKED` |
| 5 | state mismatch | `STATE_HASH_MISMATCH`, `STATE_PROVIDER_MISMATCH`, `STATE_SCHEMA_INCOMPATIBLE` |
| 130 | interrupted | `INTERRUPTED` |

`worst_exit()` orders severity `1 > 3 > 4 > 5 > 130 > 2 > 0`, so a run that both failed a format
and downgraded another reports 1.

---

## 2. Manual test scenarios

Legend: **P0** release blocker · **P1** must pass before release · **P2** should pass · **P3**
nice to have. **[QUOTA]** = spends DeepL characters. **[VERIFIED]** = I executed it while
writing this plan and the "Actual" line is the real output.

### 2.A Smoke and environment

#### TC-01 — Provider, extractor and exporter inventory · P1 · [VERIFIED]
*Refs* FR-13, FR-23, NFR-09, NFR-14
**Pre** `.env` holds a valid `DEEPL_API_KEY`.
**Steps** `providers` · `extractors` · `exporters`
**Expected** `deepl available=True` with `glossary=native, max_chars=100000`; `local`
unavailable with an install hint; `pymupdf` available, `marker` unavailable with a hint;
`md`/`epub`/`pdf-overlay` available, `pdf` available with
`engines: native (available), weasyprint (unavailable)`. **Exit 0.**
**Actual** exactly as expected.

#### TC-02 — `--help` is the only documentation a first-time user needs · P2
*Refs* NFR-14
**Steps** `--help`, then `run --help`, `translate --help`, `export --help`.
**Expected** Every option carries a default and (where applicable) its
`BOOK_TRANSLATOR_*` env var. **Exit 0.**
**Note (finding)** `--fresh` exists on `extract` and `run` **only**. There is no CLI way to
archive just the translation state while keeping the extraction; `Orchestrator.translate(fresh=True)`
exists in code but is not exposed. Confirm this is intended, or document it.

#### TC-03 — Log file cannot be written · P2 · [VERIFIED]
*Refs* NFR-11
**Steps** `extract -i docs/test_doc.pdf -o <a directory you cannot write to>`
**Expected** `warning log file not writable: [WinError 5] ...`, the command still runs and
reports its real error. **Exit 1** (for the input error, not for the log).
**Actual** as expected — logging never becomes fatal.

### 2.B Reflow pass, end to end

#### TC-04 — Extraction of a digital PDF · P0 · [VERIFIED]
*Refs* US-1, FR-02...FR-05, E-02, E-03
**Pre** empty output dir.
**Steps** `extract -i docs/test_doc.pdf -o out`
**Expected** `source_book.md`, `source_profile.json`, `figures.json`, `images/` written; console
reports pages, skipped pages, extractor, detected language, figure totals. **Exit 0.**
**Actual** `extracted 3 pages (0 skipped) with pymupdf` · `language: en (0.39), images: 1` ·
`figures: 1 (vector 1, raster 0, 128 KB, largest p002-f01.png)` · source profile
`595.28x790.87 pt, body 10.5 pt, margins (43.67, 58.54, 73.23, 57.19)`.

#### TC-05 — Glossary discovery on the extracted text · P1 · [VERIFIED]
*Refs* US-3, FR-08, E-11
**Steps** `glossary -o out`
**Expected** `glossary.json` written, counts printed by status class. **Exit 0.**
**Actual** `entries: 0 (approved 0, proposed 0, ambiguous 0, rejected 0)` on the 3-page fixture.
Re-run on a longer book (`docs/test-2.pdf`) and check that `ambiguous` entries are **not**
auto-approved (E-11) and that a `--min-count` change moves the counts.

#### TC-06 — Translate, reflow, happy path · P0 · **[QUOTA]**
*Refs* US-6...US-10, FR-11, FR-14, FR-16, FR-19, FR-27
**Pre** TC-04 done; `--dry-run` first (TC-38) to know the cost.
**Steps** `translate -i docs/test_doc.pdf -o out`
**Expected** every chunk COMPLETED, `summary.json` written with `chars_sent_this_run`,
`provider_calls`, duration and outputs; `logs/run-<id>.jsonl` holds one `chunk_completed` per
chunk. No API key anywhere in the log, summary or DB (TC-63). **Exit 0.**

#### TC-07 — Export md + epub + pdf · P0 · [VERIFIED]
*Refs* US-10, FR-19, FR-20, FR-21, FR-36, FR-50, E-15
**Pre** a completed reflow pass.
**Steps** `export -o out -f md -f epub -f pdf`
**Expected** three files; EPUB has `lang="tr"` and a heading-derived ToC; the native PDF uses
the source page design; `images/pNNN-fKK.tr.png` written and referenced only from the
*translated* documents. **Exit 0.**
**Actual** `wrote translated_book.md (10166 bytes)` · `wrote translated_book.epub (106329
bytes)` · `pdf engine: native (page source)` · `wrote translated_book.pdf (151634 bytes)` ·
`figure p002-f01.png translated into p002-f01.tr.png (15 labels)` · warning
`chapter_split_fallback:h2` · **exit 0**.

#### TC-08 — EPUB opens in a real reader · P1
*Refs* NFR-13, E-15, CR-34 regression
**Steps** Open `translated_book.epub` in Calibre and in Thorium.
**Expected** Turkish characters render correctly; the ToC matches the headings; a paragraph that
starts `3. Bölüm ...` is a **paragraph**, not an `<ol>` item (CR-34 regression — check the XHTML
for `<ol start="3">`); figure images display.

#### TC-09 — Reflow PDF page design comes from the source · P2
*Refs* Addendum A.3, FR-50, CR-65, CR-66
**Steps** `export -o out -f pdf` with the default `--pdf-page-size source --pdf-margin source`;
then with `--pdf-page-size A4 --pdf-margin "18mm 15mm"`.
**Expected** First run's page rectangle equals `source_profile.json`; second run overrides it.
**Known gap (CR-66b)** the measured top margin counts the running header, so the body box sits
higher than the source. Record the delta; it is cosmetic, not text loss.

#### TC-10 — Hand-edited `source_book.md` is respected · P1
*Refs* O1, CR-06 regression
**Steps** After `extract`, edit a heading in `source_book.md`; run `translate --dry-run`; then
`translate`. Then run `extract` again.
**Expected** With `completed == 0` the edited file is re-chunked and used. Re-running `extract`
after completed chunks exist gives **exit 5** pointing at `--fresh`; the edited file is archived
to `state-archive/<stamp>/source_book.md` with a warning, never silently overwritten.

#### TC-11 — Code blocks, tables and inline code survive · P1
*Refs* FR-12, E-05, E-06, CR-35, CR-58
**Pre** a `source_book.md` containing a fenced code block with at least 2 consecutive blank
lines, an indented code block, a table, and a paragraph longer than the provider limit.
**Steps** `translate --dry-run`, then inspect the chunk boundaries; after a paid run, diff the
code block in `translated_book.md` against the source.
**Expected** Code identical byte for byte; no chunk splits a fence, table or heading; the
oversize paragraph is split at sentence boundaries and rejoined.

#### TC-12 — `--allow-partial` marks failed chunks visibly · P1
*Refs* FR-22, E-16
**Pre** a job with at least one FAILED chunk (force it with an invalid key mid-run, or
`UPDATE chunks SET status='FAILED'`).
**Steps** `export -o out -f md -f epub`, then the same with `--allow-partial`.
**Expected** Without the flag: **exit 1**, `EXPORT_REFUSED_PARTIAL`, nothing written. With it:
the failed chunk appears between `[UNTRANSLATED — chunk {id} — {reason}]` and
`[END UNTRANSLATED — chunk {id}]` marker blocks around the English source, the EPUB shows it as
a highlighted block with `<` and `&` escaped. **Exit 2.**

### 2.C Overlay pass, end to end

#### TC-13 — Overlay dry run reports units and characters · P0 · [VERIFIED]
*Refs* FR-41, NFR-23
**Steps** `translate -i docs/test_doc.pdf -o out --mode overlay --dry-run`
**Expected** unit counts and payload characters, nothing sent. **Exit 0.**
**Actual** `units: 0/43 completed ... 43 pending` · `overlay: 0/43 units, 0/3 pages, status
OVERLAY_EXTRACTED, 9 kept as-is` · `dry_run: 34 overlay units pending, 8951 payload characters`.
Against the reflow dry run of the same book (`9 chunks, 8991 characters`) that is **-0.4 %** —
**NFR-23 (±10 %) holds** on this fixture. Measured on `docs/test-2.pdf`:
`556 overlay units pending, 123644 payload characters`.

#### TC-14 — Overlay translate and export · P0 · **[QUOTA]**
*Refs* US-18...US-21, FR-41...FR-48
**Steps** `run -i docs/test_doc.pdf -o out --mode overlay`
**Expected** `translated_book.overlay.pdf` + `overlay_review.json`; same page count and page
size as the source; images, drawings, links and bookmarks untouched; **exit 0** even with a
non-empty review list (Q21).
**Reference values** (`docs/test_doc_output/v1_1_third/`): 43/43 units, 3/3 pages,
`placed 34 / shrunk 0 / could_not_fit 0 / kept 9`.

#### TC-15 — Overlay export from an existing pass · P0 · [VERIFIED]
*Refs* FR-49
**Pre** a completed overlay pass in `out`.
**Steps** `export -o out -f pdf-overlay`
**Expected** the PDF is rebuilt without touching the provider; placement counts printed.
**Exit 0.**
**Actual** `placed 34, shrunk below threshold 0, could not fit 0, kept original 9, review
entries 7` — identical to the reference run. **Exit 0.**

#### TC-16 — No residual English glyphs · P0
*Refs* E-39, and the 2026-09-21 `redact_bbox` fix
**Steps** Render `translated_book.overlay.pdf` at 120 dpi; for every replaced rectangle extract
the text inside it and assert no source substring remains. Then look specifically at the strip
*above* a block whose rectangle was shortened against a neighbour.
**Expected** No English left over. On `docs/test-2-math.pdf` page 1 (book p. 404) the strings
`corresponding current` and `least squares and Appendix` must **not** appear — they did before
the `redact_bbox` fix.

#### TC-17 — Display equations survive · P0
*Refs* the 2026-09-21 math-band fix, E-29
**Steps** Overlay-export `docs/test-2-math.pdf`; open pages 1-4.
**Expected** Equations (8.4)-(8.9) and (8.39)-(8.42) print with their brackets, subscripts and
**equation numbers**. Review entries on those 4 pages: `collateral_redaction` around 18,
`fragment` around 48, `could_not_fit` 0, total around 77 (before the fix: 90 / 82 / 1 / 179).
**Accepted cost** ~0.7 % of prose lines whose vertical centre falls inside a band stay English
(17 of 2560 measured on the 48-page sample).

#### TC-18 — Running headers stay English by default · P2
*Refs* FR-46
**Steps** Overlay-export once without and once with `--overlay-translate-headers`.
**Expected** Default: header/footer text unchanged, page numbers never sent. With the flag:
headers translated, page numbers still untouched. Changing the flag on a **started** pass must
be **exit 5** (`STATE_HASH_MISMATCH` on the unit-set signature).

#### TC-19 — Shared per-page font scale is a product decision · P1
*Refs* CR-95, CR-96, R7, R8
**Steps** Export the same overlay pass twice: `--overlay-uniform-scale` (default) and
`--no-overlay-uniform-scale`. Put the pages side by side.
**Expected** Both produce a readable page. Record distinct font sizes per page (reference doc:
5 / 4 / 4 with uniform scale) and the derived line height (1.274 / 1.292 against a ~1.26
source rhythm). **Ask the user which they prefer** — this is a preference, not a pass/fail.
**Watch for** a paragraph that fits at scale 1.0 being pulled down with the crowded group; it is
capped at 10 % (`SHARED_MAX_DROP`) and recorded as `page_shared_scale` in the review list, so
`overlay_review.json` grows by one entry per shared group below 0.9.

#### TC-20 — Figure labels are painted into a copy · P1 · [VERIFIED]
*Refs* Addendum A.2, FR-35, CR-97
**Steps** After a reflow export, inspect `images/`.
**Expected** `pNNN-fKK.png` (English, untouched) **and** `pNNN-fKK.tr.png`; `source_book.md` and
`figures.json` unchanged; only the translated documents point at `.tr.png`; a bold running
number such as `2.` stays bold (`prefix_style` in `figures.json`).
**Actual** `figure p002-f01.png translated into p002-f01.tr.png (15 labels)`; both files present.

#### TC-21 — A figure whose labels could not be painted keeps its legend · P1
*Refs* CR-57, FR-35
**Steps** Make one label untranslatable, or run with a wrong `--input` (TC-35b).
**Expected** warning `figure_labels_not_painted:N` (and/or `figure_label_truncated:N:k`); the
legend list stays in the document so no paid translation is lost.
**Actual (via TC-35b)** `figure_labels_not_painted:1` appeared as expected.

#### TC-22 — Label-dense full-page diagram is kept · P1
*Refs* CR-83, R1
**Steps** Extract a page that is one full-page schematic with 30 or more short labels and a
short caption, no prose.
**Expected** **1 figure** in `figures.json`, **no** `figure_page_dropped:<page>:text_heavy`
warning. Conversely a scanned/OCR page (full-page image under a text layer) **must** drop.
**Measured fill ratios** reference p.2 = 0.146 · 40-label schematic = 0.19 · bordered index page
= 0.755; the trigger is >= 0.50, so a schematic whose labels cover more than half its own area
would be dropped — hunt for one. Also note `_PAGE_LABEL_MAX_CHARS = 40`: a label longer than 40
characters counts as prose.

#### TC-23 — Rejections are visible · P2
*Refs* CR-85, CR-86, R2, R3
**Steps** Run `extract`, and separately `translate --mode overlay --dry-run` in a clean
directory with no `figures.json`.
**Expected** `figure_candidate_rejected:<page>:<reason>` reaches the job warnings on **both**
paths (the overlay path passes its own `notes` list). Compare the two.

### 2.D Two passes in one output directory

#### TC-24 — Reflow and overlay coexist · P1 · [VERIFIED]
*Refs* E-51, Addendum A.1, Q14
**Steps** In one directory run a reflow pass and an overlay pass, then `status`, then `export`
with no `--format`.
**Expected** `status` shows both (`counts` for chunks, `overlay` for units); `export` without
`--format` writes `md,epub` for a reflow pass and `pdf-overlay` for an overlay pass.
**Actual** on a directory holding both, `export` with no format produced the overlay PDF,
exit 0.

#### TC-25 — Default-format rule when only one pass exists · P2 · [VERIFIED]
*Refs* FR-49
**Steps** In an overlay-only directory: `export -f md`. In a reflow-only directory:
`export -f pdf-overlay`.
**Expected** A clear refusal naming the missing pass. **Exit 1**
(`OVERLAY_STATE_MISSING` / "run `translate` first").
**Actual (empty dir)** `...; run 'translate' first`, **exit 1** for both `-f md` and
`-f pdf-overlay`.

#### TC-26 — `run --mode overlay --format md` · P2
*Refs* FR-49
**Expected** Refused **before extraction** (exit 1) when there is no reflow pass — no wasted
work, no provider contact.

### 2.E Resume, interrupt, crash, cost safety

#### TC-27 — Resume never re-sends a completed unit · P0 · **[QUOTA]**
*Refs* US-8, FR-17, NFR-03, E-09
**Steps** `translate --limit 5`; note `provider_calls` and `chars_sent_total`. Run `translate`
again without `--limit`.
**Expected** The second run bills only the remaining units. `claim_select`
(`src/database/repository.py:520`) filters on `status == PENDING` only, so a COMPLETED row can
never be selected. Cross-check `runs.provider_calls` against completed chunks.
**Reference** the 3-page live run: a second `run` sent **0 characters**.

#### TC-28 — First Ctrl+C is graceful · P0
*Refs* E-09, NFR-02, CR-07, CR-31
**Steps** Start `translate` on a book big enough to take a minute; press Ctrl+C once during a
request.
**Expected** log line `finishing N in-flight requests (grace 30s); press Ctrl+C again to abort`;
in-flight completions are written; **exit 130**; `runs.outcome = INTERRUPTED`; the next run
re-queues nothing that completed. With `--grace-seconds 120` the grace is capped at
`lease_stale_s - 10` = 50 s and a `grace_capped` warning appears.

#### TC-29 — Second Ctrl+C aborts · P1
**Expected** Tasks cancelled, their rows released back to PENDING, **exit 130**. Note that the
process may still wait for the HTTP timeout (CR-31) — record how long.

#### TC-30 — Ctrl+C during `extract` / `glossary` / `export` · P1
**Expected** **Exit 130**, lease released, run row `INTERRUPTED`, no half-written artifact
(exports are temp-file + rename).

#### TC-31 — Hard kill mid-run · P0
*Refs* NFR-02, E-09, CR-44
**Steps** `taskkill /F /PID <pid>` while a batch is completing. Restart `translate`.
**Expected** `re-queued N units left in PROCESSING by a previous run`; state DB consistent; at
most **one batch** of units is re-billed (CR-44 accepted risk, up to 50 paid units in overlay
mode). Measure the actual re-billed characters and record them.

#### TC-32 — Two shells on one output directory · P1
*Refs* E-20
**Steps** Start a long `translate`; from a second shell run `translate` on the same `-o`.
**Expected** **Exit 4** with
`another process holds the state lock (pid ..., run ..., last heartbeat ...)`. Then kill the
first process and retry: still exit 4 until 60 s (`lease_stale_s`) pass, or immediately with
`--force-unlock`.

#### TC-33 — `status` from a second shell during a long run · P2
*Refs* CR-18, CR-93, R11
**Steps** During a long `extract` and during a 300-page overlay export, run `status` from
another shell.
**Expected** Read-only `status` works, never blocks, never migrates. Grep `logs/*.jsonl` for
`lease_keepalive_failed` and for an unexplained exit 1 (two heartbeats writing the same row —
CR-93).

#### TC-34 — State write failure during `translate` · P1
*Refs* CR-03
**Steps** Make `translation_state.db` read-only, or fill the disk, mid-run.
**Expected** **Exit 1** with a `state_error:` warning in the summary — **never exit 0**.

### 2.F Source-file binding

#### TC-35 — `export --input` matrix · P0 · [VERIFIED]
*Refs* CR-39, CR-92, doc 06 §7.1 and §7.3

| # | Situation | Formats | Expected | Actual |
|---|---|---|---|---|
| a | source moved, `--input` points at the very same file | any | success, exit 0 | not run |
| b | `--input` points at a *different but existing* PDF | `md`,`epub` | md+epub written, warning `figure_translate_failed:input_changed:<name>`, legends kept, **exit 2** | **exit 2**, both files written, warning present plus `figure_labels_not_painted:1` |
| c | `--input` points at a different PDF | `pdf-overlay` | nothing written, **exit 5** | **exit 5**, `state_hash_mismatch ... nothing was written` |
| d | `--input` points at a missing file | any | **exit 1** `input_not_found` | **exit 1** |
| e | source simply missing, no `--input` | `md`,`epub` | exit 0, `figure_translate_failed:input_missing`, legends kept | not run |
| f | export run from a different working directory | any | the stored absolute path is used | not run |

#### TC-36 — Swap the PDF before `translate --mode overlay` · P0
*Refs* FR-18, E-12
**Expected** **Exit 5**, **0 provider calls**, state archived not overwritten.

#### TC-37 — Provider switch on a started job · P2
*Refs* E-13
**Steps** `translate --provider local` on a job bound to `deepl`.
**Expected** **Exit 5** (`STATE_PROVIDER_MISMATCH`) unless `--allow-provider-switch`.

### 2.G Dry run, status, JSON output

#### TC-38 — `--dry-run` costs nothing and reports the bill · P0 · [VERIFIED]
*Refs* FR-27, NFR-03
**Steps** `translate -i docs/test_doc.pdf -o out --dry-run`
**Expected** `dry_run: N chunks pending, C payload characters would be sent to deepl`,
`chars sent: 0 this run, 0 total`, **exit 0**, and **no** `glossary_bound` event in the log
(the glossary is not uploaded on this path).
**Actual** `dry_run: 9 chunks pending, 8991 payload characters would be sent to deepl
(0 already completed)`, exit 0.
**Usability note** the counts line above it says `10 pending` — that is the chunk count
including the non-translatable image line, while the dry-run line counts only translatable
chunks. The two numbers sit next to each other and look contradictory; consider labelling them.

#### TC-39 — `--dry-run` does not validate the key · P2 · [VERIFIED by code]
**Steps** Put a garbage `DEEPL_API_KEY` in `.env`, run `translate --dry-run`.
**Expected** Still exit 0 — `translator.prepare()` is skipped
(`src/pipeline/orchestrator.py:1747`). Use `providers` to check the key. Make sure the README
says so.

#### TC-40 — `status` on a directory with no state · P2 · [VERIFIED]
**Expected** `no translation state at <path>`, **exit 0**; `--json` returns a well-formed object
with `"exists": false`, `"overlay": null`, `"mode": null`, `"ok": true`.
**Actual** as expected.

#### TC-41 — `status --json` on a finished overlay pass · P1 · [VERIFIED — **defect found**]
*Refs* CR-51, R13
**Steps** `status -o <overlay dir> --json`, then open `overlay_review.json`.
**Expected** the `overlay.review_counts` in `status` equal the `counts` block of
`overlay_review.json`.
**Actual — FAILS.** `status` adds the per-entry tally on top of the file's own counts for every
reason that appears in both, so the overlapping keys are **doubled**:

| Directory | `overlay_review.json` counts | `status --json` review_counts |
|---|---|---|
| `docs/test2_output` (48 pages) | `shrunk_below_threshold: 22`, `could_not_fit: 2` | `shrunk_below_threshold: 44`, `could_not_fit: 4` |
| `docs/test2_math_out` (4 pages) | `shrunk_below_threshold: 3`, `kept_original: 41` | `shrunk_below_threshold: 6`, `kept_original: 42` |

Root cause `src/pipeline/orchestrator.py:3810-3817` — the entry loop does
`counts[reason] = counts.get(reason, 0) + 1` into the dict already seeded from the file's
`counts`. See **BUG-02**.

#### TC-42 — `summary.json` matches the review list · P1 · [VERIFIED]
*Refs* CR-51
**Expected** `summary.json`'s `overlay` block equals `overlay_review.json`'s `counts`.
**Actual** they match exactly on `docs/test2_math_out` (placed 55, shrunk 3, could_not_fit 0,
kept_original 41). Only `status` is wrong. Note also that a standalone `export` in a directory
with no `summary.json` writes none (`orchestrator.py:3229`).

#### TC-43 — Non-TTY output · P2
*Refs* FR-24, NFR-11
**Steps** Pipe every command to a file, and run under a CI-like shell.
**Expected** no ESC bytes; progress throttled to one line per second or every 10th chunk;
`--json` on stdout only, human text on stderr; `--quiet` suppresses info; every failure also
appears as a `command_failed` line in `logs/run-*.jsonl`.

### 2.H Glossary

#### TC-44 — Ship-with glossary keeps terms in English · P1 · **[QUOTA]**
*Refs* README "Keeping technical terms in English", FR-09, FR-10, NFR-05
**Steps** Translate a page containing *computer vision*, *object detection*, *zero-shot
detection* twice: once without `--glossary` and once with
`--glossary glossaries/ai-ml.en-tr.json`.
**Expected** Without: "bilgisayar görme", "nesne algılama". With: the English terms survive,
with Turkish case suffixes attached by DeepL.
**How it works** both shipped files are pure identity glossaries (`target == source`:
`ai-ml.en-tr.json` 807/807 entries, `computer-vision.en-tr.json` 239/239). Identity pairs are
*dropped* before the native DeepL glossary is built
(`src/translators/deepl_translator.py:164-173`), so they instruct nothing — they only declare
"this must stay English" for the post-check. That means the effect depends entirely on DeepL's
own behaviour and **must** be measured live.

#### TC-45 — `glossary_miss:<term>` fires · P1 · **[QUOTA]**
*Refs* FR-10, NFR-05
**Expected** Every term whose source appears in the chunk (word-boundary, **case-sensitive**)
but whose target stem is absent from the translation produces `glossary_miss:<term>` in the
chunk warnings. The target side is matched Turkish-case-insensitively on the first
`max(4, len(target)-2)` characters (`src/pipeline/glossary_check.py:26-38`).
**Watch for** false misses on short targets and on terms whose source is capitalised
differently in the text.

#### TC-46 — `--strict-glossary` flags but does not fail · P1
*Refs* FR-10
**Expected** A miss sets `review_flag` on the chunk/unit, visible in `status` as
`review_chunk_ids`. **The exit code does not change** — a glossary miss never makes the run
non-zero. Confirm this is the intended contract and that the README says so.

#### TC-47 — The glossary applies to overlay units too · P1
*Refs* FR-42
**Expected** `OverlayUnitAdapter.finish` runs the same check (`src/pipeline/units.py:536`), so
`glossary_miss` warnings appear on overlay units as well. Verify on one unit.

#### TC-48 — Malformed user glossary · P1
*Refs* FR-09, E-10
**Steps** Feed a file with (a) invalid JSON, (b) a duplicate `source`, (c) a `target`
containing a newline, (d) an `approved` entry with no `target`, (e) an unknown `version`.
**Expected** Each is rejected with `glossary_invalid` and **exit 1**, before any provider
contact.

#### TC-49 — Glossary edited between runs · P2
*Refs* E-10
**Expected** Changing a target after some chunks completed prints
`consistency risk: N chunks`; `--fresh` keeps `glossary.json` in place and logs
`glossary_kept` with the approved count (a copy goes into the archive).

#### TC-50 — Accented terms · P2 · Known gap
*Refs* CR-20, R15
**Steps** Use a source containing `Müller`, `José`, `Poincaré`.
**Expected today** discovery is ASCII-only (`src/glossary/discovery.py:46-47`,
`_WORD = [A-Za-z]...`), so `Müller` is dropped and `José` becomes `Jos`. Measure how bad this is
on a real book; the same truncated keys feed the overlay glossary check.

### 2.I Error paths and exit codes

#### TC-51 — Missing / non-PDF input · P0 · [VERIFIED]
*Refs* FR-01
**Actual** `extract -i docs/nope.pdf` → `error [input_not_found] input file not found`,
**exit 1**. `extract -i README.md` → `error [input_not_pdf] input is not a .pdf file`,
**exit 1**.

#### TC-52 — Scanned PDF with no text layer · P0
*Refs* FR-06, E-01
**Steps** `extract -i scanned.pdf -o out` on a PDF whose median characters per page is below
`--min-chars-per-page` (default 50).
**Expected** `error [no_text_layer] no extractable text; run OCR first or install the [marker]
extra (--extractor marker)`, **exit 1**, and **no `translation_state.db` is created**
(`src/pipeline/orchestrator.py:1088-1091`). Verify the directory is still empty.

#### TC-53 — Permission-restricted PDF in overlay mode · P0
*Refs* E-49, FR-43
**Steps** `translate --mode overlay` on a PDF whose permission bits forbid modification, and on
an owner-encrypted one.
**Expected** `overlay_pdf_restricted`, **exit 1**, **before any provider contact**
(`src/pipeline/orchestrator.py:1690-1694`). The same PDF must still work in **reflow** mode —
the message says so.
**Known gap (CR-77)** the extractor's check and the renderer's check are not the same code; an
owner-encrypted PDF with all permissions granted is refused by one and allowed by the other.
Test all four encryption variants.

#### TC-54 — User-password PDF · P1
**Expected** **Exit 1** before any provider contact.

#### TC-55 — Bad `--pdf-font` · P1 · [VERIFIED]
*Refs* FR-53, E-55, CR-64
**Steps** `export -o out -f md -f pdf --pdf-font README.md`
**Expected** Refused **before anything is written**.
**Actual** `error [font_unsupported] --pdf-font README.md: unsupported format '.md'; use a .ttf
or .otf file; omit --pdf-font to use the built-in Charis SIL / Nimbus fonts`, **exit 1**, and
`translated_book.md` was **not** rewritten. Also test a real TTF that lacks Turkish glyphs
(expect `glyph_missing` / rejection).

#### TC-56 — Bad `--pdf-page-size` · P1 · [VERIFIED]
*Refs* CR-63
**Steps** `export -o out -f md -f epub -f pdf --pdf-page-size Foo`
**Actual** `error [export_failed] unknown PDF page size 'Foo'; use a pymupdf paper name such as
A4, A5, Letter or Legal (append -l for landscape)`, **exit 1**, **nothing written** — the page
size is resolved before the exporter loop starts, so md and epub are not produced either.
**Note** this is the documented "no output at all -> bare error" branch, but a user who asked
for three formats and got none because of a PDF-only option may find it surprising. Raise with
the product owner (see RISK-08).

#### TC-57 — Optional exporter missing · P1 · [VERIFIED]
*Refs* FR-21, NFR-09, E-53
**Steps** `export -o out -f md -f pdf --pdf-engine weasyprint` with weasyprint absent.
**Actual** md written and reported, `warning: export_failed:pdf:PDF engine 'weasyprint' is
unavailable: install the [pdf] extra and its native Pango/Cairo libraries, or use --pdf-engine
native`, **exit 2**.

#### TC-58 — A format fails mid-loop; the others are kept · P1 · [VERIFIED]
*Refs* CR-91, R4, doc 06 §7.3 "Fix-round additions"
**Steps** Make `translated_book.pdf` unwritable (I created a directory with that name), then
`export -o out -f md -f epub -f pdf`.
**Actual** md and epub written **and reported**,
`warning: export_failed:pdf:cannot write translated_book.pdf: PermissionError`, **exit 2**.
CR-91 behaves as designed. Also run the `export_error:` variant (a required exporter failing
with a USER error) and confirm the exit code is 1 while the written files are still listed.

#### TC-59 — Export while the output PDF is open in a viewer · P2
**Expected** A clean error naming the file; the previous file is intact (temp + rename).

#### TC-60 — Windows console encoding · P1
*Refs* NFR-13, E-15
**Steps** Run `status` and an `export` in cmd.exe (code page 850/1252) and in PowerShell 5.1
with Turkish text in the headings; pipe `--json` through a file and read it back.
**Expected** No mojibake, no `UnicodeEncodeError`; `--json` round-trips. Also test an output
path with spaces, brackets and non-ASCII characters (`çıktı [test]`).

#### TC-61 — Corrupt state · P1
*Refs* CR-67
**Steps** Put invalid JSON into `overlay_blocks.line_boxes`; delete a unit row; hold
`BEGIN IMMEDIATE` from a second connection during an overlay export.
**Expected** `state_corrupt` with no raw traceback, the run row `FAILED`, the lease released, no
partial PDF. **Known gap (CR-67 PARTIAL)** the orchestrator's outer handler still maps these to
**exit 1**; the design wanted 4 (`STATE_LOCKED`) / 5 (`STATE_CORRUPT`). Record the actual code.

#### TC-62 — Quota, auth and rate limit · P0 · **[QUOTA]**
*Refs* E-07, E-08, FR-15, NFR-04
**Steps** (a) invalid key, (b) a key whose quota is exhausted, (c) enough concurrency to draw a
429.
**Expected** (a) and (b): job `PAUSED` (or `OVERLAY_PAUSED`), **exit 3**, the chunk back to
`PENDING`, resume works afterwards. (c): `limiter_changed` / `chunk_rescheduled` events, permits
halved with a 30 s cooldown, retry with full-jitter backoff (base 2 s, cap 60 s, 5 retries), and
`runs.provider_calls` must never exceed completed + retried — nothing sent twice.
**Note** the DeepL SDK's 429 exception carries no `Retry-After`, so the floor is never applied
(CR-08); this is the one remaining live unknown.

#### TC-63 — The API key never leaks · P0
*Refs* NFR-10, FR-26
**Steps** After a real run, grep `logs/*.jsonl`, `summary.json`, `overlay_review.json`, the
console capture and `translation_state.db` for the key (Free `...:fx` and Pro shapes).
**Expected** Zero hits. Also confirm job ids and archive paths are **not** masked (they were
over-redacted at one point). `overlay_review.json` contains book text by design — confirm it is
never echoed to the console or the log.

### 2.J Schema migration

#### TC-64 — A newer schema is refused cleanly · P1 · [VERIFIED]
*Refs* CR-73/CR-98, doc 07
**Steps** `UPDATE schema_meta SET schema_version = 99`, then `status` and `export`.
**Actual** both print
`error [state_schema_incompatible] state created by a newer tool version (schema v99, this tool
supports v4)`, **exit 5**, and **no** backup directory is created. Correct.

#### TC-65 — A v1 directory migrates on the first write command · P1
*Refs* doc 07 §8.2, H38
**Steps** Take a real v1 output directory; run `status` (must stay v1, create nothing); then
`export`.
**Expected** `state-archive/<stamp>-pre-migrate-v1/translation_state.db` holds the untouched v1
file; the live file is v4; row counts unchanged; the v1.0 tool refuses the migrated file
cleanly.
**Failure paths to try** read-only output directory, `state-archive` existing as a *file*, full
disk. Each must name the problem and leave the file at v1. **Known gap (CR-98)** the migration
path catches a narrow exception tuple, so an `OSError` here escapes as a raw traceback and
`engine.dispose()` is skipped — on Windows that leaves an open handle.

#### TC-66 — **A v2 directory with overlay rows migrates correctly** · P0 · [VERIFIED — **FAILS**]
*Refs* doc 07, the v2→v3 and v3→v4 steps
**Pre** a state directory written by a pre-math build: schema v2 **with rows in
`overlay_blocks`**. `docs/test_doc_output/v1_1_third` (43 rows) and `docs/test2_output`
(1102 rows) are both exactly this.
**Steps** Copy the directory, run any write command (`export -f md` is enough), then
`export -f pdf-overlay`.
**Expected** migration to v4, `redact_bbox` `NULL` on every row, overlay export succeeds.
**Actual — FAILS.** Every row's `redact_bbox` holds the literal string `'redact_bbox'`; the
overlay export dies with `error [state_corrupt] cannot read the overlay units from the state
database`, **exit 1**. `translate --mode overlay --dry-run` fails the same way. `status` still
reports `OVERLAY_EXPORTED, 43/43 units` because it only runs aggregate queries.
See **BUG-01** — this is the release blocker.

#### TC-67 — Fresh file and migrated file have the same shape · P2
*Refs* O-19
**Expected** `PRAGMA table_info`, CHECK constraint names and index definitions of a
freshly-created v4 file equal those of a v1→v4 and a v2→v4 migrated file. `tests/test_migration.py`
covers v1 and v3; **add v2-with-rows** (see §3).

#### TC-68 — The user is told a migration happened · P2 · [VERIFIED — **defect found**]
**Steps** Run `export -f md` on a v2 directory and read the console, `summary.json` and
`logs/run-*.jsonl`.
**Actual** nothing anywhere: no console line, no warning in the summary, no log event. The only
trace is that `state-archive/20260921T105704Z-pre-migrate-v2/` appears. The newest JSONL held
only `lease_acquired`, `figure_translated`, `exported`. See **BUG-04**.

#### TC-69 — `--fresh` archives the right things · P1
*Refs* FR-40, §5.8
**Expected**
* `extract --fresh` / `run --fresh`: the DB **and** `source_book.md`, `summary.json`,
  `figures.json`, `overlay_review.json`, `source_profile.json`, `images/`,
  `translated_book.*` move into `state-archive/<stamp>/`; `glossary.json` is **copied** there
  and **kept** in place.
* `translate` has **no `--fresh` flag on the CLI** (see TC-02).
* Archives are never pruned.

### 2.K Non-functional spot checks

#### TC-70 — Overlay visual fidelity · P1
*Refs* NFR-21, NFR-24
**Expected** masked-render pixel diff = 0 at 150 dpi outside the replaced rectangles; image and
drawing counts per page unchanged; output size <= 1.5x input + 2 MB.
**Known observation** on the 48-page run the raster count on page 8 went 63 -> 64. Never
explained. Investigate before release.

#### TC-71 — Determinism · P2
*Refs* NFR-17
**Expected** Two `extract` runs produce byte-identical PNGs and inventory (timestamps aside);
`--no-figures` output is byte-identical to v1.

#### TC-72 — Performance · P2 · after CR-49 is fixed
*Refs* NFR-20, NFR-27, CR-49, R14
**Expected** detection + rendering <= 1 s per page at 200 dpi; overlay page build <= 2 s per
page, 300 pages <= 10 min. Today `find_tables` runs 2-3x per page (~0.23 s/page extra, ~70 s on
300 pages) — **do not run a book over ~250 pages in overlay mode until CR-49 is fixed.**

#### TC-73 — Memory on a large book · P2
*Refs* NFR-01, E-19
**Expected** Chunks/units streamed from the DB, not held in memory. Watch RSS during a 300-page
`translate` and `export`.

#### TC-74 — Many figures · P2
*Refs* CR-87/88, R9
**Expected** `export` on a document with 100+ translated figures finishes in reasonable time;
the source PDF is opened **once** per document, not once per figure.

#### TC-75 — Rotated and offset pages · P2
*Refs* CR-53
**Steps** Overlay-export a book with `/Rotate 90/180/270` pages and cropbox-offset pages.
**Expected** Text lands in the right place, nothing clipped.
---

## 3. Automated test plan — the gaps in the current 749

The suite is strong: 749 tests, mypy strict on 60 files, ruff clean, and every closed code-review
finding carries a named test. The gaps below are what it still cannot see.

### 3.1 Highest priority — the gap that let BUG-01 through

| # | New test | Layer | Why it is missing today |
|---|---|---|---|
| A1 | **v2 file *with rows* migrates to v4** — build a v2 `overlay_blocks` whose DDL genuinely lacks `redact_bbox` (not the current DDL with `'math'` removed), insert 2 rows, migrate, assert `redact_bbox IS NULL` on both and that `OverlayBlockRepository` can read them | `tests/test_migration.py` | `test_20` builds its "v2" table from the **current** metadata minus `'math'`, so the source table already has `redact_bbox`; and it migrates an **empty** table, so the copying `INSERT` is never exercised with rows. Two independent reasons the defect is invisible. |
| A2 | **Round-trip a real overlay pass across the migration** — export `pdf-overlay` from a migrated v2 directory and compare placement counts with the same pass before migration | integration | Nothing today exports from a migrated file. |
| A3 | **A rebuild migration never invents a value** — a generic guard: for any table-rebuild step, assert the copied column list is the intersection of old and new columns, not the new column list | `tests/test_migration.py` | The bug class is "SQLite treats an unknown double-quoted identifier as a string literal". One assertion kills the whole class. |

### 3.2 Reporting parity

| # | New test | Layer |
|---|---|---|
| B1 | `Orchestrator._review_counts` on a review file whose `counts` block **and** entries both mention `shrunk_below_threshold` / `could_not_fit` / `kept_original` — assert no double counting (BUG-02). The existing `test_status_reports_the_overlay_pass_and_review_counts` deliberately uses a `counts` block containing only `placed`, which is exactly the case that cannot fail. | unit |
| B2 | `status --json` vs `summary.json` vs `overlay_review.json`: a property-style test asserting the three agree on every shared key | integration |
| B3 | A standalone `export` in a directory without `summary.json` — pin whether it writes one (today it does not) | integration |

### 3.3 Portability of the regression net — **the fixture problem**

`docs/*.pdf` is git-ignored (copyrighted excerpts), and the tests that use them have **no skip
guard**. Measured: with `docs/test_doc.pdf` and `docs/test-2.pdf` absent,

```
60 failed, 674 passed, 1 skipped, 15 errors in 48.36s
```

They do not skip; they fail. Everything that protects the two real-world fixes lives in that
set — `test_math_band_on_the_real_document`, `test_no_redaction_rect_reaches_a_kept_unit_on_the_real_document`,
`test_no_painted_rect_overlaps_another_on_the_real_document`,
`test_test_doc_page_one_body_units_are_never_shrunk_past_the_cap`, and all of
`tests/test_overlay_extractor.py`'s document-level invariants.

| # | Work | Layer |
|---|---|---|
| C1 | `pytest.mark.skipif(not TEST_DOC_PDF.exists())` (and the same for `test-2.pdf`) on every fixture-bound test, or a session fixture that skips | `tests/conftest.py` |
| C2 | **Synthesise a math fixture** with PyMuPDF: a page with a CMMI/CMSY display equation, an equation number at the right margin, and prose flowing around it. This makes the math-band rule testable without the copyrighted PDF and is the single most valuable new test in this plan | `tests/conftest.py` + `test_overlay_extractor.py` |
| C3 | Synthesise the overlapping-rect fixture (two blocks whose boxes overlap so one is cut back) so the `redact_bbox` invariant is provable without `test-2.pdf` | same |
| C4 | Fix the README: it says "Everything else runs without them", which is not what happens | doc |

### 3.4 Content and provider payload

| # | New test | Layer |
|---|---|---|
| D1 | **Control characters** — extend `test_control_characters_never_reach_the_provider` to the *batch* path: assemble a 50-unit batch where 1 unit carries U+0000/U+0001/U+0014/U+0015 and assert the whole batch still serialises. The current test covers `protect()` and the DeepL context window; the batch-level blast radius (194 units lost) is what actually hurt | unit |
| D2 | XML-hostile prose — `<`, `>`, `&`, inline code and URLs through `tag_handling="xml"` with a fake provider that round-trips the payload (CR-09 without spending money) | unit |
| D3 | `--allow-non-english` still sends `source_lang="EN"` (CR-10) — pin the current behaviour so the future fix is a deliberate change | unit |
| D4 | Glossary discovery with accented words (CR-20) — pin today's ASCII-only behaviour | unit |

### 3.5 Lifecycle

| # | New test | Layer |
|---|---|---|
| E1 | SIGKILL simulation: kill the process between `claim` and `complete`, restart, assert `re-queued N` and that at most one batch is re-billed (CR-44 bound) | integration, fake provider |
| E2 | Two-writer lease: real second process, assert exit 4 and the holder pid in the message | integration |
| E3 | `export_error:` (as opposed to `export_failed:`) with files already on disk — assert exit 1 **and** the written files listed (CR-91's other branch) | integration |
| E4 | Migration failure paths: read-only output dir, `state-archive` present as a file, and a step raising `OSError` — the last one is CR-98 and currently escapes as a traceback | `tests/test_migration.py` |

### 3.6 Pyramid balance

Current shape is healthy (many unit, fewer integration, no e2e). Keep it: everything above is a
unit or integration test except A2, which needs a real PDF and belongs in the manual set if
C2/C3 cannot synthesise it.

---

## 4. Regression checklist

Run this before every release. It is deliberately short; the full suite covers the rest.

### 4.1 Automated gate (must be green)

```
.venv\Scripts\python.exe -m pytest -q          # expect 749 passed, 1 skipped
.venv\Scripts\python.exe -m mypy src           # expect 60 files, no issues
.venv\Scripts\python.exe -m ruff check src tests
```

### 4.2 The four real-world fixes — never ship without these

| # | Fix being protected | Check | Pass criterion |
|---|---|---|---|
| R-1 | **C0 control characters** (whole DeepL batches were dropped with `HTTP 400 Tag handling parsing failed`) | `translate --dry-run` cannot see this — it needs a paid run, or `test_control_characters_never_reach_the_provider` plus new test D1. On a paid run of `docs/test-2.pdf`, grep the log for `HTTP 400` and confirm `failed == 0` | 0 failed units; before the fix 194 failed |
| R-2 | **Display-equation bands** | Overlay-export `docs/test-2-math.pdf`; visually confirm (8.4)-(8.9) and (8.39)-(8.42) keep brackets, subscripts and equation numbers; count review entries | ~77 entries total (`collateral_redaction` ~18, `fragment` ~48, `could_not_fit` 0). Regression looks like 179 / 90 / 82 / 1 |
| R-3 | **Box overlap / placement geometry** | On `docs/test-2.pdf`: 794 units, 556 translatable, **0 placement overlaps**. On `docs/test_doc.pdf`: 43 / 34 / 0 | any non-zero overlap is a regression |
| R-4 | **Residual source glyphs beside a shortened block** (`redact_bbox`) | On `docs/test-2-math.pdf` page 1, search the output text for `corresponding current` and `least squares and Appendix` | both absent. Present = regression |

Plus, because BUG-01 lives in the same area:

| R-5 | **Migration of a v2 directory with overlay rows** | Copy a v2 directory, run `export -f md` then `export -f pdf-overlay` | exit 0; `SELECT DISTINCT redact_bbox FROM overlay_blocks` returns only JSON arrays and `NULL`, never the string `redact_bbox` |

### 4.3 Reference numbers to diff against

`docs/test_doc.pdf` (3 pages), overlay pass:

```
43 units / 34 translatable / 3 pages
placed 34 · shrunk 0 · could_not_fit 0 · kept 9
dry-run: 34 units, 8951 characters   (reflow: 9 chunks, 8991 characters)
distinct font sizes per page: 5 / 4 / 4
```

`docs/test-2.pdf` (48 pages), overlay pass:

```
900 units / 641 translatable  (1102 / 816 before the math-band fix)
61 bands over 22 pages, 501 source lines taken out of translation
translated-unit x band intersection: 0
dry-run: 556 units, 123644 characters
```

`docs/test-2-math.pdf` (4 pages): 96 units / 55 translatable / 41 kept ·
`placed 55 · shrunk 3 · could_not_fit 0 · review entries 52`.

### 4.4 Ten-minute smoke (no provider contact)

1. `providers` / `extractors` / `exporters` — availability as in TC-01.
2. `extract -i docs/test_doc.pdf -o <tmp>` — 3 pages, 1 figure, exit 0.
3. `glossary -o <tmp>` — exit 0.
4. `translate -i docs/test_doc.pdf -o <tmp> --dry-run` — 9 chunks / 8991 chars, exit 0.
5. `translate -i docs/test_doc.pdf -o <tmp> --mode overlay --dry-run` — 34 units / 8951 chars.
6. On a copy of a finished directory: `export -f md -f epub -f pdf` — exit 0, three files.
7. Same directory: `export -f pdf-overlay` — exit 0, placement counts match §4.3.
8. `export --input <a different pdf> -f pdf-overlay` — exit 5, nothing written.
9. `export --input <a different pdf> -f md` — exit 2, md written, `input_changed` warning.
10. `status -o <tmp> --json` — parses, and its `overlay.review_counts` equal
    `overlay_review.json`'s `counts` (this one fails today, BUG-02).

---

## 5. Risk register

Probability x Impact, with a release verdict for each. "Blocking" means the release should not
ship without it.

### 5.1 New findings from this QA pass

| ID | Finding | Prob. | Impact | Blocking? |
|---|---|---|---|---|
| **BUG-01** | v2→v3 table rebuild writes the literal string `redact_bbox` into every row; overlay reads then fail with `state_corrupt` | **Certain** for any v2 directory with overlay rows | **High** — the user's 48-page book directory becomes unusable; paid translations are stranded (recoverable, but only by hand) | **YES** |
| **BUG-02** | `status --json` doubles overlay review counts for reasons present in both the file `counts` and the entries | Certain | Low-medium — a user reads 4 `could_not_fit` where there are 2 and hunts for a defect that is not there | No, but cheap |
| **BUG-03** | The regression net for the math-band and `redact_bbox` fixes is unrunnable without the git-ignored fixture PDFs (60 failed / 15 errors on a clean checkout), and the README claims otherwise | Certain on any machine but this one | Medium — the highest-risk area has no portable protection | No, fix in the next round |
| **BUG-04** | A schema migration is completely silent: no console line, no summary warning, no log event | Certain | Low-medium — combined with BUG-01 the user cannot tell what changed their file | No |
| **DOC-01** | README says `translation_state.db … schema v2`; the current version is 4. `docs/durum.md` says the folder is not a git repository (it is, 2 commits) and its table says 722 tests while its footer says 749 | Certain | Low | No |

### 5.2 Code-review items still open (NOT DONE)

| ID | What | Prob. of biting | Impact | Verdict |
|---|---|---|---|---|
| CR-65 | `source_profile.json` values are not range-checked on load | Low — only a hand-edited or corrupt profile | Medium — an absurd page size produces an unusable PDF, but nothing is lost | Not blocking. Ticket; add TC-09's corrupt-profile variant. |
| CR-66(b) | Margin measurement counts the running header, so the reflow PDF body box sits too high | **Certain** on any book with running headers | Low — cosmetic, reflow PDF only, no text loss | Not blocking. Ticket. |
| CR-69 | Duplicate constraint name `ck_overlay_blocks_keep_reason` in `models.py` | Certain (already in every file) | Low today, **high later** — renaming it needs a table rebuild | Not blocking, **but do it in the same PR as BUG-01**: that fix already rebuilds `overlay_blocks`, so the rename is free now and expensive after release (CR-100 says the same). |
| CR-71 | `complete()` ignores `result.chunk_id` instead of asserting it | Low — `zip(..., strict=True)` upstream catches length drift | High if it ever fires (a translation written to the wrong unit) | Not blocking; one-line guard, take it. |
| CR-72 | `overlay_unit_count` / `overlay_tool_version` written, never read | Certain | Low — the "this pass came from another tool version" check does not exist | Not blocking. |
| CR-73 / CR-98 | `open_database` catches a narrow exception tuple around the migration; a non-DB error escapes as a raw traceback and skips `engine.dispose()` | Low-medium — needs a disk/OS error during migration | Medium — raw traceback instead of "your backup is here", and a leaked file handle on Windows | Not blocking on its own, **but it sits on the same code path as BUG-01**. Change to `except Exception` in the same PR. |

### 5.3 Code-review items still PARTIAL

| ID | What | Prob. | Impact | Verdict |
|---|---|---|---|---|
| CR-44 | Per-unit completion commits widen the re-billing window to one batch on SIGKILL | Low (needs a hard kill) | Medium — up to ~50 paid overlay units re-sent | Not blocking; it is the design's accepted "one batch" risk. Measure in TC-31. |
| CR-48 | Observability gaps in overlay mode; no progress output during a long export; event names not aligned with NFR-30 | Certain | Low-medium — a 300-page export looks hung | Not blocking; annoying on long books. Ticket. |
| CR-49 | `find_tables` runs 2-3x per page in overlay extraction | Certain | Medium — roughly doubles extraction time; ~70 s extra on 300 pages | **Conditionally blocking**: not for books up to ~250 pages, but the review's own approval condition says fix it before running anything larger. Keep that condition. |
| CR-67 | A DB exception during overlay export is caught one layer down but the orchestrator still reports exit 1 instead of 4/5 | Low | Low — wrong exit code, message is fine | Not blocking. |
| CR-77 | The extractor's and the renderer's PDF-permission checks are different code and disagree on owner-encrypted files | Low | Medium — a PDF refused by one path is accepted by the other | Not blocking; test all four encryption variants (TC-53) and record the disagreement. |
| CR-82 | Hygiene: 6 unjustified `noqa: BLE001` | Certain | Low | Not blocking. |

### 5.4 Risks carried from the fix round (unchanged)

| ID | Risk | Verdict |
|---|---|---|
| RISK-01 | `_PAGE_TEXT_FILL_RATIO >= 0.50` is a geometric heuristic; a schematic whose labels cover more than half its own area is dropped. Measured fills: 0.146 / 0.19 / 0.755 | TC-22. Not blocking; needs a real counter-example before changing the threshold. |
| RISK-02 | `_PAGE_LABEL_MAX_CHARS = 40` — a longer label counts as prose | TC-22. Not blocking. |
| RISK-03 | `overlay_review.json` can now hold several `page_shared_scale` records per page, so `counts.entries` and `summary.json`'s `overlay_review_entries` rise; the CLI prints the raw number | Not blocking; document what the number means. |
| RISK-04 | The reference page deliberately shows two body sizes (8.50 pt and 9.11 pt) | Product decision, TC-19. |
| RISK-05 | CR-85's **rule** half was not done — only the visibility of the 60 % raster rejection | Not blocking. |
| RISK-06 | Mathematics set in `CMR`/`CMBX` is not recognised as a band; part of equation (8.5) on p.19 is still translated | Documented limitation. Not blocking. |
| RISK-07 | ~0.7 % of prose lines inside a band stay English | Documented, accepted trade-off. |
| RISK-08 | An invalid PDF-only option (`--pdf-page-size Foo`) aborts a multi-format export before md/epub are written, contrary to the spirit of CR-91 | Not blocking; product call — either validate PDF options only when the pdf format runs, or say so in the README. |
| RISK-09 | Free-tier DeepL is capped at 500k characters/month; the 48-page sample alone cost 127,707. A 200-page book will not fit | Not a defect; put the `--dry-run` estimate in front of the user before any long run. |

### 5.5 Process note

While this QA pass ran, `docs/test2_final/` appeared in the working tree (untracked, 13:49).
Its `summary.json` shows `command=run`, `mode=overlay`, `chars_sent_this_run = 0` and a
`dry_run: 556 overlay units pending, 123644 payload characters` warning — i.e. it was a
`run --mode overlay --dry-run` against `docs/test-2.pdf`, executed by one of the code-reading
helpers this session delegated to, against the instruction not to invoke `run`. **No provider
characters were spent** (verified: `chars_sent_this_run = 0`, and the dry-run path returns
before `translator.prepare()`), but the directory is untracked project clutter and should be
deleted or added to `.gitignore`. The measurement it produced is reused in TC-13 and §4.3.

---

## 6. Bug report template

```
## BUG-{id} — {one-line summary}

Severity:   P0 blocker / P1 / P2 / P3
Found by:   TC-{id}
Build:      git {sha} (or: working tree, {date})
Environment: Windows 11 / Python 3.11.9 / SQLite 3.45.1 / PyMuPDF {ver}
Mode:       reflow | overlay      Provider: deepl | none (offline)

### Steps to reproduce
1.
2.

### Expected
{the contract: which FR/NFR/E/CR says so, and the expected exit code}

### Actual
{verbatim console output, including the exit code}

### Evidence
- command transcript
- logs/run-<id>.jsonl  (attach, it contains no key and no book text)
- summary.json / overlay_review.json excerpt
- SQL: SELECT ... FROM ...;   with the real result
- screenshot or page render if visual

### Blast radius
- which existing output directories / books are affected
- is paid translation lost, stranded, or safe?
- is the damage reversible, and how?

### Suggested fix
{file:line and the change, if known}
```

### 6.1 Filed by this pass

---

```
## BUG-01 — Migrating a v2 state file poisons `overlay_blocks.redact_bbox`
             with the literal string "redact_bbox"

Severity:   P0 — release blocker
Found by:   TC-66
Build:      working tree, 2026-09-21
Environment: Windows 11 / Python 3.11.9 / SQLite 3.45.1
Mode:       overlay (offline; no provider contact needed to reproduce)

### Steps to reproduce
1. Take a state directory written before the math fix, i.e. schema v2 with rows in
   overlay_blocks. Both of these qualify:
      docs/test_doc_output/v1_1_third   (v2, 43 rows)
      docs/test2_output                 (v2, 1102 rows — the 48-page book)
2. Copy it (the migration is destructive in place).
3. Run any write command, e.g.  export -o <copy> -f md
4. Run                           export -o <copy> -f pdf-overlay

### Expected
Step 3 migrates v2 -> v3 -> v4 and leaves redact_bbox NULL on every row
("NULL = same as the placement rect"). Step 4 exits 0 and reproduces the
overlay PDF.

### Actual
Step 3 succeeds silently and bumps the file to v4.
Step 4:
    error [state_corrupt] cannot read the overlay units from the state database
    exit=1
translate --mode overlay --dry-run fails identically.
status still prints "overlay: OVERLAY_EXPORTED, 43/43 units, 3/3 pages" because
it only runs aggregate queries — so the directory looks healthy until you use it.

### Evidence
    sqlite> SELECT redact_bbox, count(*) FROM overlay_blocks GROUP BY 1;
    ('redact_bbox', 43)

Root cause — src/database/session.py:289-311, _upgrade_v2_to_v3:

    columns = ", ".join(f'"{c.name}"' for c in table.columns)
    ...
    INSERT INTO overlay_blocks ({columns}) SELECT {columns} FROM overlay_blocks_v2

`table` is the CURRENT (v4) metadata, so `columns` includes "redact_bbox", but the
renamed v2 source table does not have that column. SQLite's double-quoted-string
misfeature then reads "redact_bbox" as a string literal instead of raising:

    >>> c.execute('CREATE TABLE old (a TEXT, b TEXT)')
    >>> c.execute("INSERT INTO old VALUES ('x','y')")
    >>> c.execute('CREATE TABLE new (a TEXT, b TEXT, c TEXT)')
    >>> c.execute('INSERT INTO new ("a","b","c") SELECT "a","b","c" FROM old')
    >>> c.execute('SELECT * FROM new').fetchall()
    [('x', 'y', 'c')]            # <- the column NAME became the value

_upgrade_v3_to_v4 then finds the column already present and skips, so the garbage
survives.

Why the suite does not catch it — tests/test_migration.py:424
test_20_v2_file_widens_keep_reason_to_math builds its "v2" table by taking the
CURRENT DDL and deleting 'math' from the CHECK, so the source table already has
redact_bbox; and it migrates an EMPTY table, so the copying INSERT never runs with
a row. Two independent reasons.

### Blast radius
Every state directory created before schema v3 that holds overlay rows. On this
machine that is docs/test_doc_output/v1_1_third (43 rows) and docs/test2_output
(1102 rows — the real 48-page book, 127,707 paid characters).
Paid translations are NOT lost: translated_text is intact and readable by SQL, and
state-archive/<ts>-pre-migrate-v2/translation_state.db holds the untouched original.
But the tool cannot read its own file, and the failure mode is silent until you
export.

Recovery (verified): UPDATE overlay_blocks SET redact_bbox = NULL
                     WHERE redact_bbox = 'redact_bbox';
after which `export -f pdf-overlay` exits 0 with
placed 34 / shrunk 0 / could_not_fit 0 / kept 9 — identical to the reference run.

### Suggested fix
1. In _upgrade_v2_to_v3, copy the INTERSECTION of the old and new column sets:
       old_cols = _existing_columns(conn, "overlay_blocks_v2")
       shared   = [c.name for c in table.columns if c.name in old_cols]
   and use `shared` on both sides of the INSERT.
2. Add a repair pass for files already damaged (set redact_bbox = NULL wherever it
   is not valid JSON), or document the one-line UPDATE above in the release notes.
3. Add tests A1 and A3 from the QA plan §3.1.
4. While overlay_blocks is being rebuilt anyway, take CR-69 / CR-100 (the duplicate
   ck_overlay_blocks_keep_reason constraint name) in the same PR.
```

---

```
## BUG-02 — `status --json` doubles the overlay review counts

Severity:   P2
Found by:   TC-41
Build:      working tree, 2026-09-21

### Steps to reproduce
1. status -o docs/test2_output --json
2. open docs/test2_output/overlay_review.json

### Expected
status's overlay.review_counts equals the file's own "counts" block.

### Actual
    overlay_review.json : shrunk_below_threshold 22, could_not_fit 2, kept_original 286
    status --json       : shrunk_below_threshold 44, could_not_fit 4, kept_original 286
and on docs/test2_math_out:
    overlay_review.json : shrunk_below_threshold 3,  kept_original 41
    status --json       : shrunk_below_threshold 6,  kept_original 42

### Evidence / root cause
src/pipeline/orchestrator.py:3805-3817 seeds `counts` from the file's "counts"
block and then, for every entry, does
    counts[reason] = counts.get(reason, 0) + 1
so any reason that is BOTH a summary key and an entry reason is counted twice.
`placed` and `entries` are unaffected (placed is never an entry reason; entries
uses setdefault), which is why the existing tests pass:
tests/test_orchestrator.py:1906 writes a review file whose counts block contains
only "placed".

### Blast radius
Reporting only. No data loss. But `could_not_fit` is the number a user checks to
decide whether a page is broken, and it is inflated 2x.

### Suggested fix
Tally the entry reasons into a separate dict and only fill keys the file's counts
block does not already provide (`counts.setdefault(reason, tally[reason])`), or
drop the file's counts in favour of the tally. Add test B1 (§3.2).
```

---

```
## BUG-03 — The regression tests for the math-band and redact_bbox fixes cannot
             run on a clean checkout

Severity:   P2 (P1 for anyone but the original machine)
Found by:   §3.3

### Steps to reproduce
Copy tests/ to a directory whose ../docs holds no PDFs and run pytest.

### Expected
Per the README ("Everything else runs without them") the fixture-bound tests skip.

### Actual
    60 failed, 674 passed, 1 skipped, 15 errors in 48.36s
They fail and error; there is no skipif guard anywhere except one for marker-pdf.
docs/*.pdf is git-ignored on purpose (copyrighted), so this is what every fresh
clone sees.

### Blast radius
Everything protecting the two bugs found on the real book —
test_math_band_on_the_real_document,
test_no_redaction_rect_reaches_a_kept_unit_on_the_real_document,
test_no_painted_rect_overlaps_another_on_the_real_document,
test_test_doc_page_one_body_units_are_never_shrunk_past_the_cap and the whole
document-level part of tests/test_overlay_extractor.py.

### Suggested fix
C1-C4 in §3.3: skip guards, plus a synthesised PyMuPDF math fixture and a
synthesised overlapping-rect fixture so the invariants are provable without the
copyrighted PDFs. Correct the README sentence.
```

---

```
## BUG-04 — A schema migration happens silently

Severity:   P3
Found by:   TC-68

### Steps to reproduce
Run `export -o <a v2 directory> -f md` and read the console, summary.json and
logs/run-*.jsonl.

### Expected
The user is told the state was migrated and where the backup is.

### Actual
Nothing. The newest JSONL contained only lease_acquired, figure_translated,
exported. The only trace is that state-archive/<ts>-pre-migrate-v2/ appears.

### Suggested fix
One warning ("state migrated v2 -> v4, backup in state-archive/...") plus a
`schema_migrated` log event with from/to/backup_path.
```

---

## 7. Go / No-Go

### 7.1 Result

| | |
|---|---|
| Scenarios written | 75 |
| Executed in this pass (offline) | 24 marked **[VERIFIED]** |
| Passed | 21 |
| Failed | 3 (TC-41, TC-66, TC-68) |
| Blocked / deferred | 51 — 6 are marked **[QUOTA]** and spend DeepL characters; the rest need a real long book, a scanned PDF, an encrypted PDF, a second shell, or a human eye |
| Automated gate | 749 passed / 1 skipped · mypy 60 files clean · ruff clean |

| Severity | Count | IDs |
|---|---|---|
| P0 | 1 | BUG-01 |
| P1 | 0 | — |
| P2 | 3 | BUG-02, BUG-03, RISK-08 |
| P3 | 2 | BUG-04, DOC-01 |

### 7.2 Decision: **CONDITIONAL GO**

The product is in good shape. The pipeline works end to end in both modes, the two defects the
real 48-page book exposed are genuinely fixed and measurably so, the error paths behave
according to the documented exit-code contract in every case I could exercise, and the automated
gate is clean. Nothing in this pass suggests paid translation can be silently lost.

What stops an unconditional go is BUG-01: a migration that runs automatically, on the user's
existing directories, and quietly makes them unreadable. It is the exact class of failure the
tool's whole design — archives, hash binding, `Result[T]`, never re-send a completed chunk —
exists to prevent, and it currently affects the user's real 48-page book directory.

### 7.3 Conditions for GO

**Must (release blockers):**

1. **Fix BUG-01** — copy the intersection of the old and new columns in `_upgrade_v2_to_v3`.
2. **Add tests A1 and A3** (§3.1): a v2 file with *rows* in a table that genuinely lacks the new
   column, and a generic "a rebuild never invents a value" assertion.
3. **Verify the fix against a real v2 directory** — copy `docs/test2_output` (1102 overlay rows),
   migrate, then `export -f pdf-overlay`, and confirm exit 0 and
   `SELECT DISTINCT redact_bbox` returns only JSON and NULL.
4. **Say what to do about the already-damaged directories** — either an automatic repair pass
   during migration, or the one-line `UPDATE` in the release notes. `docs/test2_output` and
   `docs/test_doc_output/v1_1_third` are both in this state the moment a write command touches
   them.
5. **Run the [QUOTA] scenarios once** on the smallest useful fixture before release: TC-62
   (quota / auth / 429), TC-44 and TC-45 (glossary applied and missed), TC-27 (resume bills
   nothing twice), TC-63 (no key leak). These close CR-08 / CR-09 / CR-10, deferred three
   review rounds in a row, and they are the only untested path to the user's money.

**Should (same PR, all cheap):**

6. Fix BUG-02 (`status` double count) and add test B1.
7. Change `open_database`'s migration `except` tuple to `except Exception` (CR-73 / CR-98) — it
   is on the same code path as BUG-01.
8. Take CR-69 / CR-100 (duplicate constraint name) while `overlay_blocks` is being rebuilt
   anyway; after release it costs another rebuild.
9. Add the one-line guard for CR-71.
10. Announce migrations (BUG-04) and correct README + `durum.md` (DOC-01).

**Next round (ticket, not blocking):**

11. BUG-03 — skip guards plus synthesised math and overlap fixtures (§3.3). This is the most
    valuable engineering work in the plan: it makes the highest-risk area testable by anyone.
12. CR-49 before any book over ~250 pages goes through overlay mode. Keep this condition
    exactly as the code reviewer wrote it.
13. CR-48 (progress output on long exports), CR-65, CR-66(b), CR-67, CR-72, CR-77, CR-82.
14. RISK-08 — decide whether an invalid PDF-only option should abort a multi-format export.

### 7.4 Scope statement to put in the release notes

> Ready for prose-heavy books. Mathematics set in Computer Modern *math* fonts is preserved as
> untouched bands; mathematics set in the *text* fonts (`CMR`/`CMBX`) is not recognised and may
> still be garbled. Roughly 0.7 % of prose lines adjacent to an equation stay in English.
> Scanned PDFs and PDFs whose permission bits forbid modification are out of scope for overlay
> mode. Overlay text is set in Charis SIL, not the source typeface. Running headers stay English
> unless `--overlay-translate-headers` is given.

---

## 8. Türkçe özet — ne yapılmalı

Sırayla:

1. **BUG-01'i düzelt.** `src/database/session.py` içindeki `_upgrade_v2_to_v3` fonksiyonu, satır
   kopyalarken **yeni** tablonun kolon listesini kullanıyor; eski tabloda olmayan `redact_bbox`
   için SQLite kolon adını metin sanıp değer olarak yazıyor. Çözüm: iki tablonun **ortak**
   kolonlarını kopyala.
2. **İki test ekle**: satır *içeren* bir v2 dosyasıyla göç testi, ve "yeniden kurulan bir tablo
   asla değer uydurmaz" genel kontrolü.
3. **Gerçek bir v2 klasörüyle doğrula** — `docs/test2_output` (1102 overlay satırı) kopyasını
   göç ettir, `export -f pdf-overlay` çalıştır, exit 0 bekle.
4. **Zaten bozulmuş klasörler için ne yapılacağını söyle.** Elle düzeltmesi tek satır:
   `UPDATE overlay_blocks SET redact_bbox = NULL WHERE redact_bbox = 'redact_bbox';`
   Bunu denedim, sonrasında overlay export sorunsuz çalıştı.
5. **DeepL gerektiren 5 senaryoyu bir kez koş** (TC-62, TC-44, TC-45, TC-27, TC-63) — en küçük
   PDF ile. Üç inceleme turudur ertelenen CR-08/09/10 ancak böyle kapanır.
6. Ucuz olanları aynı PR'da al: `status` sayaç hatası, `except Exception`, tekrar eden CHECK
   adı, göç bildirimi, README'deki "schema v2" ifadesi.
7. Sonraki tura bırak: test fixture'larının taşınabilirliği (en değerlisi budur), CR-49 (250
   sayfadan büyük kitaplardan önce), geri kalan PARTIAL maddeler.

---
✅ QA Engineer tamamlandı.
✅ Tüm workflow tamamlandı.

**Sonraki adım:** BUG-01 için DB Developer'a dön (şema göçü düzeltmesi + iki test), ardından
küçük düzeltmeler ve DeepL kotası gerektiren 5 senaryonun canlı koşusu.
