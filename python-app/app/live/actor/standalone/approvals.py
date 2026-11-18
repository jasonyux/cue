"""Permission requests of a standalone run: Claude asks the backend, and the person answers on the run's card.

Claude Code asks before a call its permission mode or the user's settings hold back. Through the SDK the question is a
callback (``ask``), which waits for the answer. A run keeps its open requests here, oldest first: the card shows the
oldest, its Allow once and Deny buttons answer it (``answer``), and then the next one is shown. Claude waits as long as
it takes; stopping the run ends the wait.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from ....errors import RunnerCoreError

MODES = ("once", "deny")  # what the console can answer


class Approvals:
    """The requests one run's Claude is waiting on."""

    def __init__(self, events: Any) -> None:
        self.events = events
        self.queue: list[tuple[str, dict[str, Any], asyncio.Future[str]]] = []  # tool, its input, the answer to come; oldest first

    async def ask(self, tool: str, args: dict[str, Any], context: Any) -> PermissionResultAllow | PermissionResultDeny:
        """The SDK's permission callback: the request waits its turn on the card, and the person's answer is Claude's."""
        answer: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.queue.append((tool, args, answer))
        if len(self.queue) == 1:
            await self.announce()
        mode = await answer
        return PermissionResultAllow(updated_input=args) if mode == "once" else PermissionResultDeny(message="The user denied this.")

    async def answer(self, mode: str) -> None:
        _tool, _args, answer = self.queue.pop(0)
        answer.set_result(mode)
        await self.events.emit("run_progress", "ok", f"Approval answered: {'allow' if mode == 'once' else 'deny'}.")
        if self.queue:
            await self.announce()

    async def announce(self) -> None:
        """The oldest request as the console shows it, with Allow once and Deny on the run's card."""
        tool, args, _answer = self.queue[0]
        await self.events.emit("run_progress", "warn", f"Claude is waiting for your approval to use {tool}.",
                               f"{tool} {json.dumps(args)}\nAllow or deny it on this run's card. Claude waits until you answer.")


WAITING: dict[str, Approvals] = {}  # run id → its requests, while the run lasts


async def answer(run_id: str, mode: Any) -> None:
    """Answer the oldest request this run's Claude is waiting on."""
    if mode not in MODES:
        raise RunnerCoreError(f"mode must be one of {', '.join(MODES)}.", code="invalid_request", status_code=400)
    approvals = WAITING.get(run_id)
    if approvals is None or not approvals.queue:
        raise RunnerCoreError("This run is not waiting for an approval.", code="approval_not_pending", status_code=409,
                              hint="The request was answered already, or the run ended.")
    await approvals.answer(mode)
