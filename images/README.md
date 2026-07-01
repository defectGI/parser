# images/

Parse sırasında IR'a konan görsel işaretlerini (`<imageN>`) işler.

- `image_handler.py` — işarete karşılık gelen görseli alır, OCR'a sokar, sonucu
  `ocr_output_control.py` ile doğrulatır; anlamlıysa düzeltilmiş metni IR'da işaretin yerine
  yazar. Anlamlı/anlamsız fark etmeksizin görseli sha256 (`image_id`) ile
  `storage/images/` blob store'da immutable ve dedup'lı saklar, `storage/db/` içinde bir
  kayıt açar (image_id, doc_id, locator, ocr_text, ocr_meaningful, mime, width/height,
  access_level).
- `ocr_output_control.py` — OCR çıktısının anlamlı olup olmadığını LLM'e sorar, yazım
  hatalarını ve format bozulmalarını düzeltir.

Kural: ham görsel byte'ı IR'ye gömülmez, sadece `image_id` referansı taşınır. OCR anlamsızsa
işaret IR'dan kaldırılır (indekslenmez) ama blob + sqlite kaydı korunur.
