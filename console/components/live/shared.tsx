// What both consoles share: the request helper, the polled-state hook, the run card with its model-call folds, events,
// screenshots and permission prompt, and the small helpers around proposals and tasks. /v0 is the debug console
// (LiveConsole.tsx), /v1 the pilot (../v1).

import {
  type LiveProposal,
  type LiveProposalSet,
  type LiveSessionStatus,
  type LiveTaskChange,
  type LiveTaskFlag,
  type LiveTaskOp,
  type RunDetail,
  type RunEvent,
  type RunRecord,
  type RunUsage,
} from "@cue/contracts";
import { Fragment, useCallback, useEffect, useState, type ReactNode } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { formatClock } from "../helpers";
import { requestRunnerJson } from "../runner-request";

export const DEFAULT_MAX_TURNS = 50; // model calls a run or a chat turn may make unless the box says otherwise (the backend allows up to 300)

export const AGENT_LABELS: Record<string, string> = { desktop: "Desktop agent", cmux: "cmux agent (Claude Code)", claude: "Claude Code", codex: "Codex" };

export type Parser<T> = { parse(value: unknown): T };
export const rawJson: Parser<unknown> = { parse: (value) => value };
export function describeError(payload: unknown, fallback: string) {
  if (payload && typeof payload === "object" && "error" in payload && typeof payload.error === "string") {
    const hint = "hint" in payload && typeof payload.hint === "string" ? ` ${payload.hint}` : "";
    return `${payload.error}${hint}`;
  }
  return fallback;
}

// Why the observer did not read the latest look (latestSnapshot.blocked), as the monitor says it: the Permissions settings.
export const blockedWhy = (reason: string) => reason === "site" ? "a Permissions rule blocks this page" : reason === "default" ? "not listed in Permissions" : reason === "ask" ? "waiting for your answer" : "a Permissions rule blocks it";

export function clockOf(value: string | undefined | null) {
  return value ? formatClock(value) : "";
}

export function usageLine(usage: RunUsage | null | undefined) {
  if (!usage) return "—";
  return `${usage.calls} calls · ${usage.inputTokens} in · ${usage.outputTokens} out${usage.costUsd !== undefined ? ` · $${usage.costUsd.toFixed(4)}` : ""}`;
}

// How long a finished run kept the agent busy, from its record ("38s", "4m 12s", "1h 05m"); null while it still runs.
export function runTime(run: RunRecord): string | null {
  const ms = run.durationMs ?? (run.completedAt ? Date.parse(run.completedAt) - Date.parse(run.startedAt) : null);
  if (ms === null || !Number.isFinite(ms) || ms < 0) return null;
  const total = Math.round(ms / 1000);
  const hours = Math.floor(total / 3600), minutes = Math.floor((total % 3600) / 60), seconds = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  return minutes ? `${minutes}m ${String(seconds).padStart(2, "0")}s` : `${seconds}s`;
}

// The proposer's estimate of the person's own time for a proposal, rounded the way one would say it ("~45 s", "~10 min",
// "~1.5 h"); null on older proposals that carry none.
export function byHand(proposal: LiveProposal): string | null {
  const seconds = proposal.humanSeconds;
  if (!seconds || seconds <= 0) return null;
  if (seconds < 60) return `~${Math.max(5, Math.round(seconds / 5) * 5)} s`;
  if (seconds < 3600) return `~${Math.round(seconds / 60)} min`;
  return `~${Number((seconds / 3600).toFixed(seconds < 36_000 ? 1 : 0))} h`;
}

// The by-hand estimate as a quiet note beside a proposal's task; nothing when the proposal has none.
export function ByHand({ proposal, className = "est" }: { proposal: LiveProposal; className?: string }) {
  const estimate = byHand(proposal);
  return estimate ? <span className={className} title="Cue's rough estimate of the time this takes you by hand">{estimate} by hand</span> : null;
}

// Marks a suggestion the idle trigger made: from the window in front and the last few minutes, where the timer's rounds read its inputs setting.
export function OnPause({ set }: { set: LiveProposalSet }) {
  return set.trigger === "idle" ? <span className="est" title="Made when you paused, from the window in front and the last few minutes">· on a pause</span> : null;
}

export async function callRunner<T>(runnerBaseUrl: string, backend: string, path: string, schema: Parser<T>, init?: RequestInit, timeoutMs = 30_000): Promise<T> {
  const headers = { "Content-Type": "application/json", "X-CUA-Backend": backend };
  const { response, payload } = await requestRunnerJson(`${runnerBaseUrl}${path}`, { ...init, headers, cache: "no-store" }, timeoutMs);
  if (!response.ok) throw new Error(describeError(payload, `Runner returned ${response.status}.`));
  return schema.parse(payload);
}

// The backend mirrors the top proposal in the flat fields; older stored proposal sets are backfilled there too.
// What a history search asked for, on one line: the keywords, the regex, the kinds; or the slice alone.
export function searchLabel(search: { query: string; regex?: string | null | undefined; kinds?: string[] | undefined }) {
  return [search.query, search.regex && `/${search.regex}/`, search.kinds?.length && search.kinds.join(", ")].filter(Boolean).join(" · ") || "(the slice)";
}

export function proposalsOf(item: LiveProposalSet): LiveProposal[] {
  if (item.proposals) return item.proposals;
  if (item.task && item.action && item.action !== "none") {
    return [{ kind: item.kind ?? "act", task: item.task, reason: item.reason, confidence: item.confidence }];
  }
  return [];
}

export function ProposalLine({ proposal }: { proposal: LiveProposal }) {
  return (
    <>
      <span className={`liveKind ${proposal.kind}`}>{proposal.kind}</span>
      {" "}— {proposal.task}{" "}
      <ByHand proposal={proposal} className="liveMuted" />
      {proposal.reason ? <div className="liveMuted">{proposal.reason}</div> : null}
    </>
  );
}

export const FALLBACK_NOTE = "answered without thinking: the reasoning hit the token limit";

// Accept a proposal: one button; where the text goes (the run prompt or the chat) is picked from the menu that opens on
// hover or focus. Without a chat destination (a stored session) it is the plain button.
export function AcceptButton({ label, disabled, onRun, onChat }: { label: string; disabled: boolean; onRun: () => void; onChat?: (() => void) | undefined }) {
  if (!onChat) return <button type="button" className="primary liveInline" disabled={disabled} onClick={onRun}>{label}</button>;
  return (
    <span className="liveMenu">
      <button type="button" className="primary liveInline" disabled={disabled} aria-haspopup="menu">{label} ▾</button>
      <span className="liveMenuItems" role="menu">
        <button type="button" className="secondary liveInline" role="menuitem" disabled={disabled} onClick={onRun}>→ One-shot task</button>
        <button type="button" className="secondary liveInline" role="menuitem" disabled={disabled} onClick={onChat}>→ Chat</button>
      </span>
    </span>
  );
}

// What the duplicate filter held back from a round: one line with the scores, the proposals themselves on demand.
export function FilteredProposals({ item, allFiltered }: { item: LiveProposalSet; allFiltered: boolean }) {
  const filtered = item.filtered ?? [];
  const scores = filtered.map((entry) => entry.score.toFixed(2)).join(", ");
  const stale = filtered.every((entry) => entry.source === "stale"); // an idle round the person went on before
  return (
    <details className="liveFiltered">
      <summary className="liveMuted">{stale ? `${filtered.length} not shown: you went on before ${filtered.length === 1 ? "it was" : "they were"} ready`
        : `${allFiltered ? `all ${filtered.length}` : filtered.length} filtered as duplicate${filtered.length === 1 ? "" : "s"} (score${filtered.length === 1 ? "" : "s"} ${scores})`}</summary>
      <ol>
        {filtered.map((entry, index) => (
          <li key={`${item.ts}-filtered-${index}`}>
            <ProposalLine proposal={entry} />
            <div className="liveMuted">{entry.source === "stale" ? entry.note : `${entry.score.toFixed(3)} · matched “${entry.matched}” · ${entry.note}`}</div>
          </li>
        ))}
      </ol>
    </details>
  );
}

export function decisionLabel(feedback: NonNullable<LiveProposalSet["feedback"]>) {
  if (feedback.decision === "reject") return feedback.rejectReason ? `rejected (${feedback.rejectReason})` : "rejected";
  return feedback.choice ? `accepted choice ${feedback.choice + 1}` : "accepted";
}

export type TaskEdit =
  | { op: "status"; taskId: string; step?: number; done: boolean }
  | { op: "rename"; taskId: string; title: string }
  | { op: "update"; taskId: string; remove: number[] } // entries to the trash, by their number as shown
  | { op: "update"; taskId: string; steps: { text: string }[] } // entries added by hand
  | { op: "add"; taskId: null; title: string; steps: { text: string }[] } // a task added by hand
  | { op: "delete"; taskId: string } // the task to the trash
  | { op: "restore"; taskId: string; step?: number }; // back from the trash: the task, or one entry by its number in the trash
export const TASK_FLAG_REASONS = ["made up", "duplicate", "wrong level", "already done"]; // after a thumbs down; anything else is typed

// Thumbs up or down on a task or one of its entries (step null: the task), then the reason after a thumbs down. One
// flag per task or entry: a second click updates it, the active thumb clicked again removes it.
export type TaskFlagHandlers = {
  flag: (taskId: string, step: number | null, verdict: "up" | "down") => Promise<LiveTaskFlag | null>;
  reason: (flagId: string, reason: string) => void;
  unflag: (flagId: string) => void;
  busy: boolean;
};
// One task-list op in words, for the Tasks panel's latest-change line and the timelines' task-update cards.
export function describeOp(op: LiveTaskOp, titles: Record<string, string>): string {
  const task = op.taskId ? titles[op.taskId] ?? "a task" : "a task";
  switch (op.op) {
    case "add": return `added ${op.title ?? "a task"}`;
    case "update": return [
      op.steps?.length ? `added ${op.steps.map((step) => step.text).join("; ")} to ${task}` : "",
      op.reword?.length ? `reworded ${op.reword.map((change) => `entry ${change.step} of ${task} to ${change.text}`).join("; ")}` : "",
      op.remove?.length ? `trashed ${op.remove.length} entr${op.remove.length === 1 ? "y" : "ies"} of ${task}` : "",
    ].filter(Boolean).join("; ") || `updated ${task}`;
    case "status": return `${op.done ? "marked" : "reopened"} ${op.step ? `entry ${op.step} of ${task}` : task}${op.done ? " done" : ""}`;
    case "rename": return `renamed ${task} to ${op.title ?? ""}`;
    case "delete": return `trashed ${task}`;
    case "restore": return `restored ${task}`;
  }
}

// A belief tick in the timelines' task-update card. When a call was not applied, each round's calls come first, ✓ applied
// or ✗ with the result the model read, then the net result; otherwise, and for ticks from before the per-call log, the
// ops applied and any held or refused results.
export function ChangeBody({ change, titles }: { change: LiveTaskChange; titles: Record<string, string> }) {
  const calls = change.calls ?? [];
  const held = [...(change.warned ?? []), ...(change.rejected ?? [])];
  const result = <ul className="ops">{change.ops.map((op, index) => <li key={index}>{describeOp(op, titles)}</li>)}{change.ops.length ? null : <li className="muted">nothing applied</li>}</ul>;
  if (calls.every((call) => call.applied)) {
    return <>{result}{!calls.length && held.length ? <><h3>Not applied</h3><ul className="ops">{held.map((text, index) => <li key={index}>{text}</li>)}</ul></> : null}</>;
  }
  return <>
    {[...new Set(calls.map((call) => call.round))].map((round) => <Fragment key={round}><h3>Round {round}</h3><ul className="ops">
      {calls.filter((call) => call.round === round).map((call, index) => <li key={index}>{call.applied ? "✓" : "✗"} {call.op ? describeOp(call.op, titles) : call.tool}{call.applied ? null : <p className="muted">{call.result}</p>}</li>)}
    </ul></Fragment>)}
    <h3>Result</h3>{result}
  </>;
}

// The standing task for one-off things that serve no goal, named in the belief prompt (python-app, belief/prompts.py).
// It is not a goal like the others, so it is drawn apart and listed after them.
export const SIDEQUESTS = "Sidequests";

export function mergedEvents(detail: RunDetail, streamed: RunEvent[] | undefined) {
  if (!streamed?.length) return detail.events;
  const seen = new Set(detail.events.map((event) => event.id));
  return [...detail.events, ...streamed.filter((event) => !seen.has(event.id))];
}

export type Turn = { number: number | null; events: RunEvent[] };

// Each agent marks every model reply ("Responses API turn N completed" from the desktop agent, "Claude turn N completed"
// from the cmux agent); the events after it, until the next marker, are what that reply led to (its tool calls and
// their output). Events before the first marker are setup.
export function turnsOf(events: RunEvent[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn = { number: null, events: [] };
  for (const event of events) {
    const match = event.type === "run_progress" ? /^(?:Responses API|Claude|Codex) turn (\d+) completed/.exec(event.message) : null;
    if (match) {
      turns.push(current);
      current = { number: Number(match[1]), events: [] };
    }
    current.events.push(event);
  }
  turns.push(current);
  return turns.filter((turn) => turn.events.length > 0);
}

// The cmux agent's Claude asked for permission and nothing has answered yet: no answer event, no tool result.
export function pendingApproval(turns: Turn[]): RunEvent | null {
  const events = turns[turns.length - 1]?.events ?? [];
  const asked = events.map((event) => event.message.startsWith("Claude is waiting for your approval")).lastIndexOf(true);
  if (asked < 0) return null;
  const answered = events.slice(asked + 1).some((event) => event.type === "function_call_completed" || event.message.startsWith("Approval "));
  return answered ? null : events[asked] ?? null;
}

export function toolRunning(turn: Turn): boolean {
  const requested = turn.events.map((event) => event.type).lastIndexOf("function_call_requested");
  return requested >= 0 && !turn.events.slice(requested).some((event) => event.type === "function_call_completed");
}

// A tool call's detail is its name and its raw JSON arguments, "exec_py {...}". With both desktops, exec_py's
// arguments carry the target, and the label says which desktop ran the code: "exec_py (remote)".
export function toolLabel(detail: string | undefined): string {
  const space = detail?.indexOf(" ") ?? -1;
  if (!detail || space < 0) return detail || "tool";
  const name = detail.slice(0, space);
  try {
    const target: unknown = JSON.parse(detail.slice(space + 1)).target;
    return typeof target === "string" ? `${name} (${target})` : name;
  } catch {
    return name; // the model's arguments were not JSON; the call itself was refused
  }
}

const ARGUMENT_KEYS = ["command", "file_path", "path", "url", "pattern", "query", "code", "description", "prompt"]; // the argument a call is told by

// A tool call's detail ("Bash {...}") as it reads best, as Claude Code shows it: the name (an MCP tool's as "server · tool"),
// the one argument that says what it does, and the description Claude Code gives a shell command. Its first line: a
// permission request's detail says how to answer below the call.
export function callParts(detail: string | undefined): { name: string; argument: string; description: string } {
  const raw = detail?.split("\n")[0] ?? "";
  const space = raw.indexOf(" ");
  const name = (space < 0 ? raw : raw.slice(0, space)).replace(/^mcp__([^_]+(?:-[^_]+)*)__/, "$1 · ") || "tool";
  let argument = space < 0 ? "" : raw.slice(space + 1);
  let description = "";
  try {
    const args = JSON.parse(argument) as Record<string, unknown>;
    const key = ARGUMENT_KEYS.find((item) => typeof args[item] === "string") ?? Object.keys(args).find((item) => typeof args[item] === "string");
    argument = key ? String(args[key]) : "";
    description = key !== "description" && typeof args.description === "string" ? args.description : "";
  } catch {
    // not JSON: the arguments read as they are
  }
  return { name, argument, description };
}

export function turnSummary(turn: Turn): string {
  const parts: string[] = [];
  const usage = turn.number === null ? "" : (turn.events[0]?.detail ?? "").split(" · ").slice(1).join(" · "); // "1234 in · 88 out · 0 reasoning"
  if (usage) parts.push(usage);
  const tools = turn.events.filter((event) => event.type === "function_call_requested").map((event) => toolLabel(event.detail));
  const lines = turn.events.filter((event) => event.type === "tool_progress").length;
  if (tools.length) parts.push(`${tools.join(", ")}${lines ? ` · ${lines} output line${lines === 1 ? "" : "s"}` : ""}`);
  if (turn.events.some((event) => event.type === "run_progress" && event.message.startsWith("Model returned a final response"))) parts.push("final response");
  if (turn.events.some((event) => event.type === "run_failed")) parts.push("failed");
  return parts.join(" · ");
}

export function runState(turns: Turn[], running: boolean): string {
  const calls = turns.filter((turn) => turn.number !== null).length;
  const count = `${calls} model call${calls === 1 ? "" : "s"}`;
  if (!running) return count;
  const last = turns[turns.length - 1];
  const asking = pendingApproval(turns);
  if (last && asking) return `${count} · call ${last.number}: ${asking.message}`; // a waiting tool looks stuck otherwise
  if (last && last.number !== null && toolRunning(last)) {
    const lines = last.events.filter((event) => event.type === "tool_progress").length;
    const requested = last.events.filter((event) => event.type === "function_call_requested");
    return `${count} · call ${last.number}: ${toolLabel(requested[requested.length - 1]?.detail)} running${lines ? `, ${lines} output line${lines === 1 ? "" : "s"} so far` : ""}`;
  }
  return `${count} · waiting for model call ${calls + 1}`;
}

// A <details> whose body is built only while it is open: a collapsed run or model call costs its summary line, not its
// events and screenshots. ``open`` is what the parent asks for; the person's clicks override it until that changes.
export function Fold({ className, open, summary, children }: { className: string; open: boolean; summary: ReactNode; children: () => ReactNode }) {
  const [shown, setShown] = useState(open);
  useEffect(() => { setShown(open); }, [open]);
  return (
    <details className={className} open={shown} onToggle={(event) => setShown(event.currentTarget.open)}>
      <summary>{summary}</summary>
      {shown ? children() : null}
    </details>
  );
}

export function EventList({ events }: { events: RunEvent[] }) {
  return (
    <ol className="liveList liveEvents">
      {events.map((event) => (
        <li key={event.id}><span className="liveTime">{clockOf(event.createdAt)}</span> <strong>{event.type}</strong> {event.message}{event.detail ? <div className="liveMuted">{event.detail.slice(0, 600)}</div> : null}</li>
      ))}
    </ol>
  );
}

export const FINAL_NOTE = "Model final response: "; // the note every agent ends a completed run with
export const MARKDOWN_LINKS: Components = { a: (props) => <a {...props} target="_blank" rel="noreferrer" /> }; // keep the console open

// How the run ended: the model's final response rendered as markdown, since models answer in it, or for a run that
// ended without one the last note (a failure's error code or hint).
export function RunOutcome({ notes }: { notes: string[] }) {
  const final = notes.find((note) => note.startsWith(FINAL_NOTE));
  if (final) {
    return (
      <div className="liveMarkdown">
        <p className="liveMuted">Final response</p>
        <Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_LINKS}>{final.slice(FINAL_NOTE.length)}</Markdown>
      </div>
    );
  }
  const last = notes[notes.length - 1];
  return last ? <p>{last}</p> : null;
}

export type RunCardProps = {
  detail: RunDetail;
  events: RunEvent[];
  runnerBaseUrl: string;
  open: boolean;
  onStop?: ((runId: string) => void) | undefined;
  onAnswer?: ((runId: string, mode: "once" | "deny") => void) | undefined; // answers a cmux run's permission request
  turn?: boolean; // shown under its chat message, which carries the prompt and the notes: the card leaves them out
};

export function RunCard({ detail, events, runnerBaseUrl, open, onStop, onAnswer, turn = false }: RunCardProps) {
  const chatTurn = turn || Boolean(detail.chat);
  const running = detail.run.status === "running";
  // Placeholder frames ("no app window observed yet") are flagged by the backend; they are filler, not agent work.
  const shots = (detail.browser?.screenshots ?? []).filter((shot) => !shot.placeholder);
  const took = runTime(detail.run); // the agent's own time on the task, shown right after its cost
  const summary = (
    <>
      <strong>{detail.run.id}</strong> · {detail.run.status}{detail.actorAgent ? ` · ${AGENT_LABELS[detail.actorAgent] ?? detail.actorAgent}` : ""} · {clockOf(detail.run.startedAt)}{detail.run.usage ? ` · ${usageLine(detail.run.usage)}` : ""}
      {took ? <span title="Time the agent took on this task"> · ⏱ {took}</span> : null}
      {chatTurn ? null : <span className="liveMuted livePrompt">{detail.run.prompt}</span>}{/* a chat turn's prompt and context are shown with its message */}
    </>
  );
  return (
    <Fold className={`liveRunCard ${detail.run.status}`} open={open} summary={summary}>
      {() => {
        const turns = turnsOf(events);
        const asking = running ? pendingApproval(turns) : null;
        const ask = asking ? callParts(asking.detail) : null;
        return (
          <>
            {detail.liveContext && !chatTurn ? <details><summary>Live context given to the agent</summary><pre>{detail.liveContext}</pre></details> : null}
            {running && onStop ? (
              <div className="liveActions"><button type="button" className="secondary liveInline" onClick={() => onStop(detail.run.id)}>Stop run</button></div>
            ) : null}
            <p className="liveMuted liveRunState">{running ? <><span className="liveDot running" aria-hidden="true" /></> : null}{runState(turns, running)}</p>
            {ask && onAnswer ? (
              <div className="liveActions liveApproval">
                <code title={ask.description}>{ask.argument || ask.name}</code>
                <button type="button" className="primary liveInline" onClick={() => onAnswer(detail.run.id, "once")}>Allow once</button>
                <button type="button" className="secondary liveInline" onClick={() => onAnswer(detail.run.id, "deny")}>Deny</button>
              </div>
            ) : null}
            {turns.map((turn, index) => (
              <Fold key={turn.number ?? "setup"} className="liveTurn" open={index === turns.length - 1} summary={<>
                <strong>{turn.number === null ? "Setup" : `Model call ${turn.number}`}</strong>
                {turnSummary(turn) ? ` · ${turnSummary(turn)}` : ""} <span className="liveMuted">({turn.events.length} events)</span>
              </>}>
                {() => <EventList events={turn.events} />}
              </Fold>
            ))}
            {shots.length ? (
              <>
                {!running ? <p className="liveMuted">All {shots.length} screenshots from this run, in order. Click one to open it full size.</p> : null}
                <div className={running ? "liveShots" : "liveShots liveShotsAll"}>
                  {/* While running: the latest few, compact. Finished: every capture, each loaded as it scrolls into view. */}
                  {(running ? shots.slice(-6) : shots).map((shot) => (
                    <a key={shot.id} href={`${runnerBaseUrl}${shot.url}`} target="_blank" rel="noreferrer">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={`${runnerBaseUrl}${shot.url}`} alt={shot.label} title={shot.label} loading="lazy" decoding="async" />
                    </a>
                  ))}
                </div>
              </>
            ) : null}
            <RunOutcome notes={detail.run.summary?.notes ?? []} />
          </>
        );
      }}
    </Fold>
  );
}

// State fed by polling: a value whose JSON equals the current one is dropped, so a poll that brings nothing new
// re-renders nothing. Takes a value or an updater, like useState's setter. The updater is pure (no ref written inside
// it): React's dev mode calls updaters twice and keeps the second result.
export function usePolled<T>(initial: T): [T, (next: T | ((current: T) => T)) => void] {
  const [value, setValue] = useState(initial);
  const set = useCallback((next: T | ((current: T) => T)) => {
    setValue((current) => {
      const resolved = typeof next === "function" ? (next as (current: T) => T)(current) : next;
      return JSON.stringify(resolved) === JSON.stringify(current) ? current : resolved; // the same reference: no render
    });
  }, []);
  return [value, set];
}


export type Ask = NonNullable<NonNullable<LiveSessionStatus["session"]>["permissionAsks"]>[number]; // a permission question waiting: an app looked at under ask, or one the agent needs
export const wants = (item: Ask) => `Cue wants to ${item.source === "agent" && item.level === "use" ? "use" : "look at"} ${item.app}`; // the question as a line
