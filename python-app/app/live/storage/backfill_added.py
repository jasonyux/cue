"""Dates checklist entries from before ``addedAt`` was stored: each entry's time is the model call's or operator edit's
that added it, found by the entry's ``callId`` in every session's live_model_calls.jsonl (a call's row starts with its id
and time) and in the ops log, data/memory/events.jsonl (an edit's row carries its callId). Entries whose call is not found
stay as they are.

    python -m app.live.storage.backfill_added --data-root data [--write]

Without --write it only reports. Run it with the backend stopped: it rewrites data/memory/tasks.json.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from ..utils.jsonl import read_json, write_json

HEAD = re.compile(r'^\{"id": "([0-9a-f]{12})", "ts": "([^"]+)"')


def call_times(root: Path) -> dict[str, str]:
    """Every call id the data root knows → when it was made."""
    when: dict[str, str] = {}
    for path in [*root.glob("sessions/*/live_model_calls.jsonl"), *root.glob("sessions/*/events.jsonl"), root / "memory" / "events.jsonl"]:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                head = HEAD.match(line)
                if head:
                    when.setdefault(head.group(1), head.group(2))
                elif '"callId"' in line[:400]:
                    row = json.loads(line)
                    if row.get("callId") and row.get("ts"):
                        when.setdefault(row["callId"], row["ts"])
    return when


def main() -> None:
    parser = argparse.ArgumentParser(description="Date checklist entries from the call that added them.")
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--write", action="store_true", help="rewrite tasks.json; without it, only report")
    args = parser.parse_args()
    tasks_file = args.data_root / "memory" / "tasks.json"
    data = read_json(tasks_file) or {}
    when = call_times(args.data_root)
    dated = unknown = 0
    for task in data.get("tasks") or []:
        for step in task.get("steps") or []:
            if step.get("addedAt"):
                continue
            ts = when.get(step.get("callId") or "")
            if ts:
                step["addedAt"] = ts
                dated += 1
            else:
                unknown += 1
    print(f"{dated} entries dated, {unknown} without a known call; {'written' if args.write else 'not written (add --write)'}")
    if args.write and dated:
        write_json(tasks_file, data)


if __name__ == "__main__":
    main()
