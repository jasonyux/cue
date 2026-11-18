"""The recorder: the observer subprocess that writes snapshots.jsonl, one snapshot every few seconds.

"The window the user is working in" means the focused (key) window of the frontmost application: the one that
receives their keystrokes. Other windows of the same app, and other apps, are not read on that tick.

Runs as ``python -m app.live.observer.recorder --session-dir <dir>`` so accessibility calls never block the server.
Identical consecutive reads collapse into screen_unchanged rows. Screenshots follow ``--screenshots``: on each app
switch (default), on every distinct snapshot (needed when a model loop wants images), or never. The permissions
(settings.yaml, ``permissions``) decide per look what is read; the recorder reads them again whenever the file changes.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ...config import SETTINGS_FILE
from ..config import SCREENSHOT_MODES, LiveSettings
from ..permissions import Reason
from ..proposer.notify import send_notification
from ..storage.paths import SessionPaths
from ..storage.records import new_id
from ..storage.stream import Stream
from ..utils.jsonl import append_jsonl
from ..utils.time import timestamp
from .sources import AccessibilitySource, Snapshot, Source

WAKE_S = 0.5  # the nap between looks: a changed rule (an answer, a Save) or a stop is noticed within this


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "-" for character in value)[:40] or "app"


def shoot(source: Source, paths: SessionPaths, stream: Stream, snapshot: Snapshot) -> None:
    """The look's frame, saved under screenshots/ and recorded as a screenshot_taken row; best effort."""
    try:
        image = source.screenshot()
    except Exception:  # noqa: BLE001 - screenshots are best effort
        image = None
    if image is not None:
        name = f"{snapshot.ts.replace(':', '-')}-{safe_name(snapshot.app)}.png"
        image.save(paths.screenshots / name)
        record(paths, stream, {"ts": snapshot.ts, "kind": "screenshot_taken", "app": snapshot.app, "window": snapshot.window, "path": name})


def record(paths: SessionPaths, stream: Stream, row: dict[str, Any]) -> None:
    """One observer row: to the session's snapshots.jsonl, and to the stream under the same kind and id."""
    row = {"id": new_id(), **row}
    append_jsonl(paths.snapshots, row)
    stream.emit(row["kind"], row, ts=row["ts"], session_id=paths.root.name, id=row["id"])


def run_observer(
    source: Source,
    paths: SessionPaths,
    *,
    stream: Stream,
    interval: float,
    screenshot_mode: str = "switch",
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_ticks: int | None = None,
    refresh: Callable[[], object] = lambda: None,  # rereads the rules; truthy when they changed
    notify: Callable[[Snapshot], None] = lambda snapshot: None,
) -> int:
    """The observation loop. Returns the number of ticks taken (tests inject a fake source and clock)."""
    if screenshot_mode not in SCREENSHOT_MODES:
        raise ValueError(f"screenshot_mode must be one of {SCREENSHOT_MODES}.")
    last_hash: str | None = None
    last_app: str | None = None
    last_excluded = False
    last_stub: Snapshot | None = None  # the latest look that was blocked, while it is the latest
    changed = False  # the rules changed since the last look
    asked: set[str] = set()  # the apps whose question was notified: once each per session
    ticks = 0
    asked_now = False  # the next look was asked for by the idle trigger (the look flag)
    while not should_stop():
        started = clock()
        changed = bool(refresh()) or changed
        if paths.pause_flag.exists():
            record(paths, stream, {"ts": timestamp(), "kind": "observer_paused"})
        else:
            # The rules changed while the latest look was a stub: that app again, now, wherever its window is (the answer was given on
            # the console, and the person expects to see the app it was about). Still blocked: a stub with the new reason.
            again = last_stub if changed and last_stub is not None and last_stub.pid else None
            changed = False
            try:
                snapshot = source.snapshot(again)
            except Exception as error:  # noqa: BLE001 - keep observing through transient failures
                snapshot = Snapshot(ts=timestamp(), app="", error=f"{type(error).__name__}: {error}"[:300])
            if snapshot is None:  # the console itself: nothing recorded, nothing changes
                pass
            elif snapshot.hash and snapshot.hash == last_hash:
                record(
                    paths,
                    stream,
                    {"ts": snapshot.ts, "kind": "screen_unchanged", "app": snapshot.app, "window": snapshot.window,
                     **({"excluded": True, "reason": snapshot.reason, "pid": snapshot.pid, "bundle": snapshot.bundle} if snapshot.excluded else {})},  # still blocked: the monitor says so
                )
                if asked_now and not snapshot.excluded and snapshot.error is None:  # the idle trigger sees the screen, changed or not
                    shoot(source, paths, stream, snapshot)
            else:
                record(paths, stream, snapshot.wire())
                if snapshot.excluded and snapshot.reason == Reason.ASK and snapshot.app not in asked:
                    asked.add(snapshot.app)
                    notify(snapshot)
                observed = not snapshot.excluded and snapshot.error is None
                # A switch: another app, or the same app after a stub (blocked, or asked and now answered): a fresh frame either way.
                wanted = screenshot_mode == "always" or (screenshot_mode == "switch" and (snapshot.app != last_app or last_excluded))
                if observed and (wanted or asked_now):  # a look the idle trigger asked for always has its frame
                    shoot(source, paths, stream, snapshot)
                last_hash = snapshot.hash or None
                if observed or snapshot.excluded:  # a blocked app counts as a switch: coming back takes a fresh frame
                    last_app, last_excluded = snapshot.app, snapshot.excluded
                    last_stub = snapshot if snapshot.excluded else None
        asked_now = False
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        remaining = max(0.0, interval - (clock() - started))
        while remaining > 0 and not should_stop():  # in naps, so a changed rule is applied by a look within WAKE_S
            nap = min(WAKE_S, remaining)
            sleep(nap)
            remaining -= nap
            if refresh():
                changed = True
                break
            if paths.look_flag.exists():  # the idle trigger wants the screen as it is now, not as it was at the last look
                paths.look_flag.unlink(missing_ok=True)
                asked_now = True
                break
    return ticks


def settings_stamp(paths: SessionPaths) -> tuple[int | None, ...]:
    """When settings.yaml and the session's own were last written: a change to either means the rules are read again."""
    return tuple(path.stat().st_mtime_ns if path.exists() else None for path in (SETTINGS_FILE, paths.settings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Continuous accessibility observer.")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--stream", required=True, help="data/memory/stream, the folder of daily parts: every row goes there too")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--max-elements", type=int, default=400)
    parser.add_argument("--screenshots", choices=SCREENSHOT_MODES, default="switch")
    args = parser.parse_args(argv)
    paths = SessionPaths(Path(args.session_dir)).create()
    stream = Stream(Path(args.stream))
    stopping = False

    def request_stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, request_stop)
    settings = LiveSettings.load()
    source = AccessibilitySource(max_elements=args.max_elements, permissions=settings.permissions_with(paths.settings))
    seen = [settings_stamp(paths)]

    def refresh() -> bool:
        """The rules again when settings.yaml or the session's own changed (a Save, an answer, a hand edit); a file that
        fails to load keeps the last ones. True when they changed."""
        if settings_stamp(paths) == seen[0]:
            return False
        seen[0] = settings_stamp(paths)
        try:
            source.permissions = LiveSettings.load().permissions_with(paths.settings)
        except Exception as error:  # noqa: BLE001 - a half-written file; the next change is read again
            print(f"permissions not reloaded: {error}", file=sys.stderr)
        return True

    def notify(snapshot: Snapshot) -> None:
        """The first time an app asks in this session: a notification, so the question is seen with the page hidden."""
        if settings.notify:
            send_notification("Cue", f"Cue wants to look at {snapshot.app}: answer on the Zen page.")

    record(paths, stream, {"ts": timestamp(), "kind": "observer_started", "interval": args.interval, "screenshots": args.screenshots})
    run_observer(source, paths, stream=stream, interval=args.interval, screenshot_mode=args.screenshots, should_stop=lambda: stopping, refresh=refresh,
                 notify=notify)
    record(paths, stream, {"ts": timestamp(), "kind": "observer_stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
