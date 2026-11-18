"use client";

import type { RunDetail, RunEvent } from "@cue/contracts";
import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { MARKDOWN_LINKS, RunCard, runTime } from "../live/shared";
import { type Call, type Step, callLine, callTime, failureOf, stepsOf, workSentence } from "../live/steps";

type Props = {
  detail: RunDetail;
  events: RunEvent[];
  runnerBaseUrl: string;
  finalText?: string; // the agent's reply, shown by the message itself: narration that repeats it is left out
  onAnswer?: ((runId: string, mode: "once" | "deny") => void) | undefined;
};

// How long the run has been going, ticking while it runs.
function useElapsed(startedAt: string, running: boolean): string {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  const total = Math.max(0, Math.round((now - Date.parse(startedAt)) / 1000));
  return total >= 60 ? `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s` : `${total}s`;
}

// A message on its way, before its run exists: the bar a running turn shows, its clock from the send, so the turn
// that takes its place reads as the same line going on.
export function Starting() {
  const [since] = useState(() => new Date().toISOString());
  return <div className="turn running"><div className="turn-bar"><button type="button" className="turn-fold" aria-expanded disabled>{`Working · ${useElapsed(since, true)}`}</button></div></div>;
}

// One run of calls: a sentence that opens into one line per call, each opening into its output. While a call is out,
// the line says what it is doing now.
function Work({ calls, live }: { calls: Call[]; live: boolean }) {
  const current = calls.find((call) => !call.endedAt);
  const sentence = workSentence(calls.filter((call) => call.endedAt));
  return (
    <details className="turn-work" open={live}>
      <summary>
        {current ? <><i className="turn-now" /><span>{callLine(current).verb} <code>{callLine(current).target}</code></span>{sentence ? <span className="turn-sofar"> · {sentence.charAt(0).toLowerCase() + sentence.slice(1)} so far</span> : null}</> : sentence}
      </summary>
      <ol className="turn-calls">
        {calls.map((call) => {
          const line = callLine(call);
          return (
            <li key={call.id}>
              <details className="turn-call">
                <summary title={call.description || call.target}><span className="turn-verb">{line.verb}</span><span className="turn-tgt">{line.target}</span><span className="turn-dur">{call.waiting ? "waiting for you" : callTime(call)}</span></summary>
                {call.output ? <pre className="turn-out">{call.output}</pre> : null}
              </details>
            </li>
          );
        })}
      </ol>
    </details>
  );
}

// The agent's turn, compact: a bar that says how long it worked and folds its steps (narration, runs of calls); the steps
// stay open while it runs. "raw" swaps in the run card with every model call and event, for debugging; History always
// shows that card.
export function Turn({ detail, events, runnerBaseUrl, finalText, onAnswer }: Props) {
  const running = detail.run.status === "running";
  const [open, setOpen] = useState(running);
  const [raw, setRaw] = useState(false);
  useEffect(() => { setOpen(running); }, [running]);
  const elapsed = useElapsed(detail.run.startedAt, running);
  const took = runTime(detail.run) ?? elapsed;
  const bar = running ? `Working · ${elapsed}` : detail.run.status === "completed" ? `Worked ${took}` : detail.run.status === "cancelled" ? `Stopped after ${took}` : `Failed after ${took}`;
  const final = finalText?.trim();
  const settledAt = running ? null : detail.run.completedAt ?? events[events.length - 1]?.createdAt ?? detail.run.startedAt;
  const steps: Step[] = stepsOf(events, settledAt).filter((step) => step.kind !== "say" || step.text !== final);
  const waiting = steps.flatMap((step) => step.kind === "work" ? step.calls : []).find((call) => call.waiting);
  const failure = detail.run.status === "failed" ? failureOf(events, detail.run.summary?.notes ?? []) : "";
  return (
    <div className={`turn ${detail.run.status}`}>
      <div className="turn-bar">
        <button type="button" className="turn-fold" aria-expanded={open || raw} onClick={() => { if (raw) setRaw(false); else setOpen((value) => !value); }}>{bar}</button>
        {steps.length ? <button type="button" className="turn-raw" onClick={() => setRaw((value) => !value)}>{raw ? "steps" : "raw"}</button> : null}
      </div>
      {failure ? <p className="turn-fail" role="alert">{failure}</p> : null}{/* why it failed, in the open whatever the fold: the one thing to read */}
      {raw ? <RunCard detail={detail} events={events} runnerBaseUrl={runnerBaseUrl} open onAnswer={onAnswer} turn />
        : open && steps.length ? (
          <div className="turn-steps">
            {steps.map((step, index) => step.kind === "say"
              ? <div key={step.id} className="turn-say"><Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_LINKS}>{step.text}</Markdown></div>
              : <Work key={step.id} calls={step.calls} live={running && index === steps.length - 1} />)}
            {waiting && onAnswer ? (
              <div className="turn-ask"><code title={waiting.description || undefined}>{waiting.target || waiting.name}</code>
                <button type="button" className="btn primary sm" onClick={() => onAnswer(detail.run.id, "once")}>Allow once</button>
                <button type="button" className="btn sm" onClick={() => onAnswer(detail.run.id, "deny")}>Deny</button></div>
            ) : null}
          </div>
        ) : null}
    </div>
  );
}
