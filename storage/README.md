# storage/

Parser'ın çalışma zamanı verisi. Kod değil, veri klasörleridir.

- `raw/` — ham girdi dosyaları. İşlem bitince de silinmez, korunur.
- `output/` — sonuç IR çıktısı (`ParsedDocument`), JSON olarak. Görsel/tablo zenginleştirme
  sonuçları da bu IR'a geri yazıldığı için nihai sonuç burada yaşar.
- `images/` — görsel blob store. sha256 (`image_id`) ile adreslenir, immutable ve dedup'lıdır
  (aynı görsel tekrar geçerse tek kopya tutulur).

Not: Ayrı bir veritabanı yoktur; tüm kayıt/durum IR JSON'ında tutulur. Chunk şeması da bu
depoya ait değildir; chunklama ayrı bir bileşenin sorumluluğundadır.
