"""The preference updater's tools: one write of the whole file, checked against the token cap, beside the history search
(``storage/search.py``, the same tool the proposer has). A reply with no write call leaves the file as it is."""

from __future__ import annotations

from typing import Any

from ..belief.tools import encoding

WRITE = "write_preferences"
WRITE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": WRITE,
        "description": "Replace the preferences file with this text. Leave it out when nothing should change.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "The whole file, Markdown, within the token cap."}},
            "required": ["text"],
        },
    },
}
MAX_ROUNDS = 4  # replies per tick beyond the searches: one, then up to three more to fix a rejected write


def check(call: dict[str, Any], cap: int) -> tuple[str | None, str]:
    """One write call → the file text and the result the model reads, or no text and why the call was rejected."""
    arguments = call.get("arguments")
    text = arguments.get("text") if isinstance(arguments, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None, "Rejected: text is missing or empty."
    tokens = len(encoding().encode(text))
    if tokens > cap:
        return None, f"Rejected: the file is {tokens} tokens; the cap is {cap}. Cut {tokens - cap} tokens."
    return text.strip() + "\n", "Accepted."
