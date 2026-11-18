"""Propose: every few minutes, tasks + worklog + recent raw observations + current window → a ranked list of proposals.

Pipeline position: one window of the stream (the task list as it last carried it, this session's worklog and observer
rows, the person's decisions, the runs in progress) → text sections [+ screenshots] → one model call (thinking on by
default) → proposals.jsonl and the stream → notify.py. ``Proposer.run_window`` is the whole step, top to bottom;
``RankedProposalsStrategy`` is the default strategy. To try another way of proposing, implement ``messages`` and ``parse``
against ``ProposerInput`` → ``ProposalSet`` and pass it as ``strategy``. The record shape is ``storage/records.py``.
What the proposer is shown is the ``proposerInputs`` setting (``proposer.inputs``: any of tasks, worklog, snapshots,
current_window, feedback; screenshots have their own flag). ``ProposerInput.input`` is a dict with exactly those keys the
setting allows: the task list, the worklog lines, this window's raw rows, the newest readable row, the person's recent
decisions on proposals. That setting is the ablation knob; a strategy renders each key it finds.
With ``proposer_search_rounds`` on, the timer's proposer also gets one tool, ``search_history`` (the history index's
tool, ``storage/search.py``, the same any stage or agent can be given): the model may search before it answers, up to
that many rounds, each logged like any call; the searches it made go on the record (``searches``), which the console's
"why?" shows.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

from ...usage import record_usage
from ..config import LiveSettings
from ..model_loop import ModelLoop, StageInput, Strategy
from ..observer.digest import build_window_digest, excerpt_tree, latest_snapshot
from ..storage.paths import SessionPaths
from ..storage.records import Decision, ModelCall, ProposalSet, Snapshot, Summary, Task
from ..storage.search import TOOL, TOOL_NAME, HistoryIndex, run_tool
from ..storage.stream import Cursor, Stream, Window
from ..utils.embed import EmbeddingClient
from ..utils.jsonl import append_jsonl
from ..utils.llm import LiveModelClient, ModelReply, add_usage, normalize_usage, parse_json_object
from ..utils.prompting import decision_lines, summary_lines, task_lines
from .dedup import filter_duplicates, references
from .prompts import build_proposer_messages

CHAT_MINUTES = 60  # the chat turn the proposer is shown: the latest, when within the last hour, so it does not propose what was just asked


def shown_ids(given: ProposerInputs) -> set[str]:
    """The ids the prompt shows, which a proposal may cite: worklog lines, decisions, chat turns, tasks."""
    ids = {item.id for item in given.get("worklog", [])} | {item.id for item in given.get("feedback", [])}
    ids |= {str(turn.get("id") or "") for turn in given.get("chat", [])} | {task.id for task in given.get("tasks", [])}
    return ids - {""}


def shown_decisions(given: ProposerInputs) -> dict[str, dict[str, Any]]:
    """The decisions the prompt shows, by id, in a search result's shape: they come from any session, so the console
    may not hold the record a proposal cites; a cited one goes on the set (``cited``) with its text."""
    verdicts = {"accept": "accepted", "reject": "declined"}
    return {item.id: {"id": item.id, "ts": item.ts, "kind": "decision", "sessionId": item.session_id,
                      "text": f"{verdicts.get(item.decision, 'proposed')}: {item.task}", "line": decision_lines([item])}
            for item in given.get("feedback", []) if item.id}


def cite(proposals: ProposalSet, shown: set[str], found: dict[str, dict[str, Any]]) -> ProposalSet:
    """The proposals' evidence kept to ids the prompt showed (a made-up id is dropped), and the search results among
    them on the set as ``cited``, so the console can show one from another session."""
    kept = [proposal.model_copy(update={"evidence": [entry for entry in proposal.evidence if entry in shown]}) for proposal in proposals.proposals]
    cited = [found[entry] for entry in dict.fromkeys(entry for proposal in kept for entry in proposal.evidence) if entry in found]
    return proposals.model_copy(update={"proposals": kept, "cited": cited})


def attach(messages: list[dict[str, Any]], urls: list[str]) -> list[dict[str, Any]]:
    """The images on the last message, as the client would put them, so they are part of the conversation itself."""
    if not urls:
        return messages
    last = dict(messages[-1])
    last["content"] = [{"type": "text", "text": str(last.get("content") or "")}, *({"type": "image_url", "image_url": {"url": url}} for url in urls)]
    return [*messages[:-1], last]


class ProposerInputs(TypedDict, total=False):
    """What the proposer is shown, keyed by the ``proposer.inputs`` names; a key is present only when the
    setting includes it."""

    tasks: list[Task]               # the belief updater's task list, across sessions
    worklog: list[Summary]          # the session's last proposer_context_summaries worklog lines, oldest first
    snapshots: list[Snapshot]       # this window's raw rows
    current_window: Snapshot | None  # the newest readable row, or None when nothing was readable
    feedback: list[Decision]         # recent decisions on proposals across sessions, plus the latest undecided rounds here
    chat: list[dict[str, Any]]       # the chat's recent turns (Window.chat), so it does not propose what was asked
    preferences: str                 # the person's standing preferences (data/memory/preferences.md), when the file has any


@dataclass
class ProposerInput(StageInput[ProposerInputs]):
    """One proposer window: the sources the setting allows, the tasks already running, and the proposal cap."""

    running: list[str]
    limit: int
    searches: int = 0            # history searches the round may make: the tool is given when positive
    history: str | None = None   # a line for the prompt, when the tool is given: how many lines are searchable and how far back they go


class RankedProposalsStrategy:
    """Up to ``limit`` proposals of kind explain / lookup / act, best first, as a JSON object."""

    def messages(self, inputs: ProposerInput) -> list[dict[str, Any]]:
        seen = inputs.input  # an input missing from the dict has no section in the prompt
        current = seen.get("current_window")
        return build_proposer_messages(
            now=inputs.now,
            tasks=task_lines(seen["tasks"], now=inputs.now) if "tasks" in seen else None,
            worklog=summary_lines(seen["worklog"]) if "worklog" in seen else None,
            feedback=decision_lines(seen["feedback"]) if "feedback" in seen else None,
            digest=build_window_digest(seen["snapshots"], max_chars=12_000, tree_chars=1200) if "snapshots" in seen else None,
            current_tree=(excerpt_tree(current.tree, None, 6000) if current else "") if "current_window" in seen else None,
            screenshots=inputs.screenshots,
            limit=inputs.limit,
            running=inputs.running,
            chat=seen.get("chat"),
            searches=inputs.searches,
            history=inputs.history,
            preferences=seen.get("preferences"),
        )

    def parse(self, reply: ModelReply, inputs: ProposerInput) -> ProposalSet:
        try:
            return ProposalSet.from_model_output(parse_json_object(reply.content), inputs.limit)
        except ValueError as error:
            return ProposalSet(summary="(model output was not a JSON object)", reason=str(error), raw=reply.content[:600])


class Proposer(ModelLoop[ProposerInput, ProposalSet]):
    role = "proposer"
    output = "proposals"
    event = "proposals_made"

    def __init__(self, paths: SessionPaths, client: LiveModelClient, settings: LiveSettings, *, stream: Stream, session_id: str,
                 usage: dict[str, Any] | None = None, strategy: Strategy[ProposerInput, ProposalSet] | None = None,
                 embedder: EmbeddingClient | None = None, feedback: Path | None = None, index: HistoryIndex | None = None,
                 preferences: Path | None = None) -> None:
        """``embedder`` and ``feedback`` (data/memory/feedback.jsonl) serve the duplicate filter; both are given when
        ``settings.proposer_dedup`` is on. ``index`` is the history the search tool reads; None leaves the tool out.
        ``preferences`` is the person's preferences file (data/memory/preferences.md), read at each round."""
        super().__init__(paths, client, settings, strategy or RankedProposalsStrategy(), stream=stream, session_id=session_id, usage=usage)
        self.embedder = embedder
        self.feedback = feedback
        self.index = index
        self.preferences = preferences

    def standing(self) -> str:
        """The preferences file as it stands; "" without one."""
        return self.preferences.read_text().strip() if self.preferences is not None and self.preferences.exists() else ""

    def wants_images(self) -> bool:
        return self.settings.proposer_screenshots

    def thinking(self) -> bool:
        return self.settings.proposer_thinking

    def max_tokens(self) -> int | None:
        return self.settings.proposer_max_tokens or None

    async def run_window(self, window: Window, *, finalize: Callable[[ProposalSet], ProposalSet] | None = None,
                         end_cursor: Cursor | None = None) -> ProposalSet:
        """One window → one ranked set of proposals: each source the setting allows, read from the window; the task
        runs in progress at its end, so the proposer does not propose them again."""
        rows = window.observer_rows()
        shown = self.settings.proposer_inputs
        given: ProposerInputs = {}
        if "tasks" in shown:
            given["tasks"] = window.tasks()
        if "worklog" in shown:
            given["worklog"] = window.last_worklog(self.settings.proposer_context_summaries)
        if "snapshots" in shown:
            given["snapshots"] = rows
        if "current_window" in shown:
            given["current_window"] = latest_snapshot(rows)
        if "feedback" in shown:
            given["feedback"] = sorted(
                window.decisions(self.settings.proposer_feedback_rejected, self.settings.proposer_feedback_accepted)
                + window.undecided(self.settings.proposer_feedback_recent), key=lambda item: item.ts
            )
        if "chat" in shown:
            given["chat"] = window.chat(CHAT_MINUTES)[-1:]
        if "preferences" in shown and self.standing():
            given["preferences"] = self.standing()
        rounds = self.settings.proposer_search_rounds if self.index is not None else 0
        inputs = ProposerInput(
            now=window.end,
            window_start=window.start,
            input=given,
            screenshots=self.screenshots(rows),
            settings=self.settings,
            running=window.running(),
            limit=self.settings.max_proposals,
            searches=rounds,
            history=self.history_note() if rounds else None,
        )
        # The screenshots go into the user message itself, so they stay in front of the model through the tool rounds.
        messages = attach(self.strategy.messages(inputs), [shot.url for shot in inputs.screenshots])
        tools = [TOOL] if rounds else None
        # 1. tool-use loop
        reply = await self.call_model(messages, [], window=window, tools=tools)
        usage = reply.usage
        searches: list[dict[str, Any]] = []
        found = shown_decisions(given)  # what a proposal may cite that the console may not hold: the decisions shown, then every search result
        for done in range(rounds):
            if not reply.tool_calls:
                break
            tool_results = self.call_tools(reply.tool_calls, searches, found)
            messages = [*messages, reply.message(), *tool_results]
            reply = await self.call_model(
                messages,
                images=[],
                window=window,
                tools=tools,
                tool_choice="auto" if done + 1 < rounds else "none" # the last round answers
            )
            usage = add_usage(usage, reply.usage)
        # 2. parse the final reply; its citations kept to what the prompt showed
        proposals = cite(self.strategy.parse(reply, inputs), shown_ids(given) | set(found), found).model_copy(update={"searches": searches})
        # 3. dedup previous proposals
        if self.settings.proposer_dedup:
            proposals = await self.dedup(proposals, window)
        return self.write(proposals, replace(reply, usage=usage, images=len(inputs.screenshots)), rows, window=window, finalize=finalize)

    def history_note(self) -> str:
        """A line for the user message: how many lines the search tool can reach and the date of the oldest."""
        assert self.index is not None
        count, oldest = self.index.span()
        return f"History searchable with search_history: {count} lines back to {oldest}." if count else "History searchable with search_history: nothing yet."

    def call_tools(self, calls: list[dict[str, Any]], searches: list[dict[str, Any]], found: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """The tool results for a reply's calls, each search noted on ``searches`` for the record and its results in
        ``found`` by id, for the citations."""
        assert self.index is not None
        results = []
        for call in calls:
            if call["name"] == TOOL_NAME:
                content, note = run_tool(self.index, call["arguments"])
                found.update({hit["id"]: hit for hit in note.pop("found") if hit["id"]})
                searches.append(note)
            else:
                content = f"Unknown tool {call['name']!r}: only {TOOL_NAME} exists."
            results.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        return results

    async def dedup(self, proposals: ProposalSet, window: Window) -> ProposalSet:
        """The duplicate filter over the parsed set. An embeddings request it makes is logged like a model call, its
        usage goes on the record (the cost panel's "proposer: duplicate filter" row) and into the session's totals."""
        assert self.embedder is not None and self.feedback is not None, "the duplicate filter needs an embedder and the feedback file"
        made = len(self.embedder.calls)
        filtered = await filter_duplicates(proposals, references(self.feedback, window, self.settings), self.embedder,
                                           self.settings.proposer_dedup_threshold)
        calls = self.embedder.calls[made:]
        if not calls:
            return filtered  # every text was cached: nothing was spent
        usage: dict[str, Any] = {}
        for call in calls:
            append_jsonl(self.paths.model_calls, ModelCall(id=uuid4().hex[:12], ts=window.end, kind="embedding", window_start=window.start,
                                                           window_end=window.end, request=call["request"], response=call["response"]).wire())
            usage = add_usage(usage, normalize_usage(call["response"].get("usage")))
        model = str(calls[-1]["response"].get("model") or self.embedder.endpoint.model)
        record_usage(self.usage, model, usage)
        return filtered.model_copy(update={"embedding": {"model": model, "usage": usage}})
