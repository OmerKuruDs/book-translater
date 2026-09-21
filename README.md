# book-translator

Translate an English PDF book into Turkish **without losing the page**.

Two modes:

* **overlay** — the translation is written back into the *source PDF itself*. Same pages, same
  page size, same images, drawings, links and bookmarks; only the text is replaced, in its own
  rectangle, at its own font size, weight, alignment and colour. Display equations are detected
  and left untouched.
* **reflow** — the text is re-typeset as Markdown, EPUB, or a fresh PDF whose page design is
  derived from the source.

Every stage is resumable: progress lives in `<output>/translation_state.db`, so an interrupted
run continues where it stopped and never pays for the same characters twice.

Provider: DeepL. Design documents and the full engineering history are in `docs/`.

## Status

Working and exercised on a real 48-page book. The 8-step workflow this project follows has one
step left: **QA**. Known limits are listed under *Limitations* below and tracked in
`docs/durum.md` (Turkish) and `docs/04_code_review.md`.

## Install

Requires Python 3.11+.

```
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on Linux/macOS
pip install -e .
```

Set your DeepL key in a `.env` file next to where you run the tool (it is git-ignored):

```
DEEPL_API_KEY=your-key-here
```

`book-translator providers` tells you whether the key is picked up. `--dry-run` reports exactly
how many characters a run would send, without contacting the provider.

## Usage (v1.1)

Two ways to get a Turkish book out of an English PDF; both keep their progress in
`<output>/translation_state.db` and can be interrupted and resumed at any time.

```
# overlay: the translation is written back INTO the source PDF layout (default PDF output)
book-translator run -i book.pdf -o out --mode overlay
#   -> out/translated_book.overlay.pdf + out/overlay_review.json

# reflow: re-typeset text as Markdown / EPUB (and a native PDF on request)
book-translator run -i book.pdf -o out                     # md + epub
book-translator run -i book.pdf -o out -f md -f epub -f pdf

# stage by stage (every stage is resumable; translate is the only one that calls the provider)
book-translator extract   -i book.pdf -o out
book-translator glossary  -o out                           # edit out/glossary.json, approve terms
book-translator translate -i book.pdf -o out [--mode overlay] [--dry-run]
book-translator export    -o out [-f md -f epub -f pdf -f pdf-overlay]
book-translator status    -o out [--json]
```

* **`--mode overlay` / `--format pdf-overlay`** — page blocks are translated one by one and
  placed into their original rectangles with the original font size, weight, alignment and
  colour; images, drawings, links and bookmarks of the source stay untouched. Blocks that had
  to shrink below `--overlay-min-scale` (0.65), sentence fragments split by a page break and
  blocks kept in English are listed in `overlay_review.json`. Body paragraphs of one page share
  one font scale (`--no-overlay-uniform-scale` lets every block shrink on its own). Exit code:
  `0` even with a non-empty review list, `2` when a block could not be placed, a page has no
  text layer, or `--allow-partial` was used. Both passes can live in the same output
  directory; `export` without `--format` writes `md,epub` for a reflow pass and `pdf-overlay`
  for an overlay pass.
* **Figures** — vector/raster figures are rendered to `images/pNNN-fKK.png` and referenced
  from the Markdown. Their labels travel to the provider as a legend block and are painted back
  into a copy of the figure, `images/pNNN-fKK.tr.png`; the English original, `source_book.md`
  and `figures.json` are never modified and only the *translated* documents point at the
  `.tr.png`. The legend list itself is hidden by default (`export --figure-legend` shows it);
  a figure whose labels could not be painted (or were truncated) keeps its legend so no
  translation is lost (`figure_labels_not_painted:N`, `figure_label_truncated:N:k`).
  `--no-figures` gives the v1 behaviour. Files in `images/` the tool did not create are never
  deleted; they are reported as `figure_orphan:<file>`.
* **Native PDF (`-f pdf`)** — rendered with PyMuPDF (no system libraries; `--pdf-engine
  weasyprint` needs the optional `[pdf]` extra). `--pdf-font FILE` is validated for Turkish
  glyph coverage before anything is written.
* **Source-derived settings** — `extract` measures the input and writes
  `source_profile.json` (page rectangle, body font size, margins, serif/sans). The reflow PDF
  uses it by default: `--pdf-page-size source`, `--pdf-margin source`. Pass a paper name
  (`A4`, `Letter-l`, …) or CSS-style margins (`"18mm 15mm"`) to override; the file can also
  be edited by hand.
* **`export --input FILE`** — the state remembers the resolved absolute path and the SHA-256
  of the PDF it was created from. `export` re-hashes that file before it writes anything,
  because two steps read the source again: the overlay export (it *is* the source PDF) and
  the figure-label step of the reflow outputs. What a bad file costs depends on which of the
  two needs it:
  * `-f pdf-overlay` — a different file is refused with exit `5`, a missing one with exit
    `1`; nothing is written.
  * `-f md` / `-f epub` / `-f pdf` — the documents do not need the PDF, only the labels
    inside the figure images do. A missing file (exit `0`) or one that changed since
    `translate` (exit `2`) is reported as `figure_translate_failed:input_missing` /
    `…:input_changed`: the file is not read, the figures keep their English labels and their
    legend lists stay in the document, so no paid translation is lost. Naming the file with
    `export --input` is still refused when it does not exist (exit `1`).

  If the PDF was moved or renamed, point `export --input` at it — it must still be the very
  same file.

### Output directory

```
out/
├── translation_state.db      state (SQLite, schema v4): job, chunks, overlay units, runs, lease
├── source_book.md            extracted BT-Markdown (may be edited before `translate`)
├── source_profile.json       page size / body font / margins measured from the input
├── figures.json              figure inventory (page, region, DPI, labels + label boxes)
├── images/
│   ├── p002-f01.png          figure as extracted (English labels)
│   └── p002-f01.tr.png       the same figure with translated labels (written by `export`)
├── glossary.json             discovered terms; only `approved` entries are applied
├── translated_book.md        reflow outputs
├── translated_book.epub
├── translated_book.pdf       (-f pdf)
├── translated_book.overlay.pdf   overlay output (--mode overlay / -f pdf-overlay)
├── overlay_review.json       blocks to look at after an overlay export (+ the pass it belongs to)
├── summary.json              last run: counts, characters sent, outputs, placement counts
├── logs/run-<id>.jsonl       structured log (never contains the API key or book text)
└── state-archive/<stamp>/    previous state moved here by --fresh (never pruned)
```

Exit codes: `0` success · `1` error (bad input, missing/invalid option, missing source PDF) ·
`2` partial output · `3` paused (quota/auth) · `4` state locked by another process ·
`5` state mismatch (changed input, changed options, provider switch) · `130` interrupted.

`export` writes one format after the other and reports every file that reached the disk, also
when a later format fails: the failing one is printed as `error: export_error:<format>:…` and
the exit code is that failure's (`1` for a bad option or unusable content, `2` when an
optional exporter is merely missing — then the line reads `warning: export_failed:…`). Only an
export that produced no file at all exits with the error alone.

## Web UI (local)

A one-page browser front end for the same pipeline: **drop a PDF → translate → download**.
It is an optional extra and the CLI does not depend on it. The interface text is Turkish.

```
pip install -e ".[web]"
book-translator web                           # http://127.0.0.1:8765
```

On Windows `web.bat` in the repository root does the same by double-click: it runs the
interpreter inside `.venv` directly, so nothing has to be activated first.

`python -m book_translator.web` is the same server, but it only resolves when the virtual
environment is active - a plain `python -m book_translator.web` in a fresh shell reports
`No module named 'book_translator'` because it reaches the system interpreter. The console
script has no such trap: it always runs the interpreter it was installed into.

`--host --port --work-root --glossaries` override the defaults on either spelling;
`uvicorn book_translator.web.app:app --host 127.0.0.1 --port 8765` works too.
Settings (`DEEPL_API_KEY`, chunk sizes, page design, …) come from the environment and the
`.env` file of the directory you start the server in, exactly as they do for the CLI.

**The estimate gate.** "Önce tahmin göster" is on by default: the server first runs the
pipeline with `--dry-run`, reports how many characters the provider *would* be sent and
waits. Nothing is sent until you press *Onayla ve çevir*. When the run finishes, the page
shows what was actually sent (this run and in total for that job).

Each job gets its own directory under the work root (`./web-jobs/<job id>/`, override with
`--work-root` or `BOOK_TRANSLATOR_WEB_WORKDIR`); it is an ordinary output directory, so
`book-translator status -o web-jobs/<job id>` and every CLI stage work on it afterwards. The
uploaded file is always stored as `input.pdf` inside that directory — the name the browser
sends is only ever displayed. The glossary box lists the `*.json` files of `./glossaries`
(`--glossaries` / `BOOK_TRANSLATOR_WEB_GLOSSARIES`) as checkboxes, **all ticked**: a book
usually needs more than one vocabulary, and the chosen files are merged before the run
(later file wins on an identical term). Untick what you do not want.

| Method | Path | |
|---|---|---|
| `GET` | `/` | the page (single file, no CDN, works offline) |
| `GET` | `/api/config` | glossary list, modes, upload limit |
| `POST` | `/api/jobs` | multipart `file`, `mode`, `glossary` (repeated, one per file), `estimate` → job |
| `POST` | `/api/jobs/{id}/start` | confirm the estimate (or resume) and translate |
| `GET` | `/api/jobs/{id}` | phase, stage, unit progress, characters, result/error |
| `GET` | `/api/jobs/{id}/download/{key}` | one produced output (`markdown`, `epub`, `pdf-overlay`, …) |

**Security.** There is no authentication, so the server binds `127.0.0.1` only; another
`--host` prints a warning. Uploads must carry a `.pdf` name *and* start with the PDF
signature, the size limit (`BOOK_TRANSLATOR_WEB_MAX_MB`, default 200) is enforced while
reading rather than trusted from the request, and downloads only ever serve a file the
export stage reported for that job. Errors are shown with the tool's own redacted
`AppError.message` plus a Turkish explanation — the API key cannot reach the browser.

Job state lives in memory: restarting the server forgets the job list, but the output
directories (and `summary.json` inside them) stay on disk.

## Layout

`src/` is the package root and is installed under the import name `book_translator`
(hatchling `sources` mapping in `pyproject.toml`). Tests and external code import
`book_translator.*`. Inside `src/` modules import each other with **relative imports**
(`from ..domain.result import Ok`), because `mypy src` checks the tree as the package
`src` and cannot resolve the renamed top-level name.

## Development

```
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,web]"   # "web" only for the browser UI and its tests
.venv/Scripts/pytest -q
.venv/Scripts/mypy src
.venv/Scripts/ruff check src tests
```

`--fresh` archives the previous state into `<output>/state-archive/<timestamp>/`; archives are
never pruned automatically.


## Keeping technical terms in English

Machine translation turns established jargon into literal Turkish: *computer vision* becomes
"bilgisayar görme", *object detection* becomes "nesne algılama", *zero-shot detection* becomes
"sıfır örneklemeyle algılama". Readers who learned the field in English find that harder to
follow, not easier.

A glossary entry whose `target` equals its `source` tells the provider to leave the term alone.
Two ready-made lists ship with the repo:

| File | Terms | Scope |
|---|---|---|
| `glossaries/computer-vision.en-tr.json` | 239 | hand-written computer-vision list, all kept in English |
| `glossaries/ai-ml.en-tr.json` | 961 | the above, the term names of Google's ML Glossary, and 154 terms given a fixed Turkish wording |

The second file mixes both uses: 807 entries map a term to itself (keep it in English) and
154 prescribe a Turkish word (`Matrix` → `Matris`, `Pixel` → `Piksel`). Where the two
sources disagreed - `Convolution`, `Tensor`, `Edge Detection`, `Downsampling` and 92 others -
the English-keeping entry won.

```
book-translator run -i book.pdf -o out --mode overlay     --glossary glossaries/ai-ml.en-tr.json
```

Measured on real book text, same sentence:

| | Output |
|---|---|
| without | "**bilgisayar görme** alanında … **nesne algılama** ve **sıfır örneklemeyle algılama**" |
| with | "**computer vision** alanında … **object detection** ve **zero-shot detection**" |

DeepL attaches Turkish case suffixes by itself (`feature matching'e`, `bounding boxlar`).

**Single generic words are left out on purpose.** `cost`, `depth`, `class`, `label`, `loss`,
`state`, `step`, `weight`, `baseline` and ~200 others would also match ordinary prose — "the
cost of the lens" must stay "lensin maliyeti". Only multi-word terms, acronyms and unambiguous
single words are kept.

**The provider applies a glossary on a best-effort basis.** When it rephrases a sentence the
term can still slip through. Every miss is reported as `glossary_miss:<term>`, and
`--strict-glossary` also flags the chunk for review.

The files are plain JSON — edit them, or point `--glossary` at your own. The same mechanism
enforces a *chosen* Turkish wording: give the entry a different `target`
(`{"source": "image", "target": "görüntü"}`) and every occurrence follows it. A user glossary
wins over the terms `book-translator glossary` discovers in the book itself.

## Test fixtures

The test suite uses PDF fixtures that are **not** in this repository: they are pages of a
copyrighted textbook and are excluded on purpose, together with every generated output
directory. Tests that need them look for:

| Path | What it is |
|---|---|
| `docs/test_doc.pdf` | a 3-page excerpt (prose + one vector figure with labels) |
| `docs/test-2.pdf` | a 48-page excerpt with display mathematics |

Supply your own PDFs at those paths to run the full suite. **Without them the suite does not
pass**: around 60 tests fail and 15 error out rather than skipping, and they include the
regression net for figure detection, display-mathematics bands and the redaction rect.
Type and lint checks are unaffected:

```
python -m pytest -q          # the suite
python -m mypy src           # strict
python -m ruff check src tests
```

## Rate limits and retries

A provider answering **HTTP 429** is asking for time, not refusing the work, so it has its
own retry budget and can never turn a unit into a permanent failure.

| Setting (`BOOK_TRANSLATOR_…`) | Default | What it does |
|---|---|---|
| `MAX_RETRIES` (`--max-retries`) | 5 | Retries for errors the unit itself causes (5xx, empty answer). A 429 does **not** spend these, and does not increase the stored `retry_count`. |
| `RATE_LIMIT_MAX_RETRIES` | 20 | How many times a unit waits out a 429 in one run. Counted in memory: a resumed job starts the count over. |
| `RATE_LIMIT_MIN_DELAY_S` | 5 | Floor under the backoff jitter for a 429. Full jitter draws from `[0, ceiling]`, so without a floor a budget can be spent in seconds without ever waiting. |
| `RATE_LIMIT_COOL_OFF_S` | 5 | After a 429 no new request leaves at all for this long. Halving the concurrency stops helping once it is down to 1 permit, which is where a throttled run lives. |
| `CONCURRENCY` (`--concurrency`) | 4 | Upper bound; the limiter halves it on every 429 and grows it back by one after 20 clean calls. On the DeepL **free** tier `--concurrency 2` starts closer to what the tier allows. The default stays 4 because it is a ceiling, not a target, and lowering it would slow every paid run to fix a free-tier symptom. |

When the rate-limit budget of a unit does run out the **job is paused** (exit code 3,
status `PAUSED`) with every unit still `PENDING` and its retry budget untouched; re-running
the same command resumes it. Nothing is lost and nothing is paid for twice.

## Limitations

* **Mathematics set in text fonts.** Display equations are detected by font (Computer Modern
  math families and anything named `*Math*`). An equation typeset with the *text* fonts
  (`CMR`/`CMBX`) is not recognised as one; widening the rule would swallow prose.
* **A few lines stay English.** A prose line whose vertical centre falls inside an equation band
  is kept with the band. Measured at 17 lines out of 2560 (0.7 %) on the 48-page sample.
* **Typeface changes.** Overlay text is written with the built-in serif (Charis SIL), not the
  source font. `--pdf-font` takes your own font (it must cover Turkish).
* **Running headers stay English** by default; `--overlay-translate-headers` turns them on.
* **Scanned PDFs are out of scope** — overlay mode needs a real text layer, and a PDF whose
  permission bits forbid modification is refused before any provider contact.

## Licence

`glossaries/ai-ml.en-tr.json` derives part of its term list from Google's Machine Learning
Glossary (<https://developers.google.com/machine-learning/glossary>), published under
CC BY 4.0. Only the term names are used, each mapped to itself; the definitions are not
redistributed.


No licence has been chosen yet, so default copyright applies: the code is public to read, but
not yet licensed for reuse. Open an issue if you need one.
