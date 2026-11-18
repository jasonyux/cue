"""Replay a stage over a stored session, offline: the same windows, read from the stream, the current settings.

    python -m app.live.replay --session data/sessions/<id> --stage proposer [--limit N] [--out DIR] [--stream FILE]

Every model call a live stage made recorded its window (``windowStart``, ``windowEnd``) in the session's
``live_model_calls.jsonl``, and the session's session.json the settings it ran with. A replay reads those windows back from the stream
through the same ``Window`` the live stage used, with the settings from ``settings.yaml`` and ``.env`` as they are now (model,
thinking, screenshots, ``proposer.inputs``, ...): change them, replay, compare. Adding a kind the stored call
did not have, screenshots to the proposer say, is therefore a settings change, not a code change. New records and model
calls go to ``<session>/replays/<stamp>-<stage>/`` with a ``replay.json`` recording the settings used; the belief stage
applies its ops to a throwaway memory there. Nothing in the session folder or the stream is modified. ``replay()`` also
takes a ``strategy`` object for callers in Python. The end of the run prints the stored and replayed records side by side.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .belief.update import BeliefUpdater
from .config import LiveSettings
from .observer.summarize import Summarizer
from .proposer.propose import Proposer
from .storage.memory import MemoryStore
from .storage.pack import compressed
from .storage.paths import SessionPaths
from .storage.stream import Stream
from .utils.jsonl import read_json, read_jsonl, write_json
from .utils.llm import LiveModelClient
from .utils.time import clock, timestamp

ROLES = {"summary": "summary", "belief": "belief", "proposer": "proposer"}  # stage → kind of its rows in live_model_calls.jsonl
OUTPUTS = {"summary": "summaries", "belief": "beliefs", "proposer": "proposals"}  # stage → the session file it wrote


def prepare_output(session: SessionPaths, out: Path) -> SessionPaths:
    """A fresh folder the replayed loop writes to; its screenshots point back at the session's."""
    out.mkdir(parents=True, exist_ok=True)
    paths = SessionPaths(out).create()
    link = paths.screenshots
    if link.is_dir() and not link.is_symlink():
        link.rmdir()
    if not link.exists():
        os.symlink(session.screenshots.resolve(), link)
    return paths


def windows_of(calls: list[dict[str, Any]], stage: str) -> list[tuple[str, str]]:
    """The windows the stage's model calls recorded, oldest first."""
    return [
        (row["windowStart"], row["windowEnd"]) for row in calls
        if row.get("kind") == ROLES[stage] and row.get("windowStart") and row.get("windowEnd")
    ]


async def replay(session: SessionPaths, stage: str, *, stream: Stream, settings: LiveSettings, client: Any, out: Path,
                 strategy: Any = None, limit: int | None = None) -> list[Any]:
    """Run ``stage`` again over every window its model calls recorded. Returns the replayed records, oldest first."""
    paths = prepare_output(session, out)
    usage: dict[str, Any] = {}
    session_id = session.root.name
    own = Stream(out / "stream")  # the replayed records' own timeline, next to them
    loop: Any
    if stage == "summary":
        loop = Summarizer(paths, client, settings, stream=own, session_id=session_id, usage=usage, strategy=strategy)
    elif stage == "belief":
        loop = BeliefUpdater(
            paths,
            client,
            settings,
            MemoryStore(out).create(),
            stream=own,
            session_id=session_id,
            usage=usage,
            strategy=strategy,
        )
    else:
        loop = Proposer(paths, client, settings, stream=own, session_id=session_id, usage=usage, strategy=strategy)
    windows = windows_of(read_jsonl(session.model_calls)[0], stage)[:limit]
    replayed = [
        await loop.run_window(stream.window_at(start, end, session_id, lookback_days=settings.stream_lookback_days))
        for start, end in windows
    ]
    write_json(
        out / "replay.json",
        {
            "session": str(session.root),
            "stage": stage,
            "strategy": type(loop.strategy).__name__,
            "settings": settings.wire(),
            "windows": len(windows),
            "usage": usage.get("usage"),
            "replayedAt": timestamp(),
        },
    )
    return replayed


def side_by_side(stored: list[dict[str, Any]], replayed: list[Any], stage: str) -> str:
    lines = []
    for old, new in zip([item for item in stored if not item.get("error")], replayed, strict=False):
        span = f"[{clock(old['windowStart'])}–{clock(old['windowEnd'])}]"
        if stage == "summary":
            lines.append(f"{span}\n  stored:   {old.get('summary', '')}\n  replayed: {new.summary}")
        elif stage == "belief":
            def ops(items: list[Any]) -> str:
                return "; ".join(f"{o['op']} {o.get('task_id') or ''} {o.get('title') or o.get('progress') or ''}".strip() for o in items) or "(no ops)"
            lines.append(f"{span}\n  stored:   {ops(old.get('ops') or [])}\n  replayed: {ops([o.wire() for o in new.ops])}")
        else:
            before = "; ".join(f"[{p.get('kind')}] {p.get('task')}" for p in old.get("proposals") or []) or "(none)"
            after = "; ".join(f"[{p.kind}] {p.task}" for p in new.proposals) or "(none)"
            lines.append(f"{span}\n  stored:   {before}\n  replayed: {after}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay the summarizer, the belief updater or the proposer over a stored "
                                                 "session's windows, read from the stream, configured by the same .env / "
                                                 "settings.yaml and .env a live session reads.")
    parser.add_argument("--session", required=True, help="data/sessions/<id>")
    parser.add_argument("--stage", choices=sorted(ROLES), required=True)
    parser.add_argument("--limit", type=int, help="only the first N windows")
    parser.add_argument("--out", help="default: <session>/replays/<stamp>-<stage>")
    parser.add_argument("--stream", help="default: the data root's memory/stream folder, two folders up from the session")
    args = parser.parse_args(argv)
    from ..config import load_environment

    load_environment()
    session = SessionPaths(Path(args.session))
    if not session.session_json.is_file():
        parser.error(f"{args.session} has no session.json")
    if compressed(session):
        parser.error(f"{args.session} is compressed: restore it first (Usage → Disk in the console)")
    stream = Stream(Path(args.stream) if args.stream else session.root.parent.parent / "memory" / "stream")
    if not stream.parts():
        parser.error(f"{stream.root} has no parts; build it with python -m app.live.storage.convert, or pass --stream")
    settings = LiveSettings.load()  # one place to configure: settings.yaml and .env; replay.json records what ran
    try:
        settings.require_models()
    except ValueError as error:
        parser.error(str(error))
    endpoint = {"summary": settings.summary, "belief": settings.belief, "proposer": settings.proposer}[args.stage]
    out = Path(args.out) if args.out else session.root / "replays" / f"{timestamp()[:19].replace(':', '')}-{args.stage}"
    async def run() -> list[Any]:
        client = LiveModelClient(endpoint)
        try:
            return await replay(session, args.stage, stream=stream, settings=settings, client=client, out=out, limit=args.limit)
        finally:
            await client.close()  # in the same event loop as the calls it made

    replayed = asyncio.run(run())
    stored, _ = read_jsonl(getattr(session, OUTPUTS[args.stage]))
    print(side_by_side(stored, replayed, args.stage))
    print(f"\n{len(replayed)} window(s) replayed → {out}")
    print(json.dumps(read_json(out / "replay.json")["usage"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
