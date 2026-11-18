"""The cross-session memory folder, data/memory/: every summary from every session, and the task list the belief
updater keeps over them. The rules that keep the list honest live here, not in the model: ids, the cap on active
tasks, pausing what has not been seen for days, and deletion as a trash that empties after days, not at once.

    data/memory/summaries.jsonl   every worklog line from every session, with its sessionId (appended by the summarizer)
    data/memory/tasks.json        the current task list plus the belief updater's cursor into stream.jsonl
    data/memory/events.jsonl      every op applied, with ts and sessionId
    data/memory/feedback.jsonl    every accept / reject of a proposal, with a digest of what the proposer saw then
    data/memory/task_feedback.jsonl  every thumbs up / down on a task or an entry, with the reason when one was given
    data/memory/embeddings.jsonl  the duplicate filter's cache: every proposal and decision text embedded, once (utils/embed.py)
    data/memory/preferences.md    the person's standing preferences, as the preference updater keeps them (preference/update.py)
    data/memory/preferences.json  that updater's cursor into the stream
    data/memory/stream/<date>.jsonl  every event of every kind, in order, in one envelope, one part per day (storage/stream.py)

What is state-like keeps one file per kind, the three above the stream; a new stateful source gets its own file too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...errors import RunnerCoreError
from ..utils.jsonl import amend_jsonl, append_jsonl, read_json, read_jsonl, rewrite_jsonl, write_json
from ..utils.time import seconds_between, timestamp
from .ops import play, visible
from .records import Decision, Summary, Task, TaskFlag, TaskOp, as_tasks
from .stream import Cursor, Stream


def flag_key(row: dict[str, Any]) -> tuple[Any, bool, str]:
    """What a flag is on: its task, whether the task itself, and the entry's text. An entry is known by its text, not its
    number: the numbers shift whenever the list is reordered (an entry added before it, or one finished)."""
    return row.get("taskId"), row.get("step") is None, row.get("text") or ""


class MemoryStore:
    def __init__(self, data_root: Path | str) -> None:
        self.root = Path(data_root) / "memory"
        self.summaries = self.root / "summaries.jsonl"
        self.tasks_file = self.root / "tasks.json"
        self.events = self.root / "events.jsonl"
        self.feedback = self.root / "feedback.jsonl"
        self.task_feedback = self.root / "task_feedback.jsonl"
        self.embeddings = self.root / "embeddings.jsonl"  # the proposer's duplicate filter: one vector per (model, text), utils/embed.py
        self.preferences_file = self.root / "preferences.md"
        self.preferences_state = self.root / "preferences.json"
        self.stream = Stream(self.root / "stream")
        self.chats = self.root / "chats"  # the conversations with the agent, one folder each (live/actor/chat.py)

    def create(self) -> MemoryStore:
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (self.summaries, self.events, self.feedback, self.task_feedback):
            path.touch(exist_ok=True)
        self.stream.root.mkdir(exist_ok=True)
        self.chats.mkdir(exist_ok=True)
        return self

    # ----- flags on tasks -------------------------------------------------------------------------------------------

    def append_task_flag(self, flag: TaskFlag) -> None:
        append_jsonl(self.task_feedback, flag.wire())

    def current_task_flag(self, task_id: str, step: int | None, text: str) -> dict[str, Any] | None:
        """The flag on a task (``step`` None) or on one of its entries, if any: the latest record for it."""
        wanted = flag_key({"taskId": task_id, "step": step, "text": text})
        return next((row for row in reversed(read_jsonl(self.task_feedback)[0]) if flag_key(row) == wanted), None)

    def update_task_flag(self, flag_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        try:
            row = amend_jsonl(self.task_feedback, lambda row: row.get("id") == flag_id, updates)
        except ValueError:
            raise RunnerCoreError("Unknown flag.", code="invalid_request", status_code=404) from None
        return row

    def remove_task_flag(self, flag_id: str) -> None:
        rows = read_jsonl(self.task_feedback)[0]
        kept = [row for row in rows if row.get("id") != flag_id]
        if len(kept) == len(rows):
            raise RunnerCoreError("Unknown flag.", code="invalid_request", status_code=404)
        rewrite_jsonl(self.task_feedback, kept)

    def task_flags(self) -> list[dict[str, Any]]:
        """The latest flag on each task and entry, what the console tags them with."""
        latest: dict[tuple[Any, bool, str], dict[str, Any]] = {}
        for row in read_jsonl(self.task_feedback)[0]:
            latest[flag_key(row)] = row
        return list(latest.values())

    # ----- decisions on proposals ---------------------------------------------------------------------------------

    def append_decision(self, decision: Decision) -> None:
        append_jsonl(self.feedback, decision.wire())

    def amend_decision(self, ts: str, session_id: str, updates: dict[str, Any]) -> None:
        """Written onto that decision's note after the fact: the digest when it lands, a reason when one is given."""
        amend_jsonl(self.feedback, lambda row: row.get("ts") == ts and row.get("sessionId") == session_id, updates)

    # ----- the summary stream -----------------------------------------------------------------------------

    def append_summary(self, summary: Summary, session_id: str) -> Summary:
        """Copy a worklog line into memory with its session; returns the summary unchanged for the session file."""
        append_jsonl(self.summaries, summary.model_copy(update={"session_id": session_id}).wire())
        return summary

    # ----- the preferences file -------------------------------------------------------------------------------------

    def preferences(self) -> str:
        """The person's standing preferences, the Markdown file as it stands; "" before the first write."""
        return self.preferences_file.read_text() if self.preferences_file.exists() else ""

    def preference_cursor(self) -> Cursor:
        """Where in the stream the preference updater has read up to; kept here like the belief updater's."""
        return Cursor.parse((read_json(self.preferences_state) or {}).get("streamCursor"))

    def save_preferences(self, text: str | None, *, cursor: Cursor, now: str) -> None:
        """A tick's outcome: the file rewritten when ``text`` is given, the cursor moved past what the tick read either way."""
        if text is not None:
            self.preferences_file.write_text(text)
        write_json(self.preferences_state, {"streamCursor": cursor.wire(), "updatedAt": now})

    # ----- the belief updater's cursor into the stream -------------------------------------------------------------

    def stream_cursor(self) -> Cursor:
        """Where in the stream the belief updater has read up to; kept here so it survives restarts and sessions."""
        return self._state()["cursor"]

    def set_stream_cursor(self, cursor: Cursor) -> None:
        """Mark everything before ``cursor`` as read (the converter, after rebuilding the stream from the history)."""
        state = self._state()
        write_json(self.tasks_file, {"tasks": state["tasks"], "streamCursor": cursor.wire(), "updatedAt": timestamp()})

    # ----- the task list --------------------------------------------------------------------------------------

    def tasks(self) -> list[Task]:
        """The list as the model and the console see it: tasks not in the trash, each without its trashed entries."""
        return visible(as_tasks(self._state()["tasks"]))

    def trash(self) -> list[Task]:
        """What can still be restored: tasks in the trash (whole), and for other tasks only their trashed entries."""
        return [task.model_copy(update={"steps": [s for s in task.steps if s.deleted_at]}) if not task.deleted_at else task
                for task in as_tasks(self._state()["tasks"]) if task.deleted_at or any(s.deleted_at for s in task.steps)]

    def apply(self, ops: list[TaskOp], *, now: str, session_id: str, call_id: str, cursor: Cursor | None,
              stale_days: int, done_step_days: int, trash_days: int) -> list[Task]:
        """Apply ops (the model's, or the operator's with ``cursor=None`` to leave the cursor alone; ``ops.play``), enforce
        the rules, save the list with the cursor (where the belief updater has read the stream up to), log what was applied.
        ``call_id`` is the model call (or operator edit) the ops came from: every task and entry added here and every op
        logged carries it. Returns the visible list, as ``tasks()`` does. The rules here, the stale pause and the trash
        emptying, are what a replay of the logged ops does not reproduce (``ops.replay``)."""
        state = self._state()
        tasks = as_tasks(state["tasks"])
        cursor = state["cursor"] if cursor is None else cursor
        applied = play(tasks, ops, now=now, session_id=session_id, call_id=call_id)
        tasks = [task for task in tasks  # the trash empties after trash_days; events.jsonl keeps every op
                 if not (task.deleted_at and seconds_between(task.deleted_at, now) > trash_days * 86400)]
        for task in tasks:
            if not task.deleted_at and task.status == "active" and task.last_seen and seconds_between(task.last_seen, now) > stale_days * 86400:
                task.status = "paused"
            # Finished entries are kept for good (the prompts show only recent ones; the console hides those older than
            # done_step_days); trashed ones are gone after trash_days.
            task.steps = [step for step in task.steps if not (step.deleted_at and seconds_between(step.deleted_at, now) > trash_days * 86400)]
        write_json(self.tasks_file, {"tasks": [task.wire() for task in tasks], "streamCursor": cursor.wire(), "updatedAt": now})
        for op in applied:
            append_jsonl(self.events, {"ts": now, "sessionId": session_id, "callId": call_id, **op.wire()})
        return visible(tasks)

    def _state(self) -> dict[str, Any]:
        data = read_json(self.tasks_file) or {}
        return {"tasks": data.get("tasks") or [], "cursor": Cursor.parse(data.get("streamCursor"))}
