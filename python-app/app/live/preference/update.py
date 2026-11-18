"""Preference update: a tick every ``preference.interval_s`` while a session runs
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TypedDict

from ..config import LiveSettings
from ..model_loop import ModelLoop, StageInput, Strategy
from ..storage.memory import MemoryStore
from ..storage.paths import SessionPaths
from ..storage.records import Decision, PreferenceUpdate, StreamEvent, Summary, Task
from ..storage.search import TOOL, TOOL_NAME, HistoryIndex, run_tool
from ..storage.stream import DECIDED_KINDS, Cursor, Stream, Window
from ..utils.llm import LiveModelClient, ModelReply, add_usage
from ..utils.prompting import decision_lines
from ..utils.time import clock, shifted, timestamp
from .prompts import build_preference_messages
from .tools import MAX_ROUNDS, WRITE, WRITE_TOOL, check

FEEDBACK_KINDS = ("proposal_accepted", "proposal_rejected", "decision_digested", "reject_reason_given", "task_flagged")
CHAT_MINUTES = 60  # the chat turns shown: this session's within the last hour


class PreferenceInputs(TypedDict):
    """What the preference updater is shown, by name."""

    preferences: str            # the file as it stands
    feedback: str               # the new reactions, rendered one per line
    earlier: list[Decision]     # the decisions before them (Window.decisions)
    tasks: list[Task]
    worklog: list[Summary]
    chat: list[dict[str, Any]]


@dataclass
class PreferenceInput(StageInput[PreferenceInputs]):
    """One preference tick."""

    searches: int = 0           # history searches the tick may make: the tool is given when positive
    history: str | None = None  # a line for the prompt, when the tool is given: how much is searchable and how far back


class PreferenceStrategy:
    """The whole file, as one tool call, or no call."""

    def messages(self, inputs: PreferenceInput) -> list[dict[str, Any]]:
        return build_preference_messages(
            now=inputs.now,
            **inputs.input,
            cap=inputs.settings.preference_cap_tokens,
            searches=inputs.searches,
            history=inputs.history,
        )

    def parse(self, reply: ModelReply, inputs: PreferenceInput) -> PreferenceUpdate:
        return PreferenceUpdate(raw=reply.content[:600] or None)


def reaction_count(events: list[StreamEvent]) -> int:
    """How many reactions the events amount to: a decision once, whichever of its events are there; a flag once."""
    keys = set()
    for event in events:
        if event.kind == "task_flagged":
            keys.add(("flag", event.data.get("id")))
        else:
            keys.add((event.session_id, event.data["ts"] if event.kind in DECIDED_KINDS else event.data.get("decisionTs")))
    return len(keys)


def reaction_lines(decisions: list[Decision], flags: list[dict[str, Any]]) -> str:
    """The new reactions as the model reads them, oldest first: each decision as the proposer's decisions block has it, each
    thumbs with what it is on and the reason."""
    lines = [(item.ts, decision_lines([item])) for item in decisions]
    for flag in flags:
        where = f'task "{flag.get("title")}"' + (f', entry "{flag.get("text")}"' if flag.get("step") is not None else "")
        reason = f": {flag['reason']}" if flag.get("reason") else ""
        lines.append((flag["ts"], f"- [{flag['ts'][:10]} {clock(flag['ts'])}] thumbs {flag.get('verdict')} on {where}{reason}"))
    return "\n".join(line for _, line in sorted(lines))


class PreferenceUpdater(ModelLoop[PreferenceInput, PreferenceUpdate]):
    role = "preference"
    output = "preferences"
    event = "preferences_updated"

    def __init__(
        self,
        paths: SessionPaths,
        client: LiveModelClient,
        settings: LiveSettings,
        memory: MemoryStore,
        *,
        stream: Stream,
        session_id: str,
        usage: dict[str, Any] | None = None,
        strategy: Strategy[PreferenceInput, PreferenceUpdate] | None = None,
        index: HistoryIndex | None = None,
    ) -> None:
        super().__init__(paths, client, settings, strategy or PreferenceStrategy(), stream=stream, session_id=session_id, usage=usage)
        self.memory = memory
        self.index = index  # the history the search tool reads; None leaves the tool out
        self.cursor = memory.preference_cursor()  # memory keeps the cursor, so it survives restarts and sessions

    def wants_images(self) -> bool:
        return False

    def thinking(self) -> bool:
        return self.settings.preference_thinking

    def max_tokens(self) -> int | None:
        return self.settings.preference_max_tokens or None

    def reads(self, event: StreamEvent) -> bool:
        return event.kind in FEEDBACK_KINDS  # any session's

    def worth_a_call(self, batch: list[StreamEvent]) -> bool:
        return reaction_count(batch) >= self.settings.preference_min_feedback

    async def tick(
        self,
        *,
        force: bool = False,
        now: str | None = None,
        finalize: Callable[[PreferenceUpdate], PreferenceUpdate] | None = None,
    ) -> PreferenceUpdate | None:
        """Too few reactions leave the cursor where it is: they wait for a later tick. The window ends a second after the
        last reaction read, since a window reads what stood strictly before its end and a reaction may land within the
        second the tick starts."""
        batch, _ = self.pending()
        if not force and not self.worth_a_call(batch):
            return None
        if batch:
            now = max(now or timestamp(), shifted(batch[-1].ts, 1))
        return await super().tick(force=force, now=now, finalize=finalize)

    def history_note(self) -> str:
        assert self.index is not None
        count, oldest = self.index.span()
        return f"History searchable with search_history: {count} lines back to {oldest}." if count else "History searchable with search_history: nothing yet."

    async def run_window(
        self,
        window: Window,
        *,
        finalize: Callable[[PreferenceUpdate], PreferenceUpdate] | None = None,
        end_cursor: Cursor | None = None,
    ) -> PreferenceUpdate:
        """One window of reactions → the file rewritten or left alone → one record. A decision is shown as it stands at the
        end (its digest and reason included), once, whichever of its events the window holds; a reply with a rejected write
        gets the result back and the model replies again, up to MAX_ROUNDS replies beyond its searches."""
        new = window.events(*FEEDBACK_KINDS, any_session=True)
        touched = {
            (event.session_id, event.data["ts"] if event.kind in DECIDED_KINDS else event.data.get("decisionTs"))
            for event in new
            if event.kind != "task_flagged"
        }
        flags: dict[str, dict[str, Any]] = {}
        for event in new:
            if event.kind == "task_flagged":
                flags[str(event.data.get("id"))] = event.data  # the latest state of each flag
        decided = window.decided()
        before = self.memory.preferences()
        given: PreferenceInputs = {
            "preferences": before,
            "feedback": reaction_lines([item for item in decided if (item.session_id, item.ts) in touched], list(flags.values())),
            "earlier": [item for item in window.decisions(self.settings.preference_context_decisions, self.settings.preference_context_decisions)
                        if (item.session_id, item.ts) not in touched],
            "tasks": window.tasks(),
            "worklog": window.last_worklog(self.settings.preference_context_summaries),
            "chat": window.chat(CHAT_MINUTES),
        }
        allowed = self.settings.preference_search_rounds if self.index is not None else 0
        inputs = PreferenceInput(
            now=window.end,
            window_start=window.start,
            input=given,
            screenshots=[],
            settings=self.settings,
            searches=allowed,
            history=self.history_note() if allowed else None
        )
        tools = [WRITE_TOOL, *([TOOL] if allowed else [])]
        messages = self.strategy.messages(inputs)
        text: str | None = None
        rejected: list[str] = []
        searches: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        for rounds in range(1, MAX_ROUNDS + allowed + 1):
            reply = await self.call_model(messages, [], window=window, tools=tools)
            usage = add_usage(usage, reply.usage)
            if not reply.tool_calls:
                break
            results = []
            for call in reply.tool_calls:
                if call["name"] == WRITE:
                    written, content = check(call, self.settings.preference_cap_tokens)
                    text = written if written is not None else text
                    if written is None:
                        rejected.append(content)
                elif call["name"] == TOOL_NAME and self.index is not None:
                    if len(searches) < allowed:
                        content, note = run_tool(self.index, call["arguments"])
                        searches.append(note)
                    else:
                        content = "No more searches this tick: write the file, or reply with no call."
                else:
                    content = f"Unknown tool {call['name']!r}."
                results.append({"role": "tool", "tool_call_id": call["id"], "content": content})
            if text is not None:
                break
            messages = [*messages, reply.message(), *results]
        changed = text is not None and text != before
        self.memory.save_preferences(text if changed else None, cursor=end_cursor if end_cursor is not None else self.cursor, now=window.end)
        update = self.strategy.parse(reply, inputs).model_copy(
            update={
                "before": before,
                "after": text if changed else None,
                "changed": changed,
                "rounds": rounds,
                "rejected": rejected,
                "searches": searches,
                "feedback": [event.id for event in new],
            }
        )
        return self.write(update, replace(reply, usage=usage), new, window=window, finalize=finalize)
