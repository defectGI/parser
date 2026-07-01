# tables/

Format parser'ların ürettiği yapılandırılmış tablo bloklarına (merges dahil tam JSON)
açıklama ekler.

- `table_describe.py` — tablonun formatlı içeriğine ve önündeki/arkasındaki metne bakarak
  kısa bir `table_description` üretir (tablo ne gösteriyor). Sonuç LLM check'ten geçer;
  geçmeyenler 3 kez retry edilir, yine geçmezse işaretlenip bırakılır.

Not: tablo bloğu chunklama aşamasında atomiktir (bölünmez) — bu kural chunker'a ait, bu
depo yalnızca tablonun yapılandırılması ve açıklanmasından sorumludur.
