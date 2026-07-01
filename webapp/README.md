# webapp/

Geliştirici arayüzü. Parser pipeline'ının her aşamasını (raw → parse → image handling →
table describe) adım adım, net ve sade bir şekilde gösterir.

- Dur-kalk modu: her aşama bir butona tıklanmayı bekler; aşama bitince durur, sonuçlar
  gösterilir; butona tekrar tıklanınca sıradaki aşamaya geçilir.
- Sonuçlar gezilebilir olmalı: örn. bir görsele referans veren metne tıklanınca ilgili
  görsele/konuma yönlenebilmeli.
- Tasarım: sade, az renkli, net, temiz, açıklayıcı.

Not: bu arayüz yalnızca parser aşamalarını kapsar; chunklama/RAPTOR aşamaları bu deponun
kapsamı dışındadır.
