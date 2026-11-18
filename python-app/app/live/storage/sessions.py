"""Stored sessions: finding them on disk and reading older session.json files with today's keys."""

from __future__ import annotations

import re
from collections.abc import Iterator
from itertools import islice
from pathlib import Path
from typing import Any

from ...errors import RunnerCoreError
from ..utils.jsonl import read_json, read_jsonl
from .ops import replay
from .paths import SessionPaths, session_paths
from .records import BeliefUpdate, Task, as_tasks, row_kind


def validate_session_id(session_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise RunnerCoreError("Invalid session ID.", code="invalid_request", status_code=400)
    return session_id


def stored_paths(data_root: Path | str, session_id: str) -> SessionPaths:
    paths = session_paths(data_root, validate_session_id(session_id))
    if not paths.session_json.is_file():
        raise RunnerCoreError("Unknown session.", code="invalid_request", status_code=404)
    return paths


def normalize_record(record: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Fill a stored session.json from an older version with today's settings keys so the console can read it."""
    stored = record.get("settings") or {}
    settings = dict(defaults)
    if "model" in stored and "summaryModel" not in stored:  # first version: one model for both roles
        settings.update({"summaryModel": stored["model"], "proposerModel": stored["model"],
                         "summaryBaseUrl": stored.get("baseUrl"), "proposerBaseUrl": stored.get("baseUrl")})
    if "proposerObservations" in stored and "proposerInputs" not in stored:  # the setting's earlier name
        settings["proposerInputs"] = stored["proposerObservations"]
    settings.update({key: value for key, value in stored.items() if key in defaults})
    if settings.get("summaryScreenshots") or settings.get("proposerScreenshots"):
        settings["effectiveScreenshotMode"] = "always"
    else:
        settings["effectiveScreenshotMode"] = settings.get("screenshotMode", "switch")
    return {**record, "name": record.get("name"), "settings": settings, "runs": list(record.get("runs") or []),
            "chatRuns": list(record.get("chatRuns") or []),  # sessions from before the chat have none
            "stoppedAt": record.get("stoppedAt"), "stopReason": record.get("stopReason"),
            "expiresAt": record.get("expiresAt"), "usage": record.get("usage"), "lastError": record.get("lastError")}


def stored_records(root: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Every stored session's folder and session.json, newest first."""
    for folder in sorted(root.iterdir(), reverse=True):
        record = read_json(folder / "session.json")
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            yield folder, record


def previous_sessions(data_root: Path | str, limit: int = 20, defaults: dict[str, Any] | None = None, offset: int = 0,
                      named: bool | None = None) -> list[dict[str, Any]]:
    """One page of the stored sessions, newest first: ``limit`` of them from ``offset`` on, with their summary and proposal
    set counts, for the history list. ``named`` True keeps the sessions given a name, False the others, None all."""
    root = Path(data_root) / "sessions"
    if not root.is_dir():
        return []
    stored = ((folder, record) for folder, record in stored_records(root) if named is None or bool(record.get("name")) == named)
    records = []
    for folder, record in islice(stored, offset, offset + limit):
        if defaults is not None:
            record = normalize_record(record, defaults)
        paths = SessionPaths(folder)
        summaries, _ = read_jsonl(paths.summaries)
        proposals, _ = read_jsonl(paths.proposals)
        records.append({**record, "summaryCount": len(summaries), "proposalCount": len(proposals)})
    return records


def screenshot_rows(paths: SessionPaths, limit: int) -> list[dict[str, Any]]:
    """The session's stored screenshots, oldest first: when, the file name the screenshot route serves, and the window shown."""
    rows, _ = read_jsonl(paths.snapshots)
    shots = [{"ts": row.get("ts", ""), "name": row["path"], "app": row.get("app", ""), "window": row.get("window", "")}
             for row in rows if row_kind(row) == "screenshot_taken" and row.get("path")]
    return shots[-limit:]


def tasks_left(rows: list[dict[str, Any]], session_id: str) -> list[Task]:
    """The task list as the session's last belief update left it: the last row that carries the list, with the ops of the
    rows after it replayed (``ops.replay``); [] before any."""
    index = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].get("tasks") is not None), None)
    if index is None:
        return []
    later = [BeliefUpdate.parse_stored(row) for row in rows[index + 1:]]
    return replay(as_tasks(rows[index]["tasks"]), [(row.ts, session_id, row.call_id, row.ops) for row in later if not row.error])


def task_changes(paths: SessionPaths, limit: int) -> list[dict[str, Any]]:
    """The belief updates of the session that did something, oldest first: the ops applied, and the results of the calls the
    lookalike check held (``warned``) or the check refused (``rejected``), with the rounds the tick took. A tick with none is left out."""
    rows, _ = read_jsonl(paths.beliefs)
    changes = [BeliefUpdate.parse_stored(row).wire() for row in rows if (row.get("ops") or row.get("warned") or row.get("rejected")) and not row.get("error")]
    return [{key: change[key] for key in ("ts", "windowStart", "windowEnd", "ops", "callId", "rounds", "rejected", "warned", "calls")} for change in changes[-limit:]]
