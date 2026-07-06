# parsers/

Her dosya formatı için ayrı bir parser modülü, ortak `BaseParser` sözleşmesine uyar ve
girdi dosyasını `ParsedDocument` IR'ına çevirir.

- `base.py` — `BaseParser` (soyut arayüz) ve `ParsedDocument` (IR) tanımları. Tüm format
  parser'ları bunu implemente eder.
- `registry.py` — dosya uzantısı/mimetype'a göre doğru parser'ı seçer.
- `docx_parser.py`, `pptx_parser.py`, `xlsx_parser.py`, `html_parser.py`, `pdf_parser.py`,
  `markdown_parser.py` — format bazlı implementasyonlar.

Notlar:
- Her formatın şu an ne yapabildiği için: kök dizindeki `SCOPE.txt`. Bilinen açık
  sorunların kısa hatırlatma listesi için: `EKSIKLER.txt`.
- markitdown kullanılmıyor: orijinal dokümandaki byte offset bilgisini korumuyor, IR bunu
  gerektiriyor.
- `pdf_parser.py` aşağıdaki "PDF pipeline" akışını implemente eder. LLM erişimi model ve
  provider agnostiktir: `llm.get_vlm_client()` (env: `VLM_*`, `LLM_*`'a düşer; ikinci
  doğrulayıcı model `VLM2_*`). VLM konfigüre edilmemişse parser zarifçe geriler: hybrid
  sayfa code path'e düşer, scanned sayfa tam-sayfa `ImageBlock` olarak kalır (OCR'ı
  `images/` aşaması yapar).
- Tablo blokları burada, merges dahil, yapılandırılmış JSON olarak IR'a yazılır; açıklama
  üretimi `tables/` modülünün işi.
- Görsel geçen yerlere `<imageN>` gibi bir işaret konur; işaretin doldurulması `images/`
  modülünün işi.
- Listeler ayrı bir konteyner blok değildir: IR düz bir blok akışıdır, liste üyeliği sıradan
  bloklara metadata olarak yazılır (`list_id` / `list_level` / `list_ordered`). Böylece bir
  liste öğesinin içindeki tablo/görsel gerçek bir `TableBlock`/`ImageBlock` olarak korunur
  (düz metne ezilmez); bu, docx (`w:numId`/`w:ilvl`) ve pdf'in listeyi sakladığı yapıyla da
  hizalıdır. Bir "liste", aynı `list_id`'yi paylaşan bloklar gruplanarak yeniden kurulur.

## PPTX pipeline

`pptx_parser.py` slaytlari shape-shape gezer (`_walk_shapes`), group shape'lere
ozyinelemeli iner (PowerPoint'te birden fazla sekil gruplanabiliyor; ozyineleme
olmadan grup icindeki metin/tablo/gorsel tamamen kayboluyor).

- **Baslik**: gercek TITLE/CENTER_TITLE/VERTICAL_TITLE placeholder'i tanimlidir
  ve her zaman kazanir (tam enum eslesmesi - eski "TITLE" alt-string kontrolu
  SUBTITLE'i da yanlislikla yakaliyordu, duzeltildi). Placeholder yoksa (serbest
  text-box ile "baslik gibi" tasarlanmis slayt), docx'teki pseudo-header
  mantiginin ayni deseni calisir: slaytin placeholder-olmayan aday paragraflari
  `PptxHeadingConfig` ile puanlanir (bold/caps/title-case/ortalanmis/kisa/
  isolation/altcizili/font-orani agirliklari + esik), esigi gecen tek en yuksek
  puanli aday HeadingBlock'a yukseltilir (bir slaytta bir baslik olur). Saf-metin
  ipuclari `heading_heuristics.py` uzerinden docx ile paylasilir; font/bold/
  altcizili/italik sinyalleri hem run'in kendi rPr'inden hem paragrafin
  varsayilanindan (a:pPr/a:defRPr) okunur.
- **Inline formatting**: `_walk_pptx_para` her `a:p`'yi sirayla gezip a:r/a:fld'yi
  InlineRun'a cevirir (Mark: bold/italic/underline/strike/superscript/subscript),
  OOXML'in kendi resolution kuraliyla (run kendi rPr'sinde bir ozelligi set
  etmemisse paragrafin a:pPr/a:defRPr'ina duser - docx'teki `_walk_para`'nin
  aynisi). a:br (yumusak satir sonu) isaretsiz bir bosluga cevrilir; python-pptx
  bunu ham `\x0b` (dikey tab) olarak donduruyor, temizlenmezse kontrol karakteri
  basliklar dahil metne sizabiliyordu.
- **Listeler**: docx'teki gibi ayri konteyner degil, `list_id` (slayt+shape
  bazli) / `list_level` (paragraf indent) / `list_ordered` (a:buAutoNum vs
  a:buChar) metadata'si olarak bloklara yazilir.
- **Tablolar**: `TableBlock`, merge'ler dahil (gridSpan/rowSpan, python-pptx'in
  merge-cell API'siyle okunur). Hucreler hala duz metin (`text_cell`) - docx'teki
  `Cell.blocks` paralleli (hucre ici runs/nested tablo/gorsel) henuz yok.
- **Gorseller**: PICTURE shape -> `ImageBlock` (locator = media part path).
- **Gomulu OLE nesneleri** (orn. slayta yapistirilmis bir Excel tablosu):
  OOXML'in zorunlu tuttugu raster onizleme (`mc:Fallback/p:oleObj/p:pic/
  blipFill/a:blip`, ya da AlternateContent olmadan dogrudan `p:oleObj/p:pic`)
  `ImageBlock` olarak cikarilir; `alt_text`'e `ole_format.prog_id` yazilir (orn.
  "Embedded object (Excel.Sheet.12)"). Onizleme genelde EMF (vektor metafile,
  `image/x-emf`) formatinda gelir - PNG/JPEG degil; `images/` asamasinin bunu
  once rasterize etmesi gerekir, aksi halde OCR/vision modeli okuyamaz.
  Onizleme yoksa (nadir) blok hic uretilmez.
- **Speaker notes**: `slide.notes_slide` varsa ayri bir `ParagraphBlock`,
  `Span(part="ppt/notesSlides/notesSlideN.xml")` ile govdeden ayrilir; tuketiciler
  span.part'a bakarak govde/not ayrimi yapabilir.
- **Locator**: shape'lerin anlamli byte offset'i yok; `Span(part="ppt/slides/
  slideN.xml", page=N)` kullanilir (Karar B best-effort, PDF pipeline'daki gibi).

### Bilinen v1 sinirlari

- Tablo hucreleri duz metin (`text_cell`); hucre ici formatting/nested-tablo/
  gorsel modellenmiyor.
- OLE onizlemeleri EMF olarak gelebiliyor; rasterize edilmeden OCR/vision
  asamasindan gecemiyor.

## PDF pipeline

Diğer formatların aksine PDF tek bir lossless yol izlemez; kaynağa göre üç yola ayrılır ve
VLM'in ürettiği içerik bağımsız bir kaynakla doğrulanır. `pdf_parser.py` bunu implemente
eder; triage sayfa bazında yapılır (bir PDF taranmış ve dijital sayfaları karıştırabilir),
sayfa bazlı karar `metadata["pdf_pages"]`'e yazılır. Provenance etiketleri IR'da
`Block.provenance` / `Block.source_crop` alanlarında taşınır (bkz. `base.py`).

### 0. Triage

pdfplumber ile text layer var mı, coverage oranı ne?

- Text layer yok → **Scanned path**
- Text layer var + basit layout → **Code path**
- Text layer var + karmaşık bölge (tablo/multi-column tespiti) → **Hybrid path**

### 1. Code path (born-digital, basit)

pdfplumber → IR block'ları. Lossless, determinist. `text-layer-verified` etiketi. Bitti.

### 2. Hybrid path (born-digital, karmaşık)

1. Sayfayı render et
2. VLM'e ver, text layer'ı prompt'a grounding olarak ekle (document anchoring)
3. Çıktıyı text layer'la fuzzy-match
4. Eşleşen → `text-layer-verified`, eşleşmeyen → Doğrulama'ya

### 3. Scanned path

1. Render
2. Text detector (bbox'lar)
3. VLM okuma
4. Doğrulama'ya

### 4. Doğrulama (text layer'sız içerik için)

- Her VLM satırı bir detector bbox'ına map olmalı; map olmayan = uydurma şüphesi
- Şüpheli/kritik bölgeler: crop-and-reread ikinci modelle, fuzzy-match
- Domain regex (part number, birim, tarih)
- Uyuşan → `consensus-verified`, uyuşmayan → **uyarı, otomatik resolve yok**

### 5. Çıktı

IR block'ları + her block'a provenance etiketi + `unverified` olanlara kaynak crop referansı
(blob store hash). Citation pipeline trust seviyesini buradan okur.

**Özet:** Ucuzsa kodla oku, mecbursan VLM'le oku, VLM'in her dediğini bağımsız bir kaynakla
kontrol et, edemediğini etiketleyip crop'uyla sakla.

### Konfigürasyon

Modeller (hepsi opsiyonel; hiçbiri yoksa parser deterministik fallback'lerle çalışır):

- `VLM_*` — birincil vision modeli (`VLM_PROVIDER/MODEL/BASE_URL/API_KEY`; eksik olan her
  değişken `LLM_*` karşılığına düşer).
- `VLM2_*` — şüpheli bölgeleri crop-and-reread ile doğrulayan bağımsız ikinci model.
  Bilerek fallback'i yoktur: doğrulayıcının birincil modelden bağımsız seçilmesi gerekir.
- Scanned-path text detector: `pytesseract` kuruluysa otomatik kullanılır
  (`PDF_TESSERACT_LANG` ile dil); değilse doğrulama VLM2'ye yaslanır.

Ayarlar: `PDF_VLM=0` (tüm VLM kullanımını kapat), `PDF_RENDER_DPI` (150),
`PDF_CROP_DIR` (storage/images), `PDF_VLM_MAX_TOKENS` (8192), `PDF_CONTAINMENT` (0.9),
`PDF_LINE_MATCH` (0.8).

### Tablo merge'leri

pdfplumber rowspan/colspan vermez, ama tespit ettiği hücre bbox'ları (`table.cells`)
merge bilgisini geometride taşır: birleşik bir hücre tek, birden fazla ızgara
sınırını kapsayan bbox olarak görünür; kapladığı diğer ızgara konumlarının bbox'ı
yoktur. Bu ayrımdan (kapsanan boş vs. gerçekten kenarlıksız/boş hücre) `Merge`
kayıtları deterministik olarak çıkarılır (`_table_to_data`). Hybrid (VLM) yolda da
aynı geometrik tablo kullanılır: VLM'in "table" bloğu sayfadaki pdfplumber
tablosuyla sırayla (reading-order) eşleştirilir ve VLM'in düz JSON grid'i yerine
gerçek text-layer'dan gelen merge-aware tablo konur — sayı uyuşmazsa eşleşmeyen
tablo VLM'in kendi grid'ine düşer.

### Başlık seviyesi

Code path'te bir satırın başlık olup olmadığı kendi sütununun/sayfasının medyan
gövde metni büyüklüğüne göre yerel olarak karar verilir (`_is_heading`) — bu
değişmedi. Ama hangi seviyeye (1-6) denk geldiği artık **doküman geneli** bir
font-büyüklüğü sıralamasıyla belirlenir: `parse()`, sayfaları asıl işlemeden
önce tüm "code" ve "hybrid" (VLM'siz kalırsa code path'e düşebileceği için o
da dahil) sayfaları tarayıp tüm başlık-adayı büyüklükleri tek bir kümede
toplar (`_page_heading_candidates`), sıralar, ve bu ortak listeyi her sayfaya
geçirir (`_specs_from_lines`'a `heading_sizes` parametresi olarak). Böylece
50. sayfadaki bir bölüm başlığı, o sayfada ondan büyük başka bir şey olmadığı
için yanlışlıkla level 1'e terfi etmez — 1. sayfadaki daha büyük bir başlık
varsa level 2 (veya daha aşağı) kalır, doküman genelinde tutarlı bir hiyerarşi
çıkar. Hybrid sayfalarda VLM başarıyla okuduysa başlık seviyesi hâlâ VLM'in
kendi tahminidir (bu ayrı bir mekanizma); bu sıralama sadece VLM'siz/başarısız
kalıp code path'e düşen hybrid sayfalar için devreye girer.

### Inline formatting (runs)

Bold/italic her zaman kelimenin kendi font adından okunur (ör. "Arial-BoldMT")
— docx'te bir run'ın rPr'ini okumanın PDF karşılığı, determinist, VLM
tahmini değil. Code path'te bu doğrudandır (`_word_marks`/`_line_runs`).
Hybrid path'te sayfanın kendi kelime+font akışı (`_word_marks_stream`, code
path'in sütun/gutter mantığıyla aynı reading-order) çıkarılır; bir blok metni
zaten text layer'a karşı doğrulanıp `text-layer-verified` olduysa, o metnin
kendi kelimeleri bu akışa **ileri yönlü, hiç geri sarmayan** bir işaretçiyle
hizalanır (`_align_runs`) ve eşleşen kelimeler kendi font marks'ını alır.
Doğrulanamamış (`consensus-verified`/`unverified`) bloklar hizalamaya hiç
girmez — metni text layer'la zaten örtüşmüyor, hizalama da güvenilir olmaz.
Scanned sayfada (text layer'ın kendisi yok) `runs` doldurulmaz; doğrulanacak
bağımsız bir font kaynağı yok, bu kapsam dışı kalır.

### Bilinen v1 sınırları

- Hem hybrid hem scanned sayfada VLM'in "figure" dediği ama pdfplumber'ın nesne
  modelinde karşılığı bulunamayan figürler (ör. vektör çizim, taranmış arka
  plan raster'ına gömülü alt-figür) `unverified` bir `ImageBlock` olarak kalır;
  VLM kendi bbox tahminini verdiyse audit crop ona göre dar tutulur, vermediyse
  hiç crop üretilmez (tam sayfa dökümü yanıltıcı olacağından tercih edilmez).
- Hybrid sayfada VLM'in figür olarak saymadığı (VLM'in eksik saydığı) gömülü
  görseller sayfa akışının sonuna, konumsuz ekleniyor; VLM'in figür dediği ve
  pdfplumber'daki bir raster'la eşleşenler ise hem VLM'den gelen reading-order
  konumunu hem pdfplumber'dan gelen gerçek bbox'ı birlikte alıyor.
- Scanned sayfada `runs` (inline formatting) doldurulmaz (yukarıya bkz.).
- `Span`'de byte offset yok, sadece sayfa numarası.
