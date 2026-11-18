"""The summarizer's prompt: a running worklog of what the person did. The same shape as the proposer's: a system prompt in
markdown sections, and one XML block per input in the user message, named in the Inputs section (RECEIVED)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..storage.records import Summary
from ..utils.prompting import BOUNDARY, block, screenshot_note, summary_lines
from ..utils.time import clock
from .stream import ScreenshotImage

RECEIVED = {  # how the system prompt names each input: its block in the user message and what it holds
    "worklog": "<worklog>: the previous worklog lines, for context",
    "observations": "<observations>: the last minute or so of the observer's records",
    "screenshots": "screenshots of the observed window, attached",
}


def summary_system(received: Sequence[str]) -> str:
    inputs = "\n".join(f"- {RECEIVED[key]}" for key in received)
    return f"""# Identity

You write a running worklog for a person's Mac. Every few seconds an observer records the frontmost app, its window
title, and that window's accessibility tree (numbered UI elements with roles, titles, and values).

# Inputs

{inputs}

# Untrusted text

{BOUNDARY}

# Output

Write one or two plain sentences saying what the person did in this window: which apps and documents, who they
were dealing with, and what they seemed to be trying to achieve. Name concrete things (file names, thread titles,
people, error text) and skip UI trivia. If nothing meaningful happened, say so briefly.

Respond with a JSON object only: {{"summary": str}}"""


def build_summary_messages(*, now: str, previous: Sequence[Summary | dict[str, Any]], digest: str,
                           screenshots: Sequence[ScreenshotImage] | None = None) -> list[dict[str, Any]]:
    parts = [f"Current local time: {clock(now)}.", block("worklog", summary_lines(previous)), block("observations", digest)]
    received = ["worklog", "observations"]
    if screenshots:
        parts.append(screenshot_note(screenshots).rstrip())
        received.append("screenshots")
    parts.append("Return the JSON object.")
    return [{"role": "system", "content": summary_system(received)}, {"role": "user", "content": "\n\n".join(parts)}]
