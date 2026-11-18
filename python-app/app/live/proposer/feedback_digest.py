"""The digest stored with a decision on a proposal: what the proposer saw that round, in a few dozen words.

The proposer logged its exact request when it proposed; the person's accept or reject arrives minutes later. At that
moment the proposer's own model condenses that input, text and the same screenshots, and the result is stored with the
decision and copied to memory, so later rounds can be shown a sentence per decision (what the person was doing, what
was proposed, what they decided) instead of the whole input.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from ...errors import RunnerCoreError
from ..observer.stream import latest_screenshots
from ..storage.pack import model_calls
from ..storage.paths import SessionPaths
from ..storage.records import ModelCall
from ..utils.jsonl import append_jsonl, read_jsonl
from ..utils.llm import ModelReply
from ..utils.time import timestamp
from .prompts import CLOSING

DIGEST_WORDS = 50
DIGEST_SYSTEM = (f"Below is the input an assistant received while watching a person work on their Mac, with the screenshots it "
                 f"was given, if any. In at most {DIGEST_WORDS} words, say what the person was doing and what the situation was. "
                 "Plain prose; no advice, no proposals.")

PROPOSER_KINDS = ("proposer", "proposer_idle")  # the model calls a proposal set can come from (proposer/quick.py)


def proposer_request(paths: SessionPaths, proposals_ts: str) -> dict[str, Any]:
    """The logged call of the proposer round that ended at ``proposals_ts`` (the timer's or the idle trigger's), from
    live_model_calls.jsonl."""
    call = next((item for item in model_calls(paths)
                 if item.get("kind") in PROPOSER_KINDS and item.get("windowEnd") == proposals_ts), None)
    if call is None:
        raise RunnerCoreError("No logged proposer request for this proposal set.", code="invalid_request", status_code=404)
    return call


def user_parts(call: dict[str, Any]) -> list[dict[str, Any]]:
    """The parts of the logged user messages: text parts, and image parts with their data replaced by a size note."""
    parts: list[dict[str, Any]] = []
    for message in (call.get("request") or {}).get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        parts.extend(content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}])
    return parts


def input_text(call: dict[str, Any]) -> str:
    """What the proposer read, without the closing instruction to answer in JSON."""
    text = "\n\n".join(str(part.get("text") or "") for part in user_parts(call) if part.get("type") == "text")
    return text.removesuffix(CLOSING).rstrip()


def input_images(call: dict[str, Any], paths: SessionPaths, max_side: int) -> list[str]:
    """The same screenshots the proposer was given: as many as its request carried, the newest of that window."""
    count = sum(1 for part in user_parts(call) if part.get("type") == "image_url")
    if not count:
        return []
    window = [row for row in read_jsonl(paths.snapshots)[0] if call.get("windowStart", "") <= row.get("ts", "") <= call.get("windowEnd", "")]
    return [shot.url for shot in latest_screenshots(window, paths, limit=count, max_side=max_side)]


async def feedback_digest(client: Any, paths: SessionPaths, proposals_ts: str, *, max_side: int) -> ModelReply:
    """One non-thinking call over that input, logged to live_model_calls.jsonl with kind ``proposer_feedback``."""
    call = proposer_request(paths, proposals_ts)
    reply = await client.complete([{"role": "system", "content": DIGEST_SYSTEM}, {"role": "user", "content": input_text(call)}],
                                  thinking=False, images=input_images(call, paths, max_side) or None, max_tokens=200)
    append_jsonl(paths.model_calls, ModelCall(id=uuid4().hex[:12], ts=timestamp(), kind="proposer_feedback", window_start=proposals_ts, window_end=proposals_ts,
                                              request=reply.request, response=reply.response).wire())
    return reply
