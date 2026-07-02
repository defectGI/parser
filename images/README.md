# images/

Parse sırasında IR'a konan görsel işaretlerini (`<imageN>`) işler.

- `image_handler.py` — işarete karşılık gelen görseli alır, OCR'a sokar, sonucu
  `ocr_output_control.py` ile doğrulatır; anlamlıysa düzeltilmiş metni IR'da işaretin yerine
  yazar. Anlamlı/anlamsız fark etmeksizin görseli sha256 (`image_id`) ile
  `storage/images/` blob store'da immutable ve dedup'lı saklar. Görselin kaydı (image_id,
  locator, ocr_text, ocr_meaningful, mime, width/height) IR'da `ImageBlock` üzerinde tutulur;
  `doc_id` ve `access_level` dokümandan gelir. Ayrı bir veritabanı yoktur.
- `ocr_output_control.py` — OCR çıktısının anlamlı olup olmadığını LLM'e sorar, yazım
  hatalarını ve format bozulmalarını düzeltir.

Kural: ham görsel byte'ı IR'ye gömülmez, sadece `image_id` referansı taşınır. OCR anlamsızsa
işaret IR'dan kaldırılır (indekslenmez) ama blob + IR'daki kayıt korunur.
