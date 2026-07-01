# parsers/

Her dosya formatı için ayrı bir parser modülü, ortak `BaseParser` sözleşmesine uyar ve
girdi dosyasını `ParsedDocument` IR'ına çevirir.

- `base.py` — `BaseParser` (soyut arayüz) ve `ParsedDocument` (IR) tanımları. Tüm format
  parser'ları bunu implemente eder.
- `registry.py` — dosya uzantısı/mimetype'a göre doğru parser'ı seçer.
- `docx_parser.py`, `pptx_parser.py`, `xlsx_parser.py`, `html_parser.py`, `pdf_parser.py`,
  `markdown_parser.py` — format bazlı implementasyonlar.

Notlar:
- markitdown kullanılmıyor: orijinal dokümandaki byte offset bilgisini korumuyor, IR bunu
  gerektiriyor.
- `pdf_parser.py` için özel durumlar henüz netleşmedi.
- Tablo blokları burada, merges dahil, yapılandırılmış JSON olarak IR'a yazılır; açıklama
  üretimi `tables/` modülünün işi.
- Görsel geçen yerlere `<imageN>` gibi bir işaret konur; işaretin doldurulması `images/`
  modülünün işi.
