"""Rebuild data/memory/stream/ from the per-kind files: the history recorded before the stream existed, in the stream's
envelope and its daily parts, so the stages and the replay can read it.

    python -m app.live.storage.convert --data-root data

Stop the backend first. Reads every session folder and the memory files, writes the stream in time order with fresh
event ids (an existing stream folder is moved aside as stream.bak-<stamp>, nothing is deleted), ends it with the task
list as tasks.json holds it, and marks the whole stream as read for the belief updater (``streamCursor`` in tasks.json).
The per-kind files are not touched otherwise. Operator task edits in events.jsonl carry no task list, so they are not replayed;
the closing tasks_edited event stands for them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ..utils.jsonl import read_json, read_jsonl
from ..utils.time import timestamp
from .memory import MemoryStore
from .ops import visible
from .paths import SessionPaths
from .records import as_tasks, row_kind
from .stream import OBSERVER_KINDS

Event = tuple[str, str, str, str, dict[str, Any]]  # ts, kind, session id, call id, data
SKIPPED: list[tuple[str, str, dict[str, Any]]] = []  # rows the stream cannot carry (session, file, row), reported at the end


def session_events(paths: SessionPaths) -> list[Event]:
    """One session's history as stream events: every row of every per-kind file it holds."""
    sid, events = paths.root.name, []
    for row in read_jsonl(paths.snapshots)[0]:
        kind = row_kind(row)
        if kind in OBSERVER_KINDS and row.get("ts"):
            events.append((row["ts"], kind, sid, "", row))
    for path, kind in ((paths.summaries, "summary_written"), (paths.beliefs, "belief_updated"), (paths.proposals, "proposals_made")):
        events += [(row["ts"], kind, sid, row.get("callId") or "", row) for row in read_jsonl(path)[0] if row.get("ts")]
    for row in read_jsonl(paths.runs)[0]:
        events.append((row["ts"], "run_started" if row.get("event") == "started" else "run_finished", sid, "", row))
    for row in read_jsonl(paths.feedback)[0]:
        if not row.get("ts") or not row.get("proposalsTs"):
            SKIPPED.append((sid, "feedback", row))  # a row from before decisions named their proposal set
            continue
        call_id = row.get("callId") or ""
        events.append((row["ts"], "proposal_accepted" if row.get("decision") == "accept" else "proposal_rejected", sid, call_id, row))
        landed = {key: row[key] for key in ("context", "model", "usage", "digestError") if row.get(key)}
        if landed:
            events.append((row["ts"], "decision_digested", sid, call_id, {"proposalsTs": row["proposalsTs"], "decisionTs": row["ts"], **landed}))
        if row.get("rejectReason"):
            reason = {"proposalsTs": row["proposalsTs"], "decisionTs": row["ts"], "reason": row["rejectReason"], "typed": bool(row.get("reasonTyped"))}
            events.append((row["ts"], "reject_reason_given", sid, call_id, reason))
    events += [(row["ts"], "summary_flagged", sid, "", row) for row in read_jsonl(paths.summary_feedback)[0] if row.get("ts")]
    return events


def convert(data_root: Path) -> int:
    """Build the stream from the history. Returns the number of events written."""
    memory = MemoryStore(data_root).create()
    events: list[Event] = []
    sessions = data_root / "sessions"
    for folder in sorted(sessions.iterdir()) if sessions.is_dir() else []:
        if (folder / "session.json").is_file():
            events += session_events(SessionPaths(folder))
    events += [(row["ts"], "task_flagged", row.get("sessionId") or "", row.get("callId") or "", row)
               for row in read_jsonl(memory.task_feedback)[0] if row.get("ts")]
    stored = read_json(memory.tasks_file) or {}
    if stored.get("tasks"):  # the list as it stands now closes the history, so the stages find it as the latest
        tasks = [task.wire() for task in visible(as_tasks(stored["tasks"]))]
        events.append((stored.get("updatedAt") or timestamp(), "tasks_edited", "", "", {"ops": [], "tasks": tasks}))
    events.sort(key=lambda event: event[0])
    if memory.stream.parts():
        memory.stream.root.rename(memory.stream.root.with_name(f"stream.bak-{timestamp()[:19].replace(':', '')}"))
    for ts, kind, session_id, call_id, data in events:
        memory.stream.emit(kind, data, ts=ts, session_id=session_id, call_id=call_id)
    memory.set_stream_cursor(memory.stream.end_cursor())  # the belief updater has seen all of this
    return len(events)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild data/memory/stream.jsonl from the per-kind files.")
    parser.add_argument("--data-root", default="data")
    args = parser.parse_args(argv)
    count = convert(Path(args.data_root))
    print(f"{count} event(s) → {Path(args.data_root) / 'memory' / 'stream'}/")
    for sid, file, row in SKIPPED:
        print(f"skipped {sid}/{file}: {str(row)[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
