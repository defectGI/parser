"""base.py boundary hardening: a malformed serialized IR fails loudly at
`from_dict`/`from_json` (IRParseError naming the path) instead of leaking a None
inward; a file newer than this reader is rejected; optional-but-absent fields are
NOT errors."""

from __future__ import annotations

import json

import pytest

from parsers.base import (
    IR_VERSION, IRParseError, ParagraphBlock, ParsedDocument, migrate_dict,
)


def _min_doc_dict(**over) -> dict:
    d = {"ir_version": IR_VERSION, "doc_id": "d", "source_path": "x",
         "fmt": "markdown", "blocks": []}
    d.update(over)
    return d


# --- happy path ---------------------------------------------------------------


def test_valid_roundtrip():
    doc = ParsedDocument(doc_id="d", source_path="x", fmt="markdown",
                         blocks=[ParagraphBlock(id="b0", text="hi")])
    back = ParsedDocument.from_json(doc.to_json())
    assert back.doc_id == "d"
    assert back.blocks[0].text == "hi"


def test_absent_optional_field_is_not_an_error():
    # heading_path/provenance/etc. omitted -> None, not a failure
    doc = ParsedDocument.from_dict(_min_doc_dict(
        blocks=[{"type": "paragraph", "id": "b0", "text": "hi"}]))
    assert doc.blocks[0].heading_path is None
    assert doc.blocks[0].provenance is None


# --- missing required fields fail with a located error ------------------------


def test_missing_doc_field_raises_with_path():
    bad = _min_doc_dict()
    del bad["doc_id"]
    with pytest.raises(IRParseError) as ei:
        ParsedDocument.from_dict(bad)
    assert "doc_id" in str(ei.value)


def test_block_missing_type_names_index():
    bad = _min_doc_dict(blocks=[{"id": "b0", "text": "hi"}])
    with pytest.raises(IRParseError) as ei:
        ParsedDocument.from_dict(bad)
    assert ei.value.path == "blocks[0]"
    assert "type" in ei.value.reason


def test_block_missing_required_field_names_index():
    # a heading with no 'id' -> _base_kwargs KeyError, surfaced as blocks[1]
    bad = _min_doc_dict(blocks=[
        {"type": "paragraph", "id": "b0", "text": "ok"},
        {"type": "heading", "text": "no id", "level": 1}])
    with pytest.raises(IRParseError) as ei:
        ParsedDocument.from_dict(bad)
    assert ei.value.path == "blocks[1]"
    assert "id" in ei.value.reason


def test_unknown_block_type_rejected():
    bad = _min_doc_dict(blocks=[{"type": "sparkle", "id": "b0"}])
    with pytest.raises(IRParseError) as ei:
        ParsedDocument.from_dict(bad)
    assert "sparkle" in str(ei.value)


# --- version gate / migration seam --------------------------------------------


def test_newer_version_rejected():
    with pytest.raises(IRParseError) as ei:
        migrate_dict(_min_doc_dict(ir_version=IR_VERSION + 1))
    assert "ir_version" in str(ei.value)


def test_non_int_version_rejected():
    with pytest.raises(IRParseError):
        migrate_dict(_min_doc_dict(ir_version="4"))


def test_from_json_rejects_future_file():
    text = json.dumps(_min_doc_dict(ir_version=IR_VERSION + 5))
    with pytest.raises(IRParseError):
        ParsedDocument.from_json(text)


def test_migrate_passes_current_version_through():
    d = _min_doc_dict()
    assert migrate_dict(d) is d  # no rewriting needed today
