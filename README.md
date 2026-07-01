# parser

Farklı dosya formatlarını (docx, pptx, xlsx, html, pdf, markdown) ortak bir ara temsile
(IR — `ParsedDocument`) çeviren, görselleri OCR'dan geçirip anlamlılığını doğrulayan ve
tabloları yapılandırıp açıklayan modüler bir parser.

IR, JSON olarak serileştirilir (`storage/parsed/` altında `.json` dosyaları). Ham görsel
byte'ı bu JSON'a gömülmez, sadece `image_id` referansı taşınır.

Chunklama, RAPTOR ve chunk şeması bu deponun kapsamı dışındadır; parser yalnızca IR üretir.

## Pipeline

1. `storage/raw/` — girdi dosyası olduğu gibi korunur.
2. `parsers/` — dosya tipine uygun parser, ortak `ParsedDocument` IR'ına çevirir → `storage/parsed/`.
3. `images/` — parse sırasında konan `<imageN>` işaretlerini OCR'dan geçirir, anlamlılığını LLM
   ile doğrular, sonucu IR'da işaretin yerine yazar. Ham görsel `storage/images/` blob store'da
   sha256 (`image_id`) ile immutable ve dedup'lı saklanır.
4. `tables/` — yapılandırılmış tablo bloklarına kısa bir açıklama (`table_description`) ekler,
   sonucu LLM check'ten geçirir.
5. `storage/db/` — görsel kayıtları ve tablo açıklama/LLM check durumları SQLite'ta tutulur.
6. `webapp/` — geliştirici arayüzü; parser aşamalarını adım adım, dur-kalk modunda gösterir.

## Klasörler

- `parsers/` — format bazlı parser'lar + `BaseParser`/`ParsedDocument` sözleşmesi
- `images/` — `image_handler` ve `ocr_output_control`
- `tables/` — `table_describe`
- `storage/` — ham veri, parse çıktısı, görsel blob store, sqlite db
- `webapp/` — geliştirici arayüzü
