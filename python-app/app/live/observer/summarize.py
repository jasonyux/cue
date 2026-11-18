"""Summarize: every minute or so, the new snapshots become one worklog line (thinking off by default, fast).

Pipeline position: this session's observer rows in the stream → digest (text) [+ screenshots] → one model call →
summaries.jsonl, and the stream.
``Summarizer.run_window`` is the whole step, top to bottom; ``WorklogSummaryStrategy`` is the default strategy. To try
another way of summarizing, implement ``messages`` and ``parse`` against ``SummaryInput`` → ``Summary`` and
pass it as ``strategy``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from ..config import LiveSettings
from ..model_loop import ModelLoop, StageInput, Strategy
from ..storage.paths import SessionPaths
from ..storage.records import Snapshot, Summary
from ..storage.stream import Cursor, Stream, Window
from ..utils.llm import LiveModelClient, ModelReply, parse_json_object
from .digest import build_window_digest
from .prompts import build_summary_messages

CONTEXT_LINES = 5  # the previous worklog lines the model is shown, for continuity: all it needs to write the next one


class SummaryInputs(TypedDict):
    """What the summarizer is shown, by name."""

    snapshots: list[Snapshot]   # this session's observer rows in the window
    worklog: list[Summary]      # this session's last CONTEXT_LINES worklog lines, so the new line continues them


@dataclass
class SummaryInput(StageInput[SummaryInputs]):
    """One summarizer window."""


class WorklogSummaryStrategy:
    """One or two sentences on what the person did in the window, as a JSON object."""

    def messages(self, inputs: SummaryInput) -> list[dict[str, Any]]:
        seen = inputs.input
        digest = build_window_digest(seen["snapshots"], max_chars=16_000, tree_chars=1800)
        return build_summary_messages(now=inputs.now, previous=seen["worklog"], digest=digest, screenshots=inputs.screenshots)

    def parse(self, reply: ModelReply, inputs: SummaryInput) -> Summary:
        try:
            text = str(parse_json_object(reply.content).get("summary") or "").strip() or "(empty summary)"
        except ValueError:
            text = reply.content.strip()[:400] or "(no summary)"
        return Summary(summary=text)


class Summarizer(ModelLoop[SummaryInput, Summary]):
    role = "summary"
    output = "summaries"
    event = "summary_written"

    def __init__(self, paths: SessionPaths, client: LiveModelClient, settings: LiveSettings, *, stream: Stream, session_id: str,
                 usage: dict[str, Any] | None = None, strategy: Strategy[SummaryInput, Summary] | None = None) -> None:
        super().__init__(paths, client, settings, strategy or WorklogSummaryStrategy(), stream=stream, session_id=session_id, usage=usage)

    def wants_images(self) -> bool:
        return self.settings.summary_screenshots

    def thinking(self) -> bool:
        return self.settings.summary_thinking

    def max_tokens(self) -> int | None:
        return self.settings.summary_max_tokens or None

    async def run_window(self, window: Window, *, finalize: Callable[[Summary], Summary] | None = None,
                         end_cursor: Cursor | None = None) -> Summary:
        """One window of observer rows → one worklog line."""
        rows = window.observer_rows()
        given: SummaryInputs = {"snapshots": rows, "worklog": window.last_worklog(CONTEXT_LINES)}
        inputs = SummaryInput(
            now=window.end,
            window_start=window.start,
            input=given,
            screenshots=self.screenshots(rows),
            settings=self.settings,
        )
        messages = self.strategy.messages(inputs)
        reply = await self.call_model(messages, [shot.url for shot in inputs.screenshots], window=window)
        summary = self.strategy.parse(reply, inputs)
        return self.write(summary, reply, rows, window=window, finalize=finalize)
