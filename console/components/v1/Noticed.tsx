"use client";

import type { LiveProposalSet, RunDetail } from "@cue/contracts";
import { useRef, useState } from "react";

import { ByHand, OnPause, clockOf, decisionLabel, proposalsOf, runTime } from "../live/shared";
import { useDismiss } from "./dismiss";
import type { Decision } from "./useSession";

type Props = {
  proposalSets: LiveProposalSet[];
  reasons: string[]; // the one-click reasons after "Not now", from the backend
  runs: Record<string, RunDetail>;
  busy: boolean;
  running: boolean; // a run in flight: Do it waits for it
  compact: boolean; // a permission question shares the band: the stated reason goes behind hover
  onDecide: (item: LiveProposalSet, decision: Decision, choice: number) => Promise<void>;
  onReason: (item: LiveProposalSet, reason: string, typed: boolean) => Promise<void>;
  onChat: (item: LiveProposalSet, choice: number) => Promise<void>; // Edit first: accept, and put it in the chat box
  onSend: (item: LiveProposalSet, choice: number) => Promise<boolean>; // Do it: accept, and send it to the chat as written
};

// What the run a proposal became did, when one was started from it.
export function outcomeOf(item: LiveProposalSet, runs: Record<string, RunDetail>): string {
  const run = Object.values(runs).find((detail) => detail.run.proposal?.proposalsTs === item.ts);
  if (!run) return "";
  const cost = run.run.usage?.costUsd;
  const took = runTime(run.run); // the agent's time on it, beside the cost
  return ` · ${run.chat ? "chat" : "temporary run"} · ${run.run.status}${cost !== undefined ? ` · $${cost.toFixed(2)}` : ""}${took ? ` · ⏱ ${took}` : ""}`;
}

// The newest suggestion with its actions, and the earlier ones of the session behind "Earlier".
export function Noticed({ proposalSets, reasons, runs, busy, running, compact, onDecide, onReason, onChat, onSend }: Props) {
  const [open, setOpen] = useState(false);
  const dropdown = useRef<HTMLDivElement>(null);
  useDismiss(open, [dropdown], () => setOpen(false)); // the earlier suggestions close on a press elsewhere
  const [alternatives, setAlternatives] = useState(false);
  const withProposals = [...proposalSets].filter((item) => !item.error && proposalsOf(item).length > 0).reverse(); // newest first
  const [current, ...earlier] = withProposals;
  if (!current) return <div className="noticed none">Nothing suggested yet. A suggestion appears here when it has seen enough.</div>;
  const proposals = proposalsOf(current);
  const [top, ...others] = proposals;
  const notNow = async (item: LiveProposalSet, reason: string, typed: boolean) => {
    await onDecide(item, "reject", 0);
    await onReason(item, reason, typed);
  };
  const actions = (item: LiveProposalSet, choice: number) => (
    <div className="actions">
      <span className="accept"><span className="face" aria-hidden="true">→ Accept</span>{/* under the pointer it comes apart: send as is, or edit first */}
        <button type="button" className="btn primary sm go" disabled={busy || running} title="Send it to the chat as it is" onClick={() => void onSend(item, choice)}>→ Do it</button>
        <button type="button" className="btn primary sm pen" disabled={busy} aria-label="Edit first" title="Change it in the chat box first" onClick={() => void onChat(item, choice)}>✎</button>
      </span>
      <span className="menu">
        <button type="button" className="btn sm ghost" disabled={busy}>Not now ▾</button>
        <span className="items">
          {reasons.map((reason) => <button key={reason} type="button" onClick={() => void notNow(item, reason, false)}>{reason}</button>)}
          <button type="button" onClick={() => { const typed = window.prompt("Why not? A few words.")?.trim(); if (typed) void notNow(item, typed, true); }}>other…</button>
        </span>
      </span>
    </div>
  );
  return (
    <div className="noticed">
      <div className="kind">💡 Noticed · {clockOf(current.ts)}<OnPause set={current} />
        {earlier.length ? <button type="button" className="all" onClick={() => setOpen((value) => !value)}>Earlier · {earlier.length} {open ? "▴" : "▾"}</button> : null}
      </div>
      {top ? <p title={compact ? top.reason : undefined}><b>{top.task}</b><ByHand proposal={top} /></p> : null}
      {top?.reason && !compact ? <p className="why">{top.reason}</p> : null}
      {current.feedback ? (
        <div className="actions"><span className="decision">{decisionLabel(current.feedback)} · {clockOf(current.feedback.ts)}
          {current.feedback.digestError ? <span title={current.feedback.digestError}> · digest failed</span> : current.feedback.digested === false ? <span> · <span className="dot" />digesting…</span> : null}
          {outcomeOf(current, runs)}</span></div>
      ) : actions(current, 0)}
      {others.length ? (
        <div className="actions"><button type="button" className="more" onClick={() => setAlternatives((value) => !value)}>{alternatives ? "Hide" : `${others.length} alternative${others.length === 1 ? "" : "s"}`}</button></div>
      ) : null}
      {alternatives ? (
        <ol className="alts" start={2}>
          {others.map((proposal, index) => (
            <li key={`${current.ts}-${index + 1}`}><b>{proposal.task}</b><ByHand proposal={proposal} />{proposal.reason ? <div className="why">{proposal.reason}</div> : null}{current.feedback ? null : actions(current, index + 1)}</li>
          ))}
        </ol>
      ) : null}
      {open ? (
        <div className="dropdown" ref={dropdown}>
          <ul>
            {earlier.map((item) => {
              const first = proposalsOf(item)[0];
              const state = item.feedback ? (item.feedback.decision === "accept" ? "accepted" : "") : "waiting";
              return (
                <li key={item.ts} className={state}>
                  <i /><time>{clockOf(item.ts)}</time>
                  <span className="what">{first?.task}<OnPause set={item} /></span>
                  <span className="status">{item.feedback ? `${decisionLabel(item.feedback)}${outcomeOf(item, runs)}` : "no answer"}
                    {item.feedback?.decision === "accept" ? null : <button type="button" className="btn sm ghost" disabled={busy} onClick={() => void onChat(item, 0)}>Chat</button>}</span>
                </li>
              );
            })}
          </ul>
          <p className="foot">Every suggestion of the session stays here with what you did about it. Older sessions are in History.</p>
        </div>
      ) : null}
    </div>
  );
}
