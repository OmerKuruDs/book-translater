# Solution Design v1.1: Figures, Native Reflowed PDF, Overlay PDF (`book-translator`)

**Baseline:** v1 as implemented and reviewed (`docs/02_solution_design.md`, `docs/03_db_design.md`, `docs/04_code_review.md` accepted deviations, `docs/durum.md`).
**Requirements:** `docs/05_change_request_v1_1_ba.md` — US-14…23, FR-28…53, NFR-16…32, E-23…57; the user accepted every recommended default of Q10–Q23 and the order **F1 → F3 → F2**.
**Date:** 2026-09-18
**Status:** Solution Architect output → DB Architect (v1.1)

---

## Türkçe Özet

Bu doküman v1.1 değişiklik talebinin (şekil koruma F1, weasyprint'siz reflow PDF F3, sayfa düzenini koruyan "overlay" PDF F2) teknik tasarımıdır. v1 tasarımını (doc 02) **genişletir**; v1 sözleşmelerinde yapılan her değişiklik §1 ve §2.4'te açıkça listelenmiştir. Kararların özü:

- **F1 (şekiller):** Şekil bölgeleri PyMuPDF `get_drawings()` + raster görsellerden tespit edilir (yol sayısı ≥ 5 veya raster, alan ≥ %2, birleştirme boşluğu 12 pt, dekoratif çizgiler/tablo kenarları/sayfa arka planı hariç). Bölge `<output>/images/p002-f01.png` olarak 200 DPI'da kesilir. `source_book.md`'deki görsel referansı tek satırdır: `<!-- image:3 src="images/p002-f01.png" -->` — v1'in `<!-- image:3 -->` jetonunun **üst kümesi**; chunker onu yine IMAGE bloğu / çevrilmeyen tek chunk olarak görür. Şekil içindeki etiketler akışa girmez; alt yazı normal paragraf olarak çevrilir; etiket lejantı `<!-- legend:3 -->` işaretli bir liste olarak `source_book.md`'ye yazılır, alt yazıyla **aynı chunk'ta, tek istekte, alt yazı context olarak** çevrilir ve assembler `- English — Türkçe` biçiminde eşler. Şekil envanteri DB'de değil, `figures.json` dosyasındadır → **F1 için DB değişikliği yok.**
- **F3 (yerel PDF):** PyMuPDF `Story` + `DocumentWriter` ile HTML→PDF; `--pdf-engine native` varsayılan, `weasyprint` opsiyonel. **Font paketlenmez:** PyMuPDF'in kendi içindeki Charis SIL / Nimbus Sans / Nimbus Mono fontları tüm Türkçe glifleri içeriyor (bu oturumda doğrulandı, §0); `--pdf-font` ile geçersiz kılınabilir ve glif kapsaması önceden denetlenir.
- **F2 (overlay):** Çeviri birimi = satır kutularının birleşimiyle gruplanmış **metin bloğu** (PoC'deki 13/14 kalıntı hatasını düzeltir). Bloklar **yeni bir tabloda** (`chunks` yeniden kullanılmaz, gerekçe §1) v1 durum makinesiyle (PENDING→PROCESSING→COMPLETED/FAILED, lease, backoff, `--fresh`) çevrilir; istekler sayfa bazında toplanır (≤ 50 metin, sayfa metni context). Yerleştirme: `add_redact_annot(fill=False)` + `apply_redactions(images=NONE, graphics=NONE)` + `insert_htmlbox(scale_low=0.5)`; 0.65 altı küçültmeler `overlay_review.json`'a düşer; `subset_fonts()` ile dosya boyutu kaynağın ~1.0×'ı olur (PoC 1.8 MB → 112 KB, doğrulandı). Şema v1→v2 geçişi gerekir; DB Architect'e brief §6'da.

Sonraki adım DB Architect (v1.1): §6'daki "kalıcı olması gerekenler" listesi üzerinden `overlay_blocks` (+ sayfa tablosu), `jobs`/`runs` ek alanları ve 1→2 geçişi tasarlanacak.

---

## 0. Evidence gathered for this design (verified in this session, PyMuPDF 1.28.2 in `.venv`)

| # | Observation | Consequence |
|---|---|---|
| E0 | `pymupdf.Story`, `Page.insert_htmlbox(rect, text, *, css, scale_low, archive, …) -> (spare_height, scale)`, `PDF_REDACT_IMAGE_NONE`, `PDF_REDACT_LINE_ART_NONE`, `Document.subset_fonts`, `Document.set_toc/set_metadata/set_language`, `Page.insert_link`, `Font.has_glyph`, `Archive`, `DocumentWriter` all exist. | A-8 confirmed: no new native dependency for F1/F2/F3. |
| E1 | A `Story` rendered through `DocumentWriter` with plain CSS generic families embeds **Charis SIL** (serif), **Nimbus Sans**, **Nimbus Mono PS** from the pymupdf wheel; text extraction of the result contains every one of `çğıİöşüÇĞİÖŞÜ`. | Q20: no font bundling needed (§1 decision); coverage test in §8. |
| E2 | `add_redact_annot` + `apply_redactions(images=NONE, graphics=NONE)` + `insert_htmlbox` on `test_doc.pdf` page 2: no residual source substring inside the replaced rectangles; 100 × 14 pt bold 13 pt label boxes with 22-char Turkish text fit at scale 0.57–0.60, two only at the 0.5 floor. | Shrink policy thresholds are realistic; the review list will be non-empty on tight diagrams (expected, Q13/Q23). |
| E3 | The PoC file (1 832 404 B) is large because a 59 KB CFF font is embedded **once per `insert_htmlbox` call** (14 copies). `doc.subset_fonts()` + `save(garbage=4, deflate=True)` → **112 120 B** (source: 109 KB). | NFR-24 achievable with two calls; acceptance test in §8. |
| E4 | `page.get_pixmap(dpi=200, clip=…)` yields `1074 × 1323` px for a 386.0 × 476.1 pt clip (expected 1072 × 1322): PyMuPDF rounds the clip to integer device pixels. | AC US-17/1 tolerance is **± 2 px**, not ± 1 (recorded as a deviation from the BA text). |
| E5 | `test_doc.pdf` page 2: 39 drawings = 1 white page-background rectangle (97.7 % of the page, fill-only) + 38 black fill/fill-stroke paths whose union is (106.1, 146.4, 492.6, 590.9) = 36.5 % of the page — exactly the PoC crop. Pages 1 and 3 have one drawing each: a 169 pt-wide, 0-pt-high stroke (footnote rule). The axis titles "2D (what?)" / "3D (where?)" sit 9.7 / 11.6 pt above the union; the caption block starts 24 pt below it. | Detector rules in §4.1: background/rule exclusion, label attachment gap = 1.5 × line height, caption never absorbed. |
| E6 | PyMuPDF merges "13. 3D / reconstruction" and "14. Image-based / rendering" into **one** 4-line text block whose lines have no horizontal overlap; the running header is one block of three lines ("1.3", "Book overview", "19"). | Overlay units are built from **lines**, not PyMuPDF blocks (§4.4); page numbers become separate units automatically. |
| E7 | `DeepLTranslator.capabilities.supports_context` is `False` in v1 although `deepl` 1.32 `translate_text(…, context=…)` exists. | Must be flipped to `True` for FR-35/FR-42 (contract change C5). |

---

## 1. Decision summary (binding for the DB Architect and the developers)

| # | Topic | Decision |
|---|---|---|
| D1 | Feature order | F1 → F3 → F2. F1 and F3 need **no schema change**; F2 needs schema v2 (§6). |
| D2 | Image reference form in BT-Markdown (Q19) | One line, HTML comment, superset of the v1 token: `<!-- image:N src="images/p002-f01.png" -->`. `N` = the v1 sequential image id; `src` = POSIX path relative to the output directory. The v1 token `<!-- image:N -->` is the same grammar with no attributes and means "placeholder without file" (marker extractor, `--no-figures`, or a user who removed the path). Grammar: `^<!--\s*image:(\d+)((?:\s+[a-z]+="[^"\n]*")*)\s*-->[ \t]*$`; only `src` is defined in v1.1, unknown attributes are ignored. Chunker: IMAGE block, `translatable=False`, own chunk of kind `IMAGE` — unchanged contract, so `chunks.kind` needs no change. |
| D3 | Legend representation | Emitted by the extractor into `source_book.md` (keeps the lossless invariant) as a LIST block introduced by a marker line: `<!-- legend:N more="K" -->` followed directly by `- label` lines. The marker is a non-translatable prefix of the LIST block; the block is packed into the **same chunk as the preceding caption paragraph** and isolated from other prose, so caption + labels go to the provider in **one batch request** with the caption as `context`. The assembler pairs source and translated items into `- English — Türkçe`. No new `ChunkKind`. |
| D4 | Figure inventory storage | **File**: `<output>/figures.json` (pydantic schema, `version: 1`). Reasons: derived extraction data like `source_book.md`; needed only for reporting, `status --json`, overlay `--overlay-keep-figure-text`, and stale-file cleanup; no query pattern; keeps F1 free of migrations. Archived by `--fresh` together with `images/`. |
| D5 | Overlay translation unit | The **text block** (union of grouped line boxes, §4.4). One unit = one persisted row = one provider text. Provider requests batch consecutive units (≤ `max_texts_per_request`, ≥ 1 000 chars; may cross pages) — §4.6. |
| D6 | Overlay state: new table vs `chunks` | **New table(s)** (working names `overlay_blocks`, `overlay_pages`; DB Architect finalises) with the *same runtime-state column group and transitions* as `chunks`. Not `chunks` because: (a) `chunks.id == order` is a dense 1..n over the reflow chunk stream and `replace_all`/`iter_ordered`/assembly assume that stream; interleaving a second unit type breaks the lossless invariant checks; (b) a block carries ~12 geometry/style facts that are NULL for chunks and would need CHECKs conditional on a "unit type" column; (c) Q14 requires both passes to coexist in one output directory, so counts, `status`, `--retry-failed`, `reset_failed` must be scoped per pass — a table per pass gives that scope for free; (d) the state machine and repository SQL are reused verbatim through a Protocol (§3.6), so the only duplication is DDL. |
| D7 | Mode | `--mode {reflow,overlay}` on `translate` and `run` (default `reflow`); `export --format pdf-overlay`; `status` shows both passes. `--fresh` archives the whole DB (both passes) and all derived artefacts — documented; no mode-scoped fresh in v1.1 (Q14 accepted). |
| D8 | Overlay output names | `translated_book.overlay.pdf`, `overlay_review.json` (both under `translated_book.*`/explicit archive lists). |
| D9 | Fonts (Q20) | **Do not bundle DejaVu.** Default families are PyMuPDF's built-in fonts (E1): serif → Charis SIL, sans → Nimbus Sans, mono → Nimbus Mono PS (already inside the wheel; SIL OFL / URW licences are pymupdf's, not ours — A-12 becomes moot). `--pdf-font FILE` overrides for both F2 and F3 via `pymupdf.Archive` + `@font-face`; validated with `pymupdf.Font(fontfile).has_glyph()` over the Turkish set before any rendering (FR-53). The v1 §1.1 entries `exporters/fonts/DejaVu*.ttf (+)` are dropped for good. |
| D10 | PDF engine (Q18) | `--pdf-engine {native,weasyprint}` default `native` (`Story` + `DocumentWriter`). weasyprint remains selectable while importable; `exporters` lists `pdf` as available whenever the native engine is (always) and prints engine availability. |
| D11 | File size (NFR-24) | Overlay and native PDF: `doc.subset_fonts()` then `save(garbage=4, deflate=True)` via temp file + `os.replace` (E3). |
| D12 | CLI option names (final) — **[Superseded by Addendum A]** for the three defaults marked ‡: legend list **off** and an `export`/`run` option (not `extract`), `--pdf-page-size` / `--pdf-margin` default **`source`**; the fix round added `export --input` and `--overlay-uniform-scale/--no-overlay-uniform-scale` (§7.1) | F1: `--figure-dpi` (200), `--no-figures`, `--figure-legend/--no-figure-legend` (on ‡), `--figure-legend-limit` (40), `--figure-exclude-pages` ("2,5-7"), `--figure-max-mb` (8). F2: `--mode`, `--format pdf-overlay`, `--overlay-min-scale` (0.65, review threshold), `--overlay-floor-scale` (0.5, hard floor), `--overlay-translate-headers`, `--overlay-keep-figure-text`. F3: `--pdf-engine`, `--pdf-page-size` (A5 ‡), `--pdf-margin` ("18mm 15mm 20mm 15mm" ‡). Detection thresholds beyond these are settings/env only (§7). |
| D13 | Exit codes | Unchanged set 0/1/2/3/4/5/130. Missing overlay pass on `export --format pdf-overlay`, encrypted/no-modify PDF, rejected `--pdf-font` → **1** (USER errors, new `ErrorCode`s §3.7). Overlay export with `could_not_fit` blocks or pages skipped for lack of a text layer → **2**; non-empty review list alone → **0** (Q21). |
| D14 | Context (Q15 refined) | When `supports_context`, the provider request context is the **page text** of the pages the batch covers (all translatable block texts in reading order, capped at 4 000 chars around the batch). This contains the "preceding block" the BA asked for and works with batching (DeepL context is per request, not per text). DeepL `supports_context` becomes `True` (E7). |
| D15 | Figure DPI (Q16) | Default **200** (the BA's Q16 default; US-17 mentions 300 in an example only), range 72–600, size back-off floor 96. |
| D16 | Marker extractor (Q17) | Unchanged v1 behaviour: bare `<!-- image:N -->` tokens; a warning `figures_unsupported:marker` is logged when `--extractor marker` and figures are enabled. |
| D17 | Shared PDF wrapper | `extractors/_pdf.py` moves to a new leaf package `src/pdfkit/api.py` so extractors **and** exporters can use one typed pymupdf wrapper without a service → service import (layering §2.5). |
| D18 | Legacy DB | Schema **v2**. v1 files are migrated on the first write command (doc 03 §7). Read-only opens (`status`) accept v1 **and** v2 (additive migration; overlay tables absent → "no overlay pass") — refinement of doc 03 §7, so `status` keeps working on old directories. |

### 1.1 Explicit changes to v1 contracts (each is referenced later)

| Id | v1 contract | v1.1 change |
|---|---|---|
| C1 | BT-Markdown §4.1 image line `<!-- image:N -->`; chunker `_IMAGE` regex | Grammar extended with attributes (D2). Chunker, segmenter, assembler, exporters, normalizer, discovery use the shared regex from `domain/bt_syntax.py`. |
| C2 | BT-Markdown §4.1 "no raw HTML other than the image token" | Second comment form `<!-- legend:N more="K" -->` allowed, only as the first line of a LIST block (D3). |
| C3 | Chunker packing §4.3 | New rule: a legend LIST block is packed with the immediately preceding PARAGRAPH block into one chunk (kind `TEXT`, `warnings += ["legend:N"]`), flushed before and after (isolation like TABLE). |
| C4 | `Segmented(chunk_text, segments, tail)` | `+ context: Optional[str]` (caption text when the chunk holds a legend). |
| C5 | `DeepLTranslator.capabilities.supports_context = False` | `True`; `translate_text(..., context=request.context)`. LLM base unchanged (already uses it); local NMT stays `False`. |
| C6 | `ExtractOptions` / `BaseExtractor.extract(pdf_path, options, progress)` | `+ figures: FigureOptions`; `extract(..., figure_sink: Optional[FigureSink] = None)`. |
| C7 | `ExtractedDocument` | `+ figures: Tuple[FigureRecord, ...]`, `+ figure_pages_skipped: Tuple[Tuple[int, str], ...]`. `image_count` keeps counting emitted image lines. |
| C8 | v1 E-04 accepted deviation "image-only pages skipped" | With figures enabled, a page whose only content is a raster/vector figure (± caption) is **not** skipped (AC US-15/6). With `--no-figures` the v1 behaviour is byte-identical. |
| C9 | `ExportOptions` (page defaults **[Superseded by Addendum A]**: `page_size = "source"`, plus `page_rect_pt` / `body_font_pt` / `margins_pt` filled from `source_profile.json`, A.3) | `+ pdf_engine: str = "native"`, `+ page_size: str = "A5"`, `+ margins_mm: Tuple[float, float, float, float] = (18, 15, 20, 15)`. |
| C10 | `assemble(chunks, *, allow_partial, metadata, source_markdown)` | `+ pair_legends: bool = True` (identity round-trip test passes `False`, like `render_placeholders`). |
| C11 | `cleanup.Line` | `+ dir: Tuple[float, float] = (1.0, 0.0)` (line direction; rotated text detection for E-41). `_pdf.py` → `pdfkit/api.py` (D17). |
| C12 | Exporter registry | `pdf-overlay` is listed by `exporters` but is **not** a `BaseExporter` (input is a PDF + blocks, not an `AssembledDocument`); the orchestrator routes it separately (§5.5). |
| C13 | `--fresh` archive set (`OUTPUT_ARTIFACT_NAMES/GLOBS`) | `+ figures.json`, `+ overlay_review.json`, `+ images/` (directory move). |
| C14 | doc 03 §7 read-only open rule | Read-only opens accept schema 1 or 2 (D18). |
| C15 | v1 §1.4 import rules | New leaf layer `pdfkit`; edges in §2.5. |
| C16 | `PageInfo` | `+ figures: int` (figures detected on the page). |

---

## 2. Component changes per feature

Legend: `(+)` new, `(~)` changed, `(→)` moved. Layer names as in `tests/test_layering.py`.

### 2.1 F1 — Figure preservation

```
src/
  domain/
    bt_syntax.py                 (+)  IMAGE_LINE, LEGEND_MARKER, ATX, FENCE regexes + parse_image_line()/parse_legend_marker(); stdlib only
    models.py                    (~)  FigureKind, FigureRecord, FigureTotals; ExtractedDocument/PageInfo fields (C7, C16)
    result.py                    (~)  ErrorCode additions (§3.7)
  pdfkit/                        (+)  leaf package: typed pymupdf wrapper shared by extractors and exporters
    __init__.py                  (+)
    api.py                       (→)  from extractors/_pdf.py; + page_drawings(), page_pixmap_png(), page_links(), doc_toc(), ...
  extractors/
    base.py                      (~)  FigureOptions, FigureSink, extract() signature (C6)
    cleanup.py                   (~)  RawDrawing, RawPage.drawings; Line.dir (C11); is_label_like(), caption_pattern()
    figures.py                   (+)  pure detector: RawPage -> List[FigureRegion] (§4.1); no pymupdf import
    figure_render.py             (+)  region -> PNG bytes at DPI with back-off (§4.2), via pdfkit
    figure_inventory.py          (+)  pydantic schema of figures.json (load/save/validate)
    pymupdf_extractor.py         (~)  reads drawings; excludes figure-region lines in _keep_line; emits image line + caption + legend; calls the sink
    normalizer.py                (~)  emits bare tokens through bt_syntax (no behaviour change)
  glossary/
    discovery.py                 (~)  skips legend blocks (FR-33)
  pipeline/
    chunker.py                   (~)  IMAGE regex from bt_syntax (C1); LEGEND_MARKER line class opens a LIST block (C2); legend packing rule (C3)
    segmenter.py                 (~)  legend marker kept in prefix; Segmented.context (C4)
    assembler.py                 (~)  legend pairing + ordinal escaping of items; pair_legends flag (C10)
    orchestrator.py              (~)  figure sink (writes images/ atomically), figures.json, stale image cleanup, --fresh set (C13), summary/status fields, request.context
  exporters/
    base.py                      (~)  markdown_to_html: <figure><img> for image lines with an existing file, placeholder + warning otherwise; legend list wrapped in <div class="figure-legend">; load_stylesheet unchanged
    assets/book.css              (~)  figure, figcaption, .figure-legend rules
    markdown_exporter.py         (~)  ![Şekil N](images/...) when src present and file exists; v1 placeholder otherwise
    epub_exporter.py             (~)  packages referenced PNGs as EpubImage items (images/...)
    pdf_exporter.py              (~)  weasyprint path: <img> resolves via base_url (already) — no change beyond C9
  translators/
    deepl_translator.py          (~)  supports_context=True; passes context (C5)
  cli.py                         (~)  F1 options (§7)
  config.py                      (~)  figure settings (env)
```

Responsibilities: `figures.py` owns *what is a figure* (pure, unit-testable on `RawPage` fixtures, like `cleanup.py`); `figure_render.py` owns *rendering and back-off*; `pymupdf_extractor.py` owns *where the image line, caption and legend go in the flow* and applies FR-33 exclusion in `_keep_line` so heading detection, paragraph merge, hyphen rejoin and cross-page merge skip inner labels automatically; the orchestrator owns *files on disk* (`images/`, `figures.json`) through the sink so the extractor writes nothing itself.

### 2.2 F3 — Native reflowed PDF

```
src/
  exporters/
    fonts.py                     (+)  FontSpec resolution (built-in family names / --pdf-font), Archive builder, validate_turkish_coverage() (FR-53)
    pdf_native.py                (+)  Story-based engine: HTML+CSS -> PDF bytes; page size/margins; footer page numbers; outline from headings; image sizing (E-54); CSS allowlist warning (FR-52)
    pdf_exporter.py              (~)  becomes the "pdf" facade: engine selection (options.pdf_engine), is_available() always True (native), weasyprint engine kept as-is behind "weasyprint"
    base.py                      (~)  ExportOptions (C9); list_exporters() reports engines
  cli.py                         (~)  --pdf-engine, --pdf-page-size, --pdf-margin
```

### 2.3 F2 — Overlay PDF

```
src/
  domain/
    models.py                    (~)  OverlayBlockKind, OverlayAlignment, OverlayStyle, OverlayBlock, OverlayPage, OverlayReviewReason, OverlayReviewEntry, PlacementResult, OverlayTotals; UnitStatus = ChunkStatus (alias)
  extractors/
    overlay_extractor.py         (+)  PDF -> OverlayExtraction (pages + blocks); reuses cleanup.py heuristics (band/header/page-number/heading/footnote/hyphen), find_tables cells, figure regions from figures.json (passed in)
    cleanup.py                   (~)  group_lines_into_blocks(), detect_alignment(), dominant_style() (pure)
  pipeline/
    units.py                     (+)  UnitRepository / UnitAdapter Protocols; ChunkUnitAdapter (v1 behaviour) and OverlayUnitAdapter (batch + page context + per-unit completion)
    overlay_batch.py             (+)  pure batching + context window builder (§4.6); fragment tagging
    orchestrator.py              (~)  --mode; overlay stage sequence; generalised runner over UnitRepository/UnitAdapter; refusals (FR-49, E-49); export routing for pdf-overlay; status/summary
  exporters/
    pdf_overlay_exporter.py      (+)  OverlayRenderer: source PDF + completed blocks -> translated_book.overlay.pdf + overlay_review.json (§3.6.4, §4.5)
    fonts.py                     (~)  shared with F3
  database/
    models.py                    (~)  schema v2 (DB Architect), CURRENT_SCHEMA_VERSION = 2
    session.py                   (~)  MIGRATIONS[1] = _upgrade_v1_to_v2; read-only accepts {1,2} (D18)
    repository.py                (~)  OverlayBlockRepository (same methods as ChunkRepository + claim_pending_batch, page counts), OverlayPageRepository (or folded in), JobPatch overlay fields, RunRecord.mode
  cli.py                         (~)  --mode, --format pdf-overlay, overlay options
  config.py                      (~)  overlay settings
```

### 2.4 Package layout delta (consolidated, v1 §1.1 style)

```
src/
  pdfkit/                        (+)  api.py — the only place with pymupdf `# type: ignore`s (D17)
  domain/bt_syntax.py            (+)
  extractors/figures.py          (+)   figure_render.py (+)   figure_inventory.py (+)   overlay_extractor.py (+)
  extractors/_pdf.py             (→ pdfkit/api.py)
  pipeline/units.py              (+)   overlay_batch.py (+)
  exporters/fonts.py             (+)   pdf_native.py (+)   pdf_overlay_exporter.py (+)
  exporters/fonts/DejaVu*.ttf    (dropped from the plan; never shipped — D9)
tests/
  test_figures.py                (+)   test_overlay_extractor.py (+)   test_overlay_export.py (+)   test_pdf_native.py (+)   test_migration.py (+)
  fixtures/                      (+)   generated fixture PDFs (§8), docs/test_doc.pdf referenced read-only
```

Output directory (v1 §1.4) gains:

```
<output>/
  images/p002-f01.png ...        extract stage (F1)
  figures.json                   extract stage (F1)
  translated_book.pdf            export (F3 native or weasyprint)
  translated_book.overlay.pdf    export --format pdf-overlay (F2)
  overlay_review.json            export --format pdf-overlay (F2)
```

### 2.5 Import rules (§1.4 updated) and the layering test

```
cli       →  config, logging_setup, pipeline, domain, (service registries *.base, read-only)
pipeline  →  extractors, glossary, translators, exporters, database.repository, database.session, domain, config, logging_setup
extractors / glossary / translators / exporters  →  domain, config
extractors, exporters                             →  pdfkit                      (+ new)
pdfkit    →  (stdlib, pymupdf only; nothing internal)                            (+ new leaf layer)
database  →  domain
domain    →  (stdlib, pydantic only)
```

Every new edge the developer must add to `tests/test_layering.py`:

| Edge | Change in the test |
|---|---|
| `pdfkit` layer | `ALLOWED_LAYERS["pdfkit"] = set()`; `layer_of()` already returns `"pdfkit"` from the path. |
| `extractors → pdfkit`, `exporters → pdfkit` | `ALLOWED_LAYERS["extractors"] |= {"pdfkit"}`, same for `exporters`. |
| `pipeline → pdfkit` | **Not** allowed (the orchestrator passes paths, never pymupdf objects). |
| `glossary → domain.bt_syntax`, `exporters → domain.bt_syntax` | Already covered by `→ domain`. |
| Forbidden-edge proofs to add to `test_checker_recognises_the_forbidden_edges` | `exporters.pdf_overlay_exporter → extractors.cleanup` (service → service), `pdfkit.api → domain.models` (leaf stays pure), `pipeline.orchestrator → pdfkit.api`, `database.repository → extractors.figure_inventory`. |
| Module count assertion | `assert len(modules) >= 40` → `>= 52`. |

---

## 3. Key interfaces

Signatures only (Python 3.10 typing; frozen dataclasses; `Result[T]` at every boundary). Names are binding; parameter order may be adjusted by the developer if kept keyword-only.

### 3.1 Shared BT-Markdown syntax (`domain/bt_syntax.py`)

```python
IMAGE_LINE = re.compile(r'^<!--\s*image:(?P<id>\d+)(?P<attrs>(?:\s+[a-z]+="[^"\n]*")*)\s*-->[ \t]*$')
LEGEND_MARKER = re.compile(r'^<!--\s*legend:(?P<id>\d+)(?:\s+more="(?P<more>\d+)")?\s*-->[ \t]*$')

@dataclass(frozen=True)
class ImageLine:
    image_id: int
    src: Optional[str]            # None -> v1 placeholder semantics
    raw: str

def parse_image_line(line: str) -> Optional[ImageLine]: ...
def format_image_line(image_id: int, src: Optional[str]) -> str: ...      # src=None -> "<!-- image:N -->"
def parse_legend_marker(line: str) -> Optional[Tuple[int, int]]: ...     # (image_id, more)
def format_legend_marker(image_id: int, more: int) -> str: ...
```

Chunker precedence (v1 §4.2) becomes: fence-state > heading > hr > **image line** > **legend marker** > table > footnote def > list > blockquote > paragraph. A legend marker opens a LIST block (level 0) when the next line matches `_LIST_ITEM`; otherwise it is a one-line PARAGRAPH block with `translatable=False` (orphan marker; exporters drop it).

### 3.2 Figure detection, rendering, inventory (`extractors/`)

```python
# extractors/base.py
@dataclass(frozen=True)
class FigureOptions:
    enabled: bool = True
    dpi: int = 200                        # 72..600
    dpi_floor: int = 96
    max_bytes: int = 8 * 1024 * 1024
    min_area_ratio: float = 0.02          # region area / page area
    min_paths: int = 5
    merge_gap_pt: float = 12.0
    border_pt: float = 4.0
    inner_text_overlap: float = 0.80      # FR-33
    label_attach_lines: float = 1.5       # x line height, §4.1 step 6
    legend: bool = True
    legend_limit: int = 40
    exclude_pages: FrozenSet[int] = frozenset()
    images_dir_name: str = "images"

@dataclass(frozen=True)
class ExtractOptions:                     # v1 fields unchanged, plus:
    figures: FigureOptions = FigureOptions()

FigureSink = Callable[["FigureRecord", bytes], Result[Path]]   # orchestrator-owned; writes <output>/images/<file> atomically

class BaseExtractor(ABC):
    def extract(self, pdf_path: Path, options: ExtractOptions,
                progress: Optional[ProgressCallback] = None,
                figure_sink: Optional[FigureSink] = None) -> Result[ExtractedDocument]: ...
```

```python
# extractors/cleanup.py (pure)
@dataclass(frozen=True)
class RawDrawing:
    bbox: BBox
    kind: str                    # "f" | "s" | "fs"
    item_count: int              # path segments
    fill: Optional[Tuple[float, float, float]]
    stroke: Optional[Tuple[float, float, float]]
    width: Optional[float]

@dataclass(frozen=True)
class RawPage:                   # v1 fields, plus:
    drawings: Tuple[RawDrawing, ...] = ()
    link_boxes: Tuple[BBox, ...] = ()

@dataclass(frozen=True)
class Line:                      # v1 fields, plus:
    dir: Tuple[float, float] = (1.0, 0.0)
```

```python
# extractors/figures.py (pure; no pymupdf)
class FigureKind(str, Enum): RASTER = "raster"; VECTOR = "vector"; MIXED = "mixed"

@dataclass(frozen=True)
class FigureRegion:
    page: int
    index_on_page: int            # 1-based, reading order
    kind: FigureKind
    bbox: BBox                    # final region incl. border, clipped to body band
    member_drawings: int
    member_images: int
    inner_lines: Tuple[int, ...]  # indexes into the page's kept lines (FR-33 exclusion)
    labels: Tuple[str, ...]       # distinct label texts in reading order (before legend filtering)
    caption_line_indexes: Tuple[int, ...]   # caption block lines (never inside bbox)
    caption_position: Literal["below", "above", "none"]
    warnings: Tuple[str, ...]

@dataclass(frozen=True)
class BandInfo:                   # what steps 4-5 of v1 §6.1 decided for this page
    header_bottom: float          # y1 of the lowest removed header/page-number line above the body, or 0
    footer_top: float             # y0 of the highest removed footer/page-number line below the body, or page height
    removed_line_boxes: Tuple[BBox, ...]

def detect_figures(raw: RawPage, kept_lines: Sequence[Line], band: BandInfo,
                   body_size: float, options: FigureOptions) -> List[FigureRegion]: ...
def is_decorative(drawing: RawDrawing, raw: RawPage, neighbours: Sequence[RawDrawing],
                  options: FigureOptions) -> Optional[str]: ...        # reason or None
def is_label_like(lines: Sequence[Line], body_size: float, body_width: float) -> bool: ...
def legend_labels(region: FigureRegion, limit: int) -> Tuple[Tuple[str, ...], int]: ...   # (labels, more)
```

```python
# extractors/figure_render.py
@dataclass(frozen=True)
class RenderedFigure:
    png: bytes
    dpi: int
    width_px: int
    height_px: int
    dpi_reduced: bool

def render_region(page: Any, bbox: BBox, options: FigureOptions) -> Result[RenderedFigure]: ...
    # get_pixmap(dpi, clip=bbox, alpha=False, annots=False); back-off ladder §4.2
def figure_file_name(page: int, index_on_page: int) -> str: ...        # "p{page:03d}-f{index:02d}.png"
```

```python
# domain/models.py
@dataclass(frozen=True)
class FigureRecord:                # one figures.json entry; also carried in ExtractedDocument.figures
    image_id: int                  # the N of "<!-- image:N ... -->"
    page: int
    index_on_page: int
    kind: FigureKind
    region: BBox
    file: str                      # "images/p002-f01.png"
    dpi: int
    width_px: int
    height_px: int
    bytes: int
    dpi_reduced: bool
    label_count: int
    labels: Tuple[str, ...]        # legend labels actually emitted (after filtering/limit)
    labels_more: int
    caption_present: bool
    warnings: Tuple[str, ...]

@dataclass(frozen=True)
class FigureTotals:
    count: int; raster: int; vector: int; mixed: int
    total_bytes: int; largest_file: Optional[str]; largest_bytes: int
    pages_with_figures: int; pages_skipped: Tuple[Tuple[int, str], ...]; dpi_reduced: int
```

`figures.json` (pydantic, `extractors/figure_inventory.py`): `{ "version": 1, "generated_at", "extractor", "dpi_requested", "options": {...}, "figures": [FigureRecord...], "totals": FigureTotals }`. Loaded by the orchestrator for `status --json`, stale-file cleanup and the overlay extractor (regions for `figure_label` kind).

### 3.3 Image and legend handling across the pipeline

| Module | Change |
|---|---|
| `pipeline/chunker.py` | `_classify`: `bt_syntax.IMAGE_LINE` → `IMAGE`; `LEGEND_MARKER` → `LIST` (level 0) with the marker as the block's first line. `_Packer.run`: lookahead — before appending a PARAGRAPH block whose successor is a legend LIST block, flush; after appending the legend block, flush (`reason="legend"`); chunk `warnings += [f"legend:{image_id}"]`. |
| `pipeline/segmenter.py` | LIST block whose first line is a legend marker: the marker line + `\n` goes into the first item's `prefix`; `Segmented.context` = text of the PARAGRAPH block that precedes the legend block in the same chunk (else `None`). |
| `pipeline/assembler.py` | `assemble(..., pair_legends=True)`: for a group whose source contains a legend block, zip the source items with the translated items; emit `- {esc(src)} — {esc(tr)}` where `esc` escapes a leading `\d+[.)]` as `\.`/`\)`; if item counts differ → `warnings += ["legend_mismatch:N"]`, `review_ids += chunk`, keep translated lines; if `more > 0` append `*… ve K etiket daha*`. The marker line is kept in `translated_book.md`. |
| `glossary/discovery.py` | Lines from a legend marker to the next blank line are excluded from candidate text. |
| `exporters/base.py` | `markdown_to_html(markdown_text, *, base_dir: Optional[Path] = None)`: an image line with `src` whose file exists under `base_dir` → `<figure class="figure"><img src="images/…" alt="Şekil N"/></figure>`; otherwise the v1 placeholder `<p class="figure-placeholder">` and a warning `figure_missing:N` returned through a new `HtmlRender(html, warnings)` result (the two callers adapt). `<!-- legend:N … -->` + following `<ul>` → wrapped in `<div class="figure-legend" markdown="1">` (same technique as `.untranslated`). |
| `exporters/markdown_exporter.py` | `render_image_placeholders`: `src` present and file exists → `![Şekil N](images/p002-f01.png)`; else v1 text. Legend marker lines dropped from `.md`. |
| `exporters/epub_exporter.py` | Every referenced PNG is added as `epub.EpubImage(uid=f"img{N}", file_name="images/…", media_type="image/png")`; chapter XHTML references the same relative path. |
| `exporters/pdf_native.py` | `<img>` resolved through `pymupdf.Archive(target_dir)`; explicit `width`/`height` attributes computed from `figures.json` (pt = px × 72 / dpi) scaled to the text column width / 80 % page height (E-54). |

### 3.4 Legend translation through the existing translator interface

No new provider call shape. For a legend chunk the worker sends `texts = [caption, label_1, …, label_k]` (k ≤ 40 → ≤ 41 texts ≤ DeepL's 50) as **one** `translate()` call with `TranslationRequest(context=segmented.context)` when `capabilities.supports_context`. Protection (`protect.py`) applies as for any segment. Response length mismatch → v1 `PROVIDER_EMPTY_RESPONSE` rule per chunk. Glossary: native binding applies to labels as to any text (US-16/1, E-36).

### 3.5 Native PDF engine (`exporters/pdf_native.py`, `exporters/fonts.py`)

```python
# exporters/base.py
@dataclass(frozen=True)
class ExportOptions:            # v1 fields, plus:
    pdf_engine: Literal["native", "weasyprint"] = "native"
    page_size: str = "A5"       # pymupdf.paper_rect names: A3..A6, Letter, Legal, ...
    margins_mm: Tuple[float, float, float, float] = (18.0, 15.0, 20.0, 15.0)   # top right bottom left

# exporters/fonts.py
@dataclass(frozen=True)
class FontSpec:
    family_css: str                       # 'serif' | 'sans-serif' | 'monospace' | 'BookFont'
    archive_dir: Optional[Path]           # directory added to pymupdf.Archive when a file font is used
    face_css: str                         # "" or '@font-face{font-family:"BookFont";src:url("name.ttf")}'

TURKISH_PROBE = "çğıİöşüÇĞİÖŞÜ"
def resolve_font(font_path: Optional[Path]) -> Result[FontSpec]: ...              # FR-53: FONT_UNSUPPORTED before rendering
def validate_turkish_coverage(font_path: Path) -> Result[None]: ...              # pymupdf.Font(fontfile=...).has_glyph()

# exporters/pdf_native.py
@dataclass(frozen=True)
class NativePdfResult:
    pdf: bytes; pages: int; warnings: Tuple[str, ...]   # incl. one "css_unsupported:prop,prop" (FR-52)

def render_reflowed_pdf(html_body: str, css: str, options: ExportOptions, font: FontSpec,
                        outline: Sequence[Tuple[int, str]], base_dir: Path,
                        metadata: Mapping[str, str]) -> Result[NativePdfResult]: ...
```

Behaviour: `Story(html, user_css=css + page_css, archive=Archive(base_dir [+ font dir]))` → `DocumentWriter` loop over `paper_rect(page_size)` minus margins; heading positions captured with `story.element_positions(recorder, {"page": n})` (elements with `heading` ≥ 1) → `set_toc()` with levels 1/2 (per `chapter_split_level`, AC US-23/1); the writer output is re-opened from memory and a centred footer page number (`insert_textbox`, 9 pt, same family) is added on every page after the first; footnote back-links via `Story.write_stabilized_with_links` are **best effort** (warning `pdf_links_skipped` if unavailable); metadata title/author/language `tr`/producer `book-translator <version>`; `subset_fonts()`; `save(garbage=4, deflate=True)`. Unsupported CSS: the exporter scans the user CSS (`--css`) for property names outside the MuPDF allowlist (`font-*`, `color`, `background-color`, `margin*`, `padding*`, `border*`, `text-align`, `text-indent`, `line-height`, `white-space`, `page-break-*`, `display`, `width`, `height`, `list-style*`, `vertical-align`) and `@page`/`@font-face` at-rules and emits one `css_unsupported:` warning; rendering never fails on CSS.

`PdfExporter.export()` (facade): `options.pdf_engine == "weasyprint"` and importable → v1 path unchanged; `"weasyprint"` and not importable → `Err(EXPORTER_UNAVAILABLE)` (optional exporter semantics: partial, exit 2); `"native"` → `render_reflowed_pdf`. `is_available()` → `True`. `list_exporters()` returns per-exporter `engines` availability for the `exporters` command.

### 3.6 Overlay

#### 3.6.1 Block extraction model (`domain/models.py`, `extractors/overlay_extractor.py`)

```python
class OverlayBlockKind(str, Enum):
    BODY = "body"; HEADING = "heading"; CAPTION = "caption"; HEADER = "header"; FOOTER = "footer"
    PAGE_NUMBER = "page_number"; FIGURE_LABEL = "figure_label"; TABLE_CELL = "table_cell"; FOOTNOTE = "footnote"

class OverlayAlignment(str, Enum): LEFT = "left"; CENTER = "center"; RIGHT = "right"; JUSTIFY = "justify"

@dataclass(frozen=True)
class OverlayStyle:
    font_size: float              # dominant span size (pt)
    bold: bool; italic: bool      # dominant style (Q22)
    family: Literal["serif", "sans", "mono"]
    color: str                    # "#rrggbb" of the dominant span
    prefix_style: Optional[Tuple[str, bool, bool]] = None   # (prefix_text, bold, italic) when <= 2 spans with a clear prefix pattern (E-46)

@dataclass(frozen=True)
class OverlayBlock:
    unit_id: int                  # dense 1..n per job (== order), like chunk_id
    block_id: str                 # "p002-b013" stable, for review lists and logs
    page: int
    index_on_page: int
    kind: OverlayBlockKind
    bbox: BBox                    # union of line boxes (redaction/placement rect)
    line_boxes: Tuple[BBox, ...]
    style: OverlayStyle
    alignment: OverlayAlignment
    source_text: str              # lines joined with hyphen rejoin (E-43)
    content_hash: str
    char_count: int
    translate: bool               # False -> unit is completed with source text, never sent, never redacted
    keep_reason: Optional[str]    # "page_number" | "header_footer" | "figure_text" | "rotated" | "no_letters" | "no_bbox" | "widget_overlap"
    fragment: bool                # E-42
    over_image: bool              # E-40 (informational)
    # runtime state (persisted, DB only): same fields as Chunk
    status: ChunkStatus = ChunkStatus.PENDING
    retry_count: int = 0
    next_attempt_at: Optional[datetime] = None
    last_error: Optional[str] = None
    translated_text: Optional[str] = None
    review_flag: bool = False
    warnings: Tuple[str, ...] = ()

@dataclass(frozen=True)
class OverlayPage:
    page: int; width: float; height: float; rotation: int
    has_text_layer: bool; block_count: int; translatable_count: int
    skip_reason: Optional[str]    # "no_text_layer" | None
    body_size: float

@dataclass(frozen=True)
class OverlayExtraction:
    pages: Tuple[OverlayPage, ...]
    blocks: Tuple[OverlayBlock, ...]
    overlay_sha256: str           # sha256 over (block_id, bbox, source_text, translate) in order -> pass binding
    warnings: Tuple[str, ...]

@dataclass(frozen=True)
class OverlayOptions:
    translate_headers: bool = False      # FR-46
    keep_figure_text: bool = False       # Q23
    band_ratio: float = 0.12; header_footer_page_ratio: float = 0.30   # reuse v1 values
    line_gap_ratio: float = 0.5          # §4.4
    min_h_overlap: float = 0.30

def extract_overlay(pdf_path: Path, options: OverlayOptions,
                    figure_regions: Mapping[int, Sequence[BBox]],
                    progress: Optional[ProgressCallback] = None) -> Result[OverlayExtraction]: ...
def check_modifiable(pdf_path: Path) -> Result[None]: ...        # E-49: encrypted / no PDF_PERM_MODIFY -> OVERLAY_PDF_RESTRICTED
```

#### 3.6.2 Translation units through the v1 state machine (`pipeline/units.py`, `database/repository.py`)

The v1 runner (`_run_job`, `_worker`, `_drain`, heartbeat, limiter, exit-code chain) is kept and parameterised. The repository Protocol is satisfied structurally by both `ChunkRepository` (unchanged public API) and the new `OverlayBlockRepository`:

```python
U = TypeVar("U")                       # Chunk | OverlayBlock

# NOTE: the UnitAdapter signature shown in this section is superseded by A-D6 (Addendum A.4):
# adapters own their repository (`adapter.repo`), `claim(job_id, permits, unit_cap, now, run_id)`,
# `group(units)`, `finish(...) -> List[Result[TranslationResult]]`. Fix round: a batch-level
# provider Err is handled ONCE per batch (one limiter reaction, one backoff, one next_attempt_at).
class UnitRepository(Protocol[U]):     # exactly the v1 ChunkRepository methods (doc 02 §2.5 + A1..A5) with U instead of Chunk:
    def replace_all(self, job_id: str, units: Iterable[U]) -> Result[int]: ...
    def requeue_processing(self, job_id: str) -> Result[int]: ...
    def claim_pending(self, job_id: str, limit: int, now: datetime, run_id: str) -> Result[List[U]]: ...
    def complete(self, job_id: str, unit_id: int, result: TranslationResult) -> Result[bool]: ...
    def reschedule(self, job_id: str, unit_id: int, error: AppError, next_attempt_at: datetime) -> Result[bool]: ...
    def fail(self, job_id: str, unit_id: int, error: AppError) -> Result[bool]: ...
    def release(self, job_id: str, unit_ids: Sequence[int]) -> Result[int]: ...
    def reset_failed(self, job_id: str) -> Result[int]: ...
    def counts(self, job_id: str, now: Optional[datetime]) -> Result[StatusCounts]: ...
    def earliest_next_attempt(self, job_id: str) -> Result[Optional[datetime]]: ...
    def iter_ordered(self, job_id: str, batch_size: int = 200) -> Iterator[U]: ...
    def totals(self, job_id: str) -> Result[ChunkTotals]: ...
    def failed_ids(self, job_id: str) -> Result[List[int]]: ...
    def review_ids(self, job_id: str) -> Result[List[int]]: ...
    def count_completed_with_other_glossary(self, job_id: str, glossary_hash: str) -> Result[int]: ...
    def count_completed_by_run(self, run_id: str) -> Result[Tuple[int, int]]: ...

class OverlayBlockRepository:          # UnitRepository[OverlayBlock] + overlay-specific reads
    def claim_pending_batch(self, job_id: str, max_units: int, min_chars: int,
                            now: datetime, run_id: str) -> Result[List[OverlayBlock]]: ...
        # atomic like claim_pending: eligible units in order until >= min_chars or max_units (may span pages)
    def page_texts(self, job_id: str, pages: Sequence[int]) -> Result[Dict[int, List[str]]]: ...   # context window (source_text of translatable units, in order)
    def page_progress(self, job_id: str) -> Result[Tuple[int, int]]: ...   # (pages fully completed, pages with translatable units)
    def iter_page(self, job_id: str, page: int) -> Iterator[OverlayBlock]: ...

class UnitAdapter(Protocol[U]):
    name: Literal["reflow", "overlay"]
    def claim(self, repo: UnitRepository[U], job_id: str, free: int, now: datetime, run_id: str) -> Result[List[U]]: ...
    def build_request(self, units: Sequence[U], repo: UnitRepository[U]) -> Result["UnitBatch"]: ...
        # reflow: one chunk -> segments (+ legend context); overlay: N units -> N texts + page context
    def finish(self, batch: "UnitBatch", texts: Sequence[str], response: ProviderResponse) -> List[TranslationResult]: ...
        # reflow: reassemble + glossary_check; overlay: 1:1, glossary_check per unit, empty/truncation rule per unit

@dataclass(frozen=True)
class UnitBatch:
    unit_ids: Tuple[int, ...]; texts: Tuple[str, ...]; context: Optional[str]; chars: int; protect: ...
```

Runner contract (unchanged semantics, now per batch): a batch holds one limiter permit and one `translate()` call; `Ok` → `complete()` per unit in order (each its own `BEGIN IMMEDIATE`; a crash between two completes leaves the rest PROCESSING → re-queued at the next run, re-sent — bounded by one batch, documented); `Err` → the v1 scope routing applied to **every** unit of the batch (`reschedule` / `fail` / release + PAUSE). Non-translatable units are completed without a provider call (v1 §5.2 step 1). Progress events count units. `--limit N` counts units.

#### 3.6.3 Job status, binding and refusals

`jobs` gets an independent workflow position for the overlay pass (facts in §6): `NONE → OVERLAY_EXTRACTED → OVERLAY_TRANSLATING → (OVERLAY_PAUSED | OVERLAY_TRANSLATED) → OVERLAY_EXPORTED`. Pre-flight for `translate --mode overlay` = v1 5.1 steps 1–8 with these insertions: after step 4 (input hash) → `check_modifiable(input_pdf)` (E-49, exit 1, before any provider contact); after step 6 (glossary) → block extraction when the overlay table is empty, else `overlay_sha256` re-check (`STATE_HASH_MISMATCH` unless `--fresh`) — the hash includes the two options that change translatability (`translate_headers`, `keep_figure_text`), so switching them on a started pass is a mismatch by design.

#### 3.6.4 Placement engine and review model (`exporters/pdf_overlay_exporter.py`)

```python
class OverlayReviewReason(str, Enum):
    SHRUNK_BELOW_THRESHOLD = "shrunk_below_threshold"; COULD_NOT_FIT = "could_not_fit"; FRAGMENT = "fragment"
    KEPT_ORIGINAL = "kept_original"; GLYPH_MISSING = "glyph_missing"; PROVIDER_EMPTY = "provider_empty"
    RESIDUAL_GLYPHS = "residual_glyphs"; OVER_IMAGE = "over_image"          # v1.1 additions to the BA list
    COLLATERAL_REDACTION = "collateral_redaction"                          # neighbour lost glyphs (rare)

@dataclass(frozen=True)
class OverlayReviewEntry:
    page: int; block_id: str; unit_id: int; reason: OverlayReviewReason
    scale: Optional[float]; source_text: str; translated_text: Optional[str]; note: str = ""

@dataclass(frozen=True)
class PlacementResult:
    unit_id: int; placed: bool; scale: float; spare_height: float; truncated: bool
    residual: bool; reasons: Tuple[OverlayReviewReason, ...]

@dataclass(frozen=True)
class OverlayRenderOptions:
    review_threshold: float = 0.65; floor_scale: float = 0.5
    font: FontSpec; title: Optional[str]; author: Optional[str]; producer: str
    translate_headers: bool; allow_partial: bool

@dataclass(frozen=True)
class OverlayArtifact:
    pdf_path: Path; review_path: Path; bytes_written: int; pages: int
    placed: int; shrunk_below_threshold: int; could_not_fit: int; kept_original: int
    pages_skipped: Tuple[int, ...]; review: Tuple[OverlayReviewEntry, ...]; warnings: Tuple[str, ...]

class OverlayRenderer:
    def render(self, source_pdf: Path, pages: Sequence[OverlayPage],
               blocks_by_page: Callable[[int], Iterator[OverlayBlock]],
               outline_map: Mapping[Tuple[int, str], str],        # (page, source heading text) -> translated
               target_dir: Path, options: OverlayRenderOptions,
               progress: Optional[ProgressCallback] = None) -> Result[OverlayArtifact]: ...
```

Per page (details in §4.5): units with `translate and status == COMPLETED` are placed; `translate == False` units are untouched; with `allow_partial`, FAILED/PENDING units are untouched and listed as `kept_original` with note `partial`. Pages with `skip_reason` are copied unchanged. Links/widgets: `page.get_links()` snapshot before redaction, missing links re-inserted after (`insert_link`) — AC US-21/3; a unit whose rect intersects a widget annotation is `kept_original:widget_overlap`; pre-existing redaction annotations are detached before and re-added after `apply_redactions` (E-50) and counted in the summary.

Header/footer/page numbers (FR-46): kinds `HEADER`/`FOOTER` → `translate = options.translate_headers`; `PAGE_NUMBER` → never; a unit with no letter (`[^\W\d_]`) → never (`no_letters`, covers "1.3", "19", "©").

Outline (FR-47): `doc.get_toc()` entries whose destination page holds a `HEADING` unit within 3 pt of the destination point and whose title equals the unit's `source_text` (whitespace-normalised) are retitled with the unit's `translated_text`; others kept. Metadata: `set_metadata({title, author (preserved unless --author), producer, modDate})`, `set_language("tr")`.

### 3.7 Error codes added (`domain/result.py`)

| Code | Scope | Exit | Used when |
|---|---|---|---|
| `FIGURE_SINK_FAILED` | JOB_FATAL | 1 | `images/` not writable during extraction (E-57 semantics: no state written) |
| `OVERLAY_STATE_MISSING` | USER | 1 | `export --format pdf-overlay` without an overlay pass; reflow formats with only an overlay pass (FR-49) |
| `OVERLAY_PDF_RESTRICTED` | USER | 1 | encrypted / no-modify PDF in overlay mode (E-49) |
| `OVERLAY_HASH_MISMATCH` | — | — | **not added**: reuse `STATE_HASH_MISMATCH` (exit 5) with a message naming the overlay pass |
| `FONT_UNSUPPORTED` | USER | 1 | `--pdf-font` fails coverage/format validation (FR-53, E-55) |
| `OVERLAY_EXPORT_PARTIAL` | — | — | **not added**: `could_not_fit` / skipped pages map to exit 2 through `ExportOutcome.exit_code` like FAILED chunks |

`exit_code_for()` maps the new USER codes to 1 (default branch); no change to `_MISMATCH_CODES`/`_PARTIAL_CODES`.

---

## 4. Algorithms

### 4.1 Figure-region detection (`extractors/figures.py`)

Runs per page **after** v1 steps 4–5 (band lines, page numbers) and step 13 (tables), on `RawPage` + `kept_lines` + `BandInfo`.

1. **Page exclusion.** `page in options.exclude_pages` → no figures, `figure_pages_skipped += (page, "excluded")`.
2. **Decorative filter** (FR-30, E-24; each rule logs a reason at DEBUG):
   - *background*: `area(d) ≥ 0.90 × page area`, or fill-only (`kind == "f"`) with a fill whose channels are all ≥ 0.95 and `item_count ≤ 1`;
   - *rule/underline*: `height < 2 pt or width < 2 pt` **and isolated** (no non-thin drawing within `merge_gap_pt`); thin drawings next to a cluster are arrows/axes and are kept (E5: the dashed axis, the arrows);
   - *table border*: bbox inside any `RawTable.bbox` expanded by 2 pt (E-32);
   - *link box*: bbox within 1 pt of a `link_boxes` entry;
   - *bullet/glyph*: `width ≤ 0.6 × body_size and height ≤ 0.6 × body_size` and isolated;
   - *band decoration*: entirely inside the header/footer band **and** thin.
3. **Candidates** = surviving drawings + `RawImage`s with area ≥ 1 % of the page (v1 `_MIN_IMAGE_AREA_RATIO`).
4. **Clustering**: union–find; two candidates join when `rect_gap(a, b) ≤ merge_gap_pt` (12 pt; gap = max(dx, dy) of the separating distance, 0 when overlapping).
5. **Cluster acceptance** (Q16): `(drawings ≥ min_paths (5) or images ≥ 1) and union_area ≥ min_area_ratio (2 %) × page area`. A pure-text box (border only = 1 drawing) fails → text stays in the flow (E-28); formulas (few paths, small area) fail (E-29) — a failed cluster with ≥ 2 drawings is logged `figure_candidate_rejected:page:reason` so the user can react with `--figure-exclude-pages` or thresholds.
6. **Inner text** (FR-33): every kept line with `overlap(line, union) / area(line) ≥ inner_text_overlap (0.80)` is a member (`inner_lines`); its text is a label.
7. **Label attachment** (E5: axis titles just outside): iterate ≤ 3 rounds — a *label-like* text block (≤ 2 lines, ≤ 80 chars, block width ≤ 0.5 × body text width, not caption-pattern, not a band candidate) whose distance to the union ≤ `label_attach_lines × line height` is absorbed and the union grows. Prose blocks (multi-line or wide) are never absorbed even when adjacent — the two-column case E-26 is handled by *overlap*, not proximity.
8. **Body-band clip** (FR-29, E-23): the union is intersected with `[band.header_bottom + 2, band.footer_top − 2]` and additionally cut so it never intersects any `removed_line_boxes` or any band line that `is_short_band_candidate` (A-13 short-document leak). Header text therefore never enters the PNG even when the v1 repetition rule missed it.
9. **Caption association** (FR-34, E-30): the nearest text block **below** the union (gap ≤ 3 × line height) whose first line matches `caption_pattern` (`^(Figure|Fig\.|Table|Illustration|Plate|Listing)\s*[\dA-Z]`); if none, the nearest **above**. The caption block is never a member; if the union reaches into it, the union is cut 2 pt before it. `caption_position` recorded.
10. **Table precedence** (E-32): a cluster whose union intersects a `RawTable.bbox` by ≥ 50 % of the table area is dropped (`figure_dropped:table`).
11. **Two figures / sub-figures** (E-25): clusters are separate figures in reading order `(round(y0), x0)`. Caption-driven merge: two vertically adjacent clusters (gap ≤ 3 × line height, no prose block between) with a single caption below whose text contains both "(a)" and "(b)" → merged (`warnings += "merged_by_caption"`); other ambiguous adjacency is logged.
12. **Full-page figure** (E-27, C8): `_process_page` returns `skipped=False` when the page has a figure even with no prose; `PageInfo.figures` counts it.
13. **Final region** = union + `border_pt` (4 pt), clipped to the page rect and the band limits; `kind` = raster / vector / mixed.

Element emission in reading order: `_Element("image", y0=region.y0, text=str(image_id), src=file)`, then the caption paragraph (from the caption block, normal classification suppressed: never a heading), then the legend element (§4.3) — all three sorted at the region's `y0` with a stable sub-order so they stay together.

### 4.2 Deterministic file naming and DPI back-off (`extractors/figure_render.py`)

- File: `images/p{page:03d}-f{index_on_page:02d}.png` (page-based, so a re-extract with a different DPI overwrites the same names — E-35; the image id `N` is global and stored in the line and `figures.json`).
- Render: `page.get_pixmap(dpi=d, clip=region, alpha=False, annots=False)` → PNG. Expected pixel size `round(w × d / 72) ± 2` (E4).
- Back-off ladder when `len(png) > max_bytes`: `d ← max(dpi_floor, round(d × 0.75))` until it fits or `d == dpi_floor` (96); the final attempt at the floor is kept even if still above the limit (`warnings += "size_limit_exceeded"`); `dpi_reduced=True` is reported (FR-38, E-34).
- The sink writes atomically (`atomic_write_bytes`) and returns the relative path put into the image line; a sink `Err` aborts extraction with `FIGURE_SINK_FAILED` **before** the DB is created (v1 "no state on failure" rule).
- Determinism (NFR-17): same pymupdf version + same options → identical bytes; the inventory omits timestamps from the comparison (`generated_at` excluded by the test).

### 4.3 Legend building (`figures.legend_labels`, extractor serialization)

1. Labels = inner + attached lines' texts, whitespace-collapsed, in reading order `(round(y0 / line_height), x0)`; a multi-line inner block is one label (hyphen-rejoined).
2. Drop labels that are numeric/punctuation only or a single character (`^\W*\d[\d\W]*$` or `len ≤ 1`) — axis ticks (AC US-16/4).
3. De-duplicate (exact, case-sensitive) keeping the first occurrence (E-36).
4. Truncate to `legend_limit`; `more = dropped count`; a warning `legend_truncated:page:N` is logged.
5. Serialize (when `legend` is on and ≥ 1 label): `<!-- legend:N more="K" -->` + `- label` lines (labels verbatim; the ordinal escape is applied by the assembler on output, not in `source_book.md`, so the provider receives the label unchanged).
6. Zero labels → no marker line at all.

### 4.4 Overlay block grouping (fixes the PoC residual-glyph defect, E6/E-39)

Input: lines of `page.get_text("dict")` (all blocks), each with bbox, spans, `dir`.

1. Discard empty lines; lines with `dir ≠ (1, 0)` → their own unit, `translate=False`, `keep_reason="rotated"` (E-41); lines with a zero-area bbox or Type3 font (`font` name starts with `T3` / flags) → `no_bbox`.
2. Sort remaining lines by `(y0, x0)`.
3. Greedy grouping: line `L` joins the open group `G` (last line `P`) when **all** hold: vertical gap `L.y0 − P.y1 ≤ line_gap_ratio (0.5) × min(height(L), height(P))` (overlapping lines count as gap 0); horizontal overlap `overlap_x(L, G.bbox) / min(width(L), width(G.bbox)) ≥ min_h_overlap (0.30)` **or** `|L.x0 − G.x0| ≤ 1 em`; `|size(L) − size(G)| ≤ 1 pt`; same `dir`. Otherwise a new group starts. Result on E6: "13. 3D / reconstruction" and "14. Image-based / rendering" become two units (no horizontal overlap between line 2 and line 3); "1.3", "Book overview", "19" become three units (same y, no vertical adjacency ⇒ each starts a group; they never merge because grouping is by vertical succession).
4. Unit rect = union of the group's line boxes (no expansion). Where two unit rects overlap (consecutive lines of different units overlapping by ~0.4 pt, E6), the overlap band is split at its middle so rects are disjoint.
5. Kind assignment: `in_band` + `is_running_line`/`is_short_band_candidate` → HEADER/FOOTER; `is_page_number` → PAGE_NUMBER; inside a figure region (from `figures.json`, ≥ 80 % overlap) → FIGURE_LABEL; inside a `find_tables` cell → TABLE_CELL (one unit per cell, cell rect as bbox — E-45); `size ≤ 0.85 × body_size` and bottom third → FOOTNOTE; `is_heading_candidate` → HEADING; caption pattern → CAPTION; else BODY.
6. Style: dominant span by character count (`bold`/`italic` from flags/font name, `family` from font name: `Sans|Arial|Helvetica|Nimbus Sans|Verdana` → sans, `Mono|Courier|Consolas` → mono, else serif; `color` from the span). Prefix pattern (E-46, Q22): exactly two style runs where the first is ≤ 6 chars ending in `.`/`)`/`:` → `prefix_style` kept and re-applied to the same prefix in the translation when the translated text starts with it (else dominant style only).
7. Alignment (`detect_alignment`): **≥ 3 lines** (two full lines + the possibly short last one; `_JUSTIFY_MIN_LINES = 3`, A-D3 — the original "≥ 2 lines" is superseded): left edges within 1 pt **and** right edges within 1 pt (all but the last line) → JUSTIFY; left only → LEFT; right only → RIGHT; per-line `|left margin − right margin| ≤ 2 pt` relative to the group's max extent → CENTER. Single line: CENTER when centred (± 2 pt) inside the smallest drawing rect containing it (figure boxes), else LEFT.
8. Text: lines joined by v1 `join_lines(lines, compounds)` (hyphen rejoin per block, E-43; `compounds` collected once over all lines of the document); superscript spans kept inline as digits (E-44); `fragment = True` when the unit is the last BODY unit of the page and its text does not end with terminal punctuation (v1 `ends_with_terminal`), or the next BODY unit on the page starts lowercase (E-42).
9. `translate` decision (FR-46, Q23): PAGE_NUMBER → False; HEADER/FOOTER → `translate_headers`; FIGURE_LABEL → `not keep_figure_text`; rotated/no_bbox/no letters → False; else True. `over_image = True` when the rect intersects a raster image block (E-40).
10. Ordering: units numbered in `(page, y0, x0)` order → `unit_id` dense, `block_id = f"p{page:03d}-b{index:03d}"`.

### 4.5 Fit / shrink policy and page build (`exporters/pdf_overlay_exporter.py`)

Per page, in this order (all units of the page first, then insertion — so redaction is applied once):

1. **Redaction rects**: for every unit to place, `page.add_redact_annot(rect, fill=False)` (no fill painted → image pixels under text stay visible, E-40). Rect = unit bbox after the disjoint-split of §4.4 step 4.
2. `page.apply_redactions(images=PDF_REDACT_IMAGE_NONE, graphics=PDF_REDACT_LINE_ART_NONE, text=PDF_REDACT_TEXT_REMOVE)` once.
3. **Residual check** (E-39, AC US-18/3): `page.get_text("text", clip=rect).strip() == ""` for each rect; otherwise `residual` (review `residual_glyphs`, event `overlay_residual`). **Collateral check**: for every *untouched* unit of the page, `get_text(clip=its rect)` still equals its `source_text` (whitespace-normalised); otherwise review `collateral_redaction` on both units.
4. **Insertion**: `html = f'<div style="margin:0;padding:0;line-height:1.15;font-family:{family_css};font-size:{size}pt;font-weight:{bold};font-style:{italic};text-align:{align};color:{color}">{escaped text}</div>'` (prefix span when `prefix_style` applies); `spare, scale = page.insert_htmlbox(rect, html, css=font.face_css, scale_low=floor_scale, archive=font.archive)`.
   - `scale ≥ review_threshold` → placed; `floor ≤ scale < review_threshold` → placed + `shrunk_below_threshold`; `scale == -1` (does not fit even at the floor) → **ellipsis fallback**: binary search over the word count at `scale_low = floor_scale` (text + " …") until it fits; placed truncated + `could_not_fit` (E-38); exit code 2 at the end.
   - Never enlarged: `insert_htmlbox` scales down only (AC US-18/4).
5. **Glyph check**: the extracted text of the rect after insertion must contain every non-ASCII letter of the translated text; otherwise `glyph_missing` (only reachable with a user font that passed a partial probe).
6. **Links/widgets**: re-insert links that disappeared; widget-overlapping units were already `kept_original`.
7. After all pages: outline retitling (§3.6.4), metadata, `set_language("tr")`, `subset_fonts()`, `save(tmp, garbage=4, deflate=True)`, `os.replace`, smoke re-open (`page_count == source`, opens without exception), `overlay_review.json` written atomically **after** the PDF (both or the review is regenerated on the next export).

Review list content = every unit with any reason in {`shrunk_below_threshold`, `could_not_fit`, `fragment`, `kept_original` (with `keep_reason` as note, except `page_number`/`header_footer`/`no_letters` which are expected and listed only in counts), `glyph_missing`, `provider_empty` (unit `review_flag` from the translate pass), `residual_glyphs`, `over_image`, `collateral_redaction`} — sorted by `(page, index_on_page)`.

### 4.6 Per-page batching of blocks into provider requests (`pipeline/overlay_batch.py`)

- Claim: `claim_pending_batch(max_units=min(50, capabilities.max_texts_per_request), min_chars=1000)` — eligible units in `unit_id` order, stop when both `chars ≥ min_chars` and a page boundary is reached, or `max_units`. Effect: text-dense pages → 1 request per page; sparse pages (captions-only, picture books) → several pages per request. Requests ≤ pages + ceil(units/50) ≈ within 2 × reflow chunk count for normal books (NFR-23; a 300-page book with 2 500 chars/page: ~300 overlay requests vs ~600 reflow chunks).
- Context (D14): `page_texts(pages of the batch)` joined by `"\n"` in reading order, windowed to ≤ 4 000 chars centred on the batch's units; passed as `TranslationRequest.context` when `supports_context`; never billed (DeepL), never persisted.
- Chars accounting: `chars_sent` = payload only (context excluded), so NFR-23's "±10 %" comparison with the reflow pass holds.
- Failure: one `Err` → same scope routing for every unit of the batch; `PROVIDER_EMPTY_RESPONSE` on length mismatch; per-unit empty/truncated check uses the v1 counter rule (`_EMPTY_LENIENT_AT`/`_EMPTY_FAIL_AT`) with `review_flag` + review reason `provider_empty` (E-52).

### 4.7 Hyphen rejoin per block and fragments

Reuse `cleanup.collect_compounds` over all lines of the document once (mid-line hyphenated words), then `join_lines(lines_of_unit, compounds)` per unit; ambiguous joins are logged with `block_id` (not the words — CR-33). Cross-page merge is **not** applied (E-42); `fragment` flag per §4.4 step 8.

### 4.8 Outline title mapping

`(page, normalised source title) → translated_text` built from HEADING units; a ToC entry is retitled only when the destination page matches and the normalised titles are equal; entries pointing at pages without a matching HEADING unit stay English (AC US-21/2).

---

## 5. Data-flow and stage changes (orchestrator)

### 5.1 `extract` (reflow; F1)

```
validate pdf → bind/lease (as v1) → run extractor(options.figures, figure_sink)
   figure_sink: <output>/images/<file>  (mkdir, atomic write; Err -> FIGURE_SINK_FAILED, no DB created when none existed)
→ language check → preserve previous source (CR-06; now also moves the old figures.json + images/ into the same archive stamp when the new inventory differs)
→ write source_book.md, figures.json (atomic) → delete stale images/*.png not in the inventory (E-35)
→ job patch (source_md_sha256, …) → ExtractOutcome(+figures totals) → summary
```

`--no-figures` → `FigureOptions(enabled=False)`: no drawings read, no `images/`, no `figures.json`, bare tokens — byte-identical to v1 (AC US-14/4). `--extractor marker` → figures disabled with warning (D16).

### 5.2 `glossary`

Discovery excludes legend blocks (FR-33). Nothing else changes.

### 5.3 `translate --mode reflow` (default)

Chunker/segmenter changes (C1–C4) are transparent to the runner: legend chunks are ordinary `TEXT` chunks with `warnings=["legend:N"]`. Worker: `request = TranslationRequest(..., context=segmented.context if translator.capabilities.supports_context else None)`. The lossless verification (v1 §4.5) is unchanged and still passes because the legend text is part of `source_book.md`.

### 5.4 `export` (reflow formats; F1 + F3)

`_export_bound`: `markdown_to_html(..., base_dir=output_dir)`; exporters receive `target_dir` (already) and resolve `src` against it; missing files → placeholder + `figure_missing:N` warning (E-33, FR-36); orphan PNGs in `images/` not referenced by the document → `figure_orphan:file` warning (E-33). `pdf` facade chooses the engine (§3.5). Font validation (`resolve_font`) runs **before** assembly when `--pdf-font` is given and any PDF format is requested (FR-53).

### 5.5 Overlay mode (F2): stage sequence and refusal rules

```
run --mode overlay:      extract → glossary → translate(overlay) → export --format pdf-overlay
translate --mode overlay: preflight (v1 5.1 + check_modifiable after step 4)
                          → overlay units: if overlay table empty (or --fresh): extract_overlay(input, options, figure regions from figures.json) → replace_all
                                           else: overlay_sha256 == job.overlay_sha256 or STATE_HASH_MISMATCH (exit 5)
                          → requeue_processing → OVERLAY_TRANSLATING → runner(OverlayUnitAdapter, OverlayBlockRepository)
                          → OVERLAY_TRANSLATED | OVERLAY_PAUSED → summary(mode="overlay")
export --format pdf-overlay [--allow-partial]:
                          preconditions for ALL requested formats first (nothing written on any refusal):
                            pdf-overlay needs overlay units (else OVERLAY_STATE_MISSING, exit 1: "run `translate --mode overlay --input …` first")
                            md/epub/pdf need chunks (else OVERLAY_STATE_MISSING variant message, exit 1)   (FR-49)
                          overlay units all COMPLETED or --allow-partial (else EXPORT_REFUSED_PARTIAL, exit 2)
                          → OverlayRenderer.render → translated_book.overlay.pdf + overlay_review.json → OVERLAY_EXPORTED (when exit 0)
```

Exit code of the overlay export: 2 when `could_not_fit > 0` or `pages_skipped` non-empty or partial; else 0 (Q21). `run` combines codes with the v1 severity rule. `run --mode overlay --format md` → refused up front (`OVERLAY_STATE_MISSING`, "reflow formats need `--mode reflow`") unless a reflow pass already exists in the directory, in which case both are produced (E-51).

### 5.6 `status` and summary additions

`StatusReport` (+): `figures: Optional[FigureTotals]` (from `figures.json` when present), `overlay: Optional[OverlayStatus]` = `{status, counts (per unit status, waiting_backoff), pages_completed, pages_total, pages_skipped, translatable_units, kept_units, overlay_sha256, review_counts (from overlay_review.json when present), output_path}`; `mode` of the last run. `JobSummary` (+): `mode`, `figures` (extract/run), `overlay` block (translate/export in overlay mode: units per status, placed/shrunk/could_not_fit/kept counts, review path). Console: `figures: 3 (vector 2, raster 1, 412 KB, largest p002-f01.png)`; `overlay: 412/412 units, 37/37 pages, review 9 (shrunk 7, fragment 2)`.

Logging (NFR-30): events `figure_detected`, `figure_rendered`, `figure_limited`, `figure_candidate_rejected`, `legend_truncated`, `overlay_extracted`, `overlay_batch`, `overlay_block_placed`, `overlay_block_review`, `overlay_residual`, `overlay_page_skipped`, `pdf_engine_selected`, `font_validated`, `css_unsupported` — all with `run_id`/`job_id`; texts never logged above DEBUG (labels/blocks are book content).

### 5.7 Migration of a v1 state DB

`open_database` (not read-only) on `schema_version == 1` → backup to `state-archive/<ts>-pre-migrate-v1/` → `MIGRATIONS[1]` (additive: new tables, `ALTER TABLE jobs/runs ADD COLUMN …` with defaults) → `schema_version = 2` in the same transaction (doc 03 §7 rules). Read-only opens accept 1 or 2 (D18); `OverlayBlockRepository` on a v1 file reports "no overlay pass" without touching missing tables. Existing chunks/runs/lease rows are never modified. A v1 `translation_state.db` from `docs/test_doc_output/` is the migration fixture (§8).

### 5.8 `--fresh`

Archive set (C13): DB (both passes), `source_book.md`, `summary.json`, `figures.json`, `translated_book.*` (incl. `.overlay.pdf`), `overlay_review.json`, and the `images/` directory (moved as a whole); `glossary.json` copied and kept (CR-23). `translate --fresh` keeps artefacts (v1 rule) but archives the DB — i.e. both passes' state.

---

## 6. What must be persisted (brief for the DB Architect — no columns)

| Aggregate | Facts to persist | Notes |
|---|---|---|
| **Overlay unit** (new; ≤ ~30 000 per job for a 1 000-page book — ~30 units/page) | identity: dense `unit_id` (== order) per job, stable `block_id` string, page, index on page; content: source text, content hash, char count; geometry: bbox, list of line boxes; style: font size, bold, italic, family class, colour, prefix style (optional); alignment; kind (enum §3.6.1); `translate` flag + keep reason; `fragment`; `over_image`; **runtime state group identical to `chunks`** (status, retry_count, next_attempt_at, last_error_code, last_error, translated_text ⇔ COMPLETED, review_flag, warnings, provider, glossary_strategy, glossary_hash, chars_sent, chars_billed, latency_ms, attempts, last_run_id, claimed_at, completed_at, created/updated) | Same transitions and preconditions as doc 03 §5.2; `claim_pending_batch` needs "first eligible unit's neighbours in order" — an ordered range scan on (job, status, id). Geometry/style are write-once. Line boxes may be a JSON list (never queried). |
| **Overlay page** (new; ≤ 1 000 per job) | page number, width, height, rotation, has_text_layer, skip reason, block count, translatable count, body font size | Needed by export (copy-unchanged decision without reopening state), `status` (pages completed/total = units grouped by page), summary. May be folded into the unit table only if the DB Architect prefers derivation — but the "no text layer" pages have **no** units, so a page-level record is needed somewhere. |
| **Job** (existing) | overlay pass workflow position (independent of `status`), `overlay_sha256` (binding of the unit set, includes the two translatability options), overlay options used (`translate_headers`, `keep_figure_text`), overlay tool version, overlay unit count | Provider/glossary binding stays **job-level** (one provider per job; `--allow-provider-switch` semantics unchanged). |
| **Run** (existing) | `mode` (`reflow` \| `overlay`) | `command` values unchanged. Counters already generic (units instead of chunks). |
| **Figure inventory** | **Not in the DB** (D4): `figures.json`. | If the DB Architect sees a reason to persist figure counts on the job (for `status` without the file), a single JSON/blob or two integers suffice; not required. |
| **Placement outcome / review entries** | **Not persisted**: derived deterministically at export from (translated text, geometry, style, font, thresholds) and written to `overlay_review.json`; `status` reads the file. | Rationale: export is idempotent and re-runnable with different thresholds; persisting would make export a state writer (v1 export only patches job status). The DB Architect may reject this if `status` must show review counts without the file — then persist per-unit `last_scale` + `last_reason` written by export. |
| **Schema meta** | version 2; migration 1→2 additive (new tables + ADD COLUMN) | Read-only open must accept v1 (D18). |
| **Lease** | unchanged (one process per output directory covers both passes) | |

Access patterns to optimise: claim batch (ordered range on status/eligibility), counts per status per pass, pages completed/total (group by page over COMPLETED vs translatable), ordered streaming per page for export, totals per pass, failed/review id lists per pass, `count_completed_with_other_glossary` per pass. Expected volume: unit rows ~0.5–2 KB (short texts) × 30 000 max; indexes minimal, mirroring doc 03 §4.

Never persisted: provider context strings, PNG bytes, font files, API keys.

---

## 7. CLI contract delta (doc 02 §9 style) and exit codes

### 7.1 New / changed options

> **[Superseded by Addendum A]** for the rows marked ‡ (legend default and scope, PDF page size, PDF margins). The rows below the first rule are the options added by the v1.1 fix round (code review CR-39, CR-78 and the shared per-page scale).

| Command | Option | Default | Feature | Notes |
|---|---|---|---|---|
| `run`, `extract` | `--figure-dpi INT` | 200 | F1 | 72–600; env `BOOK_TRANSLATOR_FIGURE_DPI` |
| `run`, `extract` | `--no-figures` | off | F1 | v1 behaviour (bare tokens, no `images/`) |
| `run`, `export` ‡ (was `run`, `extract`) | `--figure-legend / --no-figure-legend` | **off** ‡ (was on) | F1 | Q10. Addendum A.2: the labels are translated inside the figure PNG; the list is an export decision. `extract` no longer takes the flag (it had no effect, CR-78). A figure whose labels could not be painted keeps its legend list regardless (CR-57). |
| `run`, `extract` | `--figure-legend-limit INT` | 40 | F1 | Q10 |
| `run`, `extract` | `--figure-exclude-pages TEXT` | none | F1 | `"2,5-7"`; Q16 manual override (E-29) |
| `run`, `extract` | `--figure-max-mb FLOAT` | 8 | F1 | per-figure size limit, floor 96 DPI |
| settings/env only | `BOOK_TRANSLATOR_FIGURE_MIN_AREA_PCT` (2.0), `_FIGURE_MIN_PATHS` (5), `_FIGURE_MERGE_GAP_PT` (12), `_FIGURE_BORDER_PT` (4) | | F1 | tunables, not CLI |
| `run`, `translate` | `--mode {reflow,overlay}` | reflow | F2 | D7 |
| `run`, `translate` | `--overlay-translate-headers` | off | F2 | FR-46 (page numbers never) |
| `run`, `translate` | `--overlay-keep-figure-text` | off | F2 | Q23 |
| `run`, `export` | `--format {md,epub,pdf,pdf-overlay}` | `md,epub` (reflow) / `pdf-overlay` (overlay) | F2 | repeatable; group preconditions checked first (FR-49) |
| `run`, `export` | `--overlay-min-scale FLOAT` | 0.65 | F2 | review threshold (Q13) |
| `run`, `export` | `--overlay-floor-scale FLOAT` | 0.5 | F2 | hard floor; must be ≤ min-scale |
| `run`, `export` | `--pdf-engine {native,weasyprint}` | native | F3 | Q18 |
| `run`, `export` | `--pdf-page-size TEXT` | **`source`** ‡ (was A5) | F3 | `source` = the page rectangle of the input PDF (`source_profile.json`), or a pymupdf paper name |
| `run`, `export` | `--pdf-margin TEXT` | **`source`** ‡ (was `18mm 15mm 20mm 15mm`) | F3 | `source` = measured from the input PDF, or 1, 2 or 4 CSS-style values, `mm` or `pt` |
| `export` | `--input FILE` | the path recorded in the state | fix round (CR-39, CR-92) | source PDF after a move/rename; its SHA-256 must equal `jobs.input_sha256`, checked before anything is written. A mismatch stops the run (exit 5, nothing written) **only when the overlay format is requested**; for the reflow formats the source is simply not read — md/epub are written, the figure step is skipped with a warning, exit 2 (§7.3). A missing file is exit 1 |
| `run`, `export` | `--overlay-uniform-scale / --no-overlay-uniform-scale` | on | fix round | body paragraphs of a page share one font scale (code review "Shared per-page scale") |
| `run`, `export` | `--pdf-font FILE` (v1) | none | F2, F3 | now validated for Turkish coverage before rendering (exit 1 on failure) |
| `run`, `export` | `--css FILE` (v1) | none | F3 | unsupported properties → one warning |
| `status` | `--json` (v1) | | F1, F2 | adds `figures` and `overlay` sections |
| `exporters` | | | F2, F3 | lists `pdf-overlay`; `pdf` shows `engines: native (available), weasyprint (available|unavailable)` |

Settings (`config.py`, all with `BOOK_TRANSLATOR_` prefix): `figure_*` above, `overlay_min_scale`, `overlay_floor_scale`, `overlay_batch_min_chars` (1000), `overlay_context_chars` (4000), `pdf_engine`, `pdf_page_size`, `pdf_margin`.

### 7.2 Output files

| File | Producer |
|---|---|
| `images/p{page:03d}-f{k:02d}.png` | extract (F1) |
| `figures.json` | extract (F1) |
| `translated_book.pdf` | export `--format pdf` (F3 native or weasyprint) |
| `translated_book.overlay.pdf` | export `--format pdf-overlay` (F2) |
| `overlay_review.json` | export `--format pdf-overlay` (F2): `{version:1, generated_at, thresholds, counts, entries:[OverlayReviewEntry]}` |

### 7.3 Exit codes (unchanged table, new situations mapped)

| Code | New situations in v1.1 |
|---|---|
| 0 | Overlay export with a non-empty review list but every block placed (Q21); reflow export with `figure_missing` warnings |
| 1 | `OVERLAY_STATE_MISSING` (FR-49), `OVERLAY_PDF_RESTRICTED` (E-49), `FONT_UNSUPPORTED` (FR-53), `FIGURE_SINK_FAILED` (E-57) |
| 2 | Overlay export with `could_not_fit` blocks or pages skipped for lack of a text layer, `--allow-partial` overlay; `--pdf-engine weasyprint` not installed (optional exporter) |
| 3 / 4 / 130 | As v1, for the overlay pass too |
| 5 | `STATE_HASH_MISMATCH` on the overlay unit set (options changed / units re-extracted differently); `export` with a changed source PDF **when the overlay format is requested** — the source *is* the output there, so nothing is written (CR-92) |

Fix-round additions to the contract:

- **Partial export reporting (CR-91).** One format failing no longer hides the formats already written: the exporter loop keeps going, the failure becomes an `export_error:<format>:<message>` warning, and the run reports the outputs that reached disk with `summary.json` updated. The exit code is the failure's own (a USER exporter error stays 1, above "partial") — exit 1 now means "something failed, here is what still got written". Only an export that produced **no** file at all exits with a bare error, as before (CR-63). An optional exporter that downgrades is unchanged: `export_failed:<format>` + exit 2.
- **Changed source, reflow only (CR-92).** When the source PDF's hash no longer matches and the overlay format is *not* requested, the file is never read: md/epub are written in full, the figure step is skipped with `figure_translate_failed:input_changed:<name>` (or `input_changed:<name>` when there is no figure step), the legends stay visible so no paid translation is lost, and the run exits 2 (partial output). `--input` pointing at a *wrong but existing* file behaves the same way; `--input` pointing at a missing file is still exit 1.

---

## 8. Testing strategy delta (doc 02 §11 style)

### 8.1 Fixture PDFs to add (`tests/conftest.py` builders, PyMuPDF-generated unless noted)

| Fixture | Content | Used by |
|---|---|---|
| `vector_diagram` | `docs/test_doc.pdf` page 2 (read-only, real): 38 paths + background rect + 15 labels + caption + short-doc header | F1 AC US-15/1–2, NFR-16 (pixel diff vs `deney/fig_1_12.png` ≤ 2 %), F2 grouping (13/14 split), header units |
| `decorative_rules` | running header with a rule beneath, footnote separator line, an underlined link box, three bullets drawn as paths, a 2×3 table with borders — **no** figure | AC US-15/3, E-24 |
| `two_figures` | two boxed diagrams (≥ 6 paths each) stacked with a prose paragraph between, each with "Figure 3.1"/"Figure 3.2" captions | AC US-15/4, E-25 |
| `subfigures` | two adjacent drawings with one caption "Figure 3.4 (a) … (b) …" | E-25 caption-driven merge |
| `two_column_spanning` | two text columns above and below a full-width diagram; known sentences | AC US-15/5, E-26 (100 % of column sentences in the flow) |
| `full_page_figure` | drawing + caption only | AC US-15/6, E-27, C8 |
| `raster_caption_above` | inserted PNG with "Table 2.1" caption above; a second page with a caption and no figure | E-30 |
| `table_page` | `find_tables`-detectable table with border drawings inside | E-32, E-45 (overlay cells) |
| `mixed_figure` | drawing cluster containing a raster photo | E-31 |
| `text_box` | paragraph inside a single-rectangle border | E-28 (not a figure) |
| `formula_page` | a few small vector glyph paths inline | E-29 (not a figure; rejection logged) |
| `overlay_styles` | bold/italic/centred/justified/right-aligned blocks, a footnote line, superscript marker, a hyphenated line break inside a block, a cross-page sentence | F2 style/alignment/fragment/hyphen |
| `overlay_no_text` | one scanned (image-only) page inside a text document | E-47, FR-48, exit 2 |
| `overlay_over_image` | text block drawn over an inserted image | E-40 (`fill=False` check) |
| `overlay_links_toc` | outline with 3 entries (2 matching heading blocks), an internal link annotation, a form widget | FR-47, AC US-21/2–3, E-50 |
| `overlay_encrypted` | the `overlay_styles` file saved with an owner password and no modify permission | E-49 |
| `v1_state_db` | `docs/test_doc_output/translation_state.db` copy (schema 1) | migration test |

### 8.2 Harnesses and assertions

- **Pixel-diff harness (overlay, AC US-18/2, NFR-21):** render source and output pages at 150 DPI (`get_pixmap(dpi=150)`), paint every *placed* unit rect (expanded by 1 pt) black on both, assert the remaining pixel arrays are identical (`==` on bytes); also assert `len(get_drawings())` and `len(get_images())` per page unchanged. For `overlay_over_image`, additionally assert the mean value inside the block rect after redaction (before insertion) is not white.
- **Residual-glyph check (AC US-18/3):** for every placed unit, `get_text(clip=rect)` after export equals the translated text (whitespace-normalised) and contains no 6-char substring of the source text; for every untouched unit it equals the source (collateral check).
- **Glyph coverage check (NFR-25):** E1 as a test: Story + `insert_htmlbox` with `çğıİöşüÇĞİÖŞÜ` in serif/sans/mono, extracted text contains all; `validate_turkish_coverage` rejects a font without `ğ` (a fixture TTF subset built with `pymupdf.Font` → `Font.buffer`? if impractical, monkeypatch `has_glyph`).
- **File-size check (NFR-24):** overlay of `docs/test_doc.pdf` ≤ 1.5 × source + 2 MB and exactly one non-source font family (names without a subset prefix ≤ 2 faces) on page 2; native PDF of the round-trip fixture ≤ 2 MB.
- **Fake-provider payload assertions (NFR-31, AC US-16/2–3, US-20/6):** legend chunk → exactly one `translate()` call with `[caption, *labels]` and `context == caption`; `--no-figure-legend` → no label text in any payload; image bytes never in payloads; overlay pass on `vector_diagram` → request count ≤ 2 × reflow chunk count and chars within ± 10 %; header units never sent (default) and sent with `--overlay-translate-headers` except the page number; `--overlay-keep-figure-text` → no figure label sent.
- **Resume/kill (AC US-20/1, NFR-32):** `CountingRepository` over `OverlayBlockRepository`: interrupt after K units, re-run → 0 duplicate sends; SIGKILL test pattern of v1 (`taskkill`/`os.kill`) leaves units PENDING/COMPLETED only after `requeue_processing`.
- **Migration test:** open `v1_state_db` copy with v1.1 → backup exists, `schema_version == 2`, chunk rows byte-identical (dump compare), `status` on the *unmigrated* copy (read-only) succeeds with `overlay = None`; migration failure (monkeypatched exception) → rollback, file still v1, `STATE_SCHEMA_INCOMPATIBLE` with the backup path.
- **Property test extension (NFR-29):** the hypothesis BT-Markdown generator gains the new image line (with and without `src`), the legend marker + list block (with `more`), and asserts: lossless join, tiling, legend chunk = [caption?, legend] isolation, `Segmented.context` equals the caption text, `reassemble(identity)` reproduces the chunk, and `assemble(pair_legends=False)` under identity reproduces `source_book.md`.
- **Determinism (NFR-17, AC US-14/3):** two extractions of `vector_diagram`/`two_figures` → identical file names, count, order, PNG bytes and `figures.json` minus `generated_at`.
- **Pixel size (AC US-17/1, E4):** `abs(width_px − round(w × dpi / 72)) ≤ 2`, same for height, at 72/200/300 DPI; back-off test with `--figure-max-mb 0.05` → DPI ladder ends at 96, `dpi_reduced=True`, summary reports it.
- **Layering:** `test_layering.py` updated per §2.5 with the new forbidden-edge proofs; `mypy --strict` clean; pymupdf `type: ignore`s only in `pdfkit/api.py`.
- **CLI:** `--help` shows every new option with defaults/env; `--figure-exclude-pages "2,5-7"` parsing; `--pdf-margin` 1/2/4 values; `--overlay-floor-scale > --overlay-min-scale` → USER error; `export --format pdf-overlay` on a reflow-only directory → exit 1, no file, message names `translate --mode overlay`; `--format md` on an overlay-only directory → exit 1.
- **Native PDF (US-22/23):** every paragraph string of the Markdown appears in the extracted PDF text after whitespace normalisation; A4 + 20 mm → page rect and first text `x0`/`y0` within ± 1 pt; footer number absent on page 1 and present centred on page 2+; `get_toc()` has one entry per H1/H2; unsupported CSS property → exactly one `css_unsupported:` warning and exit 0; a wide table → scaled with `table_scaled` warning; image wider than the column → scaled to column width, aspect kept; `--pdf-engine weasyprint` without weasyprint → exit 2, md/epub produced.
- **Existing suite:** all v1 tests unchanged and green (`--no-figures` byte-identity is asserted by re-running the v1 extraction fixtures through `FigureOptions(enabled=False)` and diffing against the v1 expected markdown; the weasyprint skip remains, the native engine test replaces "pdf test skipped").

CI matrix unchanged (Windows + Linux, 3.10/3.12, core extras only) — the native engine makes `export --format pdf` part of the core job (NFR-26).

---

## 9. Risks, open questions, hand-off and implementation plan

### 9.1 Risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| R13 | Detector false positives/negatives on real books (charts made only of thin stroked lines, decorative frames around chapters, page-background art) | Isolation rule for thin drawings (§4.1 step 2), `figure_candidate_rejected` log, `--figure-exclude-pages`, `--no-figures`; every new false case becomes a fixture (NFR-18); thresholds are settings. |
| R14 | `apply_redactions` removes characters of neighbouring lines whose glyph boxes intersect a unit rect (line boxes of consecutive lines overlap by fractions of a point, E6) | Disjoint-split of overlapping rects (§4.4 step 4) + collateral check with review reason (§4.5 step 3); F2 spike measures this on `test_doc.pdf` first. |
| R15 | `insert_htmlbox` line-height/margins make single-line boxes shrink unnecessarily (E2: 0.57–0.60 for 22-char labels) | Explicit `margin:0; line-height:1.15`; spike compares against `line-height:1.0`; the review threshold is user-tunable. No box growth in v1.1 (Q13); v1.2 candidate "grow downward into free space". |
| R16 | Redaction removes link annotations under text | Snapshot + re-insert (§3.6.4); test `overlay_links_toc`. |
| R17 | MuPDF `Story` CSS subset: `white-space: pre-wrap`, table layout, `max-width` may be unsupported | FR-52 allowlist warning; explicit `width`/`height` on images; code lines soft-broken at 90 chars by the exporter if `pre-wrap` proves unsupported in the F3 spike; wide tables scaled with a warning. |
| R18 | Font family mapping heuristics (serif/sans from font names) mismatch the original look | Accepted (non-goal "exact font reproduction"); dominant style + colour preserved; `--pdf-font` override. |
| R19 | Overlay batching across pages makes one provider error reschedule many units | Batch bounded by 50 texts / ~1 000–3 000 chars; `reschedule` is idempotent; cost bounded by NFR-23. |
| R20 | `figures.json`/`images/` drift from `source_book.md` after hand edits | Export renders what the Markdown references; missing → placeholder + warning, orphan → warning (E-33); never an error. |
| R21 | Schema v2 migration on a large v1 DB fails mid-way | Doc 03 §7: backup first, one transaction, rollback on error, message with backup path; additive DDL only. |
| R22 | Short documents leak the running header into overlay/figure handling (A-13) | Band + short-line heuristic clip (§4.1 step 8); header units default to kept-as-is (FR-46), so a leaked header costs nothing in overlay mode. |
| R23 | `subset_fonts()` touching source fonts and changing untouched glyph rendering | It skips already-subset fonts (prefix `ABCDEF+`); the pixel-diff harness catches any change on untouched pixels. |
| R24 | Performance: `insert_htmlbox` per unit (~30/page) and `find_tables` per page in overlay extraction | NFR-27 target ≤ 2 s/page measured in the F2 spike on a 50-page sample; `detect_tables` toggle from CR-21 applies to both extractors. |

### 9.2 Open questions (defaults chosen; "devam" accepts them)

| # | Question | Default |
|---|---|---|
| O4 | Legend rendering when the same figure has a caption above (E-30): legend after the caption (above the image) or after the image? | After the caption, wherever the caption is (caption and legend stay one chunk). |
| O5 | Should `status --json` show overlay review counts only from `overlay_review.json` (D6/§6 "not persisted")? | Yes; the DB Architect may override by persisting `last_scale`/`last_reason` per unit if `status` without the file must be complete. |
| O6 | `--figure-dpi` above 300 for vector figures produces multi-MB PNGs even under the 8 MB cap; cap the *default* at 200 but allow up to 600? | Yes (range 72–600, default 200). |
| O7 | Overlay: units with `no_letters` (pure numbers/symbols) are never sent. Should ordinal-only labels like "IV" be sent? | No (kept). |
| O8 | Should `run --mode overlay` also export reflow formats when a reflow pass exists? | Yes, only when explicitly listed in `--format` (E-51). |
| O9 | DeepL `context` for **reflow** chunks (not only legend chunks): pass the heading path as context? | No in v1.1 (unchanged v1 behaviour: "no context carry-over"); legend chunks only. |

### 9.3 Hand-off to the DB Architect (v1.1)

1. Model the **Overlay unit** and **Overlay page** aggregates (§6) with the same runtime-state group, CHECKs (`translated_text ⇔ COMPLETED`, `next_attempt_at` only when PENDING, enums, non-negative counters) and transition SQL as `chunks` (doc 03 §5.2), plus `claim_pending_batch` (ordered range, `BEGIN IMMEDIATE`, tripwire), `page_progress`, `page_texts`, `iter_page`.
2. Extend **Job** with the overlay workflow position, `overlay_sha256`, overlay options, overlay unit count; extend **Run** with `mode`. Keep provider/glossary binding job-level.
3. Schema **v2** and `MIGRATIONS[1]` (additive; ADD COLUMN with defaults; new tables; version bump last); read-only opens accept 1 and 2 (D18).
4. Index plan for the new access patterns (§6) with the same "keep hot queries off the table" discipline and the partial-index literal rule of doc 03 §4.
5. Decide O5 (placement outcome not persisted by default).
6. Volumes: units ≤ 30 000/job, ~0.5–2 KB per row; pages ≤ 1 000; runs ≤ 100 (unchanged).

### 9.4 Hand-off to the developers

- **DB Developer:** `models.py` v2, `session.py` migration + read-only rule, `repository.py` `OverlayBlockRepository`/`OverlayPageRepository`, `JobPatch`/`RunRecord` fields, tests (acceptance checks mirrored from doc 03 §10 for the new table; migration test).
- **Python developer (pipeline/extractors/exporters):** modules of §2 in the order of §9.5; contract changes C1–C16; skills: none SAP-related; use `pdfkit/api.py` for every pymupdf call.
- **Code Reviewer / QA:** the accepted deviations list of doc 04 remains valid; new review focus: layering edges (§2.5), redaction collateral (R14), `subset_fonts` effects (R23), determinism (NFR-17), payload privacy (NFR-31).

### 9.5 Sequenced implementation plan

| Step | Work | Parallelisable with |
|---|---|---|
| 0 | `domain/bt_syntax.py`, `pdfkit/api.py` (move `_pdf.py`), layering test update, `ErrorCode` additions | — (foundation, half a day) |
| F1-a | `cleanup.py` drawings/`Line.dir`; `figures.py` detector + `test_figures.py` on `RawPage` fixtures (no PDF) | F1-b |
| F1-b | `figure_render.py`, `figure_inventory.py`, `FigureOptions`, sink in the orchestrator, `--fresh` set, stale cleanup, `figures.json`, summary/status | F1-a |
| F1-c | Chunker/segmenter/assembler legend + image line (C1–C4, C10), discovery exclusion, DeepL `supports_context`, property test extension | F1-a/b |
| F1-d | Exporters: md/epub image rendering + legend wrapper + CSS; CLI options; fixtures of §8.1 (F1 rows); AC US-14…17 tests | after F1-a/b/c |
| F3-a | `exporters/fonts.py` (validation, Archive), `ExportOptions` C9, `--pdf-font` validation path | with F1-d |
| F3-b | `pdf_native.py` engine + facade, CSS allowlist, image sizing, footer/outline, `--pdf-engine/--pdf-page-size/--pdf-margin`, tests | after F3-a; with F2-a |
| F2-a | **Spike (time-boxed 1 day):** grouping + redaction + `insert_htmlbox` + `subset_fonts` on `docs/test_doc.pdf`; measure R14/R15/R24; confirm `fill=False`, link survival, `set_language` | with F3-b |
| F2-b | DB Architect → DB Developer: schema v2, repositories, migration | with F2-a |
| F2-c | `overlay_extractor.py` + `cleanup.py` grouping/alignment/style (pure tests), `overlay_batch.py`, `units.py` adapters, orchestrator mode/preflight/refusals/status | after F2-b |
| F2-d | `pdf_overlay_exporter.py` (placement, review, outline/links/metadata), CLI, pixel-diff/residual harness, fixtures of §8.1 (F2 rows), migration test | after F2-a/c |
| 7–8 | Code review (v1.1 delta) and QA (v1 + v1.1 in one pass, per `durum.md`) | — |

Milestone gates: after F1-d the v1 suite plus F1 tests are green and `docs/test_doc.pdf` page 2 yields one figure, no leaked labels, a translated caption and a legend; after F3-b `export --format pdf` runs in the Windows core CI job; after F2-d the overlay of `docs/test_doc.pdf` passes the pixel-diff, residual and size checks and reproduces the approved PoC look with the 13/14 boxes correct.

---
✅ Solution Architect (v1.1) tamamlandı.
➡️  Sonraki adım: DB Architect (v1.1)


---

## Addendum A (2026-09-18) — "detect the page, translate, write it back with the same properties"

**Status:** binding user decision, recorded during F2 implementation. It overrides §3.3 (legend display), §7.1 (defaults of `--format`, `--figure-legend`, `--pdf-page-size`, `--pdf-margin`) and D12 wherever they conflict.

**Türkçe özet.** Kullanıcı, hiç istemediği sayfa düzeni varsayılanlarını ("A5 / 12 pt" → 3 sayfalık kaynaktan 9 sayfa) reddetti. İstenen davranış: *sayfanın özelliklerini algıla → çevir → çeviriyi aynı özelliklerle geri yaz*. Bu nedenle (1) varsayılan PDF çıktısı `pdf-overlay` oldu, (2) şekil etiketleri görselin **içinde** çevriliyor (lejant listesi yalnızca `--figure-legend` ile), (3) reflow PDF'in sayfa boyutu, gövde punto ve kenar boşlukları kaynaktan türetiliyor (`source_profile.json`); CLI varsayılanları artık `source`.

### A.1 `pdf-overlay` is the default PDF output

`--format pdf` keeps meaning the *reflow* PDF and is never part of a default. The rule implemented in `pipeline/orchestrator.py::default_formats(has_reflow, has_overlay)` and applied whenever `--format` is not given (`export(formats=None)`, the CLI passes `None`):

| State of the output directory | `export` default | `run` default |
|---|---|---|
| reflow pass only (chunks exist) | `md,epub` | `md,epub` (`--mode reflow`) |
| overlay pass only (overlay units exist) | `pdf-overlay` | `pdf-overlay` (`--mode overlay`) |
| both passes | `md,epub,pdf-overlay` | `--mode reflow`: `md,epub,pdf-overlay`; `--mode overlay`: `pdf-overlay` |
| neither | `md,epub` (so the v1 refusal "run `translate` first", exit 2, is reported) | — |

`run --mode overlay` with an explicit reflow format and no existing reflow pass is refused **before extraction** (`OVERLAY_STATE_MISSING`, exit 1, E-51); with `--fresh` the old reflow pass does not count. `--mode` also reads `BOOK_TRANSLATOR_MODE`.

### A.2 Figure labels are translated in place

- The legend block (`<!-- legend:N -->` + `- label` lines) stays in `source_book.md` as the **translation vehicle**: `figure_options_from_settings` always sets `FigureOptions.legend = True` (the dataclass field remains an extractor-level switch for API users and tests). Cost note: the labels are therefore always sent to the provider in reflow mode.
- `FigureRecord` / `figures.json` gain **`label_boxes`** (one entry per emitted legend label, same order): `{text, bbox, font_size, bold, italic, family, color, alignment}` — `domain.models.LabelBox`. Geometry comes from the label's line group (`FigureRegion.label_groups`, aligned with `labels`), style from the overlay style detection of `cleanup.py` (`detect_style`, `detect_alignment` with the smallest enclosing drawing rect as container). Inventories written before this addendum load with `label_boxes = []` (no translated PNG, English figure kept).
- Export (reflow formats): after `assemble` — which pairs the legend items as `- <source> — <target>` (C10) — `pipeline/figure_labels.legend_pairs` reads the pairs, `build_replacements` maps them onto the `label_boxes`, and `exporters.figure_overlay.render_translated_figures(source_pdf, jobs, font, uniform_scale=…)` (developer B) returns one outcome per figure. The PNG is written atomically **next to** the English original as `images/pNNN-fKK.tr.png` (CR-57: the original is never overwritten; only the translated document is re-pointed at the new file). The whole document is one batch call, so the source is read and opened once (CR-61/CR-87), and each figure renders on its own one-page copy so two figures on the same page cannot affect each other (CR-88). `--no-overlay-uniform-scale` reaches the labels too (CR-89). `LabelReplacement` is defined in `domain/overlay.py`; the pipeline builds B's type from it field by field, so either definition works. Every failure is a warning, never an error: `figure_translate_unavailable` (module missing), `figure_translate_failed:<file>:<reason>` (render/write failure, missing input PDF, font); the English PNG stays. An unpaired legend (`legend_mismatch:N`) or an unchanged label produces no replacement. A re-`extract` re-renders the English PNGs, the next `export` translates them again (idempotent).
- The legend **list** is rendered in md/epub/pdf only with `--figure-legend` (settings `figure_legend`, default now **off**; option on `run` and `export` — `extract` still accepts it for compatibility, where it no longer has an effect): without it `strip_legends` removes marker, items and the truncation note from the assembled Markdown before the exporters run.

### A.3 Reflow PDF page design is derived from the source

- `extract` writes `<output>/source_profile.json` (version 1) next to `figures.json`: `page_width_pt`, `page_height_pt` (most common page size), `body_font_size_pt` (v1 character-weighted mode), `margins_pt` `[top, right, bottom, left]` (median distance of each page's text envelope to the page edges; pages without text are not measured), `serif` (dominant family of the body-sized spans), `pages_measured`, `extractor`. Pure computation: `cleanup.compute_source_profile`; I/O: `extractors/source_profile.py`. A write failure is a warning (`source_profile:<reason>`); the marker extractor writes no profile.
- `export --format pdf`: `_apply_source_profile` fills `ExportOptions.page_rect_pt` (only when `page_size == "source"`), `body_font_pt` (always, there is no user option for it) and `margins_pt` (only when `--pdf-margin source`) unless already set. Without the file: warning `pdf_page_design:source_profile.json missing …` and the engine's own fallback applies. md/epub exports never emit that warning.
- CLI/settings: `--pdf-page-size` and `--pdf-margin` default to **`source`**; "A5", "18mm 15mm 20mm 15mm" and any "12 pt" default are gone from config, options and `--help`. An explicit paper name / margin list behaves as before (`parse_pdf_margin`).

### A.4 F2-A implementation notes and deviations from §3.6 / §4.4 / §5.5

| # | Topic | Implemented | Reason |
|---|---|---|---|
| A-D1 | §4.4 step 3 "open group = last group" | A line may join **any** open group (most recent first) that satisfies the succession rule | On E6 the label columns interleave in `(y0, x0)` order ("4. Model fitting" / "3. Image processing" / "and optimization"); the literal rule split three of the fifteen labels |
| A-D2 | same-row lines | A line on the same text row never continues a group vertically; a **run-in join** merges a single-line unit with the text that follows it on the same row (gap ≤ 2 em, same size): "Figure 1.12" + caption, "1.3" + "Book overview" heading. Inside the header/footer band a numbers-only line never joins, so the running header stays three units (E6) | one CAPTION / HEADING unit instead of fragments; alignment uses per-row boxes |
| A-D3 | JUSTIFY | needs **≥ 3 lines** (2 full + the last; `_JUSTIFY_MIN_LINES = 3` in `cleanup.py`) | two ragged lines ending within 1 pt by coincidence would be stretched. (An earlier draft of this row said "≥ 4"; the code, `durum.md` and §4.4 step 7 all say 3.) |
| A-D4 | `overlay_sha256` re-check on resume | option flags compared with the job binding, then the hash is recomputed from the **stored** units (no second PDF extraction) | the input file is already bound by `input_sha256` and extraction is deterministic (asserted by a test), so re-reading the whole PDF on every resume adds cost without adding protection |
| A-D5 | `extract_overlay(figure_regions=None)` | runs the F1 detector (no rendering) when no `figures.json` exists | `translate --mode overlay` on a fresh directory yields the same FIGURE_LABEL units as after `extract`, so `--overlay-keep-figure-text` always works |
| A-D6 | `UnitAdapter` | adapters own their repository (`adapter.repo`); `claim(job_id, permits, unit_cap, now, run_id)`, `group(units)`, `finish(batch, response, FinishContext) -> List[Result[TranslationResult]]` | per-unit `Err` lets the v1 scope routing (reschedule / fail / pause, empty-response counter) apply to exactly the unit that misbehaved (E-52); a batch-level `Err` is applied to every unit |
| A-D7 | `check_modifiable` | refuses when the document is encrypted **or** the MODIFY permission bit is clear, independent of the sign of the permission word | pymupdf reports permissions as a negative two's-complement word |
| A-D8 | `status` | `overlay` section as a plain dict + `mode` of the last run; review counts = `counts` of `overlay_review.json` plus entries grouped by reason | O5 default (not persisted) |
| A-D9 | chars accounting | `chars_sent` per unit = protected payload length; the provider's batch `chars_billed` is apportioned by payload length | §4.6 |

Output files added: `source_profile.json` (extract). `--fresh` archive set += `translated_book.overlay.pdf` (already matched by `translated_book.*`), `overlay_review.json`, `source_profile.json`.

---

## Addendum B (2026-09-21) — third-pass fix round

The re-review of the v1.1 fix round (`docs/04_code_review.md` → *Third pass*) closed one Critical and a group of Warnings. Design-visible outcomes, in the order they affect the pipeline:

### B.1 Figure detection — the page safety net (CR-83, CR-84, CR-85, CR-86)

The CR-38 safety net compared the characters absorbed by a page's figures against **all** of the page's characters, so it could not tell "a figure swallowed the page's prose" apart from "the page *is* a figure". A label-dense full-page schematic reached a ratio near 1.0 and lost every figure on the page — the opposite of what F1 exists for, and against the contract that a full-page figure is supported even with no prose around it.

The net now fires on two independent triggers, both behind the 400-character floor:

| Trigger | Measure | Catches |
|---|---|---|
| Lost prose | absorbed **prose** characters (a line over `_PAGE_LABEL_MAX_CHARS` is prose, not a label) > `_PAGE_ABSORB_MAX_RATIO` of absorbed prose + prose left in the flow | a figure cluster that swallowed a column of body text |
| Text page | absorbed text fills ≥ `_PAGE_TEXT_FILL_RATIO` of the clusters' area | a bordered index / contents page, which is text with rules, not a drawing |

The denominator is prose, never the whole page, so a page made of labels is no longer evidence against its own figure. `_is_prose_cluster`'s mixed rule was tightened the same way: the 15 % area share now also requires the prose to outweigh the labels *by a factor* and at least two multi-line blocks, so a diagram with one annotation box survives.

Both rejections are now reported, not just logged: `figure_page_dropped:<page>:<reason>` and `figure_candidate_rejected:<page>:<reason>` reach the job warnings, and `detect_figure_regions` takes a `notes` list so the **overlay** pass surfaces them too (CR-86) — previously it built a throwaway context and the reason vanished.

*Known limit:* the fill trigger is a geometric heuristic. A schematic whose labels cover more than half of its own area could in principle be dropped; the threshold is a single constant.

### B.2 Figure rendering — one batch, one page copy per job (CR-87, CR-88, CR-89)

`render_translated_figures` was already the batch entry point, but the orchestrator still called the single-figure wrapper in a loop, so the source PDF was read and opened once **per figure**. The orchestrator now builds one job list per document and makes one call.

That change is only safe together with CR-88: redaction and `insert_htmlbox` mutate the page, so jobs sharing one document were not independent — a second figure on the same page rasterized what the first one wrote, and a job that failed after its redaction left the page emptied for everyone behind it. Each job now renders on its own one-page copy (`pdfkit.doc_page_copy`), closed when the job ends; the opened source document itself is never modified.

`uniform_scale` is threaded from the CLI through `_export_reflow` → `_translate_figures` → the batch call and the single-figure wrappers, so `--no-overlay-uniform-scale` reaches the figure labels as well as the body text (CR-89).

### B.3 Overlay placement — the shared page scale (CR-95, CR-96)

**Per-unit cap (user decision).** The shared scale is a *group* scale, and the group median let a page's most crowded paragraph set the size for paragraphs that fitted comfortably — a unit could be shrunk by a third and never appear in the review list. A unit the group scale would push more than `SHARED_MAX_DROP` (10 %) below its own natural scale now leaves the group and keeps its own scale; if fewer than two units remain, no group forms at all. When a page's shared factor falls below `SHARED_SCALE_NOTE` (0.9) the page gets an **informational** record in `overlay_review.json`, told apart from a unit finding by its empty `block_id` and zero `unit_id`. This raises `counts.entries` (and `summary.json`'s `overlay_review_entries`) on such pages.

**`SHARED_SPREAD` 0.15 → 0.10 (user decision).** The cap alone made the reference page *worse*: at a spread of 0.15 the two most crowded paragraphs stayed inside the group, dragged the shared scale down to where the cap then ejected the six comfortable units one by one, and page 1 came out in five sizes. Narrowing the outlier bound to 0.10 makes those two outliers from the start. Measured on `docs/test_doc.pdf` page 1: **five distinct body sizes → two** (six units at 9.11 pt, the two crowded ones at 8.50 pt), the cap never fires, `could_not_fit = 0`, `shrunk = 0`.

**Line height (CR-96).** `LINE_HEIGHT_TIGHT = 1.05` is removed. The line height is derived from the source `line_boxes` (median of consecutive line-top deltas over the font size) and clipped to `[LINE_HEIGHT_MIN, LINE_HEIGHT_MAX]` = `[1.15, 1.40]`; 1.15 is the fallback when it cannot be measured (a figure label has no line boxes). In `uniform` mode the text may be tightened before the font is shrunk, but never below `LINE_HEIGHT_MIN`. Measured on the reference page: 1.274 / 1.292, against the source's ~1.26 rhythm.

### B.4 Label prefix style (CR-97)

`prefix_style` (the bold running number of a label, "**2.** Image formation") now travels the whole figure path: `LabelBox` → `figures.json` (`label_boxes[].prefix_style`) → `LabelBoxEntry` → `LabelReplacement` → `PlacementSpec`. It reached the overlay PDF before but was dropped at the `figures.json` boundary, so `.tr.png` lost the bold prefix. Older `figures.json` files without the field still load. Label **alignment** was measured and found correct — that part of the earlier gap note was wrong.

### B.5 Export reporting and a changed source (CR-91, CR-92)

See §7.3 "Fix-round additions to the contract" and the `export --input` row in §7.1.
