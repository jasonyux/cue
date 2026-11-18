"""The event stream, data/memory/stream/<date>.jsonl: everything that happened, across sessions, one append-only part
per UTC day, and the window every stage reads its inputs from.

Every record a stage or the operator produces is appended here in a fixed envelope (``StreamEvent``): its kind, the
session it happened in, the model call it came from, and the record itself under ``data``, exactly as the per-kind
file holds it. An event goes to the part of its own ``ts``, so a part holds one day and the parts sort by name. The
per-kind files stay what the console reads; the stages and the replay read the stream, through ``Window``, so a
window's input can be rebuilt later from the kinds the stage was shown, or from more of them.

Every kind names what happened, as thing_pastTense:

    observer         screen_changed (a new read of the focused window), screen_unchanged (same as the last tick),
                     screenshot_taken, observer_paused, observer_started, observer_stopped   (data: the snapshots.jsonl row)
    stage outputs    summary_written, belief_updated, proposals_made                           (data: the record it wrote)
    task runs        run_started, run_finished                                                 (data: the runs.jsonl marker)
    chat turns       chat_message (the person's), chat_reply (the agent's, with how the turn ended)  (data: the chat.jsonl row)
    decisions        proposal_accepted, proposal_rejected, decision_digested (the digest when it lands, or digestError),
                     reject_reason_given
    flags            task_flagged (the flag as it stands after the click), task_unflagged, summary_flagged
    operator edits   tasks_edited (the ops, and the task list after them)

A row written to its own file first (a worklog line, a proposal set, a belief update, an observer row, a chat row, a run
marker, a decision) is minted its id before the write and the envelope reuses it, so the two are one id.
Nothing here is edited after the fact. What is state-like, the task list, the decisions and the flags as they stand,
keeps one file per kind next to the stream (``MemoryStore``). The task list at an earlier time is the last list carried
before it with the ops after it replayed (``Window.tasks``, ``ops.replay``): a tasks_edited event always carries the list,
a belief_updated event only when its ops would not rebuild it from the one before (a session's first, one after a rule
fired or the operator edited), so a tick costs the ops, not the list.
"""

from __future__ import annotations

import fcntl
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..utils.jsonl import read_jsonl
from ..utils.time import seconds_between
from .ops import replay
from .records import Decision, Snapshot, StreamEvent, Summary, Task, TaskOp, as_summaries, as_tasks, new_id

OBSERVER_KINDS = ("screen_changed", "screen_unchanged", "screenshot_taken", "observer_paused", "observer_started", "observer_stopped")
CONTROL_KINDS = ("observer_started", "observer_stopped")  # the observer's own markers, not observations
DECIDED_KINDS = ("proposal_accepted", "proposal_rejected")
OTHER_KINDS = ("summary_written", "belief_updated", "proposals_made", "run_started", "run_finished", "chat_message", "chat_reply", *DECIDED_KINDS,
               "decision_digested", "reject_reason_given", "task_flagged", "task_unflagged", "summary_flagged", "tasks_edited",
               "preferences_updated", "session_paused", "session_resumed")
KINDS = OBSERVER_KINDS + OTHER_KINDS
TASK_LIST_KINDS = ("belief_updated", "tasks_edited")  # the events that carry the task list after them
HEAD = re.compile(r'^\{"id": "[0-9a-f]{12}", "ts": "([^"]*)", "kind": "([a-z_]+)", "sessionId": "([^"]*)"')  # how emit() writes a line


def day_of(ts: str) -> str:
    """The part an event with this ``ts`` belongs to: its UTC date."""
    return ts[:10]


def days_before(ts: str, days: int) -> str:
    return (datetime.fromisoformat(day_of(ts)) - timedelta(days=days)).strftime("%Y-%m-%d")


def recent(summaries: list[Summary] | list[dict[str, Any]], *, now: str, minutes: int) -> list[Summary]:
    """Summaries whose window ended within the last ``minutes`` before ``now``."""
    cutoff = datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(minutes=minutes)
    kept = []
    for item in as_summaries(summaries):
        try:
            if datetime.fromisoformat(item.window_end.replace("Z", "+00:00")) >= cutoff:
                kept.append(item)
        except ValueError:
            continue
    return kept


TYPED_MIN_WORDS = 3  # a typed reason shorter than this ranks like a preset click


def worded(decision: Decision) -> bool:
    """A reason the person typed, of at least TYPED_MIN_WORDS words: their own words, saying something."""
    return decision.reason_typed and len(decision.reject_reason.split()) >= TYPED_MIN_WORDS


def recent_decisions(decisions: list[Decision], rejected: int, accepted: int) -> list[Decision]:
    """The last ``rejected`` rejected and the last ``accepted`` accepted, oldest first. Decisions with a worded reason come
    first (the latest of them), the latest others fill what is left: their own words say more than a click."""
    chosen: list[Decision] = []
    for verdict, window in (("accept", accepted), ("reject", rejected)):
        of_verdict = sorted((d for d in decisions if d.decision == verdict), key=lambda item: item.ts)  # oldest first: [-n:] is the latest n
        typed = [d for d in of_verdict if worded(d)][-window:]
        fill = window - len(typed)
        chosen += typed + ([d for d in of_verdict if not worded(d)][-fill:] if fill else [])
    return sorted(chosen, key=lambda item: item.ts)


@dataclass(frozen=True)
class Cursor:
    """Where a reader is in the stream: a part (its date) and a byte offset into it. ``Cursor()`` is the very beginning."""

    part: str = ""
    offset: int = 0

    def wire(self) -> dict[str, Any]:
        return {"part": self.part, "offset": self.offset}

    @classmethod
    def parse(cls, value: Any) -> Cursor:
        return cls(str(value.get("part") or ""), int(value.get("offset") or 0)) if isinstance(value, dict) else cls()


class Stream:
    def __init__(self, root: Path) -> None:
        self.root = root  # the folder of parts

    def parts(self) -> list[Path]:
        return sorted(self.root.glob("*.jsonl")) if self.root.is_dir() else []

    def emit(self, kind: str, data: dict[str, Any], *, ts: str, session_id: str = "", call_id: str = "", id: str | None = None) -> StreamEvent:
        """Append one event to the part of its day. ``id`` is the one the row was written with to its own file, so both
        carry the same; an event with no row of its own gets a new one. The recorder process appends too, so the write
        is taken under a lock."""
        if kind not in KINDS:
            raise ValueError(f"Unknown stream event kind {kind!r}.")
        event = StreamEvent(id=id or new_id(), ts=ts, kind=kind, session_id=session_id, call_id=call_id, data=data)
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / f"{day_of(ts)}.jsonl").open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps(event.wire(), ensure_ascii=False, default=str) + "\n")
            fcntl.flock(handle, fcntl.LOCK_UN)
        return event

    def read(self, cursor: Cursor) -> tuple[list[StreamEvent], Cursor]:
        """The events appended after ``cursor``, across parts, and the cursor after them: the live loops' cursor."""
        events: list[StreamEvent] = []
        for part in self.parts():
            if part.stem < cursor.part:
                continue
            rows, offset = read_jsonl(part, cursor.offset if part.stem == cursor.part else 0)
            events += [StreamEvent.parse_stored(row) for row in rows]
            cursor = Cursor(part.stem, offset)
        return events, cursor

    def end_cursor(self) -> Cursor:
        """After everything written so far."""
        parts = self.parts()
        return Cursor(parts[-1].stem, parts[-1].stat().st_size) if parts else Cursor()

    def events(self, *, since: str | None = None, until: str | None = None, kinds: tuple[str, ...] | None = None,
               session_id: str | None = None) -> list[StreamEvent]:
        """The events in order with ``since`` <= ts <= ``until``, of the given kinds, from one session. Only the parts of
        those days are opened, and a line is screened by its head (ts, kind, session) before it is parsed, so a window
        parses only what it asks for."""
        chosen: list[StreamEvent] = []
        for part in self.parts():
            if (since is not None and part.stem < day_of(since)) or (until is not None and part.stem > day_of(until)):
                continue
            chosen += self._scan(part, since, until, kinds, session_id)
        return chosen

    def last_before(self, ts: str, kinds: tuple[str, ...]) -> StreamEvent | None:
        """The newest event of ``kinds`` strictly before ``ts``, however far back: newest parts first, until one is found."""
        for part in reversed(self.parts()):
            if part.stem > day_of(ts):
                continue
            found = [event for event in self._scan(part, None, ts, kinds, None) if event.ts < ts]
            if found:
                return found[-1]
        return None

    def since_carried(self, ts: str, kinds: tuple[str, ...], key: str) -> list[StreamEvent]:
        """The newest event of ``kinds`` before ``ts`` whose data has ``key``, then every event of ``kinds`` after it before
        ``ts``, oldest first; [] when none carries it. One pass over the parts, newest first, as ``last_before``."""
        trail: list[StreamEvent] = []
        for part in reversed(self.parts()):
            if part.stem > day_of(ts):
                continue
            found = [event for event in self._scan(part, None, ts, kinds, None) if event.ts < ts]
            index = next((i for i in range(len(found) - 1, -1, -1) if found[i].data.get(key) is not None), None)
            if index is not None:
                return found[index:] + trail
            trail = found + trail
        return []

    def _scan(self, part: Path, since: str | None, until: str | None, kinds: tuple[str, ...] | None,
              session_id: str | None) -> list[StreamEvent]:
        chosen: list[StreamEvent] = []
        with part.open("r", encoding="utf-8") as handle:
            for line in handle:
                head = HEAD.match(line)
                if head is None or not line.endswith("\n"):
                    continue  # not a line emit() wrote, or one still being written
                ts, kind, sid = head.groups()
                if (since is not None and ts < since) or (until is not None and ts > until):
                    continue
                if (kinds is not None and kind not in kinds) or (session_id is not None and sid != session_id):
                    continue
                chosen.append(StreamEvent.parse_stored(json.loads(line)))
        return chosen

    def window_at(self, start: str, end: str, session_id: str, *, lookback_days: int) -> Window:
        return Window(self, start, end, session_id, lookback_days)


def carried(event: StreamEvent) -> dict[str, Any]:
    """An event's record with its id: the row's own when it was written with one, else the envelope's (rows from before)."""
    return {**event.data, "id": event.data.get("id") or event.id}


@dataclass
class Window:
    """One window of one session, read from the stream: the raw material every stage builds its input from, live
    (``start`` is where the new events begin, ``end`` is now) and in a replay (the bounds a model call recorded).

    The window's own events have ``start`` <= ts <= ``end``. What "stood" when the window ended, the worklog, the
    decisions, the runs in progress, is read strictly before ``end`` and at most ``lookback_days`` back; the task list
    is state, so it is found however far back it was last carried.
    """

    stream: Stream
    start: str
    end: str
    session_id: str
    lookback_days: int
    observed: list[StreamEvent] = field(init=False)  # this session's observer rows in the window: the bulk of the parts, screened by ts
    others: list[StreamEvent] = field(init=False)    # every other kind within the lookback, up to the end, any session

    def __post_init__(self) -> None:
        self.observed = self.stream.events(since=self.start, until=self.end, kinds=OBSERVER_KINDS, session_id=self.session_id)
        self.others = self.stream.events(since=days_before(self.end, self.lookback_days), until=self.end, kinds=OTHER_KINDS)

    def events(self, *kinds: str, any_session: bool = False) -> list[StreamEvent]:
        """The window's own events of ``kinds``: this session's unless ``any_session``."""
        return [
            event for event in self.observed + self.others
            if event.kind in kinds and event.ts >= self.start and (any_session or event.session_id == self.session_id)
        ]

    def before(self, *kinds: str, any_session: bool = False) -> list[StreamEvent]:
        """Events of ``kinds`` strictly before the end, within the lookback: what stood when the window ended."""
        return [
            event for event in self.others
            if event.kind in kinds and event.ts < self.end and (any_session or event.session_id == self.session_id)
        ]

    # ----- the stages' inputs -------------------------------------------------------------------------------------

    def observer_rows(self) -> list[Snapshot]:
        """This session's observer rows in the window, minus the observer's own start/stop markers."""
        return [Snapshot.model_validate(event.data) for event in self.observed if event.kind not in CONTROL_KINDS]

    def summaries(self) -> list[Summary]:
        """The worklog lines written in the window, from any session: the belief updater's batch."""
        return [Summary.parse_stored(carried(event)) for event in self.events("summary_written", any_session=True)]

    def worklog(self, minutes: int) -> list[Summary]:
        """This session's worklog lines whose window ended within ``minutes`` of the end, written before it."""
        return recent([Summary.parse_stored(carried(event)) for event in self.before("summary_written")], now=self.end, minutes=minutes)

    def earlier_worklog(self, count: int, new: Sequence[Summary]) -> list[Summary]:
        """The last ``count`` worklog lines before the end other than ``new`` (the window's own), from any session,
        oldest first: what the belief updater had already folded in when it reads the window's own lines."""
        fresh = {(item.ts, item.summary) for item in new}
        lines = [Summary.parse_stored(carried(event)) for event in self.before("summary_written", any_session=True)]
        return [item for item in lines if (item.ts, item.summary) not in fresh][-count:]

    def last_worklog(self, count: int) -> list[Summary]:
        """This session's last ``count`` worklog lines written before the end, oldest first."""
        return [Summary.parse_stored(carried(event)) for event in self.before("summary_written")][-count:]

    def tasks(self) -> list[Task]:
        """The task list as it stood at the end: the last belief update or operator edit before it that carried the list,
        with the ops of those between it and the end replayed (``ops.replay``)."""
        trail = self.stream.since_carried(self.end, TASK_LIST_KINDS, "tasks")
        return replay(as_tasks(trail[0].data["tasks"]), [(event.ts, event.session_id, event.call_id, [TaskOp.parse_stored(op) for op in event.data.get("ops") or []])
                                                        for event in trail[1:]]) if trail else []

    def decisions(self, rejected: int, accepted: int) -> list[Decision]:
        """The last ``rejected`` rejected and ``accepted`` accepted proposals before the end, from any session, each with
        the digest and the reason that had landed by then."""
        return recent_decisions(self.decided(), rejected, accepted)

    def decided(self) -> list[Decision]:
        """Every decision before the end, from any session, with the digest and the reason that had landed by then."""
        made: dict[tuple[str, str], Decision] = {}
        for event in self.before(*DECIDED_KINDS, "decision_digested", "reject_reason_given", any_session=True):
            if event.kind in DECIDED_KINDS:
                made[(event.session_id, event.data["ts"])] = Decision.from_feedback(carried(event), event.session_id)
                continue
            key = (event.session_id, event.data["decisionTs"])
            if key in made and event.kind == "decision_digested" and "context" in event.data:
                made[key] = made[key].model_copy(update={"context": event.data["context"]})
            elif key in made and event.kind == "reject_reason_given":
                made[key] = made[key].model_copy(update={"reject_reason": event.data["reason"], "reason_typed": event.data["typed"]})
        return list(made.values())

    def undecided(self, rounds: int) -> list[Decision]:
        """The top proposal of each of this session's last ``rounds`` proposal sets before the end that nobody had accepted
        or rejected by then, as decisions without a verdict: one line a round."""
        decided = {event.data["proposalsTs"] for event in self.before(*DECIDED_KINDS)}
        pending = [event for event in self.before("proposals_made") if not event.data.get("error") and event.data["ts"] not in decided]
        return [
            Decision(id=event.id, ts=event.data["ts"], session_id=self.session_id, kind=proposal["kind"], task=proposal["task"])
            for event in pending[-rounds:] for proposal in (event.data.get("proposals") or [])[:1]
        ]

    def chat(self, minutes: int | None = None) -> list[dict[str, Any]]:
        """This session's chat turns, oldest first: the person's message with the agent's reply and status once it has
        them. The window's own turns, or with ``minutes`` those within that long before the end."""
        events = self.events("chat_message", "chat_reply") if minutes is None else [
            event for event in self.before("chat_message", "chat_reply") if seconds_between(event.ts, self.end) <= minutes * 60
        ]
        turns: dict[str, dict[str, Any]] = {}
        for event in events:
            if event.kind == "chat_message":
                turns[event.data["runId"]] = {"id": event.data.get("id") or event.id, "ts": event.ts, "text": event.data["text"], "reply": None, "status": None}
            elif event.data["runId"] in turns:
                turns[event.data["runId"]].update(reply=event.data["text"], status=event.data["status"])
        return list(turns.values())

    def running(self) -> list[str]:
        """The prompts of this session's task runs that had started and not finished by the end."""
        active: dict[str, str] = {}
        for event in self.before("run_started", "run_finished"):
            if event.kind == "run_started":
                active[event.data["runId"]] = event.data.get("prompt") or ""
            else:
                active.pop(event.data["runId"], None)
        return list(active.values())
