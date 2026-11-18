"""Base class of the two model loops, the summarizer and the proposer, and the Strategy each one hosts.

``tick`` is shared: it reads what arrived in the stream since the last tick (``pending``: this session's observer
rows, or for the belief updater every session's summaries), skips batches with nothing in them, and hands the stage a
``Window`` over the stream, from the batch's first event to now; the stage's own ``run_window``, written out explicitly
in ``observer/summarize.py``, ``belief/update.py`` and ``proposer/propose.py``, pulls its inputs from the window, asks
the strategy for the messages, calls the model, parses, writes. A replay hands the same ``run_window`` a window with
the bounds a model call recorded, so the two are one path. The helpers those lines call live here: screenshot
selection, the model call with its log and truncation fallback, and writing the record with its timing and usage fields.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar
from uuid import uuid4

from ..usage import record_usage
from .config import LiveSettings
from .observer.stream import ScreenshotImage, has_activity, latest_screenshots
from .storage.paths import SessionPaths
from .storage.records import LiveRecord, ModelCall, Snapshot, StreamEvent, new_id
from .storage.stream import OBSERVER_KINDS, Cursor, Stream, Window
from .utils.jsonl import append_jsonl
from .utils.llm import LiveModelClient, ModelReply, truncated
from .utils.time import timestamp

O = TypeVar("O")


@dataclass
class StageInput(Generic[O]):
    """What every strategy receives: the records the stage is shown, and the screenshots chosen for them.

    ``input`` is typed per stage and can be any combination of things. The summarizer gets the observer rows of the
    window; the belief updater the new worklog lines; the proposer a dict keyed by input name (``tasks``, ``worklog``,
    ``snapshots``, ``current_window``, ``feedback``) holding only the inputs its setting allows, so a strategy picks
    them by name.
    """

    now: str
    window_start: str
    input: O
    screenshots: list[ScreenshotImage]
    settings: LiveSettings


I_contra = TypeVar("I_contra", bound=StageInput[Any], contravariant=True)
R_co = TypeVar("R_co", bound=LiveRecord, covariant=True)


class Strategy(Protocol[I_contra, R_co]):
    """The two decisions a stage makes: what to ask the model, and what record its answer becomes."""

    def messages(self, inputs: I_contra) -> list[dict[str, Any]]: ...

    def parse(self, reply: ModelReply, inputs: I_contra) -> R_co: ...


I = TypeVar("I", bound=StageInput[Any])
R = TypeVar("R", bound=LiveRecord)


class ModelLoop(Generic[I, R]):
    role = "model"        # kind in live_model_calls.jsonl
    output = "summaries"  # the SessionPaths attribute the records go to
    event = "summary_written"  # the records' kind in the stream, data/memory/stream.jsonl

    def __init__(self, paths: SessionPaths, client: LiveModelClient, settings: LiveSettings,
                 strategy: Strategy[I, R], *, stream: Stream, session_id: str, usage: dict[str, Any] | None = None) -> None:
        # ``usage`` is a run-like holder: record_usage() accumulates totals under its "usage" key.
        self.paths = paths
        self.stream = stream
        self.session_id = session_id
        self.client = client
        self.settings = settings
        self.strategy = strategy
        self.usage = usage if usage is not None else {}
        self.cursor = Cursor()  # where in the stream this loop has read up to
        self.ticks = 0

    # ----- per-role settings, supplied by the subclass ---------------------------------------------------

    def wants_images(self) -> bool:
        raise NotImplementedError

    def thinking(self) -> bool:
        raise NotImplementedError

    def max_tokens(self) -> int | None:
        """The role's output budget from the settings, or None for the client's automatic one."""
        raise NotImplementedError

    # ----- the shared tick and the stage's own window ----------------------------------------------------

    async def tick(self, *, force: bool = False, now: str | None = None,
                   finalize: Callable[[R], R] | None = None) -> R | None:
        """Read what is new in the stream since the last tick and run one window over it, from the first new event to
        now. None when nothing worth a call arrived.

        ``finalize`` may amend the record before it is written (the session applies notification this way).
        """
        now = now or timestamp()
        batch, next_cursor = self.pending()
        if not force and not self.worth_a_call(batch):
            self.cursor = next_cursor  # keep the cursor moving, skip the model call
            return None
        window = self.stream.window_at(
            self.window_start(batch) if batch else now,
            now,
            self.session_id,
            lookback_days=self.settings.stream_lookback_days,
        )
        record = await self.run_window(window, finalize=finalize, end_cursor=next_cursor)
        self.cursor = next_cursor
        return record

    def pending(self) -> tuple[list[StreamEvent], Cursor]:
        """The events this stage reads that arrived since the last tick, and the cursor after them."""
        events, cursor = self.stream.read(self.cursor)
        return [event for event in events if self.reads(event)], cursor

    def reads(self, event: StreamEvent) -> bool:
        """Whether a stream event is this stage's input. Default: this session's observer rows."""
        return event.kind in OBSERVER_KINDS and event.session_id == self.session_id

    def worth_a_call(self, batch: list[StreamEvent]) -> bool:
        """False when the batch holds nothing to reason about (pause markers and screenshots only)."""
        return has_activity([Snapshot.model_validate(event.data) for event in batch])

    def window_start(self, batch: list[StreamEvent]) -> str:
        """Where the window over a batch begins. Default: the first event's time."""
        return batch[0].ts

    async def run_window(self, window: Window, *, finalize: Callable[[R], R] | None = None,
                         end_cursor: Cursor | None = None) -> R:
        """One window → one record, written at ``window.end`` (now, live; the recorded end in a replay). Each stage
        writes this out in its own file. ``end_cursor`` is where the batch ends in the stream, for a stage that keeps
        its own cursor."""
        raise NotImplementedError

    # ----- helpers the stages call --------------------------------------------------------------------

    def screenshots(self, observations: list[Snapshot]) -> list[ScreenshotImage]:
        """The newest window captures in the batch, when this loop is configured to see them (models are multimodal)."""
        if not self.wants_images():
            return []
        return latest_screenshots(observations, self.paths, limit=2, max_side=self.settings.screenshot_max_side)

    async def call_model(self, messages: list[dict[str, Any]], images: list[str], *, window: Window,
                         tools: list[dict[str, Any]] | None = None, tool_choice: str = "auto") -> ModelReply:
        """One completion, logged to live_model_calls.jsonl with the window's bounds; retried without thinking when
        reasoning ate the budget."""
        reply = await self.client.complete(messages, thinking=self.thinking(), images=images or None,
                                           max_tokens=self.max_tokens(), tools=tools, tool_choice=tool_choice)
        if self.thinking() and truncated(reply):
            # Keep the cut-off attempt in the log, then answer without thinking so the window still yields a record.
            truncated_call = ModelCall(
                id=uuid4().hex[:12],
                ts=timestamp(),
                kind=f"{self.role}_truncated",
                request=reply.request,
                response=reply.response,
            )
            append_jsonl(self.paths.model_calls, truncated_call.wire())
            record_usage(self.usage, reply.model, reply.usage)
            reply = await self.client.complete(messages, thinking=False, images=images or None, max_tokens=self.max_tokens(),
                                               tools=tools, tool_choice=tool_choice)
            reply.fallback = "thinking_truncated"
        reply.call_id = uuid4().hex[:12]
        call = ModelCall(
            id=reply.call_id,
            ts=window.end,
            kind=self.role,
            window_start=window.start,
            window_end=window.end,
            request=reply.request,
            response=reply.response,
        )
        append_jsonl(self.paths.model_calls, call.wire())
        return reply

    def write(self, record: R, reply: ModelReply, rows: list[Any], *, window: Window,
              finalize: Callable[[R], R] | None = None) -> R:
        """Stamp the window, model, usage, and fallback fields on the parsed record (``observations`` counts ``rows``,
        what the stage read), then append and count it."""
        update: dict[str, Any] = {
            "id": new_id(),
            "ts": window.end,
            "window_start": window.start,
            "window_end": window.end,
            "observations": len(rows),
            "model": reply.model,
            "usage": reply.usage,
            "images": reply.images,
            "call_id": reply.call_id,
        }
        if reply.fallback:
            update["fallback"] = reply.fallback
        if reply.reasoning and "reasoning" in type(record).model_fields:
            update["reasoning"] = reply.reasoning[:4000]
        record = record.model_copy(update=update)
        if finalize is not None:
            record = finalize(record)
        append_jsonl(getattr(self.paths, self.output), record.wire())
        self.stream.emit(self.event, record.wire(), ts=window.end, session_id=self.session_id, call_id=reply.call_id, id=update["id"])
        record_usage(self.usage, reply.model, reply.usage)
        self.ticks += 1
        return record
