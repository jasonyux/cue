"""Permission requests of a cmux run: found in cmux's Feed, answered from the console.

Claude Code asks before a call when the user's settings say so (an ``ask`` rule wins over the permission mode). A
headless run cannot ask, so cmux's wrapper parks the request in cmux's Feed for about two minutes. The run remembers the
request its Claude is waiting on, and the console's Allow and Deny buttons answer it through cmux
(``feed.permission.reply``), the same call cmux's own Feed makes.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from ....errors import RunnerCoreError
from .cli import cmux

MODES = ("once", "deny")  # what the console can answer; cmux also knows always, all and bypass
LOOKUPS = 10  # the request reaches the Feed a moment after Claude's stream announces it
LOOKUP_PAUSE_S = 0.3
WAITING: dict[str, str] = {}  # run id → the cmux request its Claude is waiting on


def session_of(item: dict[str, Any]) -> str:
    """The Claude session a Feed item belongs to: the last part of its workstream id, base64."""
    part = str(item.get("workstream_id") or "").rpartition(":")[2]
    return base64.b64decode(part + "=" * (-len(part) % 4)).decode(errors="replace")


async def pending_request(session_id: str, tool: str) -> str | None:
    """The id of the Feed's pending permission request from this Claude session for ``tool``."""
    for _ in range(LOOKUPS):
        for item in reversed(json.loads(await cmux("rpc", "feed.list", "{}"))["items"]):
            if (item.get("kind"), item.get("status"), item.get("tool_name")) == ("permissionRequest", "pending", tool) and session_of(item) == session_id:
                return str(item["request_id"])
        await asyncio.sleep(LOOKUP_PAUSE_S)
    return None


async def answer(run_id: str, mode: Any) -> None:
    """Answer the request this run is waiting on. The stream then reports the answer and the run goes on."""
    if mode not in MODES:
        raise RunnerCoreError(f"mode must be one of {', '.join(MODES)}.", code="invalid_request", status_code=400)
    request = WAITING.get(run_id)
    if request is None:
        raise RunnerCoreError("This run is not waiting for an approval.", code="approval_not_pending", status_code=409,
                              hint="The request was answered already, or its two minutes ran out.")
    await cmux("rpc", "feed.permission.reply", json.dumps({"request_id": request, "mode": mode}))
