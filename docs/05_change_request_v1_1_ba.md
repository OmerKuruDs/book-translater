# Change Request v1.1 — Requirements Analysis: Figures, Layout-Preserving PDF, Native Reflowed PDF

**Project:** book-translator (Python CLI, English PDF → Turkish Markdown / EPUB / PDF)
**Baseline:** v1 as delivered (`docs/01_business_analysis.md`, `docs/02_solution_design.md`, `docs/03_db_design.md`, code-review fix round complete, live DeepL test passed on `docs/test_doc.pdf`).
**Date:** 2026-09-18
**Status:** Draft for user approval → Solution Architect

---

## Türkçe Özet

Kullanıcı canlı testte iki şey istedi:

1. **Şekiller korunsun.** `test_doc.pdf` 2. sayfadaki Şekil 1.12 vektörel çizgi çizimi (39 yol) + metin etiketlerinden oluşuyor. v1 sadece etiketleri "başlık/paragraf" sanıp çıkarıyor, çizimi tamamen kaybediyor (`source_book.md`'de `## 3D (where?)`, `## 2D (what?)` ve 13 adet başıboş satır; üst bilgi "1.3 Book overview" da sızıp "1,3" olarak çevrilmiş). Raster görseller ise sadece `<!-- image:N -->` yer tutucusu. İstenen: şekil bölgesi PNG olarak kesilip Markdown/EPUB/PDF'e gerçek görsel olarak konsun, alt yazı çevrilsin, şekil içindeki metin akışa karışmasın.
2. **Sayfa düzenini koruyan PDF.** Orijinal PDF sayfaları kalsın; her metin kutusunun içine Türkçesi yazılsın (kanıt: `docs/test_doc_output/deney/overlay_page2.png`, kullanıcı "çok iyi görünüyor" dedi). Çizimler ve resimler dokunulmadan kalsın.

Ek olarak, mevcut reflow PDF çıktısı weasyprint'e bağlı ve Windows'ta kurulamıyor; PyMuPDF ile yerel PDF üretimi (F3) bu bağımlılığı kaldırır.

Bu doküman üç özelliği **ayrı ayrı önceliklendirilebilir** biçimde tanımlar:

| Özellik | Kısa tanım | Öneri |
|---|---|---|
| **F1** Şekil koruma (Markdown/EPUB/reflow PDF) | Raster + vektör şekil bölgelerini PNG'ye kes, görsel referansı ver, etiketleri akıştan çıkar, alt yazıyı çevir | **Önce** — v1'deki gerçek bir hatayı (etiket sızması) düzeltir, tüm çıktıları iyileştirir |
| **F3** weasyprint'siz reflow PDF | PyMuPDF Story ile HTML→PDF; Windows'ta `--format pdf` çalışsın | **İkinci** — küçük iş, F1 görsellerini PDF'e taşır |
| **F2** Düzen koruyan PDF ("overlay") | Orijinal sayfa üstüne blok blok Türkçe metin | **Üçüncü** — en yüksek değer ama en yüksek risk/emek; ayrı çeviri geçişi ve yeni durum verisi gerektirir |

Kullanıcı overlay PDF'i (F2) daha acil görüyorsa F1 → F2 → F3 sırası da mümkündür; gerekçe §9'da. Açık sorular Q10–Q23'te, her biri için önerilen varsayılan var; onay ("devam") gelmeden mimari tasarıma geçilmeyecek.

---

## 1. Feature Summary

**Title:** Preserve figures as images in all outputs, add a layout-preserving PDF export, and make reflowed PDF export work without native libraries
**Module:** book-translator — extraction, chunking contract, export, translation state
**Priority:** High (F1), Medium-High (F2), Medium (F3)
**Type:** Enhancement (F1 also fixes a v1 defect: figure text leaking into the prose flow)

## 2. Business Goal

- **Problem:**
  - v1 loses every figure. Raster images become a placeholder line; vector diagrams (line art built from paths, such as Figure 1.12) are not recognised at all — their text labels are extracted as stray headings and paragraphs, translated out of context ("Recognition" → "Takdir"), and the drawing itself disappears. Technical books are unreadable without their figures.
  - v1's only rich output is reflowed (EPUB / weasyprint PDF). Readers who want the book to look like the original — same pages, same figure placement, same typography — have no option. The PoC shows this is achievable.
  - v1 PDF export depends on weasyprint, which needs GTK/Pango/Cairo on Windows and is not installed on the developer's own machine (1 test permanently skipped); `--format pdf` is effectively unavailable on the primary OS.
- **Expected benefit (measurable):**
  - 100 % of figures of a fixture corpus appear as images in `.md`, `.epub` and reflowed `.pdf` (today: 0 %).
  - 0 figure-label lines leak into the prose flow (today: 15 lines + 2 false headings on page 2 of the test document).
  - A layout-preserving PDF where every text block is Turkish, every image and drawing is byte-identical to the source, and a machine-readable list names every block that needed shrinking beyond a threshold.
  - `--format pdf` succeeds on a clean Windows install of the core package (today: exit 2 "exporter unavailable").
- **Affected user roles:**
  - **Translator / Operator** — chooses output mode, reviews the figure inventory and the overlay review list.
  - **Developer / Maintainer** — extends detection heuristics and export backends.
  - **(Indirect) Reader** — receives a book with figures and, optionally, the original page design.

### 2.1 Evidence from the live test (why this matters)

`docs/test_doc.pdf` page 2 → v1 `source_book.md` (excerpt):

```
1.3

Book overview          ← running header leaked (page 2's header differs from pages 1/3; 3-page repetition rule cannot catch it)

## 3D (where?)         ← axis label of the diagram, promoted to a heading (large font)
## 2D (what?)

2. Image formation     ← 13 node labels as bare paragraphs, out of reading order
4. Model fitting and optimization
...
Figure 1.12            ← caption correctly kept (but split from its text)
```

`translated_book.md` then contains `1,3`, `## 3D (nerede?)`, `6. Takdir`, and no diagram. The PoC (`deney/overlay_page2.png`) keeps the drawing and replaces each text block in place; it also exposes two defects that become requirements below: blocks 13 and 14 show residual English glyphs ("reconstruction", "rendering") next to the Turkish text, and the one-page overlay PDF is 1.8 MB versus 109 KB for the whole three-page source.

## 3. Goals and Non-Goals

### Goals

- **G1 (F1):** Every figure region — raster image or cluster of vector drawing — is preserved as a PNG file in `<output>/images/` and referenced from the translated Markdown, EPUB and reflowed PDF at its reading position, with its caption translated.
- **G2 (F1):** Text that lies inside a figure region never enters the paragraph flow, the glossary discovery, or the chunk stream as prose.
- **G3 (F2):** A new export format produces a PDF whose pages are the original pages with each text block's content replaced by Turkish, all non-text content untouched.
- **G4 (F2):** The overlay export reuses the v1 translation machinery (resumable state, retries, backoff, glossary, provider abstraction, status, `--fresh`).
- **G5 (F3):** `--format pdf` (reflowed) works with the core install on Windows, Linux and macOS, using the existing CSS and a font with full Turkish glyph coverage.
- **G6 (all):** v1 behaviour, outputs and tests remain unchanged when the new options are not used; the lossless chunking invariant (design §4.5) is preserved.

### Non-Goals (explicitly out of scope for v1.1)

- Translating text *inside* the cut-out figure PNGs (labels stay English in F1; a translated legend is offered instead). *Note: F2's in-place technique could later be reused to produce translated figure PNGs — recorded as a future option, not v1.1.*
- OCR of scanned pages, for any of the three features. Overlay on a page without a text layer is skipped with a warning.
- Re-typesetting the overlay page (moving blocks, reflowing across boxes, changing page count). Text is fitted into its original box only.
- Exact font reproduction in the overlay (the original font is typically a subset without Turkish glyphs; a substitute is acceptable).
- Interactive review UI. Review information is delivered as files / summary output.
- Translation memory between reflow and overlay passes (see Q14 — the two modes are separate translation passes in v1.1).
- Vector-to-vector translation of figures (SVG editing), formula recognition, chart data extraction.
- Output formats other than `.md`, `.epub`, `.pdf` (reflowed) and `.pdf` (overlay).

## 4. User Stories

Numbering continues from v1 (US-1…US-13).

### F1 — Figure preservation for Markdown / EPUB / reflowed PDF

```
US-14: Keep raster images as real images

  As a Translator,
  I want raster images in the PDF to be exported as image files and referenced
  from the translated Markdown and EPUB
  so that the reader sees the picture instead of a "not included" placeholder.

  Acceptance Criteria:
    AC-1: Given a PDF page containing an embedded raster image of at least the
               configured minimum area (default 1 % of the page)
          When extraction runs with figures enabled (default)
          Then a PNG file exists under <output>/images/ for that image, and
               source_book.md contains exactly one single-line, non-translatable
               image reference at the image's reading position instead of the
               v1 placeholder token.
    AC-2: Given the image reference produced in AC-1
          When translate and export run
          Then translated_book.md contains a Markdown image reference to the same
               file, translated_book.epub packages the file and displays it in the
               reader at that position, and the reflowed PDF (when produced) shows it.
    AC-3: Given the same PDF is extracted twice with the same settings
          When the two runs are compared
          Then the image file names, count, order and pixel content are identical
               (deterministic numbering; enables resume and re-extract).
    AC-4: Given --no-figures (or the equivalent setting) is passed
          When extraction runs
          Then behaviour is byte-identical to v1 (placeholder token, no images/
               directory) — verified by the existing extraction fixture tests.
```

```
US-15: Detect and preserve vector-drawn figures

  As a Translator,
  I want diagrams drawn as vector paths (boxes, arrows, plots) to be recognised
  as figures and cut out as images
  so that line-art diagrams like Figure 1.12 are not lost.

  Acceptance Criteria:
    AC-1: Given docs/test_doc.pdf page 2 (39 vector paths + 15 text labels + a
               caption starting "Figure 1.12")
          When extraction runs with figures enabled
          Then exactly one figure region is detected on that page, its PNG covers the
               dashed axis, all boxes and arrows, and all node labels (visually
               equivalent to docs/test_doc_output/deney/fig_1_12.png ± 2 pt border),
               and the region does not include the caption or the running header.
    AC-2: Given the same page
          When source_book.md is inspected
          Then none of the 15 label strings ("2D (what?)", "Image formation", …)
               appear as headings or paragraphs; the caption "Figure 1.12 A taxonomy …"
               appears as one paragraph directly after the image reference.
    AC-3: Given a page whose only vector drawings are decorative (a rule under the
               running header, table borders, an underline, a bullet glyph)
          When extraction runs
          Then no figure is produced for that page (false-positive fixture).
    AC-4: Given a page with two separate figures (e.g. Figure 3.1 and Figure 3.2
               stacked with prose between them)
          When extraction runs
          Then two image references are produced, each followed by its own caption,
               in reading order, and the prose between them is in the paragraph flow.
    AC-5: Given a figure spanning both columns of a two-column page, with column
               text above and below it
          When extraction runs
          Then the figure region contains only the drawing and its inner labels; the
               column text above/below is fully present in the prose flow (fixture
               with known text; 100 % of column sentences recovered).
    AC-6: Given a page that is a full-page figure (drawing + caption, no prose)
          When extraction runs
          Then the page is not treated as "image-only, skipped" (v1 E-04); it yields
               one image reference and its caption.
```

```
US-16: Translate captions and offer a label legend

  As a Translator,
  I want figure captions translated and, optionally, a translated list of the
  labels that appear inside the figure
  so that a Turkish reader can understand a diagram whose labels stay English.

  Acceptance Criteria:
    AC-1: Given a figure with a caption paragraph
          When translation and export run
          Then the caption is translated as a normal paragraph (glossary applied,
               structure post-check applies) and placed directly after the image.
    AC-2: Given a figure region containing N distinct text labels and the legend
               option is on (default per Q10)
          When export runs
          Then a legend block appears after the caption listing each label as
               "English — Türkçe" in the figure's reading order (top-to-bottom,
               left-to-right), and the labels are translated in one request with the
               caption as context (not one request per label).
    AC-3: Given the legend option is off
          When export runs
          Then no legend appears and no label text is sent to the provider.
    AC-4: Given a figure with more labels than the configured legend limit
               (default per Q10, e.g. 40) or with labels that are only numbers/
               single characters (axis ticks)
          When export runs
          Then numeric/single-character labels are excluded and the legend is
               truncated with a visible "… and K more" note; a warning is logged.
```

```
US-17: Control figure rendering and see what was produced

  As a Translator,
  I want to set the rendering resolution and limits, and see a figure inventory
  so that I can trade quality against file size and check nothing was missed.

  Acceptance Criteria:
    AC-1: Given --figure-dpi 300 (default per Q16; range 72–600)
          When extraction runs
          Then every produced PNG has a pixel size equal to region size in points ×
               dpi / 72 (± 1 px) and the DPI is recorded in the figure inventory.
    AC-2: Given the run finishes
          When I read the summary (and status --json)
          Then it lists: figures detected per page, of which raster / vector /
               mixed, total image bytes, the largest image, and any page where
               detection was skipped or limited by a threshold.
    AC-3: Given a single figure PNG would exceed the configured size limit
               (default per Q16, e.g. 8 MB)
          When rendering runs
          Then the DPI is reduced stepwise until it fits (never below 96), and the
               reduction is reported in the inventory and summary.
    AC-4: Given run --fresh or extract --fresh
          When the archive step runs
          Then the images/ directory is archived together with source_book.md and
               the state DB (no orphan images from a previous extraction remain).
```

### F2 — Layout-preserving PDF ("overlay" export)

```
US-18: Export a PDF that keeps the original page design

  As a Translator,
  I want a PDF where each page is the original page and each text block
  contains the Turkish text in the same place
  so that the translated book looks like the original (figures, tables,
  margins, page count all preserved).

  Acceptance Criteria:
    AC-1: Given a completed overlay translation pass (US-20)
          When I run export --format pdf-overlay
          Then <output>/translated_book.overlay.pdf (name per architect) exists,
               has the same page count and page sizes as the input, and opens in
               Acrobat Reader, Chrome and SumatraPDF without warnings.
    AC-2: Given any page
          When the output page is rendered at 150 DPI and compared with the source
               page rendered the same way, masking the text-block rectangles
          Then the unmasked pixels are identical (images and vector drawings are
               untouched — including redaction not blanking pixels of images that
               lie under a text block).
    AC-3: Given a text block that was replaced
          When the output page's text is extracted inside that block's rectangle
          Then it equals the translated text and contains no substring of the
               original English block (no residual glyphs — the PoC defect on
               blocks 13/14 of page 2 must not occur).
    AC-4: Given a block whose translation, at the original font size, does not fit
               the box
          When the page is built
          Then the text is shrunk uniformly (font size and line height) to the
               largest scale that fits, never clipped or overflowing into a
               neighbouring block, and never enlarged beyond the original size.
    AC-5: Given the source block is bold, italic, or a heading size
          When the block is replaced
          Then the replacement keeps the same alignment (left / justified /
               centred as detected), the same size before shrinking, and bold /
               italic at the span level where the source block carries a single
               style; mixed-style blocks keep at least the dominant style.
    AC-6: Given Turkish characters (ç, ğ, ı, İ, ö, ş, ü) in any replaced block
          When the output text is extracted
          Then every character is present (no "?" or missing-glyph boxes), and the
               file embeds exactly one substitute font family (subset) for all
               replaced text.
```

```
US-19: Know which blocks need a human look

  As a Translator,
  I want a list of blocks that were shrunk below a threshold, could not be
  placed, or were translated as sentence fragments
  so that I can proofread exactly those places in the overlay PDF.

  Acceptance Criteria:
    AC-1: Given the overlay export finishes
          When I open <output>/overlay_review.json (name per architect) or the
               summary
          Then each entry has: page number, block identifier, reason
               (shrunk_below_threshold / could_not_fit / fragment / kept_original /
               glyph_missing), the scale factor applied, the source text and the
               translated text.
    AC-2: Given the shrink threshold (default per Q13, e.g. 0.65)
          When any block is placed at a scale below the threshold
          Then it appears in the review list; blocks at or above the threshold do not.
    AC-3: Given the review list is non-empty but every block was placed
          When the command exits
          Then the exit code is 0 and the summary prints the count; if any block
               could not be placed at all, the exit code is 2 (partial).
    AC-4: Given a block whose source text ends mid-sentence at the bottom of a
               page (continuation on the next page)
          When it is translated per block
          Then it is tagged "fragment" in the review list (quality caveat, see Q15).
```

```
US-20: Run the overlay translation through the same resumable pipeline

  As a Translator,
  I want the overlay pass to use the same provider, glossary, retries, resume,
  status and --fresh semantics as v1
  so that a multi-hour overlay job is as safe and as cheap as a reflow job.

  Acceptance Criteria:
    AC-1: Given I run translate --mode overlay (option name per architect)
          When the job is interrupted after K blocks and re-run
          Then no completed block is re-sent (counting fake provider = 0 duplicates)
               and the job completes from where it stopped.
    AC-2: Given an approved glossary and the DeepL provider
          When the overlay pass runs
          Then the same provider glossary binding is used (glossary_bound event with
               the same hash) and glossary terms render identically in the overlay
               and reflow outputs (fixture check).
    AC-3: Given status is run on an output directory with an overlay pass
          When I read the output
          Then it shows the mode, blocks per status, pages completed / total, and
               the same fields as v1.
    AC-4: Given an output directory that only has a reflow pass
          When I run export --format pdf-overlay
          Then the command refuses with a message naming the missing overlay pass
               and the command to run; nothing is written; exit code 1.
    AC-5: Given --fresh
          When used with an overlay job
          Then overlay state and the overlay output are archived in the same way
               as v1 artefacts.
    AC-6: Given the provider request count for the overlay pass of the fixture book
          When compared with the reflow pass of the same book
          Then it is at most 2× the reflow chunk count (blocks are batched per page,
               not one request per block) — see NFR-23.
```

```
US-21: Keep navigation, running headers and metadata sensible

  As a Reader,
  I want page numbers, bookmarks and links to still work in the overlay PDF
  so that the document is usable, not only readable.

  Acceptance Criteria:
    AC-1: Given running headers, footers and page-number lines as detected by the
               v1 heuristic
          When the overlay is built with the default (Q12: keep as-is)
          Then those blocks are left untouched (byte-identical text) and are not
               sent to the provider; with the "translate headers" option they are
               replaced like any other block except page numbers, which are never
               sent.
    AC-2: Given the source PDF has an outline (bookmarks)
          When the overlay is built
          Then the outline exists in the output with the same targets; titles are
               replaced by the corresponding translated heading text when a
               heading block maps 1:1 to an outline entry, otherwise kept.
    AC-3: Given the source has internal link annotations (e.g. "Figure 1.11d")
          When the overlay is built
          Then the link annotations still exist with their original rectangles and
               targets (click areas may no longer align exactly with the reflowed
               words — accepted limitation, documented).
    AC-4: Given the output is inspected
          When metadata is read
          Then Language is "tr", Title is --title (or the source title), Author
               is preserved, and the producer names the tool and version.
```

### F3 — Reflowed PDF without weasyprint

```
US-22: Produce the reflowed PDF on a plain Windows install

  As a Translator on Windows,
  I want --format pdf to work with the core installation
  so that I get a PDF without installing GTK/Pango/Cairo.

  Acceptance Criteria:
    AC-1: Given the core package is installed and weasyprint is not
          When I run export --format pdf
          Then translated_book.pdf is produced, exit code 0, and `exporters`
               lists the pdf exporter as available.
    AC-2: Given the same translated_book.md
          When the reflowed PDF is produced
          Then it contains every heading, paragraph, list, table, code block,
               footnote and F1 image of the Markdown in order (text extraction of
               the PDF, after whitespace normalisation, contains every paragraph
               string of the Markdown).
    AC-3: Given Turkish characters in the text
          When the PDF text is extracted
          Then they are preserved (round-trip fixture from v1 US-10/AC-3), and the
               font used is embedded and subset.
    AC-4: Given weasyprint is installed and the user selects it explicitly
               (Q18: --pdf-engine weasyprint)
          When export runs
          Then the weasyprint path is used unchanged; otherwise the native engine is
               the default.
```

```
US-23: Keep the page design of the reflowed PDF configurable

  As a Translator,
  I want page size, margins, page numbers and fonts to be settable
  so that the reflowed PDF prints well.

  Acceptance Criteria:
    AC-1: Given no options
          When export --format pdf runs
          Then the page size is A5 with the v1 CSS margins, a centred page number
               in the footer of every page after the first, and a bookmark (outline)
               entry per H1/H2 (level per --chapter-level).
    AC-2: Given --pdf-page-size A4 --pdf-margin 20mm (option names per architect)
          When export runs
          Then the resulting pages have that size and text starts at that margin
               (± 1 pt).
    AC-3: Given --css FILE (v1 option)
          When export runs
          Then the CSS is applied; unsupported CSS properties are ignored with a
               single warning listing them (no crash).
    AC-4: Given --pdf-font FILE (v1 option) or the bundled default font
          When export runs
          Then the font is embedded and used for body text; a font without
               Turkish coverage is rejected before rendering with a message.
```

## 5. Functional Requirements

Numbering continues from v1 (FR-01…FR-27).

| ID | Requirement | Feature | User Story |
|---|---|---|---|
| FR-28 | The system shall detect figure regions on each page consisting of (a) embedded raster images and (b) clusters of vector drawing paths whose combined area and path count exceed configurable minimums; regions closer than a configurable gap shall be merged into one figure. | F1 | US-14, US-15 |
| FR-29 | Figure detection shall run after running header/footer and page-number lines are identified and shall never include those lines; the figure region shall be limited to the page body band. | F1 | US-15 |
| FR-30 | Figure detection shall exclude decorative vector elements (rules, underlines, table borders, bullet glyphs, link boxes) and drawings that lie inside a detected table. | F1 | US-15 |
| FR-31 | The system shall render each figure region to a PNG at a configurable DPI (default per Q16), with a configurable border margin, into `<output>/images/` using deterministic, stable file names derived from page and figure order. | F1 | US-14, US-17 |
| FR-32 | The system shall emit, at the figure's reading position in `source_book.md`, a single-line non-translatable image reference that identifies the file, keeping the chunker's "one line, translatable=False, own chunk" contract for images (design §4.1–4.3). | F1 | US-14 |
| FR-33 | Text blocks whose bounding box lies inside a figure region (≥ configurable overlap, default 80 %) shall be excluded from the paragraph flow, heading detection, cross-page merge, hyphen rejoin and glossary discovery. | F1 | US-15 |
| FR-34 | The caption (text block adjacent to the figure starting with a caption pattern `Figure`, `Fig.`, `Table`, `Illustration`, `Plate`, `Listing`; above or below per detection) shall be excluded from the figure region and emitted as a normal translatable paragraph immediately after the image reference. | F1 | US-16 |
| FR-35 | The system shall optionally emit a label legend after the caption: the distinct text labels inside the figure in reading order, translated in one batch with the caption as context, formatted as "English — Türkçe"; numeric and single-character labels excluded; count-limited with a visible truncation note. | F1 | US-16 |
| FR-36 | The Markdown, EPUB and reflowed-PDF exporters shall render the image reference as a real image (relative path in `.md`; packaged resource in `.epub`; embedded in `.pdf`) and shall fall back to the v1 placeholder text with a warning when the file is missing. | F1 | US-14 |
| FR-37 | The system shall record a figure inventory (page, kind, region, file, DPI, size bytes, label count, caption present) and report totals in the run summary and `status --json`. | F1 | US-17 |
| FR-38 | A figure whose PNG exceeds the configured size limit shall be re-rendered at stepwise lower DPI (not below a floor) and reported. | F1 | US-17 |
| FR-39 | `--no-figures` shall restore v1 behaviour exactly (placeholder token, no images directory). | F1 | US-14 |
| FR-40 | `--fresh` archiving shall include the images directory and the figure inventory. | F1 | US-17 |
| FR-41 | The system shall provide an overlay translation mode that extracts per-page text blocks with their page geometry (position, size, font size, style, alignment) and persists them as the translation units of a resumable job, using the v1 state machine (PENDING → PROCESSING → COMPLETED / FAILED, retries, backoff, lease, `--fresh`, `status`). | F2 | US-20 |
| FR-42 | Overlay blocks shall be translated per block; blocks of one page shall be batched into as few provider requests as the provider allows; when the provider supports untranslated context, the preceding block shall be supplied as context (Q15). | F2 | US-18, US-20 |
| FR-43 | The overlay export shall, for every translated block, remove the original text from its rectangle without altering images, vector drawings, annotations or other blocks, and insert the translated text in the same rectangle with matching alignment and style, shrinking uniformly to fit and never overflowing or clipping. | F2 | US-18 |
| FR-44 | The overlay export shall use a font with full Turkish glyph coverage (bundled default or `--pdf-font`), embedded once and subset. | F2 | US-18 |
| FR-45 | The overlay export shall produce a review list (file + summary count) with page, block id, reason, scale factor, source and translated text for every block shrunk below the threshold, not placed, kept original, translated as a fragment, or with missing glyphs. | F2 | US-19 |
| FR-46 | Running header/footer and page-number blocks (v1 heuristic) shall by default be kept untouched and not sent to the provider; an option shall translate headers/footers (page numbers never). | F2 | US-21 |
| FR-47 | The overlay export shall preserve the source outline (bookmarks) and link annotations, replace outline titles that map 1:1 to translated heading blocks, and set document metadata (Language `tr`, Title, Author, Producer). | F2 | US-21 |
| FR-48 | Pages without a text layer shall be copied unchanged and listed in the summary; the overlay export shall never fail because of such pages. | F2 | US-18 |
| FR-49 | `export --format pdf-overlay` shall refuse with a clear message when no overlay pass exists for the output directory; the reflow formats shall refuse likewise when only an overlay pass exists (the two passes are independent — Q14). | F2 | US-20 |
| FR-50 | The reflowed PDF exporter shall have a native engine that needs no libraries outside the core install, rendering the same HTML/CSS as the EPUB exporter, with page size, margins, footer page numbers, outline from headings, embedded Turkish-capable font, and F1 images. | F3 | US-22, US-23 |
| FR-51 | The native engine shall be the default; weasyprint shall remain selectable via an explicit engine option while installed (Q18). | F3 | US-22 |
| FR-52 | Unsupported CSS in the native engine shall be reported once as a warning and ignored, never fail the export. | F3 | US-23 |
| FR-53 | A font supplied via `--pdf-font` shall be validated for Turkish glyph coverage before any rendering (both F2 and F3). | F2, F3 | US-18, US-23 |

## 6. Non-Functional Requirements

Numbering continues from v1 (NFR-01…NFR-15).

| ID | Category | Requirement | Target | Feature |
|---|---|---|---|---|
| NFR-16 | Fidelity | Figure PNG covers the whole region with a small border; nothing of the drawing is cut. | Region = union of member bboxes + 4 pt border; fixture pixel diff against `deney/fig_1_12.png` region ≤ 2 % | F1 |
| NFR-17 | Determinism | Same input + settings → identical figure inventory and files. | Byte-identical PNGs and inventory on two runs (excluding timestamps) | F1 |
| NFR-18 | Precision | Detection quality on the fixture corpus (at least: `test_doc.pdf`, a two-column paper, a book page with 2 figures, a decorative-rules-only page, a table page). | 0 missed figures, 0 false figures on the corpus; each new false case becomes a fixture | F1 |
| NFR-19 | Size | Image bytes stay bounded. | Default per-figure limit 8 MB, DPI floor 96; total image bytes in summary | F1 |
| NFR-20 | Performance | Detection + rendering adds bounded time to extraction. | ≤ 1 s per page average on CPU at 200 DPI for a 300-page book | F1 |
| NFR-21 | Visual fidelity (overlay) | Non-text content unchanged. | Masked-render pixel diff = 0 at 150 DPI; image and drawing counts per page unchanged | F2 |
| NFR-22 | Text fit (overlay) | Most blocks fit without heavy shrinking; every exception is listed. | ≥ 95 % of blocks of the fixture at scale ≥ 0.75; 100 % of blocks below threshold in the review list | F2 |
| NFR-23 | Cost (overlay) | Overlay pass costs about the same characters as reflow and not many more requests. | Chars sent within ±10 % of the reflow pass; requests ≤ 2× reflow chunk count | F2 |
| NFR-24 | File size (overlay, reflow) | Output not bloated by fonts. | Output ≤ 1.5 × input size + 2 MB; one embedded subset font family for replaced text (PoC page: 1.8 MB for 1 page → must not happen) | F2, F3 |
| NFR-25 | Glyph coverage | All Turkish glyphs render in PDF outputs. | 0 missing glyphs on the round-trip fixture; `--pdf-font` validated up front | F2, F3 |
| NFR-26 | Portability | Reflowed PDF works on a clean core install. | CI on Windows + Linux runs `export --format pdf` without weasyprint | F3 |
| NFR-27 | Performance (overlay export) | Page build is fast enough for long books. | ≤ 2 s per page average; 300 pages ≤ 10 min | F2 |
| NFR-28 | Backward compatibility | v1 outputs, CLI defaults, state DB and tests unchanged unless new options are used; a v1 state DB opens under v1.1 with a clean, tested migration. | Existing 322 tests pass unchanged; migration test v1 → v1.1 | all |
| NFR-29 | Lossless contract | Chunker invariant `"".join(chunks) == source_book.md` and the property test remain valid with image references. | Property test extended with the new image line; passes | F1 |
| NFR-30 | Observability | New events logged with the v1 structure (`figure_detected`, `figure_rendered`, `figure_limited`, `overlay_block_placed`, `overlay_block_review`, `pdf_engine_selected`), correlated by run id. | JSONL log contains them; no secrets | all |
| NFR-31 | Privacy | Figure images never leave the machine; only caption and (if enabled) label text go to the provider. | Fake-provider test asserts payload contents | F1 |
| NFR-32 | Resilience (overlay) | Kill at any point leaves state consistent; export is idempotent and atomic (temp file + rename). | SIGKILL test as in v1 NFR-02 | F2 |

## 7. Edge Case Catalogue

Numbering continues from v1 (E-01…E-22).

| # | Edge case | Expected behaviour | Feature |
|---|---|---|---|
| E-23 | Running header/footer or page number lies just above/below a figure that reaches the page band (e.g. page 2 of the test doc: header "1.3 Book overview 19" above the diagram). | Header/footer/page-number lines are identified first and excluded; the figure region is clipped to the body band; header text never appears in the PNG. | F1 |
| E-24 | Page with only decorative vectors (rule under header, footnote separator line, table borders, underlined links, bullets). | No figure. Thresholds on path count and area; drawings inside table bboxes ignored; single thin horizontal lines ignored. | F1 |
| E-25 | Two or more figures on one page (stacked or side by side, e.g. (a)/(b) sub-figures). | Separate regions when the gap exceeds the merge distance; sub-figures with one shared caption ("Figure 3.4 (a) … (b) …") are merged into one figure (caption-driven merge) — ambiguous cases logged. | F1 |
| E-26 | Figure spanning both columns of a two-column page with column text above and below. | Region limited to drawing + inner labels; adjacent column text not swallowed (overlap rule, not proximity). | F1 |
| E-27 | Full-page figure (no prose on the page). | Not treated as "skipped image-only page"; image reference + caption emitted; page counted as figure page in summary. | F1 |
| E-28 | Text-heavy "figure" (a chart with many axis ticks, a diagram that is 80 % text, a boxed sidebar of pure text). | Charts/diagrams: still a figure; ticks excluded from legend. Pure-text box with no drawing besides its border: **not** a figure (border only = decorative) — text stays in the flow. Threshold is an open question (Q16). | F1 |
| E-29 | Mathematical formulas rendered as vector glyphs or Type3 fonts. | Not a figure unless they exceed the area/path thresholds; misdetection logged with page number so the user can pass a per-page exclusion (Q16 option) or `--no-figures`. | F1 |
| E-30 | Raster image with a caption above (tables, some publishers) or a caption on the facing page. | Caption pattern search both below and above; a caption with no figure on the page is left as a normal paragraph; a figure with no caption gets no caption (legend still possible). | F1 |
| E-31 | Vector figure that also contains a raster photo (mixed). | One merged region, kind "mixed". | F1 |
| E-32 | Figure region overlaps a detected table. | Table wins (v1 §6.1 step 13); drawings inside the table bbox are not figure candidates. | F1 |
| E-33 | User hand-edits `source_book.md` (design O1) and deletes or moves an image reference. | Export renders whatever references remain; a reference to a missing file → placeholder + warning; an orphan file → listed in summary, not an error. | F1 |
| E-34 | Very large figure PNG (poster-size vector plot at 300 DPI). | Stepwise DPI reduction to the size limit; floor 96 DPI; reported. | F1 |
| E-35 | Same figure referenced twice (marker extractor emits duplicates; re-extract with a different DPI). | Deterministic file names by page/order; re-extract overwrites; `--fresh` archives. | F1 |
| E-36 | Label legend when labels repeat ("Yes"/"No" on 20 arrows) or are glossary terms. | Distinct labels only; glossary applied; legend limit with truncation note. | F1 |
| E-37 | Marker extractor selected (`--extractor marker`). | Marker's own image output is mapped onto the same image reference contract; vector-figure detection is PyMuPDF-based and applies when marker does not emit the figure — architect decides whether F1 supports marker in v1.1 (Q17). | F1 |
| E-38 | Overlay: translated text 20–30 % longer than the box allows even at the shrink threshold. | Shrink further down to the hard floor (Q13); if still not fitting, place what fits at the floor size with an ellipsis marker, tag `could_not_fit`, exit 2. Never overflow. | F2 |
| E-39 | Overlay: residual original glyphs remain after redaction (PoC blocks 13/14 on page 2; multi-line block where the redaction rectangle missed a line, or rotated/overlapping spans). | Acceptance test extracts text inside every replaced rectangle and asserts no source substring remains; block rectangles are the union of all their line boxes. | F2 |
| E-40 | Overlay: text block drawn on top of a raster image (caption inside a photo, watermark). | Text removal must not blank the image pixels; the block is replaced; if the substitute cannot be laid over the image legibly it is tagged `kept_original`. | F2 |
| E-41 | Overlay: rotated or vertical text (axis labels), text in a Type3/outline font, or text with no extractable bbox. | Kept original, tagged `kept_original` with reason; never sent to the provider. | F2 |
| E-42 | Overlay: block whose text continues on the next page mid-sentence (cross-page sentence, v1 rejoined it). | Translated per block (design constraint); tagged `fragment`; preceding block passed as context when supported (Q15). Quality caveat documented for the user. | F2 |
| E-43 | Overlay: hyphenated line ends inside a block ("trans-" / "lation" on consecutive lines of one block). | Hyphens rejoined before sending (reuse v1 rule); the translated block is laid out fresh, so no hyphen artefacts. | F2 |
| E-44 | Overlay: footnote marker superscripts and footnote blocks at the page bottom in small type. | Superscript markers kept as digits; footnote blocks translated like any block; their smaller font is the base size for shrinking. | F2 |
| E-45 | Overlay: table cells (each cell a small block). | Cell text replaced in place; cells that need shrinking below threshold go to the review list; table borders untouched. | F2 |
| E-46 | Overlay: mixed styles inside one block (bold "**2.** Image formation" as in Figure 1.12 labels, italic citations inside a paragraph). | Dominant style preserved at minimum; span-level bold/italic when the source spans can be mapped to the translated text is a best-effort improvement (Q22). | F2 |
| E-47 | Overlay: page with no text layer (scanned page inside a digital book). | Copied unchanged; listed in summary; not a failure. | F2 |
| E-48 | Overlay: two-column pages. | Each block independent — no reading-order problem; but a block detected as spanning both columns (header) is handled by FR-46. | F2 |
| E-49 | Overlay: source PDF is encrypted / permission-restricted (no modify permission). | Refuse with a clear message before translating (exit 1); do not spend provider characters. | F2 |
| E-50 | Overlay: source PDF has form fields, annotations, or existing redaction annotations. | Annotations preserved; existing redaction annotations are not applied by the tool; noted in summary. | F2 |
| E-51 | Overlay: output directory already has a reflow pass. | Both passes coexist; `status` shows both; `--fresh` archives both unless a mode-scoped fresh is offered (Q14). | F2 |
| E-52 | Overlay: provider returns the same text (untranslated) or an empty string for a block. | v1 empty/truncated rule applies (reschedule, then lenient + review flag); review list reason `provider_empty`. | F2 |
| E-53 | Native reflowed PDF: CSS the engine does not support (e.g. `@page` extras, custom fonts by URL). | Ignored with a single warning listing the properties; output still produced. | F3 |
| E-54 | Native reflowed PDF: an F1 image wider than the text column or taller than the page. | Scaled to fit the column width / page height, aspect kept; never split. | F3 |
| E-55 | Native reflowed PDF: `--pdf-font` lacks Turkish glyphs or is not a TTF/OTF. | Rejected before rendering with a message; bundled default suggested. | F3 |
| E-56 | Native reflowed PDF: extremely long code block or table wider than the page. | Code wraps (`pre-wrap`) as in EPUB; wide tables scaled down to fit width with a warning. | F3 |
| E-57 | Disk full / output not writable during image rendering or PDF build. | v1 E-18 semantics: clear message, state DB untouched, partial files removed (temp + rename). | all |

## 8. Dependencies on v1 Components and Assumptions

**Touched v1 components (impact, not design):**

| v1 component | F1 | F2 | F3 |
|---|---|---|---|
| PyMuPDF extractor §6.1 steps 4–5 (header/footer, page numbers) | Reused to exclude bands (FR-29) | Reused to decide "keep as-is" blocks (FR-46) | – |
| §6.1 step 13 (tables) | Precedence over figures (E-32) | Cells as blocks (E-45) | – |
| §6.1 step 15 (images → token) | Replaced by image reference + files (FR-31/32) | – | – |
| §6.1 steps 8–10 (paragraph merge, hyphen rejoin, cross-page merge) | Must skip figure-region text (FR-33) | Hyphen rule reused per block; cross-page merge **not** applied (E-42) | – |
| Marker normalizer (`![..](..)` → token) | Must map to the new reference (E-37, Q17) | – | – |
| BT-Markdown contract §4.1 and chunker IMAGE block | Image line stays single-line, non-translatable, own chunk (FR-32, NFR-29) | – | – |
| Segmenter / protect (§4.6) | Legend labels as a batch with context (FR-35) | Blocks batched per page (FR-42) | – |
| Assembler placeholders (§8.1–8.3) | Renders images (FR-36) | – | Renders images (FR-50) |
| EPUB exporter | Packages image resources; CSS for figures/legend | – | Shares HTML/CSS |
| PDF exporter (weasyprint) | – | New format `pdf-overlay` | New default engine; weasyprint optional (FR-51) |
| State DB (`chunks` has no page geometry) | Figure inventory needs persisting (file or DB — architect) | **New persisted facts:** per-block page, rectangle, font size/style/alignment, kind (body/header/footer/figure-label/table-cell), placement outcome — hand-off to DB Architect | – |
| Orchestrator stages, `--fresh` archive, `status`, summary | Images dir + inventory (FR-37/40) | Mode-aware stages, counts, refusals (FR-41/49) | Engine reporting |
| CLI §9.2 | `--figure-dpi`, `--no-figures`, `--figure-legend/--no-figure-legend`, limits | `--mode overlay`, `--format pdf-overlay`, `--overlay-min-scale`, `--overlay-translate-headers` | `--pdf-engine`, `--pdf-page-size`, `--pdf-margin` |
| Glossary discovery | Must ignore figure-region text (FR-33) | Same glossary binding (US-20/AC-2) | – |
| Bundled fonts | – | DejaVu/Noto bundling (v1 deferred "DejaVu font paketlenmedi") — now required (FR-44) | Required (FR-50) |

**Assumptions**

- A-8: PyMuPDF (already a core dependency) provides page rendering, drawing/path access, text-block geometry, redaction, HTML box insertion and HTML→PDF layout; no new native dependency is needed for F1/F2/F3. (To be confirmed by the architect; if false, F2/F3 need a dependency decision.)
- A-9: The DeepL provider offers an unbilled "context" input; the local provider does not. Context use is best-effort (Q15).
- A-10: Figure PNGs are acceptable as the image format (lossless, universally supported by EPUB readers); SVG export of vector figures is not required.
- A-11: The user accepts that overlay quality is inherently lower for cross-page sentences and tightly typeset pages, mitigated by the review list.
- A-12: Font redistribution: the bundled font's licence permits redistribution (DejaVu, Noto) — architect confirms.
- A-13: The v1 header/footer heuristic is "good enough" for F1/F2 on books of ≥ 3 pages per chapter; short documents (like the 3-page test) may leak headers — this is a v1 limitation, not solved by v1.1 (a fixture and a warning are the expected treatment).

## 9. Priority Recommendation and Effort / Risk Comparison

| Feature | User value | Effort (rough) | Risk | Depends on | Notes |
|---|---|---|---|---|---|
| **F1** Figure preservation | High — fixes a visible defect in every rich output; needed by technical books | **Medium** (detection heuristics + rendering + three exporters + fixtures) | **Medium** — heuristic false positives/negatives; mitigated by thresholds, `--no-figures`, growing fixture corpus | v1 extractor, chunker contract | No DB schema change required if the inventory is a file (architect's call) |
| **F3** Native reflowed PDF | Medium — makes an existing feature actually usable on Windows; shows F1 images in PDF | **Low–Medium** (one new exporter engine, font bundling, CSS subset) | **Low** — engine is in a core dependency; CSS subset limitations are the main unknown | F1 for images (optional), font bundling | Also removes a permanently skipped test |
| **F2** Overlay PDF | **Highest** — the user's explicit wish, the PoC was approved | **High** (new translation unit + state, batching, fonts, fitting, redaction correctness, review list, navigation/metadata, CLI mode, migration) | **High** — residual glyphs, overflow, fonts, per-block quality, cost of a second pass, new persisted facts | Font bundling (shared with F3), DB Architect for block state | Independent of F1 in code, but F1's figure-region knowledge improves label handling |

**Recommended order: F1 → F3 → F2.**

- F1 first because it corrects wrong output that v1 ships today (figure labels as headings), benefits every format including the future overlay's caption/legend handling, and its scope is well-bounded by the PoC region (`fig_1_12.png`).
- F3 second because it is cheap, shares the font-bundling work with F2, and makes the reflowed PDF (now with F1 images) available on the user's OS — a quick, visible win.
- F2 third because it is the largest change (a second translation unit and pass, persisted geometry, a new export path) and needs the DB Architect; doing it last lets the font, image and review-list conventions settle first.

**Alternative if the user wants the overlay sooner:** F1 → F2 → F3. F3 can then be done as a small follow-up; nothing in F2 depends on F3. Doing F2 before F1 is not recommended: labels inside figures would be handled twice (once as prose leakage in reflow, once as blocks in overlay) and the caption logic would be built without the figure-region concept.

**Cost note for the user:** because reflow chunks and overlay blocks are different units, a book translated in reflow mode must be translated again for the overlay (second pass, second billing, ~same characters). Q14 asks whether that is acceptable for v1.1.

## 10. Open Questions

Numbering continues from v1 (Q1…Q9). Each has a recommended default; the user's "devam" without comment accepts the defaults.

| # | Question | Why it matters | Recommended default |
|---|---|---|---|
| Q10 | Should the translated **label legend** under a figure be on by default? Limit? | Without it a Turkish reader cannot read English labels; with it, out-of-context label translations ("Recognition" → "Takdir") may need review; costs a few characters per figure. | **On** by default, limit 40 labels, labels sent as one batch with the caption as context, glossary applied; `--no-figure-legend` to disable. |
| Q11 | Should F1 attempt to **translate labels inside the PNG** (in-place, F2 technique) in v1.1? | Would make figures fully Turkish but couples F1 to F2's hardest part (fitting, fonts). | **No** in v1.1; record as v1.2 candidate "translated figure images" once F2 is stable. |
| Q12 | Overlay: **running headers/footers** — keep as-is, translate, or drop? Page numbers? | The PoC kept "1.3 Book overview" in English and the user approved; translating them adds per-page requests and header-consistency risk. | **Keep as-is** by default; `--overlay-translate-headers` translates them (page numbers never). |
| Q13 | Overlay: **minimum shrink scale** and behaviour below it? Allow the box to grow into free space? | Turkish is ~20–30 % longer; too much shrinking is unreadable; growing boxes risks collisions. | Review threshold **0.65**, hard floor **0.5**; below the floor: place at floor size with an ellipsis and `could_not_fit`. No box growth in v1.1 (v1.2 candidate: grow downward when the gap to the next block allows). |
| Q14 | Overlay is a **separate translation pass** (separate state, second billing). Acceptable? Same output directory or separate? | Reusing reflow chunk translations for blocks would need sentence alignment across merged/rejoined text — a research item. | **Separate pass**, accepted for v1.1; both passes may live in one output directory (`status` shows both). If the DB's one-job-per-file rule makes this costly, the architect may require a separate `--output` per mode. |
| Q15 | Overlay: pass the **preceding block as untranslated context** when the provider supports it? | Improves fragments and pronouns at no billing cost on DeepL; adds request size; not available on local NMT. | **Yes** when supported; fragments still tagged in the review list. |
| Q16 | F1 **detection thresholds and DPI**: minimum path count, minimum area, merge gap, DPI default, size limit, per-page exclusions? | Determines false positives (rules, formulas) and misses (small diagrams); affects image size. | Path count ≥ 5 **or** raster present; region area ≥ 2 % of page; merge gap 12 pt; drawings inside tables ignored; DPI **200** (range 72–600); per-figure limit 8 MB, floor 96 DPI; `--figure-exclude-pages` for manual overrides. Values are tunable; fixture corpus decides. |
| Q17 | Must F1 support the **marker extractor** in v1.1? | Marker emits its own images; vector-figure detection is PyMuPDF-based. | PyMuPDF extractor only in v1.1; marker keeps v1 token behaviour with a documented note. |
| Q18 | Fate of **weasyprint**: drop, or keep as an optional engine? | Keeping it means two code paths to test; dropping removes a working (on Linux) path. | **Keep** as opt-in `--pdf-engine weasyprint` while the native engine is default; revisit after one release. |
| Q19 | **Image reference form** in `source_book.md`: must it stay hand-editable (readable path) and must it survive `translate` when `source_book.md` is edited (O1)? | Affects whether the reference carries the path or an id resolved via the inventory. | Reference must be a single readable line that a user can delete/move; representation is the architect's choice within the BT-Markdown contract. |
| Q20 | **Fonts**: bundle DejaVu (or Noto) in the package for F2/F3, or require `--pdf-font`? | Bundling adds ~1–2 MB to the wheel but makes PDF work out of the box. | **Bundle** one serif + one sans (licence permitting); `--pdf-font` overrides; coverage validated up front. |
| Q21 | Overlay **exit code** when the review list is non-empty but all blocks were placed? | Users may script on exit codes. | **0** with a summary count; **2** only when a block could not be placed or a page was skipped for lack of a text layer. |
| Q22 | Overlay: **span-level styles** (bold/italic mixed inside one block) — required or best-effort? | Faithful mapping of styles onto translated text is unreliable (word order changes). | Best-effort: dominant style guaranteed; span-level only when the block has ≤ 2 spans with a clear prefix pattern (e.g. bold "2." + regular text). |
| Q23 | Should the overlay also translate **text inside figures** (node labels) in place, as the PoC did? | It is the same mechanism and the user liked the result, but small boxes overflow (blocks 13/14) and need review. | **Yes** by default (blocks inside figure regions are treated like any block, review-list rules apply); `--overlay-keep-figure-text` keeps them English. |

---
✅ Business Analyst tamamlandı.
➡️  Sonraki adım: Solution Architect
