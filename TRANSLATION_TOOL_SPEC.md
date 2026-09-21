# Project Specification: End-to-End Book Translation CLI & Pipeline Tool

## 1. Project Overview
The objective is to build a robust, production-ready Python command-line and background processing tool that translates entire English books (scanned or digital PDFs, ~200+ pages) into grammatically accurate, context-consistent Turkish. The tool must preserve layout structure, prevent broken cross-page sentences, enforce terminology consistency via a dynamic glossary, and export the result into clean Markdown and EPUB/PDF formats.

---

## 2. Core Architecture & Pipeline Flow

The system must follow a 4-stage pipeline:

1. **Document Ingestion & Layout Parsing:**
   - Input: English PDF file.
   - Engine: `marker-pdf` (preferred for high-fidelity Markdown extraction) or `PyMuPDF` (`fitz`) fallback.
   - Output: Normalized, clean Markdown (`source_book.md`) where running headers, footers, and page numbers are removed, and hyphenated/broken sentences across page breaks are reassembled.

2. **Glossary Discovery & Extraction:**
   - Scan the extracted text for recurring entities (character names, locations, technical terms).
   - Generate a local `glossary.json` mapping English terms to approved Turkish equivalents.
   - Support uploading this glossary to translation backends (e.g., DeepL Glossary API) or injecting it into prompt templates.

3. **Asynchronous Semantic Translation Engine:**
   - **Chunking:** Split the Markdown content into semantic blocks (preserving markdown headers, code blocks, lists, and paragraph breaks; target chunk size: 1000–1500 characters).
   - **Translation Backend:** Abstract interface supporting:
     - Primary: DeepL API (with Glossary ID support).
     - Alternative/Fallback: Local neural translation via CTranslate2 (`NLLB-200` or `OPUS-MT`) or LLM post-editing.
   - **Fault Tolerance & State Management:** Persist job state to a lightweight SQLite database (`translation_state.db`). Each chunk must track `chunk_id`, `status` (`PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`), retry count, and rate-limit backoff. If interrupted, the process must resume from the last uncompleted chunk without re-translating existing chunks.

4. **Reassembly & Export:**
   - Concatenate translated chunks in their original hierarchy.
   - Export outputs:
     - `translated_book.md`
     - `translated_book.epub` (using `ebooklib` or `pandoc`)
     - Optional PDF output via `weasyprint`.

---

## 3. Tech Stack Requirements

- **Language:** Python 3.10+
- **CLI Framework:** `typer` or `click` + `rich` (for terminal progress bars and status reporting)
- **PDF Extraction:** `marker-pdf` / `pymupdf`
- **Translation APIs / Engines:** `deepl-python`, `requests`, with modular interfaces for pluggable translation providers
- **State & Storage:** SQLite (`sqlite3` / `SQLAlchemy`)
- **Export Tools:** `ebooklib`, `markdown`, `weasyprint` (optional)
- **Code Standards:** 
  - Strict type hinting (`typing`).
  - All variables, classes, methods, docstrings, and commits must be written in **English**.
  - Robust exception handling with retry logic (`tenacity` or custom exponential backoff).

---

## 4. Required Project File Structure

```text
book-translator/
├── README.md
├── requirements.txt
├── pyproject.toml
├── src/
│   ├── __init__.py
│   ├── cli.py                    # CLI entry point (arguments: --input, --output, --glossary, --provider)
│   ├── config.py                 # Environment variables and system configurations
│   ├── database/
│   │   ├── __init__.py
│   │   ├── models.py             # SQLite schema for translation state tracking
│   │   └── session.py            # DB session manager
│   ├── extractors/
│   │   ├── __init__.py
│   │   ├── base.py               # Abstract base class for extractors
│   │   └── marker_extractor.py   # Marker-pdf layout-aware extraction implementation
│   ├── glossary/
│   │   ├── __init__.py
│   │   └── manager.py            # Glossary builder, parser, and API sync
│   ├── translators/
│   │   ├── __init__.py
│   │   ├── base.py               # Abstract translation interface
│   │   ├── deepl_translator.py   # DeepL API integration with glossary support
│   │   └── local_nmt.py          # Local CTranslate2 / NLLB engine
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── chunker.py            # Semantic markdown chunker
│   │   └── orchestrator.py       # Async/sequential batch manager with resume support
│   └── exporters/
│       ├── __init__.py
│       ├── markdown_exporter.py  # Assembles final Markdown
│       └── epub_exporter.py      # Generates EPUB with clean styling
└── tests/
    ├── test_chunker.py
    └── test_glossary.py