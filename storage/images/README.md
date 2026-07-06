# storage/images/

Görsellerin immutable blob store'u. Adresleme sha256 (`image_id`) ile yapılır; aynı görsel
tekrar geçerse tek kopya tutulur (dedup). Ham görsel byte'ı parse IR'ına gömülmez, sadece
buradan `image_id` referansıyla taşınır.
