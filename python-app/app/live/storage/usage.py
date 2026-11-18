"""Token use and cost per model and module, read from the stored session files.

Every stage call is a row in summaries.jsonl, beliefs.jsonl, proposals.jsonl or preferences.jsonl carrying ``model`` and the usage block the
endpoint reported; a task run's run.json carries its accumulated usage per model. Nothing is kept in memory or written
back: the console's cost views are computed from the files on each request, for the running session, the last few days,
or everything stored, as totals or split by day. Prices come from ``app.usage`` (the price table plus
``CUA_MODEL_PRICES``); a model the table does not price costs what its runs reported themselves (a cmux run stores Claude
Code's own cost).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from ...usage import COUNTERS, estimate_cost, record_usage
from ..utils.jsonl import read_json, read_jsonl
from .paths import SessionPaths

MODULES = {"summaries": "summarizer", "beliefs": "belief updater", "proposals": "proposer",
           "feedback": "proposer: feedback digest", "preferences": "preference updater"}  # file → module; a digest is listed right under the stage it serves
FILTER = "proposer: duplicate filter"  # the embeddings call on a proposals.jsonl row (``embedding``), listed under the proposer too
LOOKALIKES = "belief updater: lookalike check"  # the same on a beliefs.jsonl row
AGENT = "agent"  # task runs started from the session
ORDER = (*MODULES.values(), FILTER, LOOKALIKES, AGENT)


def _entries(sessions: Iterable[SessionPaths], since: str | None, until: str | None) -> Iterator[tuple[str, str, str, dict[str, Any]]]:
    """Everything that cost something, as (ts, model, module, counters): each stage call, each embeddings call, and each
    run's totals per model. ``since`` and ``until`` are ISO timestamps: only what started in [since, until) counts."""
    def within(ts: str) -> bool:
        return (since is None or ts >= since) and (until is None or ts < until)

    for paths in sessions:
        for file, module in MODULES.items():
            for record in read_jsonl(getattr(paths, file))[0]:
                ts = record.get("ts", "")
                if not within(ts):
                    continue
                if record.get("usage") and record.get("model"):
                    yield ts, record["model"], module, record_usage({}, record["model"], record["usage"])["byModel"][0]
                if file in ("proposals", "beliefs") and record.get("embedding"):
                    embedding = record["embedding"]
                    yield ts, embedding["model"], FILTER if file == "proposals" else LOOKALIKES, record_usage({}, embedding["model"], embedding["usage"])["byModel"][0]
        for run_dir in sorted(paths.runs_dir.iterdir()) if paths.runs_dir.is_dir() else []:
            run = read_json(run_dir / "run.json")
            if isinstance(run, dict) and run.get("usage") and within(run.get("startedAt", "")):
                for entry in run["usage"].get("byModel") or []:
                    yield run.get("startedAt", ""), entry["model"], AGENT, entry


def _rows(entries: Iterable[tuple[str, str, str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """One row per (model, module) with the ``app.usage`` counters and ``costUsd`` when the model has a price or its runs
    reported their cost."""
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for _ts, model, module, counts in entries:
        entry = totals.setdefault((model, module), {"model": model, "module": module, **{key: 0 for key in COUNTERS}, "reported": 0.0})
        for key in COUNTERS:
            entry[key] += int(counts.get(key) or 0)
        entry["reported"] += float(counts.get("costUsd") or 0)
    rows = []
    for (model, module), entry in sorted(totals.items(), key=lambda item: (item[0][0], ORDER.index(item[0][1]))):
        reported = entry.pop("reported")
        cost = estimate_cost(model, entry)
        if cost is None and reported:
            cost = round(reported, 6)
        rows.append({**entry, "costUsd": cost} if cost is not None else entry)
    return rows


def usage_rows(sessions: Iterable[SessionPaths], since: str | None = None, until: str | None = None) -> list[dict[str, Any]]:
    """The rows over everything in the sessions that started in [since, until)."""
    return _rows(_entries(sessions, since, until))


def usage_by_day(sessions: Iterable[SessionPaths], since: str | None = None) -> list[dict[str, Any]]:
    """The same rows split by UTC day, oldest day first, only days that cost something: ``[{"day", "rows"}]``."""
    days: dict[str, list[tuple[str, str, str, dict[str, Any]]]] = {}
    for entry in _entries(sessions, since, None):
        days.setdefault(entry[0][:10], []).append(entry)
    return [{"day": day, "rows": _rows(entries)} for day, entries in sorted(days.items())]
