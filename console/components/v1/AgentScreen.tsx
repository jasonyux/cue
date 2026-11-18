"use client";

import type { RunDetail, RunEvent } from "@cue/contracts";
import { memo, useEffect, useRef } from "react";

import { AGENT_LABELS, callParts, pendingApproval, runState, turnsOf } from "../live/shared";

type Line = { kind: "say" | "tool" | "out" | "think"; text: string; state?: "running" | "waiting" };

// A call as one line: Bash(ls -la), Read(prompts.py), cmux-cua · click(…).
function callText(detail: string | undefined): string {
  const { name, argument } = callParts(detail);
  const first = argument.split("\n")[0] ?? "";
  return `${name}(${first.length > 160 ? `${first.slice(0, 160)}…` : first}${argument.includes("\n") ? " …" : ""})`;
}

// What the agent did, as terminal lines: its narration and each tool call with the first lines of its output. The call
// still out is marked as running, or as waiting when it needs the person's permission (Cue's bubble asks; the terminal
// does not repeat it); a last "reasoning" event shows as thinking.
export function agentLines(events: RunEvent[]): Line[] {
  const lines: Line[] = [];
  const lastCall = events.map((event) => event.type).lastIndexOf("function_call_requested");
  const lastCallOpen = lastCall >= 0 && !events.slice(lastCall).some((event) => event.type === "function_call_completed");
  const state = lastCallOpen ? (pendingApproval(turnsOf(events)) ? "waiting" : "running") : null;
  events.forEach((event, index) => {
    if (event.type === "function_call_requested") lines.push({ kind: "tool", text: callText(event.detail), ...(index === lastCall && state ? { state } : {}) });
    else if (event.type === "tool_progress" && event.detail) {
      const out = event.detail.split("\n").filter((line) => line.trim());
      const cut = (line: string) => (line.length > 140 ? `${line.slice(0, 140)}…` : line); // one screen line each
      lines.push({ kind: "out", text: out.slice(0, 3).map(cut).join("\n") + (out.length > 3 ? `\n… +${out.length - 3} lines` : "") });
    } else if (event.message === "Model progress." && event.detail) lines.push({ kind: "say", text: event.detail });
    else if (event.message === "Model reasoning." && index === events.length - 1) lines.push({ kind: "think", text: "thinking…" });
  });
  return lines.slice(-60);
}

// The monitor while the agent works: its terminal, and beside it its newest screenshot when it has taken one (a run
// that only uses the shell, files or the web takes none).
function AgentScreenView({ detail, events, runnerBaseUrl }: { detail: RunDetail; events: RunEvent[]; runnerBaseUrl: string }) {
  const shots = (detail.browser?.screenshots ?? []).filter((shot) => !shot.placeholder);
  const shot = shots[shots.length - 1];
  const lines = agentLines(events);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [lines.length]);
  return (
    <div className={`agentScreen${shot ? " withShot" : ""}`}>
      <div className="term">
        <div className="termHead"><b>✻ {AGENT_LABELS[detail.actorAgent ?? ""] ?? "Claude Code"}</b><span>{runState(turnsOf(events), detail.run.status === "running")}</span></div>
        <div className="termBody">
          {lines.length === 0 ? <div className="think">starting…</div> : null}
          {lines.map((line, index) => (
            <div key={index} className={line.kind}>
              {line.kind === "tool" ? <><span className="mark">⏺</span> {line.text}{line.state ? <span className="running"> {line.state === "waiting" ? "✋ waiting for you…" : "running…"}</span> : null}</>
                : line.kind === "out" ? <><span className="mark">⎿</span> {line.text}</>
                : line.kind === "think" ? <>✻ {line.text}</>
                : line.text}
            </div>
          ))}
          <div ref={end} />
        </div>
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

export const AgentScreen = memo(AgentScreenView); // rendered again only when the run or its events change: the chat box types beside it
