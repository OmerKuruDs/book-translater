# Proje Durumu — book-translator

Son güncelleme: 2026-09-21 (3. tur inceleme + düzeltme turu ✅ 722 test, canlı doğrulandı; **QA onay bekliyor**)

## Ne yapıyoruz
İngilizce PDF kitabı Türkçeye çeviren Python CLI (`book-translator`). Spec: kullanıcıdan gelen 4 aşamalı pipeline (ingestion → glossary → translation → export). Zorunlu 8 adımlı iş akışı ile ilerleniyor; her adım kullanıcı onayıyla ("devam") geçiliyor.

## Tamamlanan adımlar
| Adım | Çıktı | Durum |
|---|---|---|
| 1. Business Analyst | `docs/01_business_analysis.md` (13 US, 27 FR, 15 NFR, 22 edge case) | ✅ onaylandı, Q1–Q9 varsayılanlarla |
| 2. Solution Architect | `docs/02_solution_design.md` (katmanlar, arayüzler, chunker, orchestrator, CLI kontratı, bağımlılıklar) | ✅ onaylandı, O1–O3 varsayılanlarla |
| 3. DB Architect | `docs/03_db_design.md` (5 tablo, indeks planı, repository→SQL eşlemesi) | ✅ onaylandı, D1–D5 + A1–A5 + Q5–Q8 kabul |
| 4. DB Developer | `pyproject.toml`, `.venv`, `src/domain/{result,models}.py`, `src/database/{models,session,repository}.py`, `tests/test_repository.py` (18 kabul testi), `tests/test_result.py` | ✅ 24 test geçti, mypy strict + ruff temiz |
| 5. Python pipeline — Faz A | `src/config.py`, `src/domain/sentences.py`, `src/extractors/*`, `src/pipeline/{chunker,segmenter,backoff,assembler,glossary_check}.py`, `src/translators/*`, `src/glossary/*`, `src/exporters/*` + `tests/test_{extractor_cleanup,chunker,translators,glossary,exporters}.py` | ✅ 222 test geçti (1 skip: weasyprint yok), mypy strict + ruff temiz, import yönü kuralı ihlalsiz, uçtan uca kimlik duman testi geçti |
| 5. Python pipeline — Faz B | `src/logging_setup.py`, `src/pipeline/orchestrator.py`, `src/cli.py`, `tests/test_orchestrator.py` (30), `tests/test_cli.py` (43), `tests/conftest.py`'ye FakeTranslator/IdentityTranslator/CountingRepository/FakeClock | ✅ 295 test geçti (1 skip), mypy strict + ruff temiz, gerçek CLI ile extract→glossary→translate --dry-run→status→export denendi; log/summary'de anahtar sızıntısı yok |
| 7. Code Reviewer | `docs/04_code_review.md` (1 Blocker, 5 Critical, 17 Warning, 12 Suggestion; sapma tablosu; QA risk listesi) | ⚠️ CHANGES REQUIRED — düzeltme turu gerekli |
| 7b. Düzeltme turu | CR-01..06 + CR-07/11/12/13/14/15/16/17/23 düzeltildi; yeni `tests/test_layering.py`, `tests/test_logging_setup.py`; `JobRepository.get_single_job()`; `docs/03_db_design.md` I3 notu | ✅ 322 test geçti (1 skip), mypy+ruff temiz, 3 repro orkestratör tarafından doğrulandı |
| Canlı DeepL testi | `docs/test_doc.pdf` (3 sayfa, 8946 kr) → `docs/test_doc_output/{source_book.md,translated_book.md,translated_book.epub}` | ✅ 8/8 chunk, 3 s, exit 0; ikinci run 0 karakter gönderdi (resume OK); loglarda/DB'de anahtar yok |
| 7c. Yeniden inceleme | `docs/04_code_review.md` 'Second pass' bölümü: 15/15 bulgu kapandı; yeni CR-34 (Critical: "3. Bölüm" EPUB'da `<ol>`), CR-35..37 öneri | ✅ APPROVED WITH CHANGES |
| 7d. CR-34/36/37 düzeltmesi | `assembler.py` (CommonMark kuralıyla sıra numarası kaçışlama `3\.`, bullet/ordered ayrımı), `test_layering.py` prefix kuralı, `cli.py` command_failed job_id | ✅ 336 test geçti (1 skip), EPUB'da `<ol start="3">` yok, test_doc_output yenilendi |
| v1.1 — 1. Business Analyst | `docs/05_change_request_v1_1_ba.md` (US-14..23, FR-28..53, NFR-16..32, E-23..57, Q10..Q23) | ✅ onaylandı: varsayılanlar + F1→F3→F2 |
| v1.1 — 2. Solution Architect | `docs/06_solution_design_v1_1.md` | ✅ onaylandı (D1..D18, O4..O9 varsayılan) |
| v1.1 — 3. DB Architect | `docs/07_db_design_v1_1.md` (overlay_pages + overlay_blocks, jobs/runs ek kolonlar, şema v2, 1→2 geçiş, index planı, kabul kontrolleri O-1..O-21, DQ1..DQ8) | ✅ hazır; DB Developer onay bekliyor |
| v1.1 — adım 0 | `src/domain/bt_syntax.py`, `src/pdfkit/api.py` (eski `_pdf.py`), ErrorCode ekleri, katman testi pdfkit | ✅ orkestratör yazdı |
| v1.1 — F1 şekil koruma | X: `extractors/{figures,figure_render,figure_inventory}.py` + cleanup/base/pymupdf_extractor/orchestrator/cli/config; Y: chunker/segmenter/assembler lejant, discovery, DeepL context=True, exporters `<figure>`/EpubImage/`![Şekil N]`, book.css | ✅ 438 test (1 skip), mypy+ruff temiz; canlı run: 1 vektör şekil, 15 etiket, lejant çevrildi, EPUB'da görsel paketli → `docs/test_doc_output/v1_1/` |
| v1.1 — 4. DB Developer | `tests/fixtures/v1_schema.sql`, `src/domain/overlay.py`, `database/{models,session,repository}.py` v2 (`RuntimeStateMixin`, `OverlayBlockRepository`, `claim_pending_batch`, geçiş 1→2), `tests/test_repository_overlay.py` (21), `tests/test_migration.py` (6) | ✅ EXPLAIN doğrulandı; canlı: v1 dosya `export`'ta yedeklenip v2'ye geçti |
| v1.1 — F3 native PDF | `exporters/{fonts,pdf_native}.py`, `pdf_exporter.py` facade, `--pdf-engine/--pdf-page-size/--pdf-margin`, `tests/test_pdf_native.py` (42) | ✅ canlı: 9 sayfa, 178 KB, Charis SIL alt küme, şekil sayfa 4'te, lang tr → `docs/test_doc_output/v1_1/translated_book.pdf` |
| v1.1 — 7e. 3. tur inceleme | `docs/04_code_review.md` Third pass (Bölüm A + konsolide): 1 Critical, 15 Warning, 15 Suggestion | ✅ APPROVED WITH CHANGES |
| v1.1 — 7f. düzeltme turu | CR-83/84/85/86/87/88/89/91/92/95/96/97 + `LABEL_MAX_DROP` ve tur tabanlı yeniden gruplama; doc 06 Addendum B | ✅ 722 test (1 skip), mypy+ruff temiz, canlı doğrulandı → `docs/test_doc_output/v1_1_third/` |
| Toplam | | ✅ 722 test geçti (1 skip: boş örnek DB), mypy strict 60 dosya, ruff temiz |

## Kaldığımız yer
**3. tur (yeniden inceleme + düzeltme) tamamlandı (2026-09-21): 722 test geçti / 1 skip, mypy strict (60) + ruff temiz, canlı run doğrulandı.** Sıradaki adım: **8. QA (v1 + v1.1 tek sefer)** — kullanıcı onayı bekleniyor.

### 3. tur inceleme (Code Reviewer)
`docs/04_code_review.md` → `Third pass` bölümleri. Verdict **APPROVED WITH CHANGES**: 0 Blocker, 1 Critical (CR-83), 15 Warning (CR-84..98), 15 Suggestion (CR-99..113). Dosyada bu turdan iki bölüm var — `Bölüm A` (YR-1..YR-12 numaralı) ve `konsolide` (CR-83+, **bağlayıcı**); aralarında YR↔CR eşleme tablosu duruyor. Önceki turun Blocker'ı (CR-38) ve 3 Critical'ı (CR-39/40/41) kodda satır satır doğrulanarak kapalı bulundu.

### Düzeltme turunda kapatılanlar
| Bulgu | Ne yapıldı |
|---|---|
| CR-83 (Critical) | Sayfa güvenlik ağı emilen **düzyazıya** dayanıyor (payda artık sayfanın tamamı değil) + ikinci tetik: emilen metin kümelerin alanının ≥ %50'sini dolduruyorsa (çerçeveli dizin sayfası). Tam sayfa etiketli şema artık korunuyor |
| CR-84 | `_is_prose_cluster` karışık kuralı: `prose > 2 × label` **ve** ≥ 2 çok satırlı blok |
| CR-85 / CR-86 | Red gerekçeleri iş uyarılarına düşüyor; `detect_figure_regions(..., notes)` ile overlay yolu da taşıyor |
| CR-87 / CR-88 | Orkestratör belge başına tek `render_translated_figures` çağrısı; her iş kendi tek sayfalık kopyasında render ediliyor (`doc_page_copy` + `finally` close) |
| CR-89 | `uniform_scale` uçtan uca (CLI → `_export_reflow` → `_translate_figures` → batch → tek şekil sarmalayıcıları) |
| CR-91 | Bir format hata verse de diske yazılmış çıktılar raporlanıyor; `export_error:<format>` uyarısı, `summary.json` güncelleniyor, exit = hatanın kendi kodu (1). Hiç çıktı yoksa eski `Err` (CR-63) |
| CR-92 | Hash uyuşmazlığı: exit 5 **yalnız overlay** istendiğinde; aksi hâlde kaynak okunmaz, md/epub tam yazılır, figür adımı `figure_translate_failed:input_changed` ile atlanır, exit 2 |
| CR-95 | **Kullanıcı kararı:** birim başına %10 tavan + ortak ölçek < 0,9 olduğunda sayfa düzeyi bilgi kaydı. **Ek:** `SHARED_SPREAD` 0,15 → 0,10 |
| CR-96 | **Kullanıcı kararı:** `LINE_HEIGHT_TIGHT` (1,05) kaldırıldı; satır aralığı `line_boxes`'tan türetilip [1,15 – 1,40] arasına kırpılıyor. Referans sayfada 1,274 / 1,292 (kaynak ~1,26) |
| CR-97 | `prefix_style` uçtan uca; `.tr.png`'de kalın sıra numarası önekı korunuyor. Eski `figures.json` hâlâ yükleniyor |

### CR-95'in canlı doğrulamada ortaya çıkan yan etkisi (ve düzeltmesi)
Tavan tek başına gerçek çıktıyı **bozdu**: overlay PDF'te sayfa başına ayrık punto sayısı s.2'de 3 → 9, s.3'te 3 → 6 oldu. İki sebep, ikisi de düzeltildi:
1. **Şekil etiketleri de tavana giriyordu** — diyagram etiketleri tek ölçekten 7 ölçeğe dağılmıştı. `_placement.LABEL_MAX_DROP = 1.0` ile etiket grupları tavandan muaf; `max_drop` artık grup anahtarına göre çözülüyor (`pdf_overlay_exporter._label_max_drop` + `figure_overlay`).
2. **Gruptan çıkarılan birimler yeniden gruplanmıyordu** — her biri kendi doğal ölçeğine gidiyor, sayfa 10,33 / 10,45 / 10,50 gibi birbirinden ayırt edilemeyen puntolara bölünüyordu. `_share_scales` artık grubu **turlar hâlinde** paylaştırıyor (`_share_one` en fazla bir grup kurar, artanları geri döndürür).

Sonuç (ayrık punto / sayfa): s.1 4 → 5, s.2 3 → **4**, s.3 3 → **4**. Sayfa 1'deki 5, kararın kendisi: gövdenin 1635 karakteri **kaynak puntosunda (10,5 pt)** kalıyor, sıkışık kısım 9,44'te; önceki turda hepsi 9,5'e iniyordu.

### Canlı sonuç (`docs/test_doc_output/v1_1_third/`)
Reflow exit 0 (10/10 chunk, 8991 kr) · overlay exit 0 (43/43 birim, 3/3 sayfa) · `placed 34 / shrunk 0 / could_not_fit 0 / kept 9` (önceki turla birebir) · `figures.json`'da `prefix_style: ["2.", true, false]` · `images/p002-f01.tr.png` yazıldı, İngilizce orijinal duruyor · `overlay_review.json` 3 → 7 girdi (fark: sayfa düzeyi `page_shared_scale` bilgi kayıtları).

### İncelemenin kendi bulgularında düzeltilenler
- `durum.md`'nin "`.tr.png` etiketleri sola yaslı" maddesi **ölçülerek çürütüldü** — ortalı etiketler gerçekten ortalı. Kayıp olan yalnız kalın önek (`prefix_style`), o da düzeltildi.
- CR-83 ölçümü: inceleme 335/899 (0,373) demişti, uygulamanın kendi ölçümü 326/890 (0,366). Bulgu değişmiyor; regresyon testi ölçülen değeri sabitliyor.
- CR-95 tek başına yeterli değildi (yukarıdaki yan etki) — `SHARED_SPREAD` ve yeniden gruplama onunla birlikte gerekti.

### QA'ya devredilen açık riskler
1. Yeni "metin sayfası" tetiği (`_PAGE_TEXT_FILL_RATIO`, doluluk ≥ %50) geometrik sezgi: etiketleri kendi alanının yarısından fazlasını kaplayan bir şema düşebilir. Ölçülen doluluklar — referans s.2: 0,146 · 40 etiketli şema: 0,19 · çerçeveli dizin sayfası: 0,755.
2. `_PAGE_LABEL_MAX_CHARS = 40`: 40 karakterden uzun etiket düzyazı sayılıyor.
3. `overlay_review.json` artık sayfa başına birden fazla `page_shared_scale` kaydı taşıyabiliyor; `counts.entries` ve `summary.json`'daki `overlay_review_entries` artıyor, CLI ham sayıyı basıyor.
4. Referans sayfanın iki sıkışık paragrafı 8,50 pt'de, gövde 9,11 pt'de — amaçlanan, ama gözle görülür iki puntolu sayfa.
5. CR-85'in **kural** yarısı yapılmadı (yalnız görünürlük): %60 raster kuralının kendisi değişmedi.
6. NOT DONE: CR-65, CR-66(b), CR-69, CR-71, CR-72, CR-73, CR-98. PARTIAL: CR-44, CR-48, CR-49 (~250 sayfa üstü kitap öncesi bakılmalı), CR-67, CR-77, CR-82. CR-99..113 öneriler alınmadı.

### Güncellenen dokümanlar
`docs/06_solution_design_v1_1.md` → §7.1 `export --input` satırı, §7.3 "Fix-round additions to the contract", §979 şekil render akışı, yeni **Addendum B** (B.1–B.5). `docs/04_code_review.md` → `Third pass — fix round` kapanış tablosu + canlı doğrulama. `README.md` → exit kodları ve `--input` davranışı (Q kolu).

### Faz B'de tasarımdan sapmalar (Code Reviewer için)
- `status()`/`export` tek job'ı `database.models.Job` üzerinden okur (Faz A'da "tek job'ı getir" metodu yok; read-only bağlantıda `get_or_create_job` çalışmaz). Öneri: `JobRepository.get_single_job()` eklenmesi.
- `pipeline/orchestrator.py` → `logging_setup` import eder (`chunk_context` için; yaprak modül, döngü yok).
- `glossary` aşaması `runs` satırı yazmaz; `extract`/`translate`/`export`/`run` yazar.
- Boş/kesik yanıt kuralı: 1. kez reschedule, 2. kez lenient tamamlama + review_flag, sağlayıcı-seviyesi `PROVIDER_EMPTY_RESPONSE` 3. kez FAILED; sayaç `last_error` ön ekiyle restart'a dayanıklı.
- `--fresh`: `extract`/`run` DB + tüm artefaktları arşivler; `translate(fresh=True)` sadece DB'yi arşivler, `source_book.md`/`glossary.json` kalır. `completed == 0` iken düzenlenmiş `source_book.md` otomatik yeniden chunk'lanır (O1).
- Heartbeat gerçek `asyncio.sleep` kullanır (aralık enjekte edilebilir).
- Provider binding `translator.name` ile karşılaştırılır. `--cleanup-remote` kabul edilir ama "uygulanmadı" der. `<output>/.env` fallback'i yok (sadece cwd `.env`).
- Açık sorular: redaction deseni UUID job id'lerini de mesaj içinde `***` yapar; uzun `extract` sırasında heartbeat yok (60 s stale → başka süreç kirayı alabilir); `--allow-non-english` yine `source_lang="EN"` gönderir (TranslationRequest değişikliği gerekir).

### Faz A'da tasarımdan sapmalar (Code Reviewer için)
- Extractor: görsel-only sayfa tamamen atlanır, placeholder üretilmez (E-04 literal). `EXTRACTION_FAILED` her iki extractor'da JOB_FATAL; marker→pymupdf fallback orchestrator'ın işi. Dil kararı `check_english()` ile orchestrator'da (`--allow-non-english` ExtractOptions'ta yok). `_pdf.py` tipli pymupdf sarmalayıcı. Harfli liste işaretleri (`a)`) `-` olur.
- Chunker: CODE/IMAGE/HR/TABLE veya oversize bloktan önceki başlık taşınamaz → kapanan chunk'ın kuyruğunda kalır, `heading_stranded:<sebep>` uyarısı (tasarım HEADING chunk'ı sadece EOF'ta öngörüyordu). Blok parser BT-Markdown'ın üst kümesi (soft-wrap paragraf, `+` liste, `***` hr kabul edilir). `provider_limit < min_chars` ise min düşürülür (uyarı).
- Segmenter: `Segmented.tail` alanı eklendi; tablo ayraç satırı sonraki segmentin prefix'inde.
- DeepL: SDK 1.32'de `Translator(max_retries=)` yok → `deepl.http_client.max_network_retries = 0` süreç geneli. SDK'nın 429 istisnası Retry-After taşımıyor (getattr ile okunur, fiilen None). Sınıflandırma dışı istisna → `INTERNAL/CHUNK_FATAL`.
- Local NMT: `apply_post_replace(text, glossary, source_text=None)`; prepare her zaman POST_REPLACE; miss uyarıları translate'te düşer (7.6 post-check kapsıyor).
- LLM base: batching için `[n] text` satır protokolü.
- Glossary: stop-list kuralı biraz daha sıkı (fonksiyon kelimesiyle başlayan/biten n-gram atılır); `--force` yeniden bulunmayan el girişlerini korur; boş glossary hash'i `sha256("")` (orchestrator "glossary yok" için None yazmalı).
- Exporter: DejaVu font paketlenmedi (`font_path` yoksa CSS font stack). Markdown ext listesine `md_in_html` eklendi. EPUB bellekte üretilip atomik yazılır; dipnotlar bölüm bazında bağlanır. EPUB metadata anahtarları: `job_id`, `title`, `author`, `source_file`.
- Assembler: translatable olmayan chunk'lar statüye bakılmaksızın kaynak metinle kopyalanır.

### Faz B'ye notlar
- Chunk `warnings` makine-okunur etiketler: `undersize:*`, `oversize:*`, `heading_stranded:*`.
- `AdaptiveLimiter`: `async with limiter:`; `release()` senkron; cooldown sırasında başarı sayacı sıfırlanır.
- `DeepLTranslator.create(settings, client_factory=...)`, `LocalNMTTranslator.create(settings, engine_factory=...)` test enjeksiyonu.
- `ChunkRepository.claim_pending(job_id, limit, now, run_id)`.

## Bilinmesi gerekenler
- Ortam: `.venv/Scripts/python.exe` (Python 3.11.9), SQLite 3.45.1. Kontrol komutları: `python -m pytest -q`, `python -m mypy src`, `python -m ruff check src tests`.
- Paket adı `book_translator`, kaynak dizin `src/` (hatchling eşlemesi + `hatch_build.py` editable hook'u). `src/` içinde relative import (`from ..domain.result import ...`); testler `book_translator.*` kullanır.
- Repository kullanımı: `open_database(path, tool_version=...)` → `JobRepository(db)`, `LeaseRepository(db)`, `ChunkRepository(db, run_id=..., glossary_hash=...)`. Tüm datetime timezone-aware; sadece `session.utcnow()`.
- `reschedule`/`fail`/`complete` → `Result[bool]`; `iter_ordered` düz iterator (hata fırlatabilir).
- Windows `datetime.now()` ~15 ms çözünürlük; orchestrator zamanı kesin artan saymamalı.
- Proje klasörü kendi git deposu değil (ev dizini depo görünüyor). `git init` kararı kullanıcıda.
- Editördeki Pyright "sqlalchemy bulunamadı" uyarıları `.venv` seçilmediğinden; gerçek hata değil.

---

## Gerçek kitap denemesi (2026-09-21) — `docs/test-2.pdf`

48 sayfalık LaTeX (Computer Modern) akademik kitap bölümü — Szeliski, *Computer Vision*. Çıktı: `docs/test2_output/`.

### Bulunan ve düzeltilen hata: kontrol karakterleri tüm batch'i düşürüyor
İlk koşuda **194 birim** `DeepL rejected the request (HTTP 400)` ile başarısız oldu. Kök neden: çıkarılan metinde XML 1.0'ın yasakladığı C0 kontrol karakterleri (U+0000, U+0001, U+0014, U+0015) vardı — Computer Modern matematik fontlarının karakter kodlarından geliyor. DeepL `tag_handling="xml"` ile çağrıldığı için ayrıştırıcı **tüm isteği** reddediyor: bu karakterler yalnız 20 birimde vardı ama 50 birimlik batch'leri tamamen düşürdüler.

**Düzeltme:** `translators/protect.py` → `strip_control_chars()`; `protect()` içinde (her iki mod) ve `deepl_translator` içinde `context` penceresine uygulanıyor. Saklanan kaynak metin değişmiyor, yani hash'ler ve kaynak PDF etkilenmiyor. Testler: `test_control_characters_never_reach_the_provider`, `test_deepl_strips_control_characters_from_the_context_window`. Düzeltmeden sonra `translate --retry-failed` → **194/194 tamamlandı, 0 başarısız**. Toplam 724 test.

### Sonuç
| | |
|---|---|
| Sayfa | 48 → 48, boyut 595×791 pt birebir |
| Birim | 1102 (816 çevrildi, 286 olduğu gibi) |
| DeepL | 127.707 karakter |
| Yerleşim | placed 816, shrunk 22, could_not_fit 2 |
| Görsel / çizim | 63 → 64 raster (s.8'de +1), 214 → 214 vektör |
| İnceleme girdisi | 546 (fragment 281, collateral_redaction 177, page_shared_scale 64, shrunk 22, could_not_fit 2) |

### YENİ BULGU (Blocker sayılmalı): matematik sayfalarında denklemler bozuluyor
Düzyazı sayfaları **çok iyi** (s.393 / çıktı s.9: görseller birebir, altyazı ve başlık çevrili, atıf bağlantıları duruyor, blok hizalama korunmuş). Ama matris/denklem içeren sayfalarda çıktı kullanılamaz durumda (s.415 / çıktı s.30):
- Display denklemleri (8.39), (8.40), (8.42) dağılıyor: parantez parçaları saçılıyor, öğeler yer değiştiriyor, **denklem numaraları kayboluyor**
- Satır içi matematik bozuluyor: `[x1 y1 1]` → `x¹ ⌊ veya x¹ ⌊`
- Alt indisler üst indise dönüyor: `K₁` → `K¹`, `R₁₀` → `R10`, `f₁` → `f¹`
- Matematik sembollerinin kalınlığı kayboluyor

Sebep: matematik, her biri ayrı overlay birimi olan çok sayıda küçük metin parçasından oluşuyor; bir kısmı "olduğu gibi bırakılıyor" ama komşusunun redaksiyonu onları kırpıyor — 177 `collateral_redaction` girdisinin kaynağı bu.

**Ölçek:** 48 sayfanın **20'si** yoğun bulgulu (≥8 girdi), 10'u tamamen temiz. Yani bu kitapta sayfaların ~%40'ı etkileniyor.

**Sonuç:** araç şu hâliyle **düzyazı ağırlıklı kitaplar için hazır**, **matematik/formül ağırlıklı kitaplar için değil**. Matematik birimlerinin tek bir dokunulmaz blok olarak ele alınması (redaksiyon dışı tutulması) gerekiyor — QA'ya ve sonraki tur tasarıma taşınmalı.

### Diğer gözlemler
- Üst bilgiler varsayılan olarak İngilizce kalıyor (`--overlay-translate-headers` ile çevrilir) — tasarım gereği
- Yazı tipi Computer Modern → Charis SIL değişiyor; `--pdf-font` ile kaynak görünüme yakın bir font verilebilir
- `s.8` çıktısında raster sayısı 1 artmış (63 → 64) — incelenmedi

---

## Matematik düzeltmesi (2026-09-21) — display denklem bantları

Yukarıdaki Blocker çözüldü. Çıktı: `docs/test2_math_out/` (4 sayfalık matematik alt kümesi `docs/test-2-math.pdf`).

### Kök neden (ölçüldü)
Denklemin *etrafından akan* paragrafın PyMuPDF blok kutusu denklemin tamamını içine alıyordu. Somut: `docs/test-2.pdf` s.30'da paragraf `y=(268,391)`, denklem (8.39) ise `y=299–358`. Paragraf çevrilip kutusu redakte edilince denklem siliniyordu. İkincil olarak denklem onlarca küçük `body` birimine parçalanıyor, harf içerenler çevriliyordu.

### Çözüm — `src/extractors/overlay_extractor.py`
Şekil bölgeleri ve tablo hücreleriyle aynı kalıpta bir **display matematik bandı** katmanı:

- `MATH_FONT_RE = CM(MI|SY|EX)|MSAM|MSBM|Math` — CMR/CMBX **bilinçli olarak dışarıda** (Computer Modern'in düz metin fontları)
- `_is_math_line`: metin fontunda ≥3 harflik kelime **yok** ve matematik fontunda karakter var → display matematik satırı. Satır içi matematik tetiklemez.
- Satırlar dikey bantlara kümeleniyor (eşik gövde satır yüksekliğinden türetiliyor), bant **tek bir birim** oluyor: `keep_reason="math"`, hiç gönderilmiyor, hiç redakte edilmiyor
- Sağ kenardaki denklem numarası (`(8.39)`) banda emiliyor
- **Kritik:** kalan satırlar bantlara göre segmentlere bölünüyor ve her segment ayrı gruplanıyor → hiçbir çevrilen birimin kutusu bir bandı kesmiyor

Uygulamadan gelen dört ek (hepsi ölçümle gerekçeli): `_hangs_off_prose` (satır içi alt/üst indisler PyMuPDF'te ayrı satır olarak geliyor ve fontça display satırından ayırt edilemiyor; dikey merkez testi yutulan düzyazı satırını 69'dan 17'ye indirdi), bandı sütun genişliğine yaymama (s.19'daki matris tablosunu bozuyordu), `_clip_off_bands` (fiziken çakışan kutuları banda girmeyecek şekilde kısaltıyor), tablo hücresine giren bandı düşürme.

### Şema v2 → v3
`keep_reason` CHECK kısıtına `"math"` eklendi. SQLite CHECK'i tablo DDL'inde tuttuğu için mevcut v2 dosyaları yeni değeri **reddederdi**. `CURRENT_SCHEMA_VERSION = 3`, `_upgrade_v2_to_v3` `overlay_blocks`'u satırları koruyarak yeniden inşa ediyor. `test_20_v2_file_widens_keep_reason_to_math` bunu kanıtlıyor (göç devre dışı bırakılınca test düşüyor — mutasyonla doğrulandı).

### Sonuç — aynı 4 sayfada önce/sonra
| Bulgu | Önce | Sonra |
|---|---|---|
| `collateral_redaction` | 90 | **18** |
| `fragment` | 82 | 48 |
| `could_not_fit` | 1 | **0** |
| Toplam inceleme girdisi | 179 | **77** |

Gözle: (8.39), (8.40), (8.41), (8.42) denklemleri parantezleri, alt indisleri ve denklem numaralarıyla **doğru** basılıyor. Öncesinde hiçbiri okunabilir değildi.

Belge geneli: birim 1102 → 900, çevrilecek 816 → 641, 61 bant / 22 sayfa, 501 kaynak satırı çeviri dışına alındı. **Çevrilen birim × bant kesişimi: 0.** `docs/test_doc.pdf` (matematiksiz) birebir aynı kaldı.

### Bilinen bedel
2560 satırın **17'si (%0,7)** bandın dikey aralığına düştüğü için İngilizce kalıyor (s.415'te "be re-written as", "or" ve bir paragraf satırı). Kaybolan denklemlere karşı bilinçli takas.

**Kapsam dışı sınır:** CMR/CMBX (metin fontu) ile dizilmiş matematik satırları bant olmuyor — s.19'daki (8.5) denkleminin bir kısmı hâlâ çevriliyor. Kuralı genişletmek düzyazıyı yutar.

### DeepL kontrol karakteri düzeltmesi
Aynı gün: `strip_control_chars()` (`translators/protect.py`) — XML 1.0'ın yasakladığı C0 karakterleri sağlayıcıya giden yükten ve context penceresinden temizleniyor. Bunlar olmadan 20 bozuk birim, 50'şerlik batch'leri düşürerek 194 birimi başarısız yapıyordu (`HTTP 400 Tag handling parsing failed`).

### Olay kaydı: test dosyası kaybı ve kurtarma
Orkestratör `tests/test_migration.py`'de hatalı metin dilimlemesiyle dosyanın bir bölümünü sildi (`test_18`, `test_18b`, `test_18c`, `build_v1_file` gövdesi ve 8 yardımcı). Depo git olmadığı için geri alınamadı. `tests/__pycache__/test_migration.cpython-311.pyc` bayt kodundan yeniden kuruldu (466 satır); kurtarma sırasında `.pyc` kopyaları `import` ile üzerine yazıldığı için **ikinci bir kurtarma şansı yok**. Yeniden kurulan testlerin gerçekten koruduğu mutasyonla doğrulandı (v1→v2 `ADD COLUMN` devre dışı → `test_18`/`test_18d`/`test_19` düştü). Bayt kodda iz bırakmayan yorum satırları yeniden yazıldı, kod satırlarının tamamı bayt koddan türetildi. **Ders: bu depo git altına alınmalı.**

### Doğrulama
730 test geçti / 1 skip, mypy strict 60 dosya, ruff temiz.
