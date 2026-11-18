"use client";

import type { RunDetail, RunEvent } from "@cue/contracts";
import { memo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { AGENT_LABELS, FINAL_NOTE, MARKDOWN_LINKS, runTime } from "../live/shared";
import { callLine, callTime, failureOf, stepsOf, workSentence } from "../live/steps";

// The monitor for a run with no screen of its own to show (a coding agent in the shell and the files). Live: what it said
// last, what it is doing now, the tail of what that call printed, and what the run has amounted to so far. Over: the
// receipt, what it did as one line per call. Its newest screenshot beside, when it took one. Timeline v1 and Zen keep
// the terminal view (AgentScreen.tsx).
function NowScreenView({ detail, events, runnerBaseUrl }: { detail: RunDetail; events: RunEvent[]; runnerBaseUrl: string }) {
  const running = detail.run.status === "running";
  const settledAt = running ? null : detail.run.completedAt ?? events[events.length - 1]?.createdAt ?? detail.run.startedAt;
  const notes = detail.run.summary?.notes ?? [];
  const final = notes.find((note) => note.startsWith(FINAL_NOTE))?.slice(FINAL_NOTE.length).trim(); // the reply is the chat's: not repeated here
  const steps = stepsOf(events, settledAt).filter((step) => step.kind !== "say" || step.text !== final);
  const calls = steps.flatMap((step) => step.kind === "work" ? step.calls : []);
  const current = calls.find((call) => !call.endedAt);
  const latest = current ?? calls[calls.length - 1];
  const said = running ? [...steps].reverse().find((step) => step.kind === "say") : undefined;
  const thinking = running && !current && events[events.length - 1]?.message === "Model reasoning.";
  const failure = detail.run.status === "failed" ? failureOf(events, notes) : "";
  const tail = running && latest?.output ? latest.output.split("\n").slice(-12).join("\n") : ""; // what it is doing now; a finished run's output is history
  const sentence = workSentence(calls.filter((call) => call.endedAt));
  const took = runTime(detail.run);
  const line = current ? callLine(current) : null;
  const ended = detail.run.status === "completed" ? "Done" : detail.run.status === "cancelled" ? "Stopped" : "Failed";
  const shots = (detail.browser?.screenshots ?? []).filter((shot) => !shot.placeholder);
  const shot = shots[shots.length - 1];
  return (
    <div className={`nowScreen${shot ? " withShot" : ""}`}>
      <div className="now">
        <div className="nowHead"><b>{AGENT_LABELS[detail.actorAgent ?? ""] ?? "Agent"}</b><span>{running ? `${calls.length} step${calls.length === 1 ? "" : "s"}` : took ? `${ended} · ${took}` : ended}</span></div>
        {failure ? <p className="turn-fail" role="alert">{failure}</p> : null}
        {said ? <div className="nowSay"><Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_LINKS}>{said.text}</Markdown></div> : null}
        <div className="nowLine">
          {line ? <><i className="turn-now" /><span className="nowVerb">{line.verb}</span><code title={current?.target}>{line.target}</code></>
            : running ? <><i className="turn-now" /><span className="nowVerb">{thinking ? "Thinking…" : calls.length ? "Deciding what to do next…" : "Starting…"}</span></>
              : <span className="nowVerb">{sentence || ended}</span>}
        </div>
        {current?.description ? <div className="nowDesc">{current.description}</div> : null}
        {tail ? <pre className="nowOut">{tail}</pre> : null}
        {!running && calls.length ? (
          <ol className="nowCalls">
            {calls.map((call) => { const done = callLine(call); return <li key={call.id} title={call.description || call.target}><span className="turn-verb">{done.verb}</span><span className="turn-tgt">{done.target}</span><span className="turn-dur">{callTime(call)}</span></li>; })}
          </ol>
        ) : null}
        {running && sentence ? <div className="nowSum">{sentence} so far</div> : null}
      </div>
      {shot ? (
        <div className="shotPane">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={`${runnerBaseUrl}${shot.url}`} alt={shot.label} />
          <span className="cap">{shot.label}</span>
        </div>
      ) : null}
    </div>
  );
}

export const NowScreen = memo(NowScreenView); // rendered again only when the run or its events change: the chat box types beside it
