"""Timestamps and clocks shared by every stage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def plus_seconds(seconds: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clock(ts: str) -> str:
    """HH:MM:SS in local time from an ISO timestamp."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ts[11:19]


def date_clock(ts: str) -> str:
    """YYYY-MM-DD HH:MM in local time from an ISO timestamp."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return f"{ts[:10]} {ts[11:16]}"


def seconds_between(start: str, end: str) -> float:
    try:
        first = datetime.fromisoformat(start.replace("Z", "+00:00"))
        last = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0.0, (last - first).total_seconds())
    except ValueError:
        return 0.0


def shifted(ts: str, seconds: float) -> str:
    """``ts`` moved by ``seconds``."""
    moment = datetime.fromisoformat(ts.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
