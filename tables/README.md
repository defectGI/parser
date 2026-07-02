# tables/

Format parser'ların ürettiği yapılandırılmış tablo bloklarına (merges dahil tam JSON)
açıklama ekler.

- `table_describe.py` — tablonun formatlı içeriğine ve (opsiyonel) önündeki/arkasındaki
  metne bakarak kısa, düz-metin bir `table_description` üretir (tablo ne gösteriyor).
  Opsiyonel LLM check içerik + formatı doğrular; geçmeyenler retry edilir, yine geçmezse
  `describe_status="flagged"` ile işaretlenip bırakılır. Model/sağlayıcıdan bağımsızdır:
  tüm çağrılar `llm/` katmanındaki `LLMClient` üzerinden gider (yerel model servisi ya da
  API, aynı arayüz).

  Beklenen `table_description` formatı tek bir kaynakta (`FORMAT_SPEC`) tanımlıdır ve hem
  yazma hem de check prompt'una aynen enjekte edilir.

  Env bayrakları (hepsi opsiyonel):
  - `TABLE_LLM_CHECK` — `1` ise doğrulama + retry döngüsü çalışır (varsayılan kapalı).
  - `TABLE_CONTEXT` — `1` ise başlık kırıntısı + önceki/sonraki paragraf bağlama eklenir
    (varsayılan kapalı).
  - `TABLE_CONTEXT_BEFORE` — tablodan önce alınacak paragraf sayısı (varsayılan 1).
  - `TABLE_CONTEXT_AFTER` — tablodan sonra alınacak paragraf sayısı (varsayılan 1).
  - `TABLE_CONTEXT_MAX_CHARS` — paragraf başına bağlam bütçesi (varsayılan 400).
  - `TABLE_CHECK_RETRIES` — check açıkken azami deneme sayısı (varsayılan 3).

  LLM erişimi de env'den ayarlanır (`llm/` katmanı): `LLM_PROVIDER` (`openai` |
  `anthropic`), `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`.

Not: tablo bloğu chunklama aşamasında atomiktir (bölünmez) — bu kural chunker'a ait, bu
depo yalnızca tablonun yapılandırılması ve açıklanmasından sorumludur.
