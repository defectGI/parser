# storage/

Parser'ın çalışma zamanı verisi. Kod değil, veri klasörleridir.

- `raw/` — ham girdi dosyaları. İşlem bitince de silinmez, korunur.
- `parsed/` — parser'ların ürettiği ara IR çıktısı (`ParsedDocument`).
- `images/` — görsel blob store. sha256 (`image_id`) ile adreslenir, immutable ve dedup'lıdır
  (aynı görsel tekrar geçerse tek kopya tutulur).
- `db/` — SQLite. Görsel kayıtları ve tablo açıklama/LLM check durumları burada tutulur.

Not: chunk şeması bu depoya ait değildir; chunklama ayrı bir bileşenin sorumluluğundadır.
