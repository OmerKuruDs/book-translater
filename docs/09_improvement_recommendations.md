# Geliştirme Önerileri

Kaynak: kod incelemesi (`docs/durum.md`, `translators/`, `pipeline/orchestrator.py`) ve sektör araştırması
(BabelDOC, PDFMathTranslate, DocLayout-YOLO, PP-DocLayout, DelTA). Etki/efor tahminleri kabaca, ölçülmemiştir.

Not: BabelDOC ve PDFMathTranslate AGPL-3.0'dır; yalnızca algoritma fikri alınır, kod kopyalanmaz.
BabelDOC dizgi sabitleri (0.05/0.10 adım, 1.5→1.4→1.1 satır aralığı, 0.6/0.7 eşikleri) doküman kaynaklıdır;
kendi kitabımızda ölçülerek doğrulanmalı.

## Karar gerektiren
- [ ] **Lisans**: `pyproject.toml` MIT diyor, README "lisans yok" diyor, `LICENSE` yok, PyMuPDF AGPL-3.0.
      Seçenek A: projeyi AGPL-3.0 yap. Seçenek B: PyMuPDF'ten çık (pdfminer.six + pypdfium2, ~15-25 gün).
- [ ] Python sürümü: README 3.11+, `pyproject.toml` `>=3.10`; birini seç.

## Öncelik 1 — ölçüm ve güvenlik ağı (etki: yüksek, efor: düşük)
- [ ] Sayfa kalite skoru: UTB (çevrilmemiş blok), kutu taşması, punto varyansı, SSIM; `overlay_review.json` üzerine kur.
- [ ] Sentetik, telifsiz test PDF'leri (reportlab/LaTeX: denklem, içindekiler, şekil); şu an ~75 test fixture eksikliğinden düşüyor.
- [ ] GitHub Actions CI: pytest, mypy, ruff, görsel regresyon.

## Öncelik 2 — kalite (etki: yüksek)
- [ ] Matematik tespiti: font adı + font-boyutu varyansı + baseline kayması (CMR/CMBX kaçağı, %0,7 İngilizce kalan satır).
      Regex'i tek başına genişletme: düzyazıyı yutuyor (`durum.md`).
- [ ] İkinci geçiş LLM çevirmeni: yalnızca `glossary_miss` ve matematik içeren birimler; sözlük prompt'a enjekte edilir.
- [ ] Terim belleği (DelTA benzeri proper-noun kaydı) ile belge boyunca tutarlılık.
- [ ] Yerleşim: paragraf başına (lokal) iteratif ölçekleme; önce bbox'ı yazma yönünde genişlet, sonra ölçeği azalt.
- [ ] Font eşleme: Charis SIL yerine kaynağa yakın açık font (Computer Modern → Latin Modern / CMU / STIX).

## Öncelik 3 — kapsam ve verimlilik
- [ ] Çeviri belleği (SQLite/TMX): birebir eşleşme önbelleği, sonra bulanık eşleşme; DeepL maliyetini düşürür.
- [ ] Taranmış PDF: OCRmyPDF ile metin katmanı, ardından mevcut overlay hattı.
- [ ] Yerleşim modeli (yalnızca varyans yöntemi yetmezse): PP-DocLayout-S (Apache-2.0, CPU'da ~14 ms/sayfa).
- [ ] İnsan-döngüde inceleme arayüzü (yan yana düzeltme, düzeltmeleri TM ve sözlüğe geri besleme).

## Mühendislik
- [ ] `pipeline/orchestrator.py` (4153 satır) bileşenlere bölünsün: lease/heartbeat, extract, translate, export, özet.
- [ ] `overlay_extractor.py`, `pymupdf_extractor.py`, `cli.py` (1200-1500 satır) için aynı değerlendirme.
- [ ] Sürüm (`0.1.0`) ve ilk release; dil çifti soyutlaması (EN→TR dışı).

## Önerilen sıra
Lisans kararı → Öncelik 1 → matematik tespiti → LLM ikinci geçiş + TM → dizgi/font → OCR ve arayüz.
