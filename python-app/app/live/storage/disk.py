"""Where the data folder's bytes are, for the console's disk panel.

Each session folder is split by what fills it (the observer's screenshots, the model-call log, the files of its runs,
the screen reads; the small records together as "other"), and the rest of the data folder by what it is (the memory,
the agent's browser profile; anything else as "other"), so the parts add up to the folder and a person short of space
sees what deleting a session, or one kind of file, would free. Sizes are file sizes, summed from the folder on each
request: nothing is cached or written.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..utils.jsonl import read_json
from .pack import CALLS, INDEX, PACK, compressed
from .paths import SessionPaths

_FILES = SessionPaths(Path())
SESSION_KINDS = {  # a session folder's entry → its kind
    _FILES.screenshots.name: "screenshots",
    PACK: "screenshots",
    INDEX: "screenshots",
    _FILES.model_calls.name: "modelCalls",
    CALLS: "modelCalls",
    _FILES.runs_dir.name: "runs",
    _FILES.snapshots.name: "screenReads",
}
REST_KINDS = {"memory": "memory", "agent-browser": "agentBrowser"}  # the data folder's entries beside its sessions


def size_of(entry: os.DirEntry[str]) -> int:
    """A file's bytes, or those of every file under a folder; links are not followed."""
    try:
        if not entry.is_dir(follow_symlinks=False):
            return entry.stat(follow_symlinks=False).st_size
        return sum(size_of(item) for item in os.scandir(entry.path))
    except FileNotFoundError:  # removed since it was listed: a running session and the agent's browser write and remove files meanwhile
        return 0


def by_kind(entries: Iterable[os.DirEntry[str]], kinds: dict[str, str]) -> dict[str, int]:
    """The entries' bytes per kind, by name; an entry of no kind counts as "other"."""
    sizes = dict.fromkeys((*kinds.values(), "other"), 0)
    for entry in entries:
        sizes[kinds.get(entry.name, "other")] += size_of(entry)
    return sizes


def disk_report(data_root: Path) -> dict[str, Any]:
    """The data folder's bytes: each session by kind (oldest first) and whether it is compressed (``pack.py``), the
    rest by kind, and what its volume has free."""
    sessions = data_root / "sessions"
    stored = [SessionPaths(folder) for folder in sorted(sessions.iterdir()) if (folder / "session.json").is_file()] if sessions.is_dir() else []
    names = {paths.root.name for paths in stored}
    rest = by_kind((entry for entry in os.scandir(data_root) if entry.name != sessions.name), REST_KINDS)
    rest["other"] += sum(size_of(entry) for entry in os.scandir(sessions) if entry.name not in names) if sessions.is_dir() else 0  # a folder there that is no session
    return {
        "root": str(data_root),
        "free": shutil.disk_usage(data_root).free,
        "sessions": [{"id": paths.root.name, "name": (read_json(paths.session_json) or {}).get("name"), "compressed": compressed(paths), **by_kind(os.scandir(paths.root), SESSION_KINDS)} for paths in stored],
        "rest": rest,
    }
