"""Belief update: every few minutes, the task list + the worklog lines since the last update → tool calls on the list.

Pipeline position: every session's summaries in the stream, from the cursor memory keeps, plus the task list as the
stream last carried it → a model call (thinking on by default) with the tools add_task / update_task_checklist /
toggle_task_status / rename_task / delete_task → each call checked (``tools.check``: well-formed, a task on the list,
within the length limits) → the accepted ops through ``MemoryStore.apply`` (the rules) → tasks.json + events.jsonl,
beliefs.jsonl in the session for the history view, and the stream. When a call was rejected, the model is shown every
call's result and replies again, up to ``tools.MAX_ROUNDS`` replies per tick; accepted calls are applied as they come.
``BeliefUpdater.run_window`` is the whole step, top to bottom; ``TaskOpsStrategy`` is the default strategy. The model
only proposes changes, and a reply with no tool calls means no change. Its cursor lives in memory, not the session, so
summaries left when a session stopped mid-window are folded in at the next session's first tick.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TypedDict
from uuid import uuid4

from ...usage import record_usage
from ..config import LiveSettings
from ..model_loop import ModelLoop, StageInput, Strategy
from ..storage.memory import MemoryStore
from ..storage.ops import replay
from ..storage.paths import SessionPaths
from ..storage.records import BeliefCall, BeliefUpdate, ModelCall, Step, StreamEvent, Summary, Task, TaskOp
from ..storage.stream import Cursor, Stream, Window
from ..utils.embed import EmbeddingClient, similarities
from ..utils.jsonl import append_jsonl
from ..utils.llm import LiveModelClient, ModelReply, add_usage, normalize_usage
from .prompts import build_belief_messages
from .tools import MAX_ROUNDS, TASK_TOOLS, asked, check


class BeliefInputs(TypedDict):
    """What the belief updater is shown, by name."""

    tasks: list[Task]       # the task list as it stands
    earlier: list[Summary]  # the worklog lines before those, already folded in (Window.earlier_worklog): the arc of the work
    worklog: list[Summary]  # every session's worklog lines written since the last update
    chat: list[dict[str, Any]]  # this session's chat turns since the last update (Window.chat): the agent's work counts too
    trash: list[Task]           # what was taken off the list and can still be restored (MemoryStore.trash): not to be added back
    flags: list[dict[str, Any]]  # the person's latest thumbs up / down per task and entry (MemoryStore.task_flags)
    preferences: str            # the person's standing preferences (data/memory/preferences.md); "" before the first write


@dataclass
class BeliefInput(StageInput[BeliefInputs]):
    """One belief window."""


class TaskOpsStrategy:
    """Operations on the task list, as tool calls."""

    def messages(self, inputs: BeliefInput) -> list[dict[str, Any]]:
        seen = inputs.input
        return build_belief_messages(now=inputs.now, tasks=seen["tasks"], earlier=seen["earlier"], summaries=seen["worklog"], chat=seen["chat"],
                                     trash=seen["trash"], flags=seen["flags"], preferences=seen["preferences"])

    def parse(self, reply: ModelReply, inputs: BeliefInput) -> BeliefUpdate:
        """The record's shell from the tick's last reply; the ops come from the checked calls of every round."""
        return BeliefUpdate(raw=reply.content[:600] or None)


class BeliefUpdater(ModelLoop[BeliefInput, BeliefUpdate]):
    role = "belief"
    output = "beliefs"
    event = "belief_updated"

    def __init__(self, paths: SessionPaths, client: LiveModelClient, settings: LiveSettings, memory: MemoryStore, *,
                 stream: Stream, session_id: str, usage: dict[str, Any] | None = None,
                 strategy: Strategy[BeliefInput, BeliefUpdate] | None = None, embedder: EmbeddingClient | None = None) -> None:
        super().__init__(paths, client, settings, strategy or TaskOpsStrategy(), stream=stream, session_id=session_id, usage=usage)
        self.memory = memory
        self.embedder = embedder  # for the lookalike check (``lookalikes``); None leaves it out
        self.cursor = memory.stream_cursor()  # memory keeps the cursor, so it survives restarts and sessions
        self.shadow: list[Task] | None = None  # the list as a reader rebuilds it from this session's rows (ops.replay); None before the first row, which carries the list

    def wants_images(self) -> bool:
        return False

    def thinking(self) -> bool:
        return self.settings.belief_thinking

    def max_tokens(self) -> int | None:
        return self.settings.belief_max_tokens or None

    def reads(self, event: StreamEvent) -> bool:
        return event.kind == "summary_written"  # every session's worklog lines

    def worth_a_call(self, batch: list[StreamEvent]) -> bool:
        return bool(batch)

    def window_start(self, batch: list[StreamEvent]) -> str:
        return Summary.parse_stored(batch[0].data).window_start

    async def run_window(self, window: Window, *, finalize: Callable[[BeliefUpdate], BeliefUpdate] | None = None,
                         end_cursor: Cursor | None = None) -> BeliefUpdate:
        """One window of worklog lines → tool calls, checked and applied reply by reply → one record with the list
        after them. A reply with a rejected call gets the results of all its calls back and the model replies again,
        up to MAX_ROUNDS replies in all."""
        summaries = window.summaries()
        given: BeliefInputs = {
            "tasks": window.tasks(),
            "earlier": window.earlier_worklog(self.settings.belief_context_summaries, summaries),
            "worklog": summaries,
            "chat": window.chat(),
            "trash": self.memory.trash(),
            "flags": self.memory.task_flags(),
            "preferences": self.memory.preferences().strip(),
        }
        inputs = BeliefInput(
            now=window.end,
            window_start=window.start,
            input=given,
            screenshots=[],
            settings=self.settings,
        )
        rules = {
            "stale_days": self.settings.belief_stale_days,
            "done_step_days": self.settings.belief_done_step_days,
            "trash_days": self.settings.belief_trash_days,
        }
        messages = self.strategy.messages(inputs)
        ops: list[TaskOp] = []
        rejected: list[str] = []
        warned: list[str] = []
        calls: list[BeliefCall] = []  # every call with its result, for the record
        seen: set[str] = set()  # the adds held back so far this tick: the same again goes through
        made = len(self.embedder.calls) if self.embedder is not None else 0
        usage: dict[str, Any] = {}
        for rounds in range(1, MAX_ROUNDS + 1):
            reply = await self.call_model(messages, [], window=window, tools=TASK_TOOLS)
            usage = add_usage(usage, reply.usage)
            verdicts = [check(call, given["tasks"], given["trash"]) for call in reply.tool_calls]
            rejected += [result for op, result in verdicts if op is None]
            verdicts, held = await self.lookalikes(verdicts, given["tasks"], seen)
            warned += held
            calls += [
                BeliefCall(round=rounds, tool=str(call.get("name")), op=asked(call), applied=op is not None, result=result)
                for call, (op, result) in zip(reply.tool_calls, verdicts, strict=True)
            ]
            accepted = [op for op, _ in verdicts if op is not None]
            tasks = self.memory.apply(accepted, now=window.end, session_id=self.session_id, call_id=reply.call_id, cursor=end_cursor, **rules)
            ops += accepted
            if all(op is not None for op, _ in verdicts):
                break
            results = [
                {"role": "tool", "tool_call_id": call["id"], "content": result} for call, (_, result) in zip(reply.tool_calls, verdicts, strict=True)
            ]
            messages = [*messages, reply.message(), *results]
        # The row carries the list only when replaying its ops on the list before it would not give it: the session's first
        # row, a rule that fired, an operator edit since the last row, a multi-round tick whose numbers moved between rounds.
        expected = replay(self.shadow, [(window.end, self.session_id, reply.call_id, ops)]) if self.shadow is not None else None
        carried = expected is None or [task.wire() for task in expected] != [task.wire() for task in tasks]
        self.shadow = [task.model_copy(deep=True) for task in tasks] if carried else expected
        update = self.strategy.parse(reply, inputs).model_copy(update={"ops": ops, "tasks": tasks if carried else None, "rounds": rounds, "rejected": rejected, "warned": warned, "calls": calls,
                                                                       "embedding": self.charged(made, window)})
        return self.write(update, replace(reply, usage=usage), summaries, window=window, finalize=finalize)

    def charged(self, made: int, window: Window) -> dict[str, Any] | None:
        """The lookalike check's embeddings requests since ``made`` (the embedder's call count before the tick), each logged
        like a model call, their usage into the session's totals; {"model", "usage"} for the record (the cost panel's
        "belief updater: lookalike check" row), None when nothing was spent: every text was cached, or there is no check."""
        embedder = self.embedder
        calls = embedder.calls[made:] if embedder is not None else []
        if embedder is None or not calls:
            return None
        usage: dict[str, Any] = {}
        for call in calls:
            append_jsonl(self.paths.model_calls, ModelCall(id=uuid4().hex[:12], ts=window.end, kind="embedding", window_start=window.start,
                                                           window_end=window.end, request=call["request"], response=call["response"]).wire())
            usage = add_usage(usage, normalize_usage(call["response"].get("usage")))
        model = str(calls[-1]["response"].get("model") or embedder.endpoint.model)
        record_usage(self.usage, model, usage)
        return {"model": model, "usage": usage}

    async def lookalikes(self, verdicts: list[tuple[TaskOp | None, str]], tasks: list[Task],
                         seen: set[str]) -> tuple[list[tuple[TaskOp | None, str]], list[str]]:
        """The verdicts again, a call that adds an entry or a task held back once when what it adds embeds close to an
        entry or a title on the list: its result then names the lookalikes (open or done, and since when), and the same
        call again goes through. ``seen`` carries the held texts across the tick's rounds. Also the warnings, for the
        record. Nothing is held without an embedder or with ``belief_similar_threshold`` at 0."""
        threshold = self.settings.belief_similar_threshold
        adds = [(index, op, text) for index, (op, _) in enumerate(verdicts) if op is not None
                for text in [*(spec.text for spec in op.steps), *([op.title] if op.op == "add" and op.title else [])]]
        if self.embedder is None or not threshold or not adds:
            return verdicts, []
        entries = [(task.id, number, step) for task in tasks for number, step in enumerate(task.steps, start=1)]
        references = [step.text for _, _, step in entries] + [task.title for task in tasks]
        if not references:
            return verdicts, []
        vectors = await self.embedder.embed([text for _, _, text in adds] + references)
        scores = similarities(vectors[:len(adds)], vectors[len(adds):])
        held: dict[int, list[str]] = {}
        for row, (index, op, text) in enumerate(adds):
            key = f"{op.task_id or ''}\n{text.lower()}"
            close = sorted(((float(scores[row, col]), col) for col in range(len(references)) if scores[row, col] >= threshold), reverse=True)[:3]
            if close and key not in seen:
                held.setdefault(index, []).append(f'"{text}" is close to ' + ", ".join(self.describe(entries, tasks, col) for _, col in close))
            seen.add(key)
        warnings = []
        for index, lines in held.items():
            warnings.append("Warning, not applied: " + "; ".join(lines) + ". If it is the same work, consider merging/rewording the open entr(ies); "
                            "if it is truly a different work, call again unchanged and it will be applied.")
            verdicts[index] = (None, warnings[-1])
        return verdicts, warnings

    @staticmethod
    def describe(entries: list[tuple[str, int, Step]], tasks: list[Task], col: int) -> str:
        """One lookalike, as the warning names it: an entry with its state, or a task's title."""
        if col < len(entries):
            task_id, number, step = entries[col]
            state = f"done {step.done_at[:10]}" if step.done_at else f"open since {step.added_at[:10]}" if step.added_at else "open"
            return f'entry {number} of {task_id} "{step.text}" ({state})'
        task = tasks[col - len(entries)]
        return f'task {task.id} "{task.title}"'
