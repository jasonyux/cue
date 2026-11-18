"""The idle trigger's proposer: one short call on what is in front of the person the moment they paused.

Same records as the timer's proposer (proposals.jsonl and ``proposals_made``, stamped ``trigger: "idle"``), so the
console, the duplicate filter, the feedback and its digest take them as they are. What differs is the input, fixed
rather than ``proposer.inputs``: the timer's context (the task list, the last ``proposer_context_summaries`` worklog
lines, the recent decisions, the latest chat turn, the runs in progress), plus the screen: the window now (its tree
excerpted around the selection, the text around the selection or the cursor, the frame of the look the trigger asked
for) and the raw observations of the last ``proposer_idle_context_s`` up to when the pause began (what the person did;
what changed during the pause changed by itself). Then the prompt (``quick_system``) and fewer proposals. Its model calls
are logged as ``proposer_idle``, which a replay of the proposer leaves out.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..observer.digest import build_window_digest, excerpt_tree, latest_snapshot
from ..observer.stream import latest_screenshots
from ..storage.records import FilteredProposal, ProposalSet, Snapshot
from ..storage.stream import Cursor, Window
from ..utils.prompting import block, decision_lines, summary_lines, task_lines
from ..utils.time import clock, shifted, timestamp
from .prompts import build_proposer_messages, quick_system
from .propose import (
    CHAT_MINUTES,
    Proposer,
    ProposerInput,
    ProposerInputs,
    RankedProposalsStrategy,
    cite,
    shown_decisions,
    shown_ids,
)

STALE_NOTE = "not shown: the person went on before it was ready"


@dataclass
class QuickInput(ProposerInput):
    """One idle round: the proposer's input plus the pause that fired it."""

    paused_s: float = 0.0
    paused_at: str = ""  # when the pause began: the observations end there


def pause_note(paused_s: float, current: Snapshot | None, paused_at: str = "") -> str:
    """The lead of the user message: the pause, what is selected and the text around it (or around the cursor), and
    where the observations end."""
    where = f' on {current.app} — "{current.window}"' if current else ""
    note = f"The person paused{where}: no keyboard or mouse input for the last {paused_s:.0f} seconds."
    if current and current.selection:
        note += f'\nSelected text: "{current.selection}"'
    if current and current.around:
        mark = "the selection, between >>> and <<<" if current.selection else "the text cursor, at >>><<<"
        note += f"\nThe focused text around {mark}:\n{block('around', current.around)}"
    if paused_at:
        note += f"\n<observations> end when the pause began, at {clock(paused_at)}; <current_window> shows the screen now."
    return note


class QuickProposalsStrategy(RankedProposalsStrategy):
    """The ranked strategy's parse, with the idle prompt and every input it is given."""

    def messages(self, inputs: ProposerInput) -> list[dict[str, Any]]:
        seen = inputs.input
        current = seen.get("current_window")
        return build_proposer_messages(
            now=inputs.now,
            tasks=task_lines(seen.get("tasks") or [], now=inputs.now),
            worklog=summary_lines(seen.get("worklog") or []),
            feedback=decision_lines(seen.get("feedback") or []),
            digest=build_window_digest(seen.get("snapshots") or [], max_chars=8000, tree_chars=1200),
            current_tree=excerpt_tree(current.tree, None, 6000, near=current.selection) if current else "",
            screenshots=inputs.screenshots,
            limit=inputs.limit,
            running=inputs.running,
            chat=seen.get("chat"),
            preferences=seen.get("preferences"),
            system=quick_system,
            lead=pause_note(inputs.paused_s, current, inputs.paused_at) if isinstance(inputs, QuickInput) else pause_note(0.0, current),
        )


class QuickProposer(Proposer):
    role = "proposer_idle"  # its kind in live_model_calls.jsonl

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("strategy", QuickProposalsStrategy())
        super().__init__(*args, **kwargs)
        self.paused_s = 0.0                     # the pause of the round being made
        self.paused_at = ""                     # and when it began
        self.current: Snapshot | None = None   # the screen it is on

    def thinking(self) -> bool:
        return self.settings.proposer_idle_thinking

    async def propose(self, *, paused_s: float, current: dict[str, Any] | None, paused_at: str | None = None,
                      finalize: Callable[[ProposalSet], ProposalSet] | None = None) -> ProposalSet:
        """One round now, over the last ``proposer_idle_context_s``; ``current`` is the screen the pause is on (the
        newest readable look, which may be older than that span when nothing changed); ``paused_at`` when the pause
        began (default: ``paused_s`` before now)."""
        now = timestamp()
        window = self.stream.window_at(shifted(now, -self.settings.proposer_idle_context_s), now, self.session_id,
                                       lookback_days=self.settings.stream_lookback_days)
        self.paused_s, self.paused_at = paused_s, paused_at or shifted(now, -paused_s)
        self.current = Snapshot.model_validate(current) if current else None
        return await self.run_window(window, finalize=finalize)

    async def run_window(self, window: Window, *, finalize: Callable[[ProposalSet], ProposalSet] | None = None,
                         end_cursor: Cursor | None = None) -> ProposalSet:
        rows = window.observer_rows()
        paused_at = self.paused_at or window.end
        given: ProposerInputs = {  # the timer's context, then the screen
            "tasks": window.tasks(),
            "worklog": window.last_worklog(self.settings.proposer_context_summaries),
            "snapshots": [row for row in rows if row.ts < paused_at],  # what the person did; during the pause it changed by itself
            "current_window": self.current or latest_snapshot(rows),
            "feedback": sorted(window.decisions(self.settings.proposer_feedback_rejected, self.settings.proposer_feedback_accepted)
                               + window.undecided(self.settings.proposer_feedback_recent), key=lambda item: item.ts),
            "chat": window.chat(CHAT_MINUTES)[-1:],
        }
        if "preferences" in self.settings.proposer_inputs and self.standing():
            given["preferences"] = self.standing()
        # The frame of the look the trigger asked for (the recorder takes one for it): the screen as the person sees it now.
        frame = latest_screenshots([row for row in rows if row.ts >= paused_at], self.paths, limit=1, max_side=self.settings.screenshot_max_side)
        inputs = QuickInput(now=window.end, window_start=window.start, input=given, screenshots=frame,
                            settings=self.settings, running=window.running(), limit=self.settings.proposer_idle_proposals,
                            paused_s=self.paused_s, paused_at=paused_at)
        reply = await self.call_model(self.strategy.messages(inputs), [shot.url for shot in inputs.screenshots], window=window)
        proposals = cite(self.strategy.parse(reply, inputs), shown_ids(given), shown_decisions(given)).model_copy(update={"trigger": "idle"})
        if self.settings.proposer_dedup:
            proposals = await self.dedup(proposals, window)
        return self.write(proposals, reply, rows, window=window, finalize=finalize)


def stale(proposals: ProposalSet) -> ProposalSet:
    """An idle round the person went on before: its proposals kept on the record but out of view (and out of the
    duplicate filter's references, which read ``proposals``)."""
    kept = [FilteredProposal(**proposal.model_dump(), score=0.0, matched="", source="stale", note=STALE_NOTE)
            for proposal in proposals.proposals]
    return proposals.model_copy(update={"proposals": [], "filtered": [*proposals.filtered, *kept], "action": "none",
                                        "task": None, "effect": "stale"})
