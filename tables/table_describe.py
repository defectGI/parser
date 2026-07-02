"""Add a short `table_description` to every TableBlock via an LLM.

Pipeline per table:
    1. render the structured table to plain text (merges noted),
    2. optionally gather nearby context (heading breadcrumb + surrounding
       paragraphs) when TABLE_CONTEXT is on,
    3. ask the model for a short, plain-text description,
    4. optionally verify content + format with a second LLM call and retry up
       to TABLE_CHECK_RETRIES times, feeding the rejection reason back.

The description and check both reference the SAME canonical FORMAT_SPEC so the
writer and the checker agree on what "correct" means. Everything model- and
provider-specific lives behind `llm.LLMClient`.

Environment flags (all optional, sensible defaults):
    TABLE_LLM_CHECK         "1" to run the verify+retry loop (default off)
    TABLE_CONTEXT           "1" to include surrounding context (default off)
    TABLE_CONTEXT_BEFORE    paragraphs to include before the table (default 1)
    TABLE_CONTEXT_AFTER     paragraphs to include after the table (default 1)
    TABLE_CONTEXT_MAX_CHARS per-paragraph context budget (default 400)
    TABLE_CHECK_RETRIES     max description attempts when checking (default 3)
"""

from __future__ import annotations

import json
import os
import re

from llm import LLMClient, get_client
from parsers.base import (
    HeadingBlock,
    ParagraphBlock,
    ParsedDocument,
    TableBlock,
    TableData,
)

# The single source of truth for what a good description looks like. Injected
# verbatim into both the writer prompt and the checker prompt.
FORMAT_SPEC = (
    "The description must be:\n"
    "- Plain text only: no markdown, no line breaks, no bullet points, no code.\n"
    "- Written in the same language as the table and its context.\n"
    "- 1 to 3 sentences, at most about 60 words.\n"
    "- A summary of what the table is about and its main dimensions "
    "(what the rows and columns represent).\n"
    "- Faithful: never invent numbers, totals or facts that are not in the table, "
    "and do not enumerate every cell."
)

_DESCRIBE_SYSTEM = (
    "You write concise descriptions of tables so they can be found by search. "
    "Output only the description itself, with no preamble or quotes.\n" + FORMAT_SPEC
)

_CHECK_SYSTEM = (
    "You verify a candidate description of a table. Judge two things "
    "independently: content (is it faithful to the table and does it correctly "
    "say what the table is about?) and format (does it obey the rules below?).\n"
    + FORMAT_SPEC
    + '\n\nReturn ONLY a JSON object, no other text: '
    '{"content_ok": true/false, "format_ok": true/false, "reason": "<short reason if anything is false>"}.'
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    try:
        return int(val)
    except ValueError:
        return default


def render_table(table: TableData) -> str:
    """Render a TableData to a compact pipe-delimited text block.

    Covered (merged-away) cells render empty; merge regions are listed after the
    grid so the model can reason about spans without us duplicating content.
    """
    lines = []
    for row in table.cells:
        lines.append(" | ".join("" if cell is None else str(cell) for cell in row))
    text = "\n".join(lines)
    if table.merges:
        spans = "; ".join(
            f"cell (row {m.row}, col {m.col}) spans {m.rowspan}x{m.colspan}"
            for m in table.merges
        )
        text += f"\n[merged: {spans}]"
    return text


def _heading_breadcrumb(blocks: list, idx: int) -> str:
    """Ancestor heading chain preceding the block at `idx`, e.g. 'A > B > C'."""
    chain: list[str] = []
    needed_level: int | None = None
    for block in reversed(blocks[:idx]):
        if isinstance(block, HeadingBlock):
            if needed_level is None or block.level < needed_level:
                chain.append(block.text.strip())
                needed_level = block.level
                if block.level == 1:
                    break
    chain.reverse()
    return " > ".join(t for t in chain if t)


def _collect_paragraphs(blocks: list, indices, count: int, max_chars: int) -> list[str]:
    """Up to `count` non-empty paragraph texts in the order `indices` is walked."""
    out: list[str] = []
    if count <= 0:
        return out
    for i in indices:
        block = blocks[i]
        if isinstance(block, ParagraphBlock) and block.text.strip():
            out.append(block.text.strip()[:max_chars])
            if len(out) >= count:
                break
    return out


def build_context(blocks: list, idx: int, max_chars: int,
                  before: int = 1, after: int = 1) -> str:
    """Heading breadcrumb + up to `before`/`after` surrounding paragraphs.

    Paragraphs are always listed in document order (nearest ones included first
    when the budget is small).
    """
    parts = []
    breadcrumb = _heading_breadcrumb(blocks, idx)
    if breadcrumb:
        parts.append(f"Section: {breadcrumb}")

    # Walk outward from the table, then restore document order for readability.
    prev = _collect_paragraphs(blocks, range(idx - 1, -1, -1), before, max_chars)
    for text in reversed(prev):
        parts.append(f"Text before: {text}")

    nxt = _collect_paragraphs(blocks, range(idx + 1, len(blocks)), after, max_chars)
    for text in nxt:
        parts.append(f"Text after: {text}")

    return "\n".join(parts)


def _describe(client: LLMClient, table_text: str, context: str, hint: str) -> str:
    user = ""
    if context:
        user += f"Context around the table:\n{context}\n\n"
    user += f"Table:\n{table_text}\n\nWrite the description now."
    if hint:
        user += (
            f"\n\nA previous attempt was rejected for this reason: {hint}\n"
            "Write a new description that fixes it."
        )
    return client.complete(system=_DESCRIBE_SYSTEM, user=user, max_tokens=300).strip()


def _check(client: LLMClient, table_text: str, context: str, description: str) -> dict:
    user = ""
    if context:
        user += f"Context around the table:\n{context}\n\n"
    user += (
        f"Table:\n{table_text}\n\n"
        f"Candidate description:\n{description}\n\n"
        "Return your JSON verdict."
    )
    raw = client.complete(system=_CHECK_SYSTEM, user=user, max_tokens=200)
    match = _JSON_OBJECT.search(raw)
    if not match:
        return {"content_ok": False, "format_ok": False, "reason": "checker returned no JSON"}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"content_ok": False, "format_ok": False, "reason": "checker returned invalid JSON"}
    return parsed


def describe_tables(doc: ParsedDocument, client: LLMClient | None = None) -> ParsedDocument:
    """Fill `table_description` (and, when checking, describe_status/attempts).

    Mutates `doc` in place and returns it. No-op when the document has no
    tables — so no client/network is needed for table-free documents.
    """
    table_indices = [i for i, b in enumerate(doc.blocks) if isinstance(b, TableBlock)]
    if not table_indices:
        return doc

    if client is None:
        client = get_client()

    use_check = _env_bool("TABLE_LLM_CHECK")
    use_context = _env_bool("TABLE_CONTEXT")
    max_chars = _env_int("TABLE_CONTEXT_MAX_CHARS", 400)
    before = max(0, _env_int("TABLE_CONTEXT_BEFORE", 1))
    after = max(0, _env_int("TABLE_CONTEXT_AFTER", 1))
    max_attempts = max(1, _env_int("TABLE_CHECK_RETRIES", 3)) if use_check else 1

    for idx in table_indices:
        block: TableBlock = doc.blocks[idx]
        table_text = render_table(block.table)
        context = (
            build_context(doc.blocks, idx, max_chars, before, after)
            if use_context
            else ""
        )

        hint = ""
        passed = False
        attempts = 0
        description = ""
        while attempts < max_attempts:
            attempts += 1
            description = _describe(client, table_text, context, hint)
            if not use_check:
                break
            verdict = _check(client, table_text, context, description)
            passed = bool(verdict.get("content_ok")) and bool(verdict.get("format_ok"))
            if passed:
                break
            hint = str(verdict.get("reason") or "").strip()

        block.table_description = description
        if use_check:
            block.describe_status = "ok" if passed else "flagged"
            block.describe_attempts = attempts

    return doc
