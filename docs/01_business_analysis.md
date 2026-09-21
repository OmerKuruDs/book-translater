# Requirements Analysis: Book Translation CLI & Pipeline Tool

## 1. Feature Summary

**Title:** Translate entire English PDF books into consistent Turkish Markdown/EPUB/PDF via a resumable, glossary-aware CLI pipeline
**Module:** book-translator (standalone Python tool; new repository)
**Priority:** High
**Type:** New Feature (greenfield)

## 2. Business Goal

- **Problem:** Translating a 200+ page book by pasting text into a translation service is slow, loses layout (headings, lists, code blocks), produces sentences broken at page boundaries, and drifts in terminology (a character or technical term is rendered differently across chapters). A crash or API quota exhaustion mid-way forces a full restart and re-billing.
- **Expected Benefit (measurable):**
  - One command produces a complete translated book; no manual copy/paste.
  - Zero re-translation of already completed chunks after an interruption (0 duplicate API spend on resume).
  - Every glossary term is rendered identically in 100% of its occurrences.
  - Cross-page sentence breaks and running headers/footers are removed before translation (measurable by a fixture-based test corpus).
- **Affected User Roles:**
  - **Translator / Operator** — runs the CLI, reviews the glossary, consumes the output.
  - **Developer / Maintainer** — extends providers, extractors, and exporters.
  - **(Indirect) Reader** — consumes the EPUB/PDF; not a system user.

## 3. User Stories

### Stage 1 — Document Ingestion & Layout Parsing (Spec §2.1)

```
US-1: Extract a PDF into clean Markdown

  As a Translator,
  I want to convert an English PDF into a single normalized Markdown file
  so that the translation stage works on clean, structured text rather than raw page dumps.

  Acceptance Criteria:
    AC-1: Given a digital PDF with a text layer
          When I run the ingestion stage
          Then a source_book.md file is produced containing headings, paragraphs, lists,
               and code blocks as Markdown constructs (not flattened plain text).
    AC-2: Given a PDF whose pages carry running headers, footers, and page numbers
          When ingestion completes
          Then none of the running header/footer strings or bare page-number lines
               appear in source_book.md (verified against a fixture PDF with known headers).
    AC-3: Given a sentence that is split across two pages with a trailing hyphen
               ("trans-" / "lation")
          When ingestion completes
          Then source_book.md contains the rejoined word "translation" in a single paragraph.
    AC-4: Given a paragraph that continues on the next page without a hyphen
          When ingestion completes
          Then the two fragments are merged into one paragraph with no blank line between them.
    AC-5: Given the preferred extraction engine is unavailable or fails on the file
          When ingestion runs
          Then the fallback engine is used, the fallback is reported in the CLI output and log,
               and a source_book.md is still produced.
```

```
US-2: Validate input before spending time or money

  As a Translator,
  I want the tool to reject unusable input early
  so that I do not wait through a long extraction only to get an empty or wrong-language result.

  Acceptance Criteria:
    AC-1: Given a PDF with no extractable text layer (scanned images only)
          When ingestion runs and OCR is not available
          Then the run stops with a clear error naming the problem and suggesting OCR, and
               no translation state is created.
    AC-2: Given a PDF whose extracted text is detected as predominantly non-English
          When ingestion completes
          Then a warning is shown with the detected language, and the run continues only
               if the user passed an explicit override flag (default: stop).
    AC-3: Given an input path that does not exist or is not a PDF
          When the CLI is invoked
          Then it exits with a non-zero code and a message before doing any work.
```

### Stage 2 — Glossary Discovery & Extraction (Spec §2.2)

```
US-3: Discover recurring terms automatically

  As a Translator,
  I want the tool to propose a glossary of recurring names, places, and technical terms
  so that I can approve Turkish equivalents once and have them applied consistently.

  Acceptance Criteria:
    AC-1: Given source_book.md contains a capitalized proper noun appearing N or more times
               (N configurable, default value to be set by the architect)
          When the glossary stage runs
          Then the term appears in glossary.json with its occurrence count and an empty or
               machine-suggested Turkish target.
    AC-2: Given the glossary stage has run
          When I open glossary.json
          Then each entry has: source term, target term, occurrence count, and an
               approval status (e.g. proposed / approved).
    AC-3: Given a term that is a common English word (e.g. "The", "Chapter")
          When discovery runs
          Then it is excluded by a stop-word list and does not appear in the glossary.
```

```
US-4: Provide or edit an approved glossary

  As a Translator,
  I want to supply my own glossary file (or edit the generated one) before translation
  so that domain terms are rendered exactly as I decide.

  Acceptance Criteria:
    AC-1: Given I pass --glossary my_terms.json
          When the pipeline runs
          Then the entries in my file take precedence over any auto-discovered entries
               with the same source term.
    AC-2: Given glossary.json contains an entry marked "approved" with target "Ejderha"
               for source "Dragon"
          When the translation stage completes
          Then every occurrence of "Dragon" in the source that was translated is rendered
               as "Ejderha" (or its correct Turkish inflected form when the provider
               supports inflection) in translated_book.md — verified by a fixture test.
    AC-3: Given the glossary file is malformed (invalid JSON, missing required fields)
          When the pipeline starts
          Then it stops before translation with a message identifying the offending entry.
```

```
US-5: Push the glossary to the translation backend

  As a Translator,
  I want the approved glossary to be applied by the provider itself
  so that terminology is enforced at translation time, not patched afterward.

  Acceptance Criteria:
    AC-1: Given the DeepL provider is selected and the glossary has approved entries
          When translation starts
          Then a DeepL glossary is created (or reused if an identical one exists) and its
               ID is attached to every translation request; the ID is stored in state.
    AC-2: Given a local NMT or LLM provider is selected
          When translation starts
          Then the glossary is injected into the prompt/context in a documented format,
               and a test can confirm the injected text contains every approved term.
    AC-3: Given the provider does not support glossaries
          When translation starts
          Then the tool falls back to prompt injection or post-replacement and logs which
               strategy was used.
```

### Stage 3 — Asynchronous Semantic Translation (Spec §2.3)

```
US-6: Split the book into semantic chunks

  As a Translator,
  I want the Markdown split into chunks that never cut through a sentence, heading,
  list, table, or code block
  so that the provider always sees complete context.

  Acceptance Criteria:
    AC-1: Given source_book.md
          When chunking runs
          Then every chunk is between the configured minimum and maximum size
               (default target 1000–1500 characters) except when a single indivisible
               block (e.g. one long paragraph or code block) exceeds the maximum.
    AC-2: Given a Markdown code block (fenced with ```)
          When chunking runs
          Then the entire fence is inside exactly one chunk and is flagged as
               "do not translate".
    AC-3: Given a heading followed by its paragraph
          When chunking runs
          Then the heading is never the last line of a chunk (it stays with the
               paragraph that follows it) unless it is the final line of the document.
    AC-4: Given chunking has run
          When the chunks are concatenated in order without translation
          Then the result is byte-identical to source_book.md (lossless split).
    AC-5: Given a chunk larger than the selected provider's request limit
          When chunking runs
          Then the chunk is further split at the nearest sentence boundary and the
               split is recorded so reassembly restores the original block.
```

```
US-7: Translate chunks with fault tolerance

  As a Translator,
  I want each chunk translated with automatic retry and rate-limit handling
  so that transient API errors do not fail the whole job.

  Acceptance Criteria:
    AC-1: Given a chunk in PENDING status
          When the orchestrator processes it
          Then its status moves PENDING -> PROCESSING -> COMPLETED and the translated
               text is stored with the chunk.
    AC-2: Given the provider returns a rate-limit response (HTTP 429)
          When the orchestrator handles it
          Then the chunk is retried after an exponential backoff (respecting Retry-After
               when present), retry count is incremented, and no other error is raised.
    AC-3: Given a chunk fails more than the configured maximum retries
          When the orchestrator handles it
          Then the chunk is set to FAILED with the last error message stored, the job
               continues with other chunks, and the final summary lists all FAILED chunks.
    AC-4: Given N worker slots are configured
          When translation runs
          Then no more than N chunks are in PROCESSING at any moment (test with a fake
               provider that records concurrency).
    AC-5: Given a chunk flagged "do not translate"
          When translation runs
          Then it is copied verbatim to the translated side and marked COMPLETED without
               calling the provider.
```

```
US-8: Resume an interrupted job

  As a Translator,
  I want to re-run the same command after a crash or Ctrl+C
  so that only unfinished chunks are translated and nothing is billed twice.

  Acceptance Criteria:
    AC-1: Given a job with 40 COMPLETED and 60 non-COMPLETED chunks in translation_state.db
          When I re-run the CLI with the same input
          Then exactly the 60 non-COMPLETED chunks are sent to the provider (verified via
               a counting fake provider).
    AC-2: Given a chunk was left in PROCESSING by a killed process
          When the job resumes
          Then that chunk is treated as PENDING (re-queued), not skipped and not stuck.
    AC-3: Given the input PDF has changed since the state DB was created
               (content hash differs)
          When I re-run the CLI
          Then the tool refuses to resume, explains the mismatch, and offers an explicit
               flag to start fresh (which archives or deletes the old state).
    AC-4: Given a state DB from a different tool version with an incompatible schema
          When I re-run the CLI
          Then a clear error is shown rather than a crash or silent corruption.
```

```
US-9: Switch translation providers

  As a Translator,
  I want to choose the provider with --provider (deepl / local / llm)
  so that I can trade cost, quality, and offline capability.

  Acceptance Criteria:
    AC-1: Given --provider deepl and DEEPL_API_KEY is not set
          When the CLI starts
          Then it fails fast with a message naming the missing variable, before extraction.
    AC-2: Given --provider local and the model files are not present
          When the CLI starts
          Then it fails fast with instructions on how to obtain the model.
    AC-3: Given a job started with provider A
          When it is resumed with provider B
          Then the tool warns that mixing providers may reduce consistency and requires
               an explicit confirmation flag (default: refuse).
```

### Stage 4 — Reassembly & Export (Spec §2.4)

```
US-10: Reassemble and export the translated book

  As a Translator,
  I want the translated chunks merged back in original order and exported
  so that I receive a readable book, not a pile of fragments.

  Acceptance Criteria:
    AC-1: Given all chunks are COMPLETED
          When export runs
          Then translated_book.md is produced with the same heading hierarchy
               (same count and nesting of #, ##, ### lines) as source_book.md.
    AC-2: Given translated_book.md exists
          When EPUB export runs
          Then translated_book.epub is produced, opens in a standard reader, has a
               table of contents derived from headings, and declares language "tr".
    AC-3: Given Turkish characters (ç, ğ, ı, İ, ö, ş, ü) in the translation
          When any export runs
          Then the characters are preserved byte-for-byte (UTF-8) in .md, .epub and .pdf.
    AC-4: Given the optional PDF exporter dependency is not installed
          When I request PDF output
          Then the tool reports that PDF export is unavailable and still produces
               .md and .epub; exit code indicates partial success.
    AC-5: Given some chunks are FAILED
          When export runs
          Then export is refused by default; with an explicit "allow partial" flag the
               failed chunks are emitted with a visible marker (e.g. the untranslated
               source wrapped in a clearly labelled block) and listed in the summary.
```

### CLI & Operations (Spec §3, §4)

```
US-11: Run the whole pipeline with one command

  As a Translator,
  I want a single command with --input, --output, --glossary, --provider
  so that the common case needs no configuration file.

  Acceptance Criteria:
    AC-1: Given a valid PDF and a configured provider
          When I run the CLI with only --input and --output
          Then all four stages run in order and the outputs listed in US-10 appear
               under --output.
    AC-2: Given I pass --help
          When the CLI runs
          Then every option is listed with a one-line description and its default.
    AC-3: Given the run completes
          When I inspect the exit code
          Then it is 0 for full success, a distinct non-zero code for partial
               (FAILED chunks), and another for hard failure.
```

```
US-12: See progress and status

  As a Translator,
  I want a live progress bar and a status command
  so that I know how far a multi-hour job has advanced.

  Acceptance Criteria:
    AC-1: Given translation is running in a terminal
          When chunks complete
          Then a progress indicator shows completed/total chunks, failed count, and
               elapsed time, updated at least once per completed chunk.
    AC-2: Given a state DB exists
          When I run the status sub-command
          Then it prints counts per status (PENDING/PROCESSING/COMPLETED/FAILED), the
               provider used, and the input file hash — without touching the provider.
    AC-3: Given output is not a TTY (e.g. piped to a log file)
          When the CLI runs
          Then progress falls back to plain line-based log output with no control codes.
```

```
US-13: Run individual stages

  As a Developer / Translator,
  I want to run a single stage (extract, glossary, translate, export)
  so that I can review or fix intermediate artifacts before continuing.

  Acceptance Criteria:
    AC-1: Given a sub-command for each stage
          When I run "extract" alone
          Then only source_book.md is produced and no provider is contacted.
    AC-2: Given I edited glossary.json by hand
          When I run "translate"
          Then the edited glossary is used (no re-discovery overwrites my edits unless
               I pass a force flag).
```

## 4. Functional Requirements

| ID | Requirement | Spec ref | User Story |
|---|---|---|---|
| FR-01 | The system shall accept an English PDF file as input and reject non-existent, non-PDF, or unreadable files before any processing. | §2.1 | US-2, US-11 |
| FR-02 | The system shall extract the PDF into a single normalized Markdown file (`source_book.md`) preserving headings, paragraphs, lists, tables, and code blocks. | §2.1 | US-1 |
| FR-03 | The system shall remove running headers, footers, and page numbers from extracted text. | §2.1 | US-1 |
| FR-04 | The system shall rejoin hyphenated words and continuing paragraphs split across page breaks. | §2.1 | US-1 |
| FR-05 | The system shall use a preferred high-fidelity extraction engine and fall back to a secondary engine when the preferred one is unavailable or fails, reporting which was used. | §2.1 | US-1 |
| FR-06 | The system shall detect a missing text layer and stop with an actionable message (OCR itself is out of scope unless the extraction engine provides it). | §2.1 | US-2 |
| FR-07 | The system shall detect the predominant source language and warn/stop when it is not English unless overridden. | §2.1 | US-2 |
| FR-08 | The system shall scan the extracted text for recurring entities (proper nouns, technical terms) and write candidates to `glossary.json` with occurrence counts and approval status. | §2.2 | US-3 |
| FR-09 | The system shall accept a user-supplied glossary file whose entries override auto-discovered ones and shall validate its structure before translation. | §2.2 | US-4 |
| FR-10 | The system shall apply approved glossary entries via the provider's native glossary mechanism when available, otherwise via prompt injection or post-processing, and record the strategy used. | §2.2 | US-5 |
| FR-11 | The system shall split Markdown into semantic chunks within a configurable size range (default 1000–1500 chars) without cutting sentences, headings, lists, tables, or code blocks, and the split shall be lossless. | §2.3 | US-6 |
| FR-12 | The system shall mark non-translatable blocks (code fences, URLs, inline code) and pass them through verbatim. | §2.3 | US-6, US-7 |
| FR-13 | The system shall expose a provider-agnostic translation interface with at least a DeepL implementation and one local/offline implementation; providers shall be selectable via `--provider`. | §2.3, §3 | US-9 |
| FR-14 | The system shall persist per-chunk state (chunk id, order, status, retry count, backoff/next-attempt time, error, source text, translated text, provider) in a SQLite database. | §2.3 | US-7, US-8 |
| FR-15 | The system shall retry failed provider calls with exponential backoff, honoring rate-limit signals, up to a configurable maximum; exhausted chunks shall be marked FAILED without aborting the job. | §2.3 | US-7 |
| FR-16 | The system shall process chunks concurrently with a configurable concurrency limit. | §2.3 | US-7 |
| FR-17 | On restart the system shall resume from non-COMPLETED chunks only, re-queue chunks left in PROCESSING, and never re-translate COMPLETED chunks. | §2.3 | US-8 |
| FR-18 | The system shall bind the state database to a content hash of the input and refuse to resume against a changed input without an explicit fresh-start flag. | §2.3 | US-8 |
| FR-19 | The system shall reassemble translated chunks in original order and hierarchy into `translated_book.md`. | §2.4 | US-10 |
| FR-20 | The system shall export `translated_book.epub` with a heading-derived table of contents, UTF-8 encoding, and language metadata "tr". | §2.4 | US-10 |
| FR-21 | The system shall optionally export PDF; absence of the optional dependency shall degrade gracefully. | §2.4 | US-10 |
| FR-22 | The system shall refuse export while FAILED chunks exist unless an explicit partial-export flag is given, in which case failed chunks are visibly marked. | §2.4 | US-10 |
| FR-23 | The system shall provide a CLI with `--input`, `--output`, `--glossary`, `--provider`, `--help`, per-stage sub-commands, and a status sub-command. | §3, §4 | US-11, US-12, US-13 |
| FR-24 | The system shall display terminal progress (completed/total/failed/elapsed) and fall back to plain logging when not attached to a TTY. | §3 | US-12 |
| FR-25 | The system shall return distinct exit codes for success, partial success, and failure. | §3 | US-11 |
| FR-26 | The system shall read secrets and configuration from environment variables (and optionally a `.env` file), never from CLI arguments or committed files. | §3 | US-9 |
| FR-27 | The system shall write a run summary at completion (chunk counts, provider, characters sent, duration, output paths). | §2.3, §3 | US-11, US-12 |

## 5. Non-Functional Requirements

| ID | Category | Requirement | Target | Spec ref |
|---|---|---|---|---|
| NFR-01 | Scale | Handle a 200+ page book (approx. 500k–800k characters, ~500–800 chunks) in a single run. | No hard cap below 1,000 pages | §1 |
| NFR-02 | Resilience | Process interruption (kill, crash, network loss) loses at most the chunks in flight; no state corruption. | State DB consistent after SIGKILL at any point | §2.3 |
| NFR-03 | Cost safety | No COMPLETED chunk is ever resent to a paid provider. | 0 duplicate sends (test-verified) | §2.3 |
| NFR-04 | Throughput | Concurrency configurable; default must stay under DeepL's documented rate limits. | Default concurrency to be set by architect; no 429 storms in normal use | §2.3 |
| NFR-05 | Consistency | Approved glossary terms rendered identically across the whole book. | 100% of occurrences (fixture test) | §2.2 |
| NFR-06 | Fidelity | Heading hierarchy, list structure, and code blocks survive round-trip. | Structural diff of source vs. translated = 0 differences | §2.1, §2.4 |
| NFR-07 | Portability | Runs on Windows, Linux, macOS with Python 3.10+. | CI matrix on at least Windows + Linux | §3 |
| NFR-08 | Modularity | Extractors, providers, and exporters are pluggable behind stable interfaces; adding a provider requires no change to the orchestrator. | New provider = one new module + registration | §2.3, §3 |
| NFR-09 | Optional deps | Heavy dependencies (marker-pdf, CTranslate2, weasyprint) are optional extras; the core install works without them. | `pip install book-translator` succeeds without GPU/ML libs | §3 |
| NFR-10 | Security | API keys only via environment; never logged, never persisted in the state DB or outputs. | Log/DB grep for key returns nothing | §3 |
| NFR-11 | Observability | Structured logging with a run id; every provider call logged with chunk id, attempt, latency, outcome. | Log level configurable | §3 |
| NFR-12 | Code quality | Strict type hints (mypy clean), English identifiers/docstrings/commits, unit tests for chunker and glossary at minimum. | mypy strict passes; tests green in CI | §3, §4 |
| NFR-13 | Encoding | All text I/O is UTF-8; Turkish characters preserved end-to-end, including on Windows consoles. | Round-trip fixture test | §1 |
| NFR-14 | Usability | A first-time user can run a full translation with `--help` alone as documentation. | README quick-start ≤ 5 commands | §3 |
| NFR-15 | Privacy | Book content is sent only to the selected provider; no telemetry. | Documented in README | §3 |

## 6. Edge Case Catalog

| # | Edge case | Expected behavior |
|---|---|---|
| E-01 | Scanned PDF with no text layer | Detect (near-zero extracted characters per page); stop with message suggesting OCR. If the preferred engine has built-in OCR, use it and warn about quality. Do not create state. |
| E-02 | Hyphenation across pages / lines | Rejoin when the hyphen ends a line and the continuation starts lowercase; keep genuine compound hyphens ("well-known") intact. Ambiguous cases logged. |
| E-03 | Running headers/footers vary slightly (e.g. chapter title in header) | Removal based on repetition frequency across pages, not exact single string; must not delete a real first-line paragraph that happens to repeat. |
| E-04 | Empty or near-empty pages (blank, image-only) | Skipped silently; page count in summary notes skipped pages. |
| E-05 | Code blocks, tables, inline code in Markdown | Never split, never translated (code); tables are translated cell-by-cell only if provider preserves pipe syntax, otherwise passed through with a warning. |
| E-06 | Single paragraph or block larger than provider limit | Further split at sentence boundaries; sub-chunks are re-merged on reassembly; hierarchy preserved. |
| E-07 | DeepL 429 rate limit | Backoff with Retry-After; concurrency reduced temporarily; not counted as a hard failure until max retries. |
| E-08 | DeepL quota exhausted (456) / auth failure (403) | Non-retryable: pause the whole job, keep state, exit with a clear message; resume later without loss. |
| E-09 | Process killed mid-chunk | Chunk remains PROCESSING; on resume it is re-queued. Translated text is only written together with the COMPLETED status (atomic). |
| E-10 | Glossary term conflicts (same source term, two targets; overlapping terms "New York" vs "York") | User-supplied entry wins over discovered; duplicate keys in one file are a validation error; longest match applied first. |
| E-11 | Glossary term is also a common word ("Will" as a name) | Discovery marks as ambiguous; not auto-approved; user decides. |
| E-12 | Resume with changed input file | Hash mismatch; refuse unless fresh-start flag; old state archived, not silently overwritten. |
| E-13 | Resume with a different provider | Warn; require confirmation flag. |
| E-14 | Non-English source detected | Warn with detected language; stop unless overridden (DeepL can auto-detect but glossary is EN→TR only). |
| E-15 | Turkish characters and dotless/dotted I in EPUB/PDF | UTF-8 everywhere; EPUB declares `lang="tr"`; PDF exporter must embed a font with Turkish glyphs. Windows console encoding forced to UTF-8. |
| E-16 | Provider returns empty or truncated translation | Treated as failure and retried; if repeated, chunk FAILED with reason "empty response". |
| E-17 | Provider alters Markdown syntax (drops `#`, changes `**`) | Post-check compares structural markers; mismatch flagged as WARNING and chunk marked for review (not silently accepted). |
| E-18 | Output directory not writable / disk full during export | Fail with clear message; state DB untouched; re-run export only. |
| E-19 | Very large book (1,000+ pages) | Memory: chunks are streamed from DB, not held all in memory; progress still accurate. |
| E-20 | Two CLI processes started on the same state DB | Second process detects a lock/lease and refuses to start (avoids double-sending). |
| E-21 | Footnotes / endnotes | Extracted as Markdown footnotes if the engine supports them; otherwise appended as a section; never lost. Exact treatment is an open question (Q5). |
| E-22 | Images / figures in PDF | Not translated; kept as image references or dropped with a placeholder line (open question Q6). Captions are translated. |

## 7. Dependencies and Assumptions

**External services**
- DeepL API (Free or Pro) — requires `DEEPL_API_KEY`; Free tier has a monthly character cap (500k) which a 200-page book can exceed. Glossary API availability depends on plan.
- Optional LLM provider for post-editing — API key via environment variable; provider choice is an open question (Q3).

**Required Python dependencies (core install)**
- Python 3.10+
- CLI framework (typer or click) + rich
- PyMuPDF (fitz) — fallback extractor, always installed
- deepl (official client), requests
- SQLite via stdlib `sqlite3` or SQLAlchemy (architect decides)
- ebooklib, markdown
- tenacity or custom backoff

**Optional / heavy dependencies (extras)**
- marker-pdf — preferred extractor; pulls PyTorch and models (multi-GB); may be slow on CPU. Must be an optional extra.
- CTranslate2 + NLLB-200 / OPUS-MT models — offline translation; model download required; GPU optional.
- weasyprint — PDF export; needs system libraries (Pango/Cairo/GTK on Windows), historically painful to install on Windows.
- pandoc — alternative EPUB path; system binary, may be absent. Tool must work without it (ebooklib as default).

**Assumptions**
- A-1: Source books are English; target is always Turkish (single language pair in v1).
- A-2: Input PDFs are mostly digital with a text layer; scanned books are a secondary case.
- A-3: The operator has a valid DeepL key with enough quota, or accepts local NMT quality.
- A-4: Runs are single-machine, single-user; no multi-node distribution.
- A-5: Marker quality on CPU is acceptable for the operator's patience (tens of minutes for 200 pages); otherwise PyMuPDF fallback is used.
- A-6: The user will review the auto-generated glossary before the translation stage; fully automatic glossary approval is not expected to be accurate.
- A-7: Copyright of the input material is the operator's responsibility; the tool does not enforce it.

## 8. Scope Boundaries

**In scope**
- PDF (digital) → Markdown → Turkish Markdown / EPUB, optional PDF.
- Header/footer/page-number removal; cross-page sentence reassembly.
- Glossary discovery, user override, provider glossary upload / prompt injection.
- Semantic chunking; DeepL provider; one local NMT provider; pluggable interface for others.
- SQLite state, retry/backoff, resume, concurrency control.
- CLI with full-run, per-stage, and status commands; progress display; summary.
- Unit tests for chunker and glossary; fixture-based tests for extraction cleanup.

**Out of scope (v1)**
- OCR of scanned PDFs (unless the extraction engine handles it natively).
- Source formats other than PDF (EPUB/DOCX input) — pluggable extractor interface leaves the door open.
- Language pairs other than EN→TR.
- LLM post-editing quality pass — optional, pluggable, not required for v1 completion.
- Human review UI / web interface.
- Translation memory across different books.
- Layout-faithful PDF reproduction (two-column, exact fonts) — export is reflowable.
- Image translation / figure text.
- Distributed or multi-machine processing.

## 9. Open Questions

| # | Question | Why it matters | Suggested default |
|---|---|---|---|
| Q1 | Is a DeepL Pro key available, or only Free? Free caps at 500k chars/month and may lack the Glossary API. | Determines whether native glossary support is realistic and whether the local NMT path is primary or fallback. | Assume Pro; design Free-tier quota exhaustion as a clean pause (E-08). |
| Q2 | Must the local NMT path (CTranslate2 + NLLB/OPUS-MT) be fully working in v1, or is a stub interface with DeepL as the only real provider acceptable for the first release? | NLLB EN→TR quality on literary text is noticeably lower; it also adds multi-GB downloads and CPU/GPU concerns. | v1 ships DeepL fully; local NMT as a working but "best-effort" provider behind an extra. |
| Q3 | Is LLM post-editing wanted, and with which provider? | Adds a second API key, cost, and a prompt-injection glossary strategy. | Pluggable interface only; no concrete LLM provider in v1. |
| Q4 | Should marker-pdf be the default extractor, or PyMuPDF (fast, light) with marker opt-in? | Marker is a heavy install and slow on CPU; a default that fails to install hurts first-run experience. | PyMuPDF default; marker enabled with `--extractor marker` when installed. |
| Q5 | How should footnotes/endnotes be treated — inline Markdown footnotes, appended section, or dropped? | Affects extraction, chunking (footnote reference integrity), and EPUB rendering. | Keep as Markdown footnotes when the engine can identify them; otherwise append as a "Notes" section. |
| Q6 | Should images/figures be carried into the EPUB, or replaced by a placeholder? | Carrying images requires extracting and packaging them; affects EPUB size and exporter complexity. | Placeholder line with figure number in v1; captions translated. |
| Q7 | Should glossary approval be interactive (CLI prompts) or file-based only? | Interactive review changes the CLI flow and testability. | File-based: generate, user edits `glossary.json`, re-run `translate`. |
| Q8 | Is PDF export required for v1 given weasyprint's Windows installation difficulty? | Could block delivery on the developer's primary OS (Windows). | Optional extra; document limitation; EPUB is the primary rich output. |
| Q9 | Chunk size unit: characters (as spec) or tokens/sentences? Should chunks carry the previous chunk as read-only context for the provider? | Context carry-over improves pronoun/tense consistency but doubles characters billed on DeepL. | Characters; no context carry-over for DeepL; optional carry-over for LLM providers. |

---
✅ Business Analyst tamamlandı.
➡️  Sonraki adım: Solution Architect
