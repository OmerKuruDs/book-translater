# High-Level Design: `book-translator`

Standalone Python 3.10+ CLI. English PDF → normalized Markdown → glossary → chunked, resumable translation (EN→TR) → Markdown / EPUB / optional PDF.

Design inputs: `docs/01_business_analysis.md` (approved) and the accepted defaults for Q1–Q9.

Decision summary (binding for the downstream agents):

| Topic | Decision |
|---|---|
| Solution type | Single installable Python package, CLI entry point `book-translator` |
| Default extractor | PyMuPDF (`pymupdf`); `marker-pdf` opt-in via `--extractor marker`, auto-fallback to PyMuPDF with a reported warning |
| Providers | DeepL fully implemented; local NMT (CTranslate2 + OPUS-MT/NLLB) best-effort behind `[local]` extra; LLM post-editing = abstract interface only |
| Chunk unit | Characters, default 1000–1500, no context carry-over for DeepL |
| Glossary approval | File-based (`glossary.json`), user file has precedence |
| Footnotes / images | Markdown footnotes when identifiable, else "Notes" section; images → non-translatable placeholder token, captions translated |
| State store | SQLite via SQLAlchemy 2.x (sync engine, WAL) |
| Retry | Custom persisted backoff policy (not tenacity) |
| CLI stack | typer + rich; pydantic-settings for config/env/.env |
| Error handling | `Result[T]` across every layer boundary; exceptions converted at adapter edges |

---

## 1. Component Diagram and Layering

### 1.1 Package layout

The original spec's `src/` tree is honored; added files are marked `(+)`.

```
src/                                  -> installed as package "book_translator"
  cli.py                              Typer app: commands, option parsing, exit-code mapping, progress UI
  config.py                           Settings (pydantic-settings): env, .env, defaults; validation
  logging_setup.py               (+)  Logging config: run_id correlation, JSONL file + rich console, secret redaction
  domain/                        (+)
    models.py                    (+)  Dataclasses/enums: ExtractedDocument, Block, Chunk, GlossaryEntry, TranslationResult, JobSummary ...
    result.py                    (+)  Result[T] = Ok[T] | Err, AppError, ErrorCode, ErrorScope
  database/
    models.py                         SQLAlchemy ORM models (designed by DB Architect)
    session.py                        Engine/session factory, PRAGMAs (WAL), schema-version check
    repository.py                (+)  JobRepository, ChunkRepository, LeaseRepository (Result-returning)
  extractors/
    base.py                           BaseExtractor ABC, ExtractOptions, registry
    pymupdf_extractor.py         (+)  Default extractor: page blocks -> cleaned Markdown
    marker_extractor.py               Opt-in extractor: marker-pdf -> normalized Markdown
    cleanup.py                   (+)  Header/footer detection, page-number stripping, hyphen rejoin, paragraph merge
    normalizer.py                (+)  Arbitrary Markdown -> "BT-Markdown" subset (unwrap, ATX headings, image tokens)
    langdetect.py                (+)  Stopword-ratio language detection (no external dependency)
  glossary/
    manager.py                        Load/validate/merge glossary files, effective glossary, precedence
    discovery.py                 (+)  Capitalized n-gram / acronym discovery with stop-list
    schema.py                    (+)  glossary.json pydantic schema + version
  translators/
    base.py                           BaseTranslator ABC, TranslatorCapabilities, TranslationRequest/ProviderResponse, registry
    deepl_translator.py               DeepL implementation (native glossary, error classification)
    local_nmt.py                      CTranslate2 provider (best-effort; post-replacement glossary strategy)
    llm_base.py                  (+)  BaseLLMTranslator: prompt-injection glossary strategy, no concrete provider
    protect.py                   (+)  Inline-code / URL protection (placeholders), provider-specific tag strategy
  pipeline/
    chunker.py                        Block parser (BT-Markdown) + packer + oversize sentence splitter + lossless verifier
    segmenter.py                 (+)  Chunk text -> translatable segments (marker prefix/suffix stripping) and re-application
    backoff.py                   (+)  BackoffPolicy (exp + full jitter + Retry-After), AdaptiveLimiter (AIMD concurrency)
    orchestrator.py                   asyncio job runner: claim/dispatch/retry/complete/cancel; stage coordination
    assembler.py                 (+)  Ordered chunk stream -> AssembledDocument (partial markers, structure check)
    glossary_check.py            (+)  Post-translation glossary/structure verification -> warnings/review flag
  exporters/
    base.py                      (+)  BaseExporter ABC, ExportOptions, ExportArtifact, registry
    markdown_exporter.py              Writes translated_book.md
    epub_exporter.py                  ebooklib EPUB (chapters, TOC, lang=tr, CSS)
    pdf_exporter.py              (+)  weasyprint (optional extra), font embedding
    fonts/DejaVuSans.ttf (+), DejaVuSerif.ttf (+)   bundled fonts with Turkish glyphs (package data)
tests/
  conftest.py                    (+)  fakes (FakeTranslator, IdentityTranslator), fixture PDF builders, tmp job dirs
  test_chunker.py                     block parser, packing rules, heading rule, oversize split, lossless property test
  test_glossary.py                    discovery, stop-list, precedence, validation errors, post-check
  test_orchestrator.py           (+)  concurrency cap, retry/backoff, 429/456/403 classification, resume, re-queue, hash mismatch, lock
  test_extractor_cleanup.py      (+)  header/footer, page numbers, hyphen rejoin, paragraph merge, no-text-layer, language detection
  test_exporters.py              (+)  heading hierarchy parity, EPUB metadata/TOC, UTF-8 round trip, partial markers
  test_result.py                 (+)  Result/AppError semantics
README.md
requirements.txt                      pinned core dependencies (mirror of pyproject core)
pyproject.toml                        hatchling; extras [marker], [local], [pdf], [dev]
```

`pyproject.toml` maps the `src` directory to the import name `book_translator` (hatchling `[tool.hatch.build.targets.wheel.sources] "src" = "book_translator"`). All internal imports are absolute `book_translator.*`; never `src.*`.

### 1.2 Component diagram

```mermaid
flowchart TD
    CLI[cli.py<br/>Typer commands, exit codes, progress UI]
    CFG[config.py<br/>Settings]
    LOG[logging_setup.py]

    subgraph PIPELINE["pipeline/ (application layer)"]
        ORCH[orchestrator.py<br/>stage coordinator + async job runner]
        CHK[chunker.py]
        SEG[segmenter.py]
        BKF[backoff.py]
        ASM[assembler.py]
        GCK[glossary_check.py]
    end

    subgraph SERVICES["services (domain adapters)"]
        EXT[extractors/<br/>pymupdf | marker]
        GLO[glossary/<br/>manager, discovery]
        TRN[translators/<br/>deepl | local | llm_base]
        EXP[exporters/<br/>markdown | epub | pdf]
    end

    subgraph DATA["database/"]
        REPO[repository.py<br/>Job/Chunk/Lease repos]
        ORM[models.py + session.py]
    end

    DOM[domain/<br/>models.py, result.py]

    CLI --> CFG
    CLI --> LOG
    CLI --> ORCH
    ORCH --> CHK
    ORCH --> SEG
    ORCH --> BKF
    ORCH --> ASM
    ORCH --> GCK
    ORCH --> EXT
    ORCH --> GLO
    ORCH --> TRN
    ORCH --> EXP
    ORCH --> REPO
    REPO --> ORM
    ORM --> SQLITE[(translation_state.db)]
    TRN --> DEEPL[DeepL API]
    TRN --> CT2[CTranslate2 model]
    EXT --> PDF[input.pdf]
    EXP --> OUT[/translated_book.md / .epub / .pdf/]
    PIPELINE -.-> DOM
    SERVICES -.-> DOM
    DATA -.-> DOM
```

### 1.3 Responsibilities

| Module | Owns | Must not |
|---|---|---|
| `cli.py` | Argument parsing, settings composition (flag > env > .env > default), console output, progress bar, mapping `Result` → exit code, UTF-8 console setup, signal wiring | Contain business rules; touch repository or providers directly |
| `config.py` | Typed settings, validation of value ranges, `.env` loading | Perform I/O beyond reading env/.env |
| `pipeline/orchestrator.py` | Stage sequencing (`extract` → `glossary` → `chunk` → `translate` → `export`), job lifecycle, resume/lock/hash checks, async dispatch, retry decisions, cancellation | Know provider-specific error types; build SQL; parse PDFs |
| `pipeline/chunker.py` | Block grammar, packing, oversize splitting, lossless verification | Call providers or DB |
| `pipeline/segmenter.py` | Chunk text ↔ translatable segments; inline protection hand-off | Persist anything |
| `pipeline/backoff.py` | Delay computation, adaptive concurrency limiter | Decide retryability (that comes from `AppError.scope`) |
| `pipeline/assembler.py` | Ordered reassembly, partial markers, structural parity check | Write files (exporters do) |
| `extractors/*` | PDF → `ExtractedDocument` (BT-Markdown + metadata + warnings) | Chunk, translate, persist |
| `glossary/*` | Discovery, file schema, merge/precedence, effective glossary | Call provider APIs (the translator binds the glossary) |
| `translators/*` | Provider I/O, exception → `AppError` classification, native glossary binding, inline protection strategy | Retry internally (client retries disabled), touch DB, know chunk ordering |
| `exporters/*` | `AssembledDocument` → file(s) | Read DB or chunks |
| `database/*` | Persistence, atomic state transitions, lease, schema versioning | Contain business decisions (e.g. what is retryable) |
| `domain/*` | Shared value types, `Result`, errors | Import anything from other layers |

### 1.4 Import rules (allowed direction only)

```
cli  →  config, logging_setup, pipeline, domain, (extractors/translators/exporters registries: read-only, for --help listings)
pipeline  →  extractors, glossary, translators, exporters, database.repository, database.session, domain, config, logging_setup
extractors / glossary / translators / exporters  →  domain, config (settings values only)
database  →  domain
domain  →  (stdlib, pydantic only)
```

Forbidden: `cli → database`, `cli → translators.deepl_translator` (only the registry), any service → `pipeline`, any service → another service package (e.g. `exporters → glossary`), `database → services`. Enforced by an import-linter contract in `[dev]` (or a simple test that walks `ast` imports).

Stage artifacts live in the `--output` directory:

```
<output>/
  source_book.md            extract stage
  glossary.json             glossary stage (discovered + user-edited)
  translation_state.db      job/chunk state (SQLite)
  translated_book.md        export stage
  translated_book.epub
  translated_book.pdf       optional
  summary.json              JobSummary of the last run
  logs/run-<run_id>.jsonl
  state-archive/<timestamp>/  archived DB on --fresh
```

---

## 2. Key Interfaces

Signatures only; no implementations. Python 3.10 typing (`Union`, `TypeVar`; no 3.12 syntax).

### 2.1 Result and error types (`domain/result.py`)

```python
T = TypeVar("T")

class ErrorScope(str, Enum):
    CHUNK_RETRYABLE = "chunk_retryable"   # back off and retry this chunk
    CHUNK_FATAL     = "chunk_fatal"       # mark this chunk FAILED, continue job
    JOB_FATAL       = "job_fatal"         # pause/stop the whole job, keep state
    USER            = "user"              # invalid input/config; nothing started

class ErrorCode(str, Enum):
    INPUT_NOT_FOUND = "input_not_found"; INPUT_NOT_PDF = "input_not_pdf"; INPUT_UNREADABLE = "input_unreadable"
    NO_TEXT_LAYER = "no_text_layer"; NON_ENGLISH_SOURCE = "non_english_source"
    EXTRACTOR_UNAVAILABLE = "extractor_unavailable"; EXTRACTION_FAILED = "extraction_failed"
    GLOSSARY_INVALID = "glossary_invalid"; GLOSSARY_BIND_FAILED = "glossary_bind_failed"
    PROVIDER_CONFIG = "provider_config"; PROVIDER_AUTH = "provider_auth"; PROVIDER_QUOTA = "provider_quota"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"; PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_BAD_REQUEST = "provider_bad_request"; PROVIDER_EMPTY_RESPONSE = "provider_empty_response"
    STATE_LOCKED = "state_locked"; STATE_HASH_MISMATCH = "state_hash_mismatch"
    STATE_PROVIDER_MISMATCH = "state_provider_mismatch"; STATE_SCHEMA_INCOMPATIBLE = "state_schema_incompatible"
    STATE_CORRUPT = "state_corrupt"; STATE_NOT_FOUND = "state_not_found"
    CHUNK_INTEGRITY = "chunk_integrity"      # lossless verification failed
    EXPORT_REFUSED_PARTIAL = "export_refused_partial"; EXPORTER_UNAVAILABLE = "exporter_unavailable"; EXPORT_FAILED = "export_failed"
    INTERRUPTED = "interrupted"; INTERNAL = "internal"

@dataclass(frozen=True)
class AppError:
    code: ErrorCode
    message: str                       # human-readable, safe to print (no secrets)
    scope: ErrorScope
    retry_after_s: Optional[float] = None   # from Retry-After when present
    context: Mapping[str, Any] = field(default_factory=dict)   # chunk_id, attempt, http_status ...
    cause: Optional[str] = None        # repr of the original exception (redacted), for logs only

@dataclass(frozen=True)
class Ok(Generic[T]):
    value: T
    def is_ok(self) -> Literal[True]: ...

@dataclass(frozen=True)
class Err:
    error: AppError
    def is_ok(self) -> Literal[False]: ...

Result = Union[Ok[T], Err]
```

Rules:

- A function that can fail for an expected reason returns `Result[T]`. It never raises across a layer boundary.
- Exception → Result boundary is the outermost public method of every adapter (`extract`, `translate`, `prepare`, `export`, every repository method). Third-party exceptions are caught there, classified, and returned as `Err`.
- Orchestrator task wrapper: any exception escaping a chunk task (programming error) becomes `Err(INTERNAL, CHUNK_FATAL)` with traceback logged; the job continues.
- CLI: `Result` → exit code (Section 9). A raw exception reaching the CLI is a bug; a last-resort handler logs it with `run_id` and exits 1.
- Invariant violations detected at runtime (e.g. lossless check fails) are returned as `Err(CHUNK_INTEGRITY, JOB_FATAL)`, not raised.

### 2.2 Extractor (`extractors/base.py`)

```python
@dataclass(frozen=True)
class ExtractOptions:
    header_footer_page_ratio: float = 0.30    # repeated on >= 30% of pages (min 3) -> running header/footer
    band_ratio: float = 0.12                  # top/bottom 12% of page height is the candidate band
    min_chars_per_page: int = 50              # median below -> NO_TEXT_LAYER
    heading_size_ratio: float = 1.15          # >= body_size * ratio -> heading candidate
    footnote_size_ratio: float = 0.85
    keep_images_as_placeholders: bool = True

ProgressCallback = Callable[[int, int], None]     # (done, total) pages

class BaseExtractor(ABC):
    name: ClassVar[str]                            # "pymupdf" | "marker"
    supports_ocr: ClassVar[bool]
    supports_footnotes: ClassVar[bool]

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool: ...             # import probe; never raises

    @abstractmethod
    def extract(self, pdf_path: Path, options: ExtractOptions,
                progress: Optional[ProgressCallback] = None) -> Result[ExtractedDocument]: ...
```

Registry: `get_extractor(name: str) -> Result[BaseExtractor]`; `"marker"` resolves to `MarkerExtractor` if available else `Err(EXTRACTOR_UNAVAILABLE, USER)` which the orchestrator turns into a fallback-with-warning when `--extractor marker` was requested (AC US-1/5).

### 2.3 Translator (`translators/base.py`)

```python
@dataclass(frozen=True)
class TranslatorCapabilities:
    supports_glossary: bool          # native provider glossary
    supports_batch: bool             # list of texts in one request
    max_chars_per_request: int       # sum of payload chars per request (DeepL: 100_000 safe; local: model-bound)
    max_texts_per_request: int       # DeepL: 50
    supports_context: bool           # read-only context field (LLM providers)
    supports_tag_protection: bool    # XML/HTML tag ignore (DeepL) vs sentinel tokens

class GlossaryStrategy(str, Enum):
    NATIVE = "native"; PROMPT = "prompt"; POST_REPLACE = "post_replace"; NONE = "none"

@dataclass(frozen=True)
class GlossaryBinding:
    strategy: GlossaryStrategy
    provider_glossary_id: Optional[str]      # DeepL glossary id
    glossary_hash: str                       # sha256 of normalized approved entries

@dataclass(frozen=True)
class TranslationRequest:
    job_id: str; run_id: str; chunk_id: int; attempt: int
    source_lang: str = "EN"; target_lang: str = "TR"
    glossary: Optional[GlossaryBinding] = None
    context: Optional[str] = None            # only used when supports_context

@dataclass(frozen=True)
class ProviderResponse:
    texts: List[str]                         # same length/order as input
    chars_billed: int
    latency_ms: int
    detected_source_lang: Optional[str] = None

class BaseTranslator(ABC):
    name: ClassVar[str]                      # "deepl" | "local" | "llm:<vendor>"
    capabilities: ClassVar[TranslatorCapabilities]

    @classmethod
    @abstractmethod
    def create(cls, settings: Settings) -> Result[BaseTranslator]: ...   # fail-fast: key present, model files present

    @abstractmethod
    async def prepare(self, glossary: EffectiveGlossary, run_id: str) -> Result[GlossaryBinding]: ...

    @abstractmethod
    async def translate(self, texts: Sequence[str], request: TranslationRequest) -> Result[ProviderResponse]: ...

    async def aclose(self) -> None: ...
```

Contract: `translate` performs exactly one provider round-trip (or `ceil(len(texts)/max_texts_per_request)` sequential requests) and never retries internally; the DeepL client is created with `max_retries=0`. Every failure is returned as `Err` with an `ErrorScope` (classification table in Section 5.3). Sync SDKs are wrapped with `asyncio.to_thread`.

`llm_base.py` defines `BaseLLMTranslator(BaseTranslator)` with abstract `complete(prompt: str) -> Result[str]` and a concrete prompt builder that injects the glossary (Section 7.5). No vendor implementation ships in v1.

### 2.4 Exporter (`exporters/base.py`)

```python
@dataclass(frozen=True)
class ExportOptions:
    title: Optional[str]; author: Optional[str]; language: str = "tr"
    chapter_split_level: int = 1            # H1; auto-falls back to H2
    css_path: Optional[Path] = None
    font_path: Optional[Path] = None         # PDF font override

@dataclass(frozen=True)
class ExportArtifact:
    path: Path; format: str; bytes_written: int; warnings: List[str]

class BaseExporter(ABC):
    name: ClassVar[str]                      # "markdown" | "epub" | "pdf"
    file_suffix: ClassVar[str]
    optional: ClassVar[bool]                 # True -> unavailable means "partial", not failure

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool: ...

    @abstractmethod
    def export(self, document: AssembledDocument, target_dir: Path,
               options: ExportOptions) -> Result[ExportArtifact]: ...
```

### 2.5 Repositories (`database/repository.py`)

All sync, `Result`-returning, one short transaction per call. The orchestrator calls them via `asyncio.to_thread` serialized by an `asyncio.Lock` (SQLite is single-writer).

```python
class JobRepository(Protocol):
    def get_or_create_job(self, spec: JobSpec) -> Result[JobRecord]: ...
    def get_job(self, job_id: str) -> Result[Optional[JobRecord]]: ...
    def update_job(self, job_id: str, patch: JobPatch) -> Result[None]: ...   # provider, glossary binding, status, md hash
    def start_run(self, job_id: str, run_id: str, command: str) -> Result[None]: ...
    def finish_run(self, job_id: str, run_id: str, outcome: RunOutcome) -> Result[None]: ...
    def schema_version(self) -> Result[int]: ...

class ChunkRepository(Protocol):
    def replace_all(self, job_id: str, chunks: Iterable[Chunk]) -> Result[int]: ...            # chunk stage (only when no COMPLETED rows or --fresh)
    def requeue_processing(self, job_id: str) -> Result[int]: ...                              # PROCESSING -> PENDING at run start
    def claim_pending(self, job_id: str, limit: int, now: datetime, run_id: str) -> Result[List[Chunk]]: ...
        # atomic: PENDING & (next_attempt_at IS NULL OR <= now) ORDER BY order LIMIT n  ->  PROCESSING
    def complete(self, job_id: str, chunk_id: int, result: TranslationResult) -> Result[bool]: ...
        # single UPDATE ... WHERE status='PROCESSING'; returns False if precondition failed
    def reschedule(self, job_id: str, chunk_id: int, error: AppError, next_attempt_at: datetime) -> Result[None]: ...
        # PROCESSING -> PENDING, retry_count += 1, last_error
    def fail(self, job_id: str, chunk_id: int, error: AppError) -> Result[None]: ...          # PROCESSING -> FAILED
    def release(self, job_id: str, chunk_ids: Sequence[int]) -> Result[int]: ...             # cancel: PROCESSING -> PENDING, retry_count unchanged
    def reset_failed(self, job_id: str) -> Result[int]: ...                                   # --retry-failed
    def counts(self, job_id: str) -> Result[StatusCounts]: ...
    def earliest_next_attempt(self, job_id: str) -> Result[Optional[datetime]]: ...
    def iter_ordered(self, job_id: str, batch_size: int = 200) -> Iterator[Chunk]: ...        # streaming for assembly (E-19)
    def totals(self, job_id: str) -> Result[ChunkTotals]: ...                                 # chars sent, billed, completed etc.

class LeaseRepository(Protocol):
    def acquire(self, job_id: str, holder: LeaseHolder, stale_after_s: int) -> Result[bool]: ...   # BEGIN IMMEDIATE; False if held & fresh
    def heartbeat(self, job_id: str, holder: LeaseHolder) -> Result[None]: ...
    def release(self, job_id: str, holder: LeaseHolder) -> Result[None]: ...
    def force_break(self, job_id: str) -> Result[None]: ...
```

---

## 3. Domain Model (`domain/models.py`)

Dataclasses (frozen where immutable); pydantic only for file schemas (`glossary.json`, `summary.json`) and settings.

```python
class BlockKind(str, Enum):
    HEADING, PARAGRAPH, LIST, TABLE, CODE, BLOCKQUOTE, FOOTNOTE, IMAGE, HR, BLANK

class ChunkKind(str, Enum):
    TEXT      # packed prose: headings + paragraphs + lists + blockquotes + footnotes
    HEADING   # heading-only chunk (only possible at end of document)
    LIST; TABLE; CODE; IMAGE; HR; FOOTNOTE   # single-block chunks

class ChunkStatus(str, Enum): PENDING, PROCESSING, COMPLETED, FAILED

@dataclass(frozen=True)
class PageInfo:
    number: int; char_count: int; skipped: bool; had_images: int

@dataclass(frozen=True)
class ExtractedDocument:
    markdown: str                       # BT-Markdown, "\n" newlines, UTF-8, ends with "\n"
    extractor_name: str
    fallback_used: bool
    pages: List[PageInfo]
    detected_language: Optional[str]    # "en", "tr", "de", ... or None
    language_confidence: float          # stopword ratio of the winning language
    removed_headers_footers: List[str]  # normalized patterns, for the log/summary
    footnote_mode: Literal["markdown", "notes_section", "none"]
    image_count: int
    warnings: List[str]

@dataclass(frozen=True)
class Block:                            # chunker-internal, not persisted
    block_id: int                       # 1-based sequential
    kind: BlockKind
    start: int; end: int                # char offsets in markdown; blocks tile the document exactly
    level: int = 0                      # heading level, list nesting
    translatable: bool = True

@dataclass(frozen=True)
class Chunk:
    chunk_id: int                       # == order, 1-based
    order: int
    kind: ChunkKind
    translatable: bool                  # False -> copied verbatim, no provider call
    source_text: str                    # exact substring of source_book.md (includes trailing blank lines)
    content_hash: str                   # sha256(source_text)
    char_count: int                     # len(source_text)
    parent_block_id: Optional[int]      # set only for sub-splits of an oversize block
    sub_index: Optional[int]            # 0..n-1 within parent; None if not a sub-split
    sub_count: Optional[int]
    heading_path: Tuple[str, ...]       # nearest H1/H2/H3 titles (for logs, EPUB chapter mapping)
    # runtime state (persisted, mutable in DB only):
    status: ChunkStatus = ChunkStatus.PENDING
    retry_count: int = 0
    next_attempt_at: Optional[datetime] = None
    last_error: Optional[str] = None
    translated_text: Optional[str] = None
    review_flag: bool = False           # structure/glossary post-check mismatch (E-17)
    warnings: Tuple[str, ...] = ()

class GlossaryStatus(str, Enum): PROPOSED, APPROVED, REJECTED, AMBIGUOUS

@dataclass(frozen=True)
class GlossaryEntry:
    source: str; target: str; count: int; status: GlossaryStatus
    origin: Literal["discovered", "user"]; note: str = ""

@dataclass(frozen=True)
class EffectiveGlossary:
    entries: Tuple[GlossaryEntry, ...]  # APPROVED with non-empty target only, sorted longest source first
    glossary_hash: str

@dataclass(frozen=True)
class TranslationResult:
    chunk_id: int
    translated_text: str                # full chunk text with markers/whitespace re-applied
    provider: str
    chars_sent: int                     # payload chars sent (0 for non-translatable)
    chars_billed: int                   # provider-reported when available
    latency_ms: int
    attempts: int
    glossary_strategy: GlossaryStrategy
    warnings: Tuple[str, ...]
    review_flag: bool

@dataclass(frozen=True)
class StatusCounts: pending: int; processing: int; completed: int; failed: int; total: int

@dataclass(frozen=True)
class JobSummary:                       # written to summary.json and printed at the end
    job_id: str; run_id: str; command: str
    input_path: str; input_sha256: str; source_md_sha256: Optional[str]
    provider: Optional[str]; glossary_strategy: Optional[GlossaryStrategy]; glossary_entries_applied: int
    extractor: Optional[str]; fallback_used: bool
    counts: StatusCounts
    failed_chunk_ids: List[int]; review_chunk_ids: List[int]
    chars_sent_this_run: int; chars_sent_total: int
    started_at: datetime; finished_at: datetime; duration_s: float
    outputs: Dict[str, str]             # {"markdown": path, "epub": path, "pdf": path}
    exit_code: int
    warnings: List[str]
```

### 3.1 What must be persisted (brief for the DB Architect; no columns/tables here)

| Aggregate | Facts to persist |
|---|---|
| Job (one per output dir) | job id, input path, input sha256, tool version, schema version, source_book.md sha256 (after extract), chunk config used (min/max), provider name, glossary binding (strategy, provider glossary id, glossary hash), job status, created/updated timestamps |
| Chunk | all `Chunk` fields above incl. runtime state; `translated_text` written only together with COMPLETED; `completed_by_run_id`; `chars_sent`, `chars_billed`, `latency_ms`, `attempts` |
| Run (one per CLI invocation) | run id, command, started/finished, outcome, exit code, counters (chunks completed/failed this run, chars sent, provider calls, 429 count), provider used, version |
| Lease | holder pid + hostname + run id, acquired_at, heartbeat_at |
| Schema meta | schema version integer, created-by tool version |

Never persisted: API keys, `.env` content, provider raw responses beyond translated text.

### 3.2 Chunk state machine

```
                 claim (run start / dispatch)
   PENDING ───────────────────────────────▶ PROCESSING
     ▲  ▲                                      │  │  │
     │  │ reschedule (CHUNK_RETRYABLE,         │  │  │ complete (atomic: text + COMPLETED)
     │  │ retry_count < max) next_attempt_at   │  │  ▼
     │  └──────────────────────────────────────┘  │ COMPLETED   (terminal; never re-sent)
     │     release (cancel/Ctrl+C) or             │
     │     requeue_processing (next run start)    │ fail (CHUNK_FATAL or retries exhausted)
     │                                            ▼
     └────────────── reset_failed (--retry-failed) ── FAILED
```

Rules:

- `PROCESSING → PENDING` re-queue at every run start is unconditional: the single-process lease guarantees no other live worker exists, so any PROCESSING row is an orphan (AC US-8/2).
- `next_attempt_at` semantics: a PENDING chunk is eligible for claim only if `next_attempt_at IS NULL OR next_attempt_at <= now` (UTC). It is set on `reschedule` and cleared on `claim`. It survives restarts, so a run that resumes seconds after a 429 storm still waits. `status` uses `next_attempt_at` only for display ("N pending, M of them waiting for backoff").
- `retry_count` counts provider attempts that ended in a retryable error; `release` (cancellation) does not increment it.
- `complete` is one `UPDATE ... SET translated_text, status='COMPLETED', ... WHERE id=? AND status='PROCESSING'`; the orchestrator treats a `False` return as an integrity warning (chunk was re-queued by a cancellation race) and does not overwrite.
- FAILED is terminal for the job run; `--retry-failed` resets FAILED → PENDING with `retry_count = 0`.
- Job status: `CREATED → EXTRACTED → CHUNKED → TRANSLATING → (PAUSED | TRANSLATED) → EXPORTED`; `PAUSED` is set on JOB_FATAL provider errors (quota/auth) and cleared on the next successful translate run start.

---

## 4. Chunking Algorithm (`pipeline/chunker.py`)

### 4.1 Input contract: BT-Markdown

Extractors (and `normalizer.py` for marker output) emit a constrained Markdown subset so the block parser is deterministic:

- Newline `\n` only; file ends with exactly one `\n`; no tabs at line start (converted to spaces).
- ATX headings only: `#{1..6} title` on one line.
- Paragraphs are a single line (no hard wraps); blocks separated by exactly one blank line.
- Fenced code: ```` ``` ```` … ```` ``` ```` (tilde fences normalized to backticks); content untouched.
- Lists: `- `, `* `, `1. ` markers; nesting by 2-space indent; continuation lines indented; list ends at a blank line.
- Tables: consecutive lines starting with `|`; second line is the delimiter row.
- Blockquote: lines starting with `> `.
- Footnote definition: `[^id]: text` (one line); references `[^id]` inline.
- Image placeholder: a single line `<!-- image:N -->` (non-translatable token; exporters render a localized placeholder).
- Horizontal rule: `---` alone on a line (only when not a setext underline; normalizer resolves this).
- No raw HTML other than the image token; the normalizer strips other tags to text.

### 4.2 Block parser

Line-oriented single pass, producing `Block(start, end)` spans that **tile** the document (`blocks[0].start == 0`, `blocks[i].end == blocks[i+1].start`, `blocks[-1].end == len(md)`). Trailing blank lines are attached to the preceding block, so no characters are orphaned. Grammar precedence per line: fence-state > heading > hr > image token > table > footnote def > list > blockquote > paragraph. Inside a fence, no other rule applies until the closing fence (unterminated fence → runs to EOF and is logged as a warning).

Block translatability: `CODE`, `IMAGE`, `HR`, `BLANK` → `translatable=False`; everything else `True`.

### 4.3 Packing

Parameters: `min_chars=1000`, `max_chars=1500`, `provider_limit = translator.capabilities.max_chars_per_request` (effective max = `min(max_chars, provider_limit)`).

```
for block in blocks:
    if block is non-translatable (CODE/IMAGE/HR):
        flush current (respecting heading rule); emit block as its own chunk (kind = CODE/IMAGE/HR, translatable=False)
        continue
    if block.size > effective_max:
        flush current; emit sub-chunks via oversize split (4.4)
        continue
    if current.size + block.size <= effective_max:
        current.append(block)
        if current.size >= min_chars and block is not HEADING: flush current
    else:
        # neither fits: choose the smaller deviation
        if (min_chars - current.size) <= (current.size + block.size - effective_max): flush current; start new with block
        else: current.append(block); flush current
flush current at EOF
```

Heading-stays-with-next rule: `flush` never closes a chunk whose trailing blocks are headings (one or more consecutive `HEADING` blocks). Those trailing headings are moved to the front of the next chunk, even if the closed chunk then falls below `min_chars`. Exception: end of document (chunk kind becomes `HEADING` if it is heading-only). This satisfies AC US-6/3.

Chunk kind: single-block chunks carry the block's kind (`LIST`, `TABLE`, `FOOTNOTE`, `CODE`, `IMAGE`, `HR`); multi-block prose chunks are `TEXT`. `TABLE` and `LIST` blocks are packed like prose (they are translatable), but a `TABLE` never shares a chunk with prose so its cell-segmentation is isolated; a `LIST` may share.

Size accounting uses `len(source_text)` (characters, incl. whitespace); the payload actually sent is smaller (markers stripped).

### 4.4 Oversize block split (parent linkage)

When a translatable block exceeds `effective_max`:

| Kind | Split points | Notes |
|---|---|---|
| PARAGRAPH / BLOCKQUOTE / FOOTNOTE | sentence boundaries: regex on `[.!?…]["'”’)]*\s+` guarded by an abbreviation list (`e.g.`, `Mr.`, `Dr.`, `vs.`, `No.`, initials `A.`), never inside inline code or link syntax | Pieces packed greedily up to `effective_max`. A single sentence above the limit is split at the last whitespace before the limit with a warning. |
| LIST | list-item boundaries (top-level items) | Nested items stay with their parent item. |
| TABLE | row boundaries | Header row belongs to sub-chunk 0 only; cell-by-cell segmentation does not need the header. |
| CODE | not split (non-translatable; no request is made) | Stored as one chunk regardless of size. |

Every piece becomes a `Chunk` with `parent_block_id = block.block_id`, `sub_index = i`, `sub_count = n`. The concatenation of a block's sub-chunks' `source_text` in `sub_index` order equals the block's span exactly (whitespace between sentences stays at the end of the preceding piece). Reassembly therefore needs no special handling — ordered concatenation restores the block — but `parent_block_id` lets the assembler and post-check treat the sub-chunks as one unit (heading parity, glossary check).

### 4.5 Lossless guarantee and verification

Invariant: `"".join(c.source_text for c in sorted(chunks, key=order)) == markdown`.

Verified in three places:

1. `chunker.chunk()` re-joins its own output and compares byte-for-byte before returning; mismatch → `Err(CHUNK_INTEGRITY, JOB_FATAL)` (never persisted).
2. `translate` stage start: `sha256(source_book.md)` must equal `job.source_md_sha256`; if chunks exist but hash differs → `Err(STATE_HASH_MISMATCH)` unless `--fresh` (the user edited `source_book.md` after chunking).
3. Tests: hypothesis property test over generated BT-Markdown documents (random mix of all block kinds, sizes 0–5×max, Turkish/Unicode text, edge whitespace) asserting the invariant, the tiling invariant for blocks, the heading rule, and `min ≤ size ≤ max` for all chunks except: last chunk, chunks immediately preceding an oversize block or a heading move, and sub-chunks (documented deviations flagged with `chunk.warnings`).

### 4.6 Segmenter (`pipeline/segmenter.py`) — chunk → provider texts

To keep Markdown syntax out of the provider's hands (E-17), the chunk text is re-parsed with the same block parser at translate time and turned into **segments**:

| Block | Segment(s) | Kept locally (prefix/suffix) |
|---|---|---|
| HEADING | title text | `## ` prefix, trailing newlines |
| PARAGRAPH | the line | trailing newlines |
| LIST | one segment per item line (text after the marker) | indent + marker (`- `, `3. `), newlines |
| TABLE | one segment per cell (delimiter row skipped, empty cells skipped) | pipes, alignment row, spacing |
| BLOCKQUOTE | one per line | `> ` |
| FOOTNOTE | text after `[^id]: ` | the definition marker |
| CODE / IMAGE / HR | none (chunk is non-translatable anyway) | whole text |

Then `translators/protect.py` masks inline code spans, URLs, and link targets inside each segment (`text` → `text + placeholder map`). Strategy is chosen by `capabilities.supports_tag_protection`: DeepL → `<x id="n"/>` tags with `tag_handling="xml"`, `ignore_tags=["x"]`; others → sentinel tokens `⟦n⟧`. After the response, placeholders are restored; a missing/duplicated placeholder counts as `PROVIDER_EMPTY_RESPONSE`-class failure once (retryable), then `review_flag=True` with the placeholder appended at segment end.

Reassembly of a chunk: `prefix + translated_segment + suffix` in order → `translated_text`. Line and blank-line structure is therefore reproduced by construction, not by the provider's goodwill. Segments of one chunk are sent as one batch request when `supports_batch`, otherwise sequentially; any failure fails the whole chunk (idempotent retry).

---

## 5. Orchestrator Design (`pipeline/orchestrator.py`)

### 5.1 Stage coordinator

`run` executes `extract → glossary → chunk → translate → export`; per-stage commands run one stage against the same output directory. Each stage validates preconditions via `Result` before doing work (e.g. `translate` requires `source_book.md` and a job with matching hashes; `export` requires all chunks COMPLETED or `--allow-partial`).

Pre-flight for `translate` (in order, all fail-fast, none touches the provider network except `prepare`):

1. Settings validation: provider key present (`PROVIDER_CONFIG`, exit 1) — before extraction in `run`.
2. Open DB, check schema version (`STATE_SCHEMA_INCOMPATIBLE`).
3. Acquire lease (`STATE_LOCKED`, exit 4) — see 5.6.
4. Input binding: `sha256(input.pdf)` vs `job.input_sha256`; mismatch → `STATE_HASH_MISMATCH` (exit 5) unless `--fresh` → archive DB to `state-archive/<ts>/`, create new job.
5. Provider binding: `job.provider` vs selected; mismatch → `STATE_PROVIDER_MISMATCH` unless `--allow-provider-switch` (recorded in run).
6. Glossary: load effective glossary; `translator.prepare()` binds (native id reuse/creation); glossary hash change on resume → warning "consistency risk: N chunks translated with previous glossary".
7. `requeue_processing()`; log count.
8. Job status → `TRANSLATING`; start run record.

### 5.2 Async job runner

```
AdaptiveLimiter(max=concurrency)            # permits 1..max, AIMD
DB lock = asyncio.Lock()                    # serializes repository calls (to_thread)
cancel = asyncio.Event()

loop:
    if cancel.is_set(): break
    free = limiter.available()
    if free > 0:
        claimed = repo.claim_pending(limit=free, now=utcnow(), run_id)
        for chunk in claimed: spawn worker(chunk)
    if no in-flight tasks and claimed == []:
        t = repo.earliest_next_attempt()
        if t is None: break                                   # nothing left
        await wait_until(t) or cancel                          # backoff wait
    else:
        await first_completed(in-flight) or cancel
```

`worker(chunk)`:

1. Non-translatable chunk → `complete()` with `translated_text = source_text`, `chars_sent = 0`, no provider call (AC US-7/5).
2. Segment + protect → `translator.translate(texts, request)` under a limiter permit; log `provider_call` with chunk id, attempt, latency, outcome, chars.
3. `Ok` → restore placeholders, reassemble, run `glossary_check` (warnings, `review_flag`), then `complete()` (atomic write of text + COMPLETED + metrics). Progress callback fires.
4. `Err` → classify by `error.scope` (5.3): `CHUNK_RETRYABLE` and `retry_count + 1 < max_retries` → `reschedule(next_attempt_at = now + backoff(attempt, retry_after))`; retries exhausted → `fail()`; `CHUNK_FATAL` → `fail()`; `JOB_FATAL` → `fail()` is NOT called: chunk is `release()`d back to PENDING, `cancel` set with reason, job status → `PAUSED`, run outcome = paused (exit 3).
5. Any exception → `Err(INTERNAL, CHUNK_FATAL)`.

Empty/whitespace-only or truncated response (`len(out) < 0.2 * len(in)` for texts > 40 chars, or wrong list length) → `PROVIDER_EMPTY_RESPONSE` (retryable) (E-16).

### 5.3 Provider error classification (DeepL mapping; other providers map analogously)

| Signal | ErrorCode | Scope | Behavior |
|---|---|---|---|
| HTTP 429 / `TooManyRequestsException` | PROVIDER_RATE_LIMITED | CHUNK_RETRYABLE | backoff (Retry-After honored), limiter halves permits |
| HTTP 5xx, timeouts, connection errors (`ConnectionException.should_retry`) | PROVIDER_TRANSIENT | CHUNK_RETRYABLE | backoff |
| Empty/short/mismatched response, missing placeholders | PROVIDER_EMPTY_RESPONSE | CHUNK_RETRYABLE | backoff; second occurrence for the same chunk flags review, third fails |
| HTTP 456 / `QuotaExceededException` | PROVIDER_QUOTA | JOB_FATAL | pause job, exit 3, message with quota guidance |
| HTTP 401/403 / `AuthorizationException` | PROVIDER_AUTH | JOB_FATAL | pause job, exit 3 |
| HTTP 400 / 413 (request too large, bad language, glossary id invalid) | PROVIDER_BAD_REQUEST | CHUNK_FATAL | FAILED immediately with message |
| Glossary creation failure at `prepare` | GLOSSARY_BIND_FAILED | JOB_FATAL | stop before sending any chunk |
| Local NMT: model load failure | PROVIDER_CONFIG | USER | fail-fast at `create` |
| Local NMT: OOM / runtime error | PROVIDER_TRANSIENT | CHUNK_RETRYABLE | backoff; limiter halves |

### 5.4 Backoff (`pipeline/backoff.py`)

`delay = min(cap, base * factor**attempt)` with full jitter `uniform(0, delay)`, then `max(jittered, retry_after_s)` when Retry-After is present; defaults `base=2s`, `factor=2`, `cap=60s`, `max_retries=5`. Delay is persisted as `next_attempt_at`, so it is honored across process restarts. Custom, not tenacity: tenacity retries in-process around a call; our retry state lives in the DB and the wait must be interruptible by cancellation and shared with the scheduler.

`AdaptiveLimiter` (E-07 "concurrency reduced temporarily"): permits start at `concurrency`; on any `PROVIDER_RATE_LIMITED` → `permits = max(1, permits // 2)` and a 30 s cooldown before growth; after 20 consecutive successes → `permits = min(max, permits + 1)`. Logged on every change.

### 5.5 Resume logic

- Same command re-run → pre-flight (5.1) → `requeue_processing` → scheduler claims only PENDING; COMPLETED rows are never selected by `claim_pending` (NFR-03 test: counting fake provider).
- `--fresh` archives the DB and any stage artifacts to `state-archive/`, never deletes silently.
- `--retry-failed` resets FAILED → PENDING before scheduling.
- `status` command opens the DB read-only, never instantiates a translator.

### 5.6 Single-process lock (E-20)

DB-level lease: `LeaseRepository.acquire` runs a `BEGIN IMMEDIATE` transaction that inserts/updates the single lease row only if absent or `heartbeat_at < now - stale_after (60 s)`. The runner heartbeats every 10 s from a background task. On clean exit the lease is released. A second process gets `STATE_LOCKED` (exit 4) with holder pid/host/time; `--force-unlock` breaks a lease the user knows is dead. The lease row also carries `run_id` so log correlation shows who held it.

### 5.7 Graceful Ctrl+C (and SIGTERM on POSIX)

- Signal wiring in `cli.py` via `signal.signal` (works on Windows; `loop.add_signal_handler` does not) calling `loop.call_soon_threadsafe(cancel.set)`.
- First signal: stop claiming; in-flight requests are allowed to finish within `--grace-seconds` (default 30); completions during grace are written; the console shows "finishing N in-flight chunks, press Ctrl+C again to abort".
- Second signal or grace expiry: in-flight tasks are cancelled; their chunks are `release()`d to PENDING (best effort; any that slip through are re-queued on the next start anyway).
- Lease released, run finished with outcome `interrupted`, `summary.json` written, exit 130.
- `asyncio.CancelledError` is never converted into `Err`; it propagates to the runner which handles the release.

### 5.8 Progress and summary

Progress events (`ProgressEvent(completed, failed, total, elapsed_s, chars_sent, permits)`) go to a callback owned by the CLI (rich bar in TTY; one log line per completed chunk otherwise). At the end the orchestrator builds `JobSummary`, writes `summary.json`, and returns `Result[JobSummary]`.

---

## 6. Extraction and Cleanup Design

### 6.1 PyMuPDF extractor (`extractors/pymupdf_extractor.py`)

Per page, `page.get_text("dict")` yields text blocks → lines → spans (text, font size, flags, bbox). Steps:

1. **Input validation** (before opening anything heavy): path exists, suffix `.pdf`, magic bytes `%PDF`, document opens, page count > 0.
2. **Raw block extraction**: for each page collect `RawBlock(page, bbox, lines[(text, size, bold, superscript, bbox)])`; image blocks (`type == 1`) become `RawImage(page, bbox)`.
3. **Missing text layer** (E-01, FR-06): median `char_count` per page < `min_chars_per_page` (50) → `Err(NO_TEXT_LAYER, USER)` with the message "no extractable text; run OCR first or install the [marker] extra (`--extractor marker`)". Image-only pages within an otherwise normal book are just skipped (E-04) and counted in `PageInfo.skipped`.
4. **Running header/footer detection** (E-03, FR-03): candidate lines are those whose bbox lies in the top or bottom `band_ratio` (12 %) of the page. Normalize: lowercase, collapse whitespace, replace digit runs with `#`, strip punctuation. Count each normalized string across pages; a string occurring on `>= max(3, page_ratio * pages)` pages is a running header/footer and all its instances are removed. Because digits are masked, "Chapter 3 — The Dragon | 47" and "Chapter 3 — The Dragon | 48" collapse to one pattern; chapter-title headers that change per chapter still repeat within their chapter and are caught when the chapter spans ≥ 3 pages (shorter chapters: caught by the page-number rule and by the "band + short line + no terminal punctuation" heuristic). A body paragraph is protected because it is outside the band; a genuine first line that repeats verbatim across ≥ 30 % of pages is practically impossible in prose.
5. **Page-number stripping**: a band line matching `^\s*(?:page\s+)?(\d{1,4}|[ivxlcdm]{1,7})(?:\s*(?:/|of)\s*\d{1,4})?\s*$` (case-insensitive) is removed; also a bare number left over after removing a header pattern.
6. **Body font size**: mode of span font sizes weighted by character count → `body_size`.
7. **Heading detection**: a block of ≤ 2 lines, ≤ 120 chars, no terminal period, `size >= body_size * 1.15` (or bold with `size >= body_size` and followed by a paragraph) → heading candidate. Distinct candidate sizes are sorted descending and bucketed (rounded to 0.5 pt) into at most three tiers → `#`, `##`, `###`. Numbered patterns (`Chapter 3`, `3.2 Title`) reinforce the tier. Title-case blocks at the top of a page with a large blank gap below are chapter titles (`#`).
8. **Line → paragraph merge within page**: consecutive lines of one block are joined with a space when the vertical gap < 1.5 × line height and the next line does not start with a list marker or indentation change; a larger gap or a first-line indent starts a new paragraph.
9. **Hyphen rejoin** (E-02, FR-04): first pass collects the set of hyphenated words that appear *mid-line* (`well-known`, `re-attach`) → "true compounds". At a line end (or page end) where a line ends with `-` and the next non-empty line starts with a lowercase letter: if `left + "-" + right` is in the compound set, keep the hyphen; else join as `left + right`. Ambiguous cases (right starts uppercase, or left is a single letter) are kept hyphenated and logged.
10. **Cross-page paragraph merge**: if the last paragraph of page N does not end with terminal punctuation (`. ! ? : ” " ’ )`) or ends with a hyphen, and the first paragraph of page N+1 starts with a lowercase letter or continues a hyphenation, they are merged (no blank line). Headings, list items, tables, and image placeholders never merge.
11. **Lists**: lines starting with `•`, `–`, `-`, `*`, `\d+[.)]`, `[a-z][.)]` at a consistent x-offset become list items (`- ` / `1. `), nesting by x-offset steps.
12. **Code blocks**: blocks whose spans are entirely monospace fonts (font name contains `Mono`, `Courier`, `Consolas`, `Code`) with ≥ 2 lines → fenced code, content verbatim (line breaks preserved).
13. **Tables**: PyMuPDF `page.find_tables()` (available in ≥ 1.23) → Markdown pipe table; text inside table bboxes is excluded from the paragraph flow. Failures fall back to plain paragraphs with a warning.
14. **Footnotes** (Q5): spans with `size <= body_size * 0.85` located in the bottom third of the page and starting with a number (or a superscript-flagged marker) are footnote definitions; superscript spans in body text become `[^k]` references with a globally sequential id `k`. Definitions are emitted as `[^k]: text` right after the paragraph that references them. Definitions with no matched reference are collected and emitted as a `## Notes` section at the end of the chapter (before the next `#`) or of the document. `footnote_mode` is reported.
15. **Images** (Q6): each `RawImage` (area ≥ 1 % of page) becomes `<!-- image:N -->` at its reading position; the text block directly below within 1.5 line heights that starts with `Figure|Fig.|Table|Illustration` is kept as a normal paragraph (translated caption).
16. **Serialization** to BT-Markdown (4.1), `\n` newlines, UTF-8.
17. **Language detection** (6.3).

### 6.2 Marker extractor (`extractors/marker_extractor.py`)

Wraps `marker-pdf` (`PdfConverter`) to obtain Markdown, then `normalizer.py`: unwrap hard-wrapped paragraphs, setext → ATX headings, `![...](...)` → `<!-- image:N -->` (alt text kept as a caption paragraph if non-empty), HTML stripped, footnotes normalized, page-number-only lines removed. Steps 4 and 10 of 6.1 are not applied (no page geometry); marker's own header/footer removal is trusted. If marker is not importable or raises, the orchestrator logs `extractor_fallback` and reruns with PyMuPDF; `ExtractedDocument.fallback_used = True` and the CLI prints it (AC US-1/5). Marker has built-in OCR, so `NO_TEXT_LAYER` from PyMuPDF suggests `--extractor marker` in its message.

### 6.3 Language detection (`extractors/langdetect.py`)

No external dependency. Package data holds ~100-word stopword lists for `en, tr, de, fr, es, it, nl, pt`. Tokenize the first 20 000 words of the extracted text (lowercased, Unicode-aware `\w+`), compute `ratio[lang] = stopword_hits / tokens` per list; winner = argmax. Decision: if winner ≠ `en` or `ratio[en] < 0.15` → `Err(NON_ENGLISH_SOURCE, USER)` with the detected language and confidence, unless `--allow-non-english` (then a warning is stored on the job and DeepL `source_lang` is left to auto-detect; glossary still EN→TR). Good enough for "is this book English?"; a `langdetect` extra is deliberately not added.

---

## 7. Glossary Design

### 7.1 Discovery (`glossary/discovery.py`)

Input: translatable text of `source_book.md` (code fences, image tokens, URLs, and inline code removed; heading text included). Steps:

1. Sentence-split (same splitter as the chunker) and tokenize with positions; mark whether a token is sentence-initial.
2. Candidate n-grams (n = 1..3) where every token is Capitalized (`[A-Z][a-z’'\-]+`) or an acronym (`[A-Z]{2,6}`), optionally joined by `of|the|de|von|van` in the middle (`Duke of York`). Sentence-initial unigrams are counted separately.
3. Stop-list (package data, ~400 entries): function words, `Chapter`, `Part`, `Section`, `Figure`, `Table`, `Note`, months, weekdays, honorifics standing alone, common sentence starters (`However`, `Then`, ...). Any n-gram consisting only of stop-list tokens is discarded.
4. Counting: `count = mid_sentence_count + sentence_initial_count`, but a candidate qualifies only if `mid_sentence_count >= 1` (protects against ordinary words that are capitalized only at sentence start).
5. Ambiguity (E-11): if the lowercase form of a unigram appears in the text ≥ `count` times as a normal word (`will`/`Will`, `mark`/`Mark`), status = `AMBIGUOUS`; else `PROPOSED`.
6. Threshold `--min-count` default **3**. Longer n-grams absorb their sub-grams when the sub-gram occurs ≥ 80 % of the time inside the longer one (`New York` absorbs `York`).
7. Output entries are sorted by count desc; `target` is empty (no machine suggestion in v1; DeepL translating names is not reliable enough to pre-fill).

### 7.2 `glossary.json` schema (`glossary/schema.py`, pydantic)

```json
{
  "version": 1,
  "source_lang": "en",
  "target_lang": "tr",
  "generated_at": "2026-09-17T10:00:00Z",
  "entries": [
    { "source": "Dragon", "target": "Ejderha", "count": 42, "status": "approved",  "origin": "discovered", "note": "" },
    { "source": "Will",   "target": "",        "count": 9,  "status": "ambiguous", "origin": "discovered", "note": "also a common word" }
  ]
}
```

Validation (AC US-4/3): unknown `version` → error; each entry needs `source` (non-empty, ≤ 200 chars) and `status` in the enum; `approved` requires non-empty `target`; duplicate `source` (case-sensitive) within one file → error naming the entry index and value; `target` must not contain newlines. Errors are `Err(GLOSSARY_INVALID, USER)` with `context={"file", "index", "source"}`.

### 7.3 Precedence and effective glossary (`glossary/manager.py`)

- `glossary` stage writes `<output>/glossary.json`. Re-running it does not overwrite an existing file unless `--force` (AC US-13/2); with `--force` it re-discovers but preserves `target`/`status` for entries whose `source` already exists (merge, then rewrite).
- `--glossary my_terms.json` (user file) is loaded in addition; on identical `source`, the user entry wins entirely (E-10). A user entry without `status` defaults to `approved`.
- Effective glossary = union, filtered to `status == approved && target != ""`, sorted by `len(source)` desc (longest match first), `glossary_hash = sha256("\n".join(f"{source}\t{target}"))` after NFC normalization.
- `translate` refuses to start if the effective glossary is empty **only** when `--require-glossary` is set; by default it warns "0 approved terms".

### 7.4 DeepL binding (`deepl_translator.prepare`)

- Glossary name: `book-translator:{glossary_hash[:16]}`; `list_glossaries()` → reuse the first with that name and `ready == True`; else `create_glossary(name, "EN", "TR", entries)`. Result stored on the job (`provider_glossary_id`, `glossary_hash`, strategy `NATIVE`) and attached to every `translate_text(..., glossary=id)` call. DeepL requires `source_lang="EN"` explicitly when a glossary is used — set it.
- On resume with a different hash: bind the new one, warn with the count of chunks already completed under the old one; old provider glossaries are left in place (`glossary --cleanup-remote` can delete `book-translator:*` glossaries not referenced by any job in the DB — nice-to-have).
- Entry limits: DeepL allows large glossaries; entries are de-duplicated and trimmed; entries whose target equals source are dropped.

### 7.5 Non-glossary providers

- `BaseLLMTranslator` builds a system prompt: fixed instructions (translate EN→TR, keep placeholders `⟦n⟧` verbatim, return only the translation) plus a terminology block `source ⇒ target` per entry, ≤ 200 entries per prompt (subset limited to entries whose `source` occurs in the chunk being translated — keeps the prompt small and deterministic). Strategy `PROMPT`. A unit test asserts every approved term appears in the built prompt (AC US-5/2).
- `local_nmt.py` (CTranslate2) cannot take instructions → strategy `POST_REPLACE`: after translation, for each effective entry whose `source` occurs in the segment (word-boundary, case-sensitive), if the untranslated `source` still appears in the output, replace it with `target` (longest match first); if neither `source` nor `target` appears, log `glossary_miss`. The strategy actually used is recorded in the job and printed at start (AC US-5/3).

### 7.6 Post-check (`pipeline/glossary_check.py`) — all providers

For each completed chunk: for every effective entry with `source` present in the chunk source, check the Turkish output contains the `target` stem (first `max(4, len-2)` characters, compared with Turkish-aware case folding: `I→ı`, `İ→i`) — this tolerates suffixes (`Ejderha` → `Ejderhanın`). Misses → `warnings += ["glossary_miss:Dragon"]`, `review_flag=True` when `--strict-glossary`. Structure check (E-17) is done by construction in the segmenter, but the check additionally verifies the count of `**`, `*`, `` ` ``, `[^` markers per segment and flags mismatches for review. Counts are surfaced in the summary and in `status`.

---

## 8. Export Design

### 8.1 Assembly (`pipeline/assembler.py`)

Streams chunks ordered by `order` from the repository (batches of 200; never loads all chunks — E-19). For each chunk: COMPLETED → `translated_text`; FAILED (only reachable with `--allow-partial`) → marker block:

```
> **[UNTRANSLATED — chunk 123 — provider_transient: connection reset after 5 attempts]**

<original source_text verbatim>

> **[END UNTRANSLATED — chunk 123]**
```

Sub-chunks of one parent are concatenated before any parity check. Output: `AssembledDocument(markdown, metadata, failed_chunk_ids, review_chunk_ids, heading_outline)`. Export without `--allow-partial` while FAILED > 0 → `Err(EXPORT_REFUSED_PARTIAL, USER)` (exit 2 semantic: "partial", not failure). PENDING/PROCESSING chunks at export time → same refusal with a message to run `translate` first.

Structural parity (NFR-06, AC US-10/1): heading sequence `[(level, order)]` of the source and translated documents must be identical; list-item counts and fence counts per chunk too. Mismatches are warnings listed in the summary (they cannot happen unless a chunk was hand-edited in the DB, because markers are re-applied locally).

### 8.2 Markdown exporter

Writes `translated_book.md` (UTF-8, `\n`, no BOM) via a temp file + atomic rename (E-18: disk full leaves no half file). Localizes `<!-- image:N -->` → `*[Şekil N — görsel dahil edilmedi]*` for the Markdown output only when `--render-placeholders` (default on for export, off for the identity round-trip test).

### 8.3 EPUB exporter (ebooklib)

1. `markdown` library → HTML (`extensions: fenced_code, tables, footnotes, sane_lists`), image tokens → `<p class="figure-placeholder">Şekil N — görsel dahil edilmedi</p>`.
2. Chapter split at `<h1>`; if the document has < 2 `h1`, split at `<h2>`; front matter before the first split heading becomes chapter 0 ("Başlangıç"). Each chapter → `EpubHtml(file_name=f"ch{idx:03d}.xhtml", lang="tr")`.
3. Metadata: `set_identifier(uuid5(job_id))`, `set_title(options.title or first H1 or input stem)`, `set_language("tr")`, author if given, `dc:source` = input file name.
4. TOC: nested `epub.Link`/`epub.Section` from H1/H2 (H3 optional flag); `EpubNcx` + `EpubNav` added; spine = `["nav", *chapters]`.
5. CSS (`exporters/assets/book.css`, embedded): serif body, code `pre` with `white-space: pre-wrap`, table borders, `.untranslated` marker style, `.figure-placeholder` italic centered.
6. Written via temp file + rename; validated by re-opening with `ebooklib.epub.read_epub` (smoke check).

### 8.4 PDF exporter (weasyprint, `[pdf]` extra)

Same HTML as EPUB with `@page` CSS (A5, margins, page numbers in the footer) and `@font-face` declarations pointing at the bundled `DejaVuSans.ttf` / `DejaVuSerif.ttf` (full Turkish glyph coverage; DejaVu license permits redistribution) unless `--pdf-font` overrides. weasyprint subsets and embeds the fonts automatically. `is_available()` probes the import and the native libraries (Pango/Cairo); failure → `Err(EXPORTER_UNAVAILABLE)` which the orchestrator reports as partial (exit 2) while `.md` and `.epub` are still produced (AC US-10/4).

### 8.5 Encoding (NFR-13, E-15)

All file I/O `encoding="utf-8"`; `cli.py` calls `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` on Windows; rich console is created with `force_terminal=None`, `legacy_windows=False`. Round-trip test writes `çğıİöşü` through all three exporters and verifies the bytes.

---

## 9. CLI Contract (`cli.py`, typer)

### 9.1 Commands

| Command | Stages | Notes |
|---|---|---|
| `book-translator run` | extract → glossary → chunk → translate → export | Full pipeline; the common case |
| `book-translator extract` | extract | Produces `source_book.md`; no provider contact; creates/updates the job record with the input hash |
| `book-translator glossary` | discovery | Writes `glossary.json`; `--force` re-discovers and merges |
| `book-translator translate` | chunk (if needed) + translate | Resumable; uses the edited glossary |
| `book-translator export` | assemble + export | Refuses on FAILED unless `--allow-partial` |
| `book-translator status` | read-only | Counts per status, backoff-waiting count, provider, input hash, glossary strategy, lease holder, last run outcome; `--json` |
| `book-translator providers` / `extractors` / `exporters` | read-only | Lists registered implementations and availability (helps first-run diagnosis) |

### 9.2 Options (defaults in parentheses)

Global (all commands): `--output/-o DIR` (required except `status` where it defaults to `.`), `--log-level` (`INFO`), `--log-file PATH` (`<output>/logs/run-<run_id>.jsonl`), `--no-progress` (auto: off when not a TTY), `--json` (machine-readable summary on stdout), `--quiet`.

`run` / `extract`: `--input/-i FILE` (required), `--extractor {pymupdf,marker}` (`pymupdf`), `--allow-non-english` (off), `--min-chars-per-page` (50), `--fresh` (off).

`glossary`: `--min-count` (3), `--force` (off), `--glossary FILE` (user file, merged for display), `--cleanup-remote` (off).

`translate` / `run`: `--provider {deepl,local}` (`deepl`), `--glossary FILE` (none), `--concurrency` (4), `--max-retries` (5), `--chunk-min` (1000), `--chunk-max` (1500), `--retry-failed` (off), `--allow-provider-switch` (off), `--require-glossary` (off), `--strict-glossary` (off), `--grace-seconds` (30), `--force-unlock` (off), `--dry-run` (chunk and report counts/characters without sending), `--limit N` (translate at most N chunks this run — useful for a quality preview).

`export` / `run`: `--format {md,epub,pdf}` (repeatable; default `md,epub`), `--allow-partial` (off), `--title`, `--author`, `--css FILE`, `--pdf-font FILE`, `--chapter-level {1,2}` (1).

`--help` lists every option with its default and the env variable that overrides it (typer `envvar=` + `show_default=True`).

### 9.3 Exit codes

| Code | Meaning | Resumable |
|---|---|---|
| 0 | Full success | – |
| 1 | Failure: invalid input/config, extraction failed, export failed, internal error | depends |
| 2 | Partial: FAILED chunks exist (translate), or export refused/partial (`--allow-partial` output, optional exporter unavailable) | yes |
| 3 | Paused: JOB_FATAL provider error (quota 456, auth 403), state intact | yes |
| 4 | Locked: another process holds the lease | yes |
| 5 | State mismatch: input hash / provider / schema version requires an explicit flag | yes (with flag) |
| 130 | Interrupted by the user (Ctrl+C / SIGTERM), state intact | yes |

`run` returns the highest-severity code reached, where severity order is `1 > 3 > 4 > 5 > 130 > 2 > 0`.

### 9.4 Configuration, env vars, `.env`

Precedence: CLI flag > process env > `.env` in the current directory (then `<output>/.env`) > default. Managed by `pydantic-settings` (`env_prefix="BOOK_TRANSLATOR_"`, `env_file=".env"`).

| Variable | Purpose | Default |
|---|---|---|
| `DEEPL_API_KEY` | DeepL auth (secret; never logged/persisted) | – |
| `DEEPL_SERVER_URL` | Override API host (Free vs Pro is auto-detected from the `:fx` key suffix) | – |
| `BOOK_TRANSLATOR_PROVIDER` | `deepl` / `local` | `deepl` |
| `BOOK_TRANSLATOR_EXTRACTOR` | `pymupdf` / `marker` | `pymupdf` |
| `BOOK_TRANSLATOR_CONCURRENCY` | worker permits | `4` |
| `BOOK_TRANSLATOR_MAX_RETRIES` | per chunk | `5` |
| `BOOK_TRANSLATOR_CHUNK_MIN` / `_CHUNK_MAX` | chars | `1000` / `1500` |
| `BOOK_TRANSLATOR_BACKOFF_BASE_S` / `_BACKOFF_CAP_S` | seconds | `2` / `60` |
| `BOOK_TRANSLATOR_LOCAL_MODEL_DIR` | CTranslate2 model directory | `~/.cache/book-translator/models` |
| `BOOK_TRANSLATOR_LOCAL_MODEL` | model id (`opus-mt-tc-big-en-tr` / `nllb-200-distilled-600M`) | `opus-mt-tc-big-en-tr` |
| `BOOK_TRANSLATOR_LOCAL_DEVICE` | `cpu` / `cuda` | `cpu` |
| `BOOK_TRANSLATOR_LOG_LEVEL` | `DEBUG..ERROR` | `INFO` |
| `BOOK_TRANSLATOR_LEASE_STALE_S` | lease staleness | `60` |
| `BOOK_TRANSLATOR_GRACE_S` | Ctrl+C grace | `30` |

Settings validation happens before any stage runs; `--provider deepl` without `DEEPL_API_KEY` → exit 1 naming the variable (AC US-9/1); `--provider local` with missing model files → exit 1 with download instructions (AC US-9/2). Key format is validated loosely (non-empty, no whitespace) — never echoed.

### 9.5 Logging (`logging_setup.py`)

- `run_id` = 12 hex chars per invocation; `job_id` = uuid stored in the DB. Both are injected into every record via a `logging.Filter` (contextvars), together with `chunk_id`/`attempt` when set by the worker.
- Console: rich handler, level from `--log-level`, warnings and above always shown even with the progress bar (rich `Progress` + `Console` share output).
- File: JSON lines at `<output>/logs/run-<run_id>.jsonl` with `ts, level, run_id, job_id, event, chunk_id, attempt, latency_ms, chars, http_status, message`. Event names are stable strings (`provider_call`, `chunk_completed`, `chunk_rescheduled`, `chunk_failed`, `job_paused`, `lease_acquired`, `extractor_fallback`, `limiter_changed`, `glossary_bound`, `glossary_miss`...).
- Redaction filter: any string equal to a configured secret, or matching the DeepL key pattern `[0-9a-f-]{36}(:fx)?`, is replaced by `***` in message and args. Logger names: `book_translator.<package>`.
- Provider request/response bodies are logged only at `DEBUG` and truncated to 200 chars.

### 9.6 Progress

TTY: rich `Progress` with columns `completed/total`, failed count (red), percentage, elapsed, ETA, `chars sent`, current permits; refreshed on each completion. Non-TTY (`not sys.stdout.isatty()` or `--no-progress`): one plain line per completed chunk `progress completed=41/100 failed=1 elapsed=00:12:03` throttled to at most one line per second (plus every 10th chunk unconditionally); no ANSI codes (AC US-12/3).

---

## 10. Dependency Plan (`pyproject.toml`)

Build backend: hatchling (`src` → `book_translator` mapping). Entry point: `book-translator = "book_translator.cli:app"`. `requires-python = ">=3.10"`.

| Group | Package | Min version | Why |
|---|---|---|---|
| core | `typer` | 0.12 | Typed CLI, envvar support, auto `--help` with defaults |
| core | `rich` | 13.7 | Progress bar, console logging (typer already depends on it) |
| core | `pydantic` | 2.6 | File schemas (glossary, summary), validation errors with paths |
| core | `pydantic-settings` | 2.2 | Env + `.env` precedence without hand-written parsing (pulls `python-dotenv`) |
| core | `pymupdf` | 1.24 | Default extractor; `find_tables`, font metadata; wheels for all OSes |
| core | `deepl` | 1.18 | Official client: glossary API, typed exceptions, `:fx` host detection |
| core | `SQLAlchemy` | 2.0 | ORM + typed models; schema-version table; in-memory SQLite for tests; the spec's `models.py`/`session.py` naming presumes it |
| core | `ebooklib` | 0.18 | EPUB 3 writing (nav + ncx) |
| core | `markdown` | 3.6 | Markdown → HTML for EPUB/PDF (footnotes, tables, fenced code extensions) |
| `[marker]` | `marker-pdf` | 1.0 | Opt-in high-fidelity extractor (pulls torch) |
| `[local]` | `ctranslate2`, `sentencepiece`, `transformers` (tokenizer only), `huggingface-hub` | 4.0 / 0.2 / 4.40 / 0.23 | OPUS-MT / NLLB inference; model download helper |
| `[pdf]` | `weasyprint` | 62 | PDF export (needs Pango/Cairo natively; documented) |
| `[dev]` | `pytest`, `pytest-asyncio`, `hypothesis`, `mypy`, `ruff`, `import-linter`, `types-Markdown` | 8 / 0.23 / 6.100 / 1.10 / 0.5 / 2 / – | Tests, property tests, strict typing, layering contract |

Decisions and rationale:

- **SQLAlchemy 2.x over raw `sqlite3`**: typed models, clean migrations path (a `schema_version` table + hand-written upgrade functions; Alembic is not required for v1), trivially swappable engine for in-memory tests, and the required `database/models.py` + `session.py` files map naturally. Sync engine (no `aiosqlite`): DB work is microseconds compared with network calls; the orchestrator serializes it through `asyncio.to_thread` + one lock. PRAGMAs: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`.
- **Custom backoff over tenacity**: retry state is persisted (`retry_count`, `next_attempt_at`) and scheduled by the orchestrator, not wrapped around a call; Retry-After handling and cancellation awareness are simpler in ~60 lines than in tenacity callbacks.
- **typer + rich over click/argparse**: envvar binding and default display in `--help` for free (AC US-11/2).
- **pydantic-settings over plain `os.environ`**: precedence, `.env`, typed coercion, one place for validation messages.
- **No `langdetect`/`httpx`**: stopword ratio suffices; the DeepL client handles HTTP.
- `requirements.txt` pins the core group (`==` versions for reproducible installs; `pyproject` keeps `>=` minimums).

---

## 11. Testing Strategy (design level)

Test doubles (`tests/conftest.py`):

- `FakeTranslator(BaseTranslator)`: configurable `capabilities`; records `calls: list[(chunk_id, attempt, texts)]`, current and max observed concurrency (increment on enter, decrement on exit, with an `asyncio.sleep(latency)`); a **script** maps `chunk_id → [outcome, ...]` where outcome ∈ `{ok, err_429(retry_after), err_5xx, err_empty, err_456, err_403, err_400, raise_exception}` consumed per attempt; default outcome `ok` returns `"TR:" + text`.
- `IdentityTranslator`: returns input texts unchanged → end-to-end invariant `translated_book.md == source_book.md` (with placeholders un-rendered).
- `CountingRepository` wrapper: counts `claim_pending`/`complete` calls to assert zero re-sends.
- Fixture PDF builders using PyMuPDF (`fitz.open()` + `insert_text`/`insert_textbox` with fonts and sizes): (a) 6 pages with running header "The Dragon Book" + footer page numbers; (b) a paragraph broken across pages with `trans-` / `lation`; (c) a non-hyphen continuation across pages; (d) one page that is image-only; (e) a file with no text layer (only an inserted PNG); (f) headings at 18/14 pt vs body 10 pt; (g) a monospace block; (h) small-font footnote lines; (i) a German-text document for language detection. Built once per session into `tmp_path_factory`.

Test matrix:

| Area | Tests |
|---|---|
| `test_chunker.py` | block tiling; each block kind; heading-stays-with-next (incl. consecutive headings, heading at EOF); packing bounds; oversize paragraph → sentence sub-chunks with `parent_block_id` and exact concatenation; oversize list/table splits; code fence never split, `translatable=False`; hypothesis property: random BT-Markdown → lossless join, tiling, bounds-with-documented-exceptions; segmenter round-trip (segments → identity → text unchanged); inline protection restore |
| `test_glossary.py` | discovery with stop-list; min-count; sentence-initial-only words excluded; ambiguity flag; n-gram absorption; schema validation errors name the entry; user-file precedence; duplicate source error; effective hash stability; prompt builder contains all terms; post-replace strategy; post-check suffix tolerance with Turkish case folding |
| `test_orchestrator.py` (pytest-asyncio, in-memory SQLite) | max concurrency ≤ N; 429 → reschedule with Retry-After ≥ header, `retry_count` increments, no exception; retries exhausted → FAILED with error, job continues; 456/403 → job PAUSED, chunk back to PENDING, exit 3; 400 → immediate FAILED; empty response → retry then review flag; non-translatable chunks never call the provider; resume: 40 COMPLETED / 60 other → exactly 60 sends; PROCESSING rows re-queued at start; hash mismatch refusal and `--fresh` archive; provider mismatch refusal; lease blocks a second runner; cancellation releases in-flight chunks to PENDING; `next_attempt_at` honored across a simulated restart; limiter AIMD behavior |
| `test_extractor_cleanup.py` | header/footer removed (fixture a); page numbers stripped; hyphen rejoin (b) and compound preservation; cross-page merge (c); image-only page skipped (d); no text layer → `NO_TEXT_LAYER`, no DB created (e); heading tiers (f); code fence (g); footnotes → markdown (h); language detection en vs de (i); marker unavailable → fallback flagged |
| `test_exporters.py` | heading hierarchy parity; partial markers under `--allow-partial` and refusal without; EPUB opens with `read_epub`, `lang == "tr"`, TOC entries match H1/H2, chapter count; Turkish bytes round trip in md/epub (pdf test skipped unless weasyprint importable); atomic write on simulated disk error leaves no partial file |
| `test_result.py` / CLI | `Result` helpers; exit-code mapping table; `--help` shows defaults; missing `DEEPL_API_KEY` fails before extraction (spy on extractor) |
| Layering | import-linter contract (Section 1.4) run in CI; `mypy --strict` |

CI matrix: Windows + Linux, Python 3.10 and 3.12; extras not installed in the core job (verifies NFR-09).

---

## 12. Risks, Mitigations, and Hand-off

### 12.1 Risks

| # | Risk | Mitigation |
|---|---|---|
| R1 | DeepL alters or drops Markdown/inline syntax | Segmenter keeps all markers local; only plain text is sent; XML tag protection for inline code/URLs; post-check + review flag |
| R2 | DeepL rate limits are undocumented; 429 storms | Default concurrency 4; AIMD limiter; persisted backoff with Retry-After; `deepl` client internal retries disabled to avoid double retry |
| R3 | Header/footer heuristic removes legitimate text or misses chapter-title headers | Band + repetition + digit masking; ambiguous removals logged with page numbers; `--extractor marker` as a second opinion; fixture corpus grows with real books |
| R4 | Heading detection by font size fails on books with unusual typography | Tiering with numbered-pattern reinforcement; user can edit `source_book.md` before `translate` (the md hash rebinds via `--fresh` for chunks only, not the PDF hash) — **open question O1** |
| R5 | Glossary hash changes mid-job → inconsistent terminology | Warning with affected chunk count; `--retry-failed` does not re-send COMPLETED; document "re-run with `--fresh` for full consistency" |
| R6 | SQLite write contention between the heartbeat task and workers | Single `asyncio.Lock` around all DB calls; WAL + busy_timeout |
| R7 | Windows: signal handling and console encoding | `signal.signal` + `call_soon_threadsafe`; stdout reconfigure; CI on Windows |
| R8 | weasyprint native deps on Windows | Optional extra; graceful exit 2; EPUB is the primary rich output |
| R9 | Local NMT quality/memory on CPU | Marked best-effort; `--limit` for previews; `PROVIDER_TRANSIENT` on OOM halves permits |
| R10 | `src` → `book_translator` package mapping confuses contributors | Documented in README and enforced by import-linter (no `src.` imports) |
| R11 | Placeholder tokens (`⟦n⟧`) translated or dropped by non-DeepL providers | Retry once, then review flag with token re-appended; DeepL uses tag handling instead |
| R12 | Very large tables/lists produce many tiny segments (many texts per request) | Batching honors `max_texts_per_request`; sequential sub-requests inside one `translate` call |

### 12.2 Open questions for the user (defaults chosen)

- **O1**: If the user hand-edits `source_book.md` after `extract`, should `translate` accept it automatically (re-chunk when no chunk is COMPLETED yet) rather than requiring `--fresh`? Default: accept automatically while `completed == 0`; require `--fresh` otherwise.
- **O2**: Should `glossary` pre-fill `target` with a DeepL single-term translation (costs characters)? Default: no.
- **O3**: EPUB chapter split fallback to H2 threshold (< 2 H1) acceptable? Default: yes.

### 12.3 Items handed to the DB Architect

1. Aggregates to model: **Job**, **Chunk**, **Run**, **Lease**, **SchemaMeta** with the facts listed in Section 3.1; one SQLite file per output directory (`translation_state.db`).
2. Chunk identity: `chunk_id == order` (1-based, dense) per job; `content_hash` for integrity; `parent_block_id`/`sub_index`/`sub_count` for sub-splits.
3. State machine and transitions (Section 3.2) with the precondition semantics each repository method needs (`claim_pending` is an atomic PENDING→PROCESSING under `BEGIN IMMEDIATE`; `complete` conditional on PROCESSING; `requeue_processing` unconditional at run start).
4. Access patterns to optimize: claim next eligible (`status = PENDING AND (next_attempt_at IS NULL OR <= now) ORDER BY order LIMIT n`), counts per status, earliest `next_attempt_at`, ordered streaming for assembly in batches, totals (sum of `chars_sent`, `chars_billed`), FAILED/review id lists, lease acquire/heartbeat.
5. `translated_text` and `status = COMPLETED` written in the same statement; `last_error` bounded in length; `warnings` as JSON text.
6. Timestamps in UTC ISO-8601 (SQLite has no native datetime); `next_attempt_at` nullable.
7. Schema versioning: an integer `schema_version` checked at open; mismatch → `STATE_SCHEMA_INCOMPATIBLE`; provide the upgrade path table (v1 only for now).
8. Never store secrets; `provider_glossary_id` is not a secret and may be stored.
9. Expected volumes: ≤ 5 000 chunks per job, ≤ 10 KB text per chunk (source + translated), runs ≤ 100 per job — indexes should be minimal.
10. PRAGMAs to be set by `session.py`: WAL, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`.
11. Lease row: single row per DB; stale detection by `heartbeat_at`.
12. Optional: a `provider_calls` table for per-attempt telemetry is **not** required — the JSONL log covers NFR-11; aggregate counters on Run/Chunk suffice.

---
✅ Solution Architect tamamlandı.
➡️  Sonraki adım: DB Architect
