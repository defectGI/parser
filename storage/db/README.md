# storage/db/

SQLite veritabanı. İçerik:

- Görsel kayıtları: `image_id`, `doc_id`, `locator`, `ocr_text`, `ocr_meaningful`, `mime`,
  `width`/`height`, `access_level` (dokümandan miras).
- Tablo açıklama/LLM check durumları: retry sayacı, geçti/geçmedi işareti.

Not: chunk şeması burada değil — chunklama ayrı bir bileşenin sorumluluğudur.
