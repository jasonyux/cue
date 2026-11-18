"""Backfill: the rows written before ids were minted get the id of their stream event.

    python -m app.live.storage.backfill_ids --data-root data [--write]

A row and its event share one id since ids were minted before the write (``records.new_id``, ``Stream.emit``'s ``id``).
Rows from before have none; their events do, so each takes its event's id, matched by kind, session and ``ts`` (the
envelope's ``ts`` is the row's at every emit site); a row with no event (a chat row from before the chat was in the
stream) is minted a new one, so every row has an id. Only the per-kind files are rewritten, a session's ``snapshots``,
``summaries``, ``proposals``, ``beliefs``, ``chat``, ``runs`` and ``feedback`` and the memory's ``summaries`` and
``feedback``; the stream is left as it is, since the belief updater's and the history index's cursors hold byte
offsets into it. The newest session is skipped while it runs, as the recorder appends to its files: run again once it
has stopped. A dry run unless ``--write``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..utils.jsonl import read_jsonl, rewrite_jsonl
from .memory import MemoryStore
from .paths import SessionPaths
from .records import new_id, row_kind

HEAD = re.compile(r'^\{"id": "([0-9a-f]{12})", "ts": "([^"]*)", "kind": "([a-z_]+)", "sessionId": "([^"]*)"')  # an envelope's head


def kind_of(file: str, row: dict[str, Any]) -> str:
    """The stream kind a per-kind file's row was emitted under."""
    if file == "snapshots":
        return row_kind(row)
    if file == "chat":
        return "chat_message" if row.get("role") == "user" else "chat_reply"
    if file == "runs":
        return "run_started" if row.get("event") == "started" else "run_finished"
    if file == "feedback":
        return "proposal_accepted" if row.get("decision") == "accept" else "proposal_rejected"
    return {"summaries": "summary_written", "proposals": "proposals_made", "beliefs": "belief_updated"}[file]


def event_ids(memory: MemoryStore) -> dict[tuple[str, str, str], list[str]]:
    """(kind, session, ts) → the ids of the events with that head, in order: read from the parts' line heads alone."""
    ids: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for part in memory.stream.parts():
        with part.open("r", encoding="utf-8") as handle:
            for line in handle:
                head = HEAD.match(line)
                if head is not None and line.endswith("\n"):
                    event_id, ts, kind, session_id = head.groups()
                    ids[(kind, session_id, ts)].append(event_id)
    return ids


def backfill_file(path: Path, file: str, session_of: Any, ids: dict[tuple[str, str, str], list[str]], *, write: bool) -> tuple[int, int]:
    """Give the file's rows without an id their event's, or a new one when no event has their head; (rows given their
    event's, rows minted a new one). ``session_of(row)`` names the row's session."""
    rows, _ = read_jsonl(path)
    taken: dict[tuple[str, str, str], int] = defaultdict(int)  # the same head twice: the ids in order
    given = missing = 0
    for row in rows:
        if row.get("id") or not isinstance(row.get("ts"), str):
            continue
        key = (kind_of(file, row), session_of(row), row["ts"])
        found = ids.get(key) or []
        if taken[key] < len(found):
            row["id"] = found[taken[key]]
            taken[key] += 1
            given += 1
        else:
            row["id"] = new_id()
            missing += 1
    if (given or missing) and write:
        rewrite_jsonl(path, rows)
    return given, missing


def live_session(folder: Path) -> bool:
    """Whether the session has not stopped: only the newest folder can be live (an older one that never stopped crashed)."""
    try:
        return json.loads((folder / "session.json").read_text(encoding="utf-8")).get("stoppedAt") is None
    except (OSError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Give rows from before ids their stream event's id.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--write", action="store_true", help="rewrite the files; without it, report only")
    args = parser.parse_args(argv)
    root = Path(args.data_root)
    memory = MemoryStore(root)
    ids = event_ids(memory)
    print(f"{sum(len(found) for found in ids.values())} events in the stream")
    total_given = total_missing = 0
    sessions = sorted(path for path in (root / "sessions").iterdir() if path.is_dir()) if (root / "sessions").is_dir() else []
    skipped = []
    for folder in sessions:
        if folder is sessions[-1] and live_session(folder):
            skipped.append(folder.name)
            continue
        paths = SessionPaths(folder)
        for file in ("snapshots", "summaries", "proposals", "beliefs", "chat", "runs", "feedback"):
            path = getattr(paths, file)
            if path.exists():
                given, missing = backfill_file(path, file, lambda _row, name=folder.name: name, ids, write=args.write)
                total_given += given
                total_missing += missing
                if given or missing:
                    print(f"{folder.name}/{path.name}: {given} given{f', {missing} minted (no event)' if missing else ''}")
    if not skipped:  # the backend appends to the memory's files while a session runs
        for file, path in (("summaries", memory.summaries), ("feedback", memory.feedback)):
            given, missing = backfill_file(path, file, lambda row: str(row.get("sessionId") or ""), ids, write=args.write)
            total_given += given
            total_missing += missing
            if given or missing:
                print(f"memory/{path.name}: {given} given{f', {missing} minted (no event)' if missing else ''}")
    for name in skipped:
        print(f"{name}: live, skipped (run again once it has stopped)")
    print(f"{'wrote' if args.write else 'would write'} {total_given + total_missing} ids: {total_given} from the stream, {total_missing} minted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
