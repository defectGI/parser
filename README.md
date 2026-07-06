# parser

Farklı dosya formatlarını (docx, pptx, xlsx, html, pdf, markdown) ortak bir ara temsile
(IR — `ParsedDocument`) çeviren, görselleri OCR'dan geçirip anlamlılığını doğrulayan ve
tabloları yapılandırıp açıklayan modüler bir parser.

IR, JSON olarak serileştirilir (`storage/output/` altında `.json` dosyaları). Görsel OCR'ı ve
tablo açıklaması gibi tüm zenginleştirme sonuçları ayrı bir veritabanı yerine doğrudan bu IR
JSON'ına geri yazılır. Ham görsel byte'ı JSON'a gömülmez, sadece `image_id` referansı taşınır.

Chunklama, RAPTOR ve chunk şeması bu deponun kapsamı dışındadır; parser yalnızca IR üretir.

Her format şu an ne yapabiliyor için: [`SCOPE.txt`](SCOPE.txt). Bilinen açık sorunların
kısa hatırlatma listesi için: [`EKSIKLER.txt`](EKSIKLER.txt).

## Pipeline

1. `storage/raw/` — girdi dosyası olduğu gibi korunur.
2. `parsers/` — dosya tipine uygun parser, ortak `ParsedDocument` IR'ına çevirir → `storage/output/`.
3. `images/` — parse sırasında konan `<imageN>` işaretlerini OCR'dan geçirir, anlamlılığını LLM
   ile doğrular, sonucu IR'da işaretin yerine yazar. Ham görsel `storage/images/` blob store'da
   sha256 (`image_id`) ile immutable ve dedup'lı saklanır; kaydın kendisi IR'da `ImageBlock`
   üzerinde durur.
4. `tables/` — yapılandırılmış tablo bloklarına kısa bir açıklama (`table_description`) ekler,
   sonucu LLM check'ten geçirir; sonuç IR'daki `TableBlock`'a yazılır.
5. `webapp/` — geliştirici arayüzü; parser aşamalarını adım adım, dur-kalk modunda gösterir.

## Klasörler

- `parsers/` — format bazlı parser'lar + `BaseParser`/`ParsedDocument` sözleşmesi
- `images/` — `image_handler` ve `ocr_output_control`
- `tables/` — `table_describe`
- `storage/` — ham veri (`raw/`), sonuç IR çıktısı (`output/`), görsel blob store (`images/`);
  bu üç yol hardcoded değil, `storage_paths.py` üzerinden `STORAGE_RAW_DIR` /
  `STORAGE_OUTPUT_DIR` / `STORAGE_IMAGES_DIR` env değişkenleriyle değiştirilebilir
  (bkz. `.env.example`) — yoksa buradaki dev-time varsayılanlara düşer.
- `webapp/` — geliştirici arayüzü
