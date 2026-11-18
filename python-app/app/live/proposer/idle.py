"""The idle trigger's clock: when to propose on a pause, beside the proposer's timer.

Like an editor's autocomplete, it fires once when the person stops typing and moving the mouse for ``idle_s`` seconds
after working, then holds: a second fire needs a new action (a key, a click, a scroll; a mouse that only moves is not
one) on a changed screen, and the cooldown. A person who walked away, or who is reading the suggestion, gets one
proposal, not one every few seconds; still no action ``away_s`` after a fire reads as away. A shown proposal left
unanswered lengthens the next pause it waits for (×1.5, up to ``IDLE_MAX_S``); an answer to it resets that. The
signal is the system's own input idle time (Quartz), never a screenshot; the screen it is on comes from the
observer's rows (``ScreenWatch``), where content that changes by itself (an animation, a ticker) is not a new screen.

``IdleTrigger`` is plain state over a monotonic clock, so tests drive it with numbers; the session polls it
(``LiveSessionManager._idle_loop``) and runs the fire (``proposer/quick.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..observer.digest import line_priority
from ..storage.records import row_kind
from ..utils.jsonl import read_jsonl
from ..utils.time import timestamp

OFF, ARMED, FIRING, SHOWN, AWAY = "off", "armed", "firing", "shown", "away"
IDLE_BACKOFF = 1.5  # an unanswered proposal multiplies the pause the next fire waits for
IDLE_MAX_S = 60.0   # up to this long (or idle_s, when that is longer)
SCREEN_CHANGE = 0.3  # the share of a window's lines that must differ for another screen (an animation changes a line or two)
INDEX = re.compile(r"^\[\d+\]\s?")


def input_idle_s() -> float | None:
    """Seconds since the last keyboard, mouse or trackpad input anywhere on this Mac; None where there is no Quartz."""
    try:
        import Quartz  # type: ignore[import-untyped]
    except ImportError:
        return None
    return float(Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType))


def action_idle_s() -> float | None:
    """Seconds since the last key, click or scroll: input that acts, unlike a mouse that only moves (or a hand
    resting on the trackpad). None where there is no Quartz."""
    try:
        import Quartz
    except ImportError:
        return None
    kinds = (Quartz.kCGEventKeyDown, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventRightMouseDown, Quartz.kCGEventOtherMouseDown,
             Quartz.kCGEventScrollWheel)
    return float(min(Quartz.CGEventSourceSecondsSinceLastEventType(Quartz.kCGEventSourceStateHIDSystemState, kind) for kind in kinds))


def screen_locked() -> bool:
    """Whether the Mac's screen is locked (nobody to propose to)."""
    try:
        import Quartz
    except ImportError:
        return False
    return bool((Quartz.CGSessionCopyCurrentDictionary() or {}).get("CGSSessionScreenIsLocked"))


@dataclass
class IdleTrigger:
    """One session's trigger. Times are monotonic seconds; ``step`` is called on every poll."""

    idle_s: float = 10.0
    cooldown_s: float = 30.0
    away_s: float = 120.0
    state: str = OFF
    threshold: float = 10.0          # the pause the next fire waits for: idle_s, lengthened by unanswered proposals
    armed_at: float = 0.0            # input after this moment counts as the work a pause must follow
    fired_at: float | None = None
    fired_ts: str | None = None      # the same moment as a timestamp, for the console
    fired_context: str | None = None  # the screen the fire was on
    shown: str | None = None         # the proposal set the last fire showed, while it is unanswered

    def configure(self, enabled: bool, idle_s: float, now: float) -> None:
        """Switch it on or off, or change the pause; turning it on arms it (only input after now counts)."""
        if idle_s != self.idle_s or self.state == OFF:
            self.idle_s = self.threshold = idle_s
        if not enabled:
            self.state = OFF
        elif self.state == OFF:
            self.state, self.armed_at = ARMED, now

    def step(self, now: float, idle: float, context: str | None, ready: bool, acted: float | None = None) -> bool:
        """One poll: ``idle`` seconds since the last input, ``acted`` since the last action (key, click, scroll; None:
        the same as ``idle``), ``context`` the screen in front, ``ready`` whether a fire may happen now (the session's
        own checks). True: fire now."""
        if self.state in (SHOWN, AWAY) and self.fired_at is not None:
            if self.resumed(now, idle if acted is None else acted):
                if context != self.fired_context and now - self.fired_at >= self.cooldown_s:
                    self._rearm()
            elif self.state == SHOWN and now - self.fired_at >= self.away_s:
                self.state = AWAY
        if self.state == ARMED and ready and now - idle > self.armed_at and idle >= self.threshold:
            self.state, self.fired_at, self.fired_ts, self.fired_context = FIRING, now, timestamp(), context
            return True
        return False

    def resumed(self, now: float, idle: float) -> bool:
        """Whether there was input (``idle`` seconds ago) since the last fire: its proposal came too late, or the person
        is back. The session passes the time since the last action, so a mouse that only moved does not count."""
        return self.fired_at is not None and now - idle > self.fired_at

    def landed(self, shown: str | None) -> None:
        """The fire is done; ``shown`` is the proposal set it put in front of the person (None: nothing shown)."""
        if self.state == FIRING:
            self.state, self.shown = SHOWN, shown

    def answered(self, proposals_ts: str) -> None:
        """The person accepted or rejected a proposal set: when it was this trigger's, the pause is back to idle_s."""
        if proposals_ts == self.shown:
            self.shown, self.threshold = None, self.idle_s

    def _rearm(self) -> None:
        if self.shown is not None:  # shown and left unanswered: wait longer before the next one
            self.threshold = min(self.threshold * IDLE_BACKOFF, max(IDLE_MAX_S, self.idle_s))
            self.shown = None
        self.state, self.armed_at = ARMED, self.fired_at or 0.0

    def wire(self, idle: float | None) -> dict[str, Any]:
        return {"enabled": self.state != OFF, "state": self.state, "idleS": self.idle_s, "thresholdS": round(self.threshold, 1),
                "inputIdleS": None if idle is None else round(idle, 1), "firedAt": self.fired_ts}


def screen_lines(tree: str) -> frozenset[str]:
    """A tree's element lines without their indices: one element more or less renumbers every line after it."""
    return frozenset(INDEX.sub("", line) for line in tree.splitlines()[1:] if line.strip())


def moved_on(before: frozenset[str], after: frozenset[str]) -> bool:
    """Whether a window's content changed enough to be another screen: what is typed or focused, or a share of its lines
    (scrolling moves them all), not a line or two that change by themselves."""
    if not before or not after:
        return before != after
    if any(line_priority(line, None) == 0 for line in before ^ after):
        return True
    return 1 - len(before & after) / len(before | after) >= SCREEN_CHANGE


class ScreenWatch:
    """A session's observer rows as they are appended: the newest look, the newest readable one, and a key that
    changes when the screen does: another app, window or selection, or ``moved_on`` from the lines the key was set on. A changed
    look whose tree differs only in content that moves by itself keeps the key, so it does not re-arm the trigger."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.latest: dict[str, Any] | None = None    # the newest look, changed or not
        self.readable: dict[str, Any] | None = None  # the newest changed look, when it has a tree the observer could read
        self.context: str | None = None
        self.window: tuple[Any, ...] | None = None  # the app, window and selection the key was set on
        self.lines: frozenset[str] = frozenset()    # and their lines
        self.screens = 0
        self.frame: str | None = None  # the time of the newest frame (a screenshot is stamped with its look's time)

    def update(self) -> None:
        rows, self.offset = read_jsonl(self.path, self.offset)
        for row in rows:
            kind = row_kind(row)
            if kind == "screenshot_taken":
                self.frame = str(row.get("ts") or "")
            if kind not in ("screen_changed", "screen_unchanged"):
                continue
            self.latest = row
            if kind == "screen_changed":  # an unchanged look is the same screen: both stay
                window, lines = (row.get("app"), row.get("window"), row.get("selection")), screen_lines(row.get("tree") or "")
                if window != self.window or moved_on(self.lines, lines):
                    self.screens += 1
                    self.context, self.window, self.lines = f"{window[0]}\x1f{window[1]}\x1f{self.screens}", window, lines
                self.readable = row if row.get("tree") and not row.get("excluded") and not row.get("error") else None

    def in_front(self) -> dict[str, Any] | None:
        """The newest look when it is readable now (not blocked, not failed), else None."""
        latest = self.latest
        if latest is None or latest.get("excluded") or latest.get("error"):
            return None
        return self.readable
