"""Judges whether a VLM's raw image transcription is meaningful (real
content, not noise from a logo/icon/decorative graphic) and cleans up
spelling/formatting mistakes in the transcription before it's kept as an
ImageBlock's `ocr_text`.
"""

from __future__ import annotations

import json
import re

from llm import LLMClient

_SYSTEM = (
    "You review a raw text transcription of an image. Decide whether the "
    "text is meaningful content (e.g. a sentence, a label, a caption, data) "
    "as opposed to noise from a decorative graphic, logo or icon with no "
    "real textual content. If meaningful, also fix any obvious OCR/spelling "
    "or formatting mistakes without changing the meaning or adding anything "
    "not present in the original.\n\n"
    "Return ONLY a JSON object, no other text: "
    '{"meaningful": true/false, "cleaned_text": "<corrected text, or "" if not meaningful>"}.'
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def check_ocr_text(client: LLMClient, raw_text: str) -> tuple[bool, str]:
    """Return (meaningful, cleaned_text).

    On any model/parse failure, treat the text as not meaningful rather than
    risk indexing noise -- the blob and the raw ImageBlock record are kept
    either way (see images/README.md); only `ocr_text` is withheld.
    """
    user = f"Raw transcription:\n{raw_text}\n\nReturn your JSON verdict."
    raw = client.complete(system=_SYSTEM, user=user, max_tokens=500)
    match = _JSON_OBJECT.search(raw)
    if not match:
        return False, ""
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return False, ""
    meaningful = bool(parsed.get("meaningful"))
    cleaned = str(parsed.get("cleaned_text") or "").strip()
    if meaningful and not cleaned:
        return False, ""
    return meaningful, cleaned
