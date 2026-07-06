# storage/output/

Nihai IR (`ParsedDocument`), JSON dosyası (`{doc_id}.json`) olarak burada tutulur. Parser
çıktısı önce buraya yazılır; `images/` ve `tables/` aşamalarının zenginleştirmeleri (OCR
metni, tablo açıklaması, LLM check durumu vb.) aynı IR'a geri işlenir. Ayrı bir veritabanı
yoktur — tüm sonuç ve durum bu dosyalarda yaşar.

Ham görsel byte'ı buraya gömülmez; görsel yalnızca `image_id` (sha256) ile referanslanır,
byte'ın kendisi `storage/images/` blob store'unda tutulur.
