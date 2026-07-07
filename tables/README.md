# tables/

Adds a description to the structured table blocks (full JSON, including merges) produced by
the format parsers.

- `table_describe.py` — generates a short, plain-text `table_description` (what the table
  shows) by looking at the table's formatted content and (optionally) the text before/after
  it. An optional LLM check verifies content + format; ones that fail are retried, and if
  still failing, are marked `describe_status="flagged"` and left as is. Model/provider
  agnostic: all calls go through the `LLMClient` in the `llm/` layer (a local model service or
  an API, same interface).

  The expected `table_description` format is defined in a single source (`FORMAT_SPEC`) and is
  injected verbatim into both the write and check prompts.

  Env flags (all optional):
  - `TABLE_LLM_CHECK` — if `1`, the verification + retry loop runs (default off).
  - `TABLE_CONTEXT` — if `1`, a heading snippet + surrounding paragraph context is added
    (default off).
  - `TABLE_CONTEXT_BEFORE` — number of paragraphs to take before the table (default 1).
  - `TABLE_CONTEXT_AFTER` — number of paragraphs to take after the table (default 1).
  - `TABLE_CONTEXT_MAX_CHARS` — context budget per paragraph (default 400).
  - `TABLE_CHECK_RETRIES` — max attempts while checking is on (default 3).

  LLM access is also configured via env (the `llm/` layer): `LLM_PROVIDER` (`openai` |
  `anthropic`), `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`.

Note: a table block is atomic (not split) at the chunking stage — that rule belongs to the
chunker; this repo is only responsible for structuring and describing the table.
