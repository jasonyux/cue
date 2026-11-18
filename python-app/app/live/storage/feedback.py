"""Operator feedback on what the session produced: accept / reject a proposal set, flag a summary.

Every decision is stored next to what it answers, as preference data for tuning the proposer and the summarizer.
The annotated lists are what the console reads: stored records, validated, with the latest decision attached.
"""

from __future__ import annotations

from typing import Any

from ...errors import RunnerCoreError
from ..utils.jsonl import amend_jsonl, append_jsonl, tail_jsonl
from ..utils.time import timestamp
from .paths import SessionPaths
from .records import ProposalFeedback, ProposalSet, Summary, SummaryFeedback

# The one-click reasons the console offers after a Reject, in order; anything else is typed. Served to the console
# with the defaults, so the list lives here only. NOT_NOW is the one the duplicate filter treats as temporary.
REJECT_REASONS = ("already done", "not now", "do it myself", "not useful")
NOT_NOW = REJECT_REASONS[1]


def annotated_proposals(paths: SessionPaths, limit: int) -> list[dict[str, Any]]:
    """Proposals with the operator's latest accept/reject decision attached, when there is one."""
    decisions: dict[str, dict[str, Any]] = {}
    for item in tail_jsonl(paths.feedback, 1000):
        if isinstance(item.get("proposalsTs"), str):
            decisions[item["proposalsTs"]] = item
    proposals = []
    for stored in tail_jsonl(paths.proposals, limit):
        proposal_set = ProposalSet.parse_stored(stored).wire()
        decision = decisions.get(proposal_set["ts"])
        if decision is not None:
            proposal_set["feedback"] = {"decision": decision["decision"], "ts": decision["ts"], "choice": decision.get("choice"),
                                        **({"id": decision["id"]} if decision.get("id") else {}),
                                        "digested": bool(decision.get("context")),  # the digest lands after the decision
                                        **({"rejectReason": decision["rejectReason"]} if decision.get("rejectReason") else {}),
                                        **({"modified": True} if decision.get("modified") else {}),  # sent to the chat with its text changed
                                        **({"digestError": decision["digestError"]} if decision.get("digestError") else {})}
        proposals.append(proposal_set)
    return proposals


def annotated_summaries(paths: SessionPaths, limit: int) -> list[dict[str, Any]]:
    """Summaries with the operator's latest flag (verdict + comment) attached, when there is one."""
    flags: dict[str, dict[str, Any]] = {}
    for item in tail_jsonl(paths.summary_feedback, 2000):
        if isinstance(item.get("summaryTs"), str):
            flags[item["summaryTs"]] = item
    summaries = []
    for stored in tail_jsonl(paths.summaries, limit):
        summary = Summary.parse_stored(stored).wire()
        flag = flags.get(summary["ts"])
        if flag is not None:
            summary["feedback"] = {"verdict": flag["verdict"], "comment": flag.get("comment", ""), "ts": flag["ts"]}
        summaries.append(summary)
    return summaries


def record_summary_feedback(paths: SessionPaths, summary_ts: str, comment: str, verdict: str = "wrong") -> dict[str, Any]:
    """Flag a summary as wrong (or confirm it) with a free-text note, keeping enough to find the model call later."""
    if verdict not in ("wrong", "ok"):
        raise RunnerCoreError("verdict must be 'wrong' or 'ok'.", code="invalid_request", status_code=400)
    if not isinstance(comment, str):
        raise RunnerCoreError("comment must be a string.", code="invalid_request", status_code=400)
    stored = next((item for item in tail_jsonl(paths.summaries, 2000) if item.get("ts") == summary_ts), None)
    if stored is None:
        raise RunnerCoreError("Unknown summary.", code="invalid_request", status_code=404,
                              hint="Refresh the summaries list and try again.")
    summary = Summary.parse_stored(stored)
    # The matching request/response in live_model_calls.jsonl has kind "summary" and the same windowEnd.
    record = SummaryFeedback(ts=timestamp(), summary_ts=summary_ts, verdict=verdict, comment=comment.strip()[:4000],  # type: ignore[arg-type]
                             window_start=summary.window_start, window_end=summary.window_end, summary=summary.summary,
                             model=summary.model, images=summary.images, observations=summary.observations).wire()
    append_jsonl(paths.summary_feedback, record)
    return record


def feedback_record(paths: SessionPaths, proposals_ts: str, decision: str, choice: int | None = None) -> ProposalFeedback:
    """Operator preference on a proposal set: accept one of its ranked proposals (``choice`` is its 0-based index,
    default the first) or reject the whole set. Nothing is executed. The record keeps every proposal that was
    offered next to the one chosen, so a later ranking model can learn from the order the operator disagreed with.
    The session appends it once the digest of that round's input is attached."""
    if decision not in ("accept", "reject"):
        raise RunnerCoreError("decision must be 'accept' or 'reject'.", code="invalid_request", status_code=400)
    stored = next((item for item in tail_jsonl(paths.proposals, 1000) if item.get("ts") == proposals_ts), None)
    if stored is None:
        raise RunnerCoreError("Unknown proposal_set.", code="invalid_request", status_code=404,
                              hint="Refresh the proposals list and try again.")
    proposal_set = ProposalSet.parse_stored(stored)
    proposals = proposal_set.proposals
    if decision == "accept" and not proposals:
        raise RunnerCoreError("This proposal_set has no proposals to accept.", code="invalid_request", status_code=400)
    if decision == "accept" and choice is None:
        choice = 0
    if choice is not None and (isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(proposals)):
        raise RunnerCoreError(f"choice must be the index of one of the proposal_set's {len(proposals)} proposal(s).",
                              code="invalid_request", status_code=400)
    chosen = proposals[choice] if choice is not None else (proposals[0] if proposals else None)
    return ProposalFeedback(ts=timestamp(), proposals_ts=proposals_ts, decision=decision, choice=choice,  # type: ignore[arg-type]
                            kind=chosen.kind if chosen else None, action=proposal_set.action,
                            task=chosen.task if chosen else None, reason=chosen.reason if chosen else "",
                            confidence=chosen.confidence if chosen else 0.0, proposals=proposals, call_id=proposal_set.call_id)


def record_sent(paths: SessionPaths, proposals_ts: str, text: str) -> dict[str, Any] | None:
    """An accepted proposal was sent to the chat. Sent as proposed, nothing is added; with its text changed first, the latest
    acceptance of the set records that (``modified``) and the text sent (``sentText``): what the person actually asked for,
    next to what was proposed."""
    latest = next((item for item in reversed(tail_jsonl(paths.feedback, 1000)) if item.get("proposalsTs") == proposals_ts), None)
    if latest is None or latest.get("decision") != "accept" or " ".join(text.split()) == " ".join((latest.get("task") or "").split()):
        return None
    return amend_jsonl(paths.feedback, lambda row: row.get("ts") == latest["ts"], {"modified": True, "sentText": text.strip()[:4000]})


def reject_reason(paths: SessionPaths, proposals_ts: str, reason: str, typed: bool) -> dict[str, Any]:
    """Why the latest rejection of a proposal set was made, given after the fact, and whether it was typed rather
    than a preset: written onto that decision's row."""
    reason = " ".join(reason.split())[:200]
    if not reason:
        raise RunnerCoreError("reason must not be empty.", code="invalid_request", status_code=400)
    latest = next((item for item in reversed(tail_jsonl(paths.feedback, 1000)) if item.get("proposalsTs") == proposals_ts), None)
    if latest is None or latest.get("decision") != "reject":
        raise RunnerCoreError("The latest decision on this proposal set is not a rejection.", code="invalid_request", status_code=400)
    return amend_jsonl(paths.feedback, lambda row: row.get("ts") == latest["ts"], {"rejectReason": reason, "reasonTyped": typed})

