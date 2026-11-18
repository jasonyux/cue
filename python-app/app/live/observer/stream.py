"""Reading observer rows for a model call: whether a batch is worth one, and the screenshots to attach."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..storage.pack import screenshot_source
from ..storage.paths import SessionPaths
from ..storage.records import Snapshot, as_snapshots
from ..storage.stream import CONTROL_KINDS
from ..utils.images import encode_screenshot

IDLE_KINDS = CONTROL_KINDS + ("observer_paused", "screenshot_taken")


@dataclass
class ScreenshotImage:
    """A stored capture encoded for a model call."""

    ts: str
    app: str
    window: str
    path: str
    url: str


def has_activity(observations: Sequence[Snapshot | dict[str, Any]]) -> bool:
    """True when the batch holds something worth a model call, not just pause markers or screenshots."""
    return any(item.kind not in IDLE_KINDS for item in as_snapshots(observations))


def latest_screenshots(observations: Sequence[Snapshot | dict[str, Any]], paths: SessionPaths, *, limit: int = 2,
                       max_side: int = 1024) -> list[ScreenshotImage]:
    """The newest ``limit`` screenshot records of a batch, oldest first, each encoded as a data URL."""
    records = [item for item in as_snapshots(observations) if item.kind == "screenshot_taken" and item.path]
    chosen: list[ScreenshotImage] = []
    for record in reversed(records):
        url = encode_screenshot(screenshot_source(paths, str(record.path)), max_side)
        if url is None:
            continue
        chosen.append(ScreenshotImage(ts=record.ts, app=record.app, window=record.window, path=str(record.path), url=url))
        if len(chosen) >= limit:
            break
    chosen.reverse()
    return chosen
