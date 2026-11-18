"""The duplicate filter: a proposal is dropped after generation when it embeds too
close to one the person has already decided on or already seen.

References, built fresh each round from what is on disk:
  decision  every accepted or rejected proposal in data/memory/feedback.jsonl, any age, except "not now" rejections
  not_now   "not now" rejections from the last ``proposer_dedup_not_now_hours``: declined for the moment, not for good
  session   every proposal this session made in the last ``proposer_dedup_session_hours``, decided or not

A proposal whose best cosine against any reference reaches ``proposer_dedup_threshold`` is moved from ``proposals``
to ``filtered`` on the record
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import LiveSettings
from ..storage.feedback import NOT_NOW
from ..storage.records import Decision, FilteredProposal, ProposalSet
from ..storage.stream import Window
from ..utils.embed import EmbeddingClient, similarities
from ..utils.jsonl import read_jsonl
from ..utils.time import clock, seconds_between


@dataclass(frozen=True)
class Reference:
    text: str
    source: str  # decision | not_now | session
    note: str    # how the console says it: "rejected 2026-10-02", "proposed 17:20 this session"


def references(feedback: Path, window: Window, settings: LiveSettings) -> list[Reference]:
    """What a new proposal is compared against, one entry per distinct text (the first occurrence names it)."""
    found: list[Reference] = []
    for row in read_jsonl(feedback)[0]:
        decision = Decision.from_feedback(row, row.get("sessionId") or "")
        if decision.decision not in ("accept", "reject"):
            continue
        if decision.decision == "reject" and decision.reject_reason == NOT_NOW:
            if seconds_between(decision.ts, window.end) > settings.proposer_dedup_not_now_hours * 3600:
                continue
            found.append(Reference(decision.task, "not_now", f"rejected, not now, {decision.ts[:10]}"))
        else:
            verdict = "accepted" if decision.decision == "accept" else "rejected"
            found.append(Reference(decision.task, "decision", f"{verdict} {decision.ts[:10]}"))
    for event in window.before("proposals_made"):
        if event.data.get("error") or seconds_between(event.ts, window.end) > settings.proposer_dedup_session_hours * 3600:
            continue
        for proposal in event.data.get("proposals") or []:
            found.append(Reference(proposal["task"], "session", f"proposed {clock(event.ts)[:5]} this session"))
    distinct: dict[str, Reference] = {}
    for item in found:
        if item.text:
            distinct.setdefault(item.text, item)
    return list(distinct.values())


async def filter_duplicates(proposal_set: ProposalSet, refs: list[Reference], embedder: EmbeddingClient,
                            threshold: float) -> ProposalSet:
    """The set with every proposal that duplicates a reference moved to ``filtered``; the top fields follow the first
    proposal left, and ``action`` is "none" when none is."""
    if not proposal_set.proposals or not refs:
        return proposal_set
    vectors = await embedder.embed([item.task for item in proposal_set.proposals] + [item.text for item in refs])
    scores = similarities(vectors[: len(proposal_set.proposals)], vectors[len(proposal_set.proposals):])  # proposals × references
    kept, filtered = [], []
    for index, proposal in enumerate(proposal_set.proposals):
        best = int(scores[index].argmax())
        if scores[index, best] >= threshold:
            filtered.append(FilteredProposal(
                **proposal.model_dump(),
                score=round(float(scores[index, best]), 3),
                matched=refs[best].text,
                source=refs[best].source,
                note=refs[best].note
            ))
        else:
            kept.append(proposal)
    top = kept[0] if kept else None
    return proposal_set.model_copy(update={
        "proposals": kept,
        "filtered": filtered,
        "action": "propose" if top else "none",
        "kind": top.kind if top else proposal_set.kind,
        "task": top.task if top else None,
        "reason": top.reason if top else "",
        "confidence": top.confidence if top else 0.0,
    })
