# images/

Parse sırasında IR'a konan görsel işaretlerini (`<imageN>`, birer `ImageBlock`) işler.

- `image_handler.py` — her `ImageBlock`'un `locator`'ına göre (docx/pptx/xlsx: zip media
  part; html/markdown: data-uri veya yerel dosya; pdf: sayfa render edilip bbox'tan kırpılır)
  ham görseli bulur, VLM ile OCR'a sokar, sonucu `ocr_output_control.py` ile doğrulatır;
  anlamlıysa düzeltilmiş metni `ImageBlock.ocr_text`'e yazar — yani IR'daki işaretin *kendi
  slotuna*, çevresindeki paragraf/hücre metnine değil (o metnin `Span`'i kaynakla byte-birebir
  kalmalı). Anlamlı/anlamsız fark etmeksizin görseli sha256 (`image_id`) ile
  `storage/images/` blob store'da immutable ve dedup'lı saklar. Görselin kaydı (image_id,
  locator, ocr_text, ocr_meaningful, mime, width/height) IR'da `ImageBlock` üzerinde tutulur;
  `doc_id` ve `access_level` dokümandan gelir. Ayrı bir veritabanı yoktur. Tablo hücresi
  içindeki görseller de (her derinlikte iç içe tablo dahil) işlenir. Uzak (http/https) `src`
  çözümlenmeden bırakılır — pipeline network isteği atmaz.
- `ocr_output_control.py` — OCR çıktısının anlamlı olup olmadığını LLM'e sorar, yazım
  hatalarını ve format bozulmalarını düzeltir.

Kural: ham görsel byte'ı IR'ye gömülmez, sadece `image_id` referansı taşınır. OCR anlamsızsa
`ocr_text` boş kalır (yalnızca `ocr_meaningful=False` işaretlenir) ama blob + IR'daki kayıt
korunur — işareti aramada/gösterimde çözmek (metne ikame etmek ya da düşürmek) okuma-zamanı
tüketicinin (webapp/chunker) işidir.
