import type { RunEvent } from "@cue/contracts";

import { callParts } from "./shared";

// A run's events as the person reads them (Turn.tsx): the agent's narration, and runs of tool calls between the
// narration, each call with what it did and what came back. The same for every agent: they all emit the same events.

export type CallKind = "shell" | "read" | "search" | "list" | "edit" | "fetch" | "desktop" | "browser" | "agent" | "other";

export type Call = {
  id: string;
  kind: CallKind;
  name: string; // as callParts reads it: "Bash", "desktop · exec_py"
  tool: string; // as the events name it: "Bash", "mcp__desktop__exec_py"; what pairs an output or a completion to its call
  target: string; // the argument that says what it did: the command, the file, the pattern, the url
  description: string; // Claude Code's own words for a shell command, when it gave any
  startedAt: string;
  endedAt: string | null;
  output: string; // the output lines the tool sent back, as recorded
  waiting: boolean; // asked for permission, not answered yet
};

export type Step = { kind: "say"; id: string; text: string } | { kind: "work"; id: string; calls: Call[] };

const SHELLS = new Set(["Bash", "shell", "bash", "exec", "exec_command", "local_shell"]);
const READ = /^(cat|head|tail|sed|less|bat|wc)\b/, SEARCH = /^(rg|grep|find|fd|ag)\b/, LIST = /^(ls|tree)\b/;

export function kindOf(name: string, target: string): CallKind {
  if (SHELLS.has(name)) {
    const command = target.replace(/^(cd\s+\S+\s*&&\s*)+/, "");
    return READ.test(command) ? "read" : SEARCH.test(command) ? "search" : LIST.test(command) ? "list" : "shell";
  }
  if (/^(desktop|cmux-cua|computer-use) · /.test(name)) return "desktop";
  if (name.startsWith("browser · ")) return "browser";
  if (/^(Read|read_file|view_file)$/.test(name)) return "read";
  if (/^(Edit|MultiEdit|Write|NotebookEdit|apply_patch|str_replace_editor|write_file|edit_file)$/.test(name)) return "edit";
  if (/^(Grep|Glob|ToolSearch|WebSearch)$/.test(name) || /search/i.test(name)) return "search";
  if (/^(WebFetch|fetch)$/.test(name)) return "fetch";
  if (/^(Task|Agent)$/.test(name)) return "agent";
  return "other";
}

// The calls may overlap (an agent asks for several at once), so an output or a completion goes to the earliest call still
// out under that tool's name. ``settledAt`` is the run's end: a call the events never closed is taken as ended then.
export function stepsOf(events: RunEvent[], settledAt?: string | null): Step[] {
  const steps: Step[] = [];
  const pending: Call[] = [];
  const work = (): Extract<Step, { kind: "work" }> => {
    const last = steps[steps.length - 1];
    if (last?.kind === "work") return last;
    const step: Extract<Step, { kind: "work" }> = { kind: "work", id: `work-${steps.length}`, calls: [] };
    steps.push(step);
    return step;
  };
  const out = (tool: string) => pending.find((call) => call.tool === tool) ?? pending[pending.length - 1];
  for (const event of events) {
    if (event.type === "function_call_requested") {
      const { name, argument, description } = callParts(event.detail);
      const call: Call = { id: event.id, kind: kindOf(name, argument), name, tool: (event.detail ?? "").split(/[\s\n]/)[0] ?? "", target: argument, description,
        startedAt: event.createdAt, endedAt: null, output: "", waiting: false };
      work().calls.push(call);
      pending.push(call);
    } else if (event.type === "tool_progress" && event.detail) {
      const call = out(event.message.replace(/ output\.$/, ""));
      if (call) call.output = call.output ? `${call.output}\n${event.detail}` : event.detail;
    } else if (event.type === "function_call_completed") {
      const call = out((event.detail ?? "").trim());
      if (call) {
        call.endedAt = event.createdAt;
        call.waiting = false;
        pending.splice(pending.indexOf(call), 1);
      }
    } else if (event.type === "run_progress" && event.message.startsWith("Claude is waiting for your approval")) {
      const call = out((event.detail ?? "").split(/[\s\n]/)[0] ?? "");
      if (call) call.waiting = true;
    } else if (event.type === "run_progress" && event.message.startsWith("Approval ")) {
      for (const call of pending) call.waiting = false;
    } else if (event.type === "run_progress" && event.message === "Model progress." && event.detail?.trim()) {
      steps.push({ kind: "say", id: event.id, text: event.detail.trim() });
    }
  }
  if (settledAt) {
    for (const call of pending) {
      call.endedAt = settledAt;
      call.waiting = false;
    }
  }
  return steps;
}

const VERBS: Record<CallKind, [string, string]> = { // done, doing
  shell: ["Ran", "Running"], read: ["Read", "Reading"], search: ["Searched", "Searching"], list: ["Listed", "Listing"], edit: ["Edited", "Editing"],
  fetch: ["Fetched", "Fetching"], desktop: ["Desktop", "Desktop"], browser: ["Browser", "Browser"], agent: ["Agent", "Agent"], other: ["Used", "Using"],
};
const NOUNS: Record<CallKind, [string, string]> = { // "ran 3 commands": the verb in a sentence, the thing counted
  shell: ["ran", "command"], read: ["read", "file"], search: ["searched", "time"], list: ["listed", "folder"], edit: ["edited", "file"],
  fetch: ["fetched", "page"], desktop: ["took", "desktop step"], browser: ["took", "browser step"], agent: ["ran", "agent"], other: ["used", "tool"],
};

const basename = (path: string) => path.split("/").filter(Boolean).pop() ?? path;

// One call as a line: the verb and what it was on. A file by its name, a command or a pattern as typed, one line of it.
export function callLine(call: Call): { verb: string; target: string } {
  const verb = VERBS[call.kind][call.endedAt ? 0 : 1];
  const first = call.target.split("\n")[0]?.trim() ?? "";
  const target = call.kind === "read" || call.kind === "edit" ? (SHELLS.has(call.name) ? first : basename(first)) : call.kind === "other" ? `${call.name}${first ? ` ${first}` : ""}` : first || call.name;
  return { verb, target: target.length > 100 ? `${target.slice(0, 100)}…` : target };
}

// What a run of calls amounted to, as one sentence: "Ran 3 commands, read 2 files, and edited 1 file".
export function workSentence(calls: Call[]): string {
  const counts = new Map<CallKind, number>();
  for (const call of calls) counts.set(call.kind, (counts.get(call.kind) ?? 0) + 1);
  const parts = [...counts].map(([kind, count]) => `${NOUNS[kind][0]} ${count} ${NOUNS[kind][1]}${count === 1 ? "" : "s"}`);
  const joined = parts.length > 2 ? `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}` : parts.join(" and ");
  return joined.charAt(0).toUpperCase() + joined.slice(1);
}

// Why a run failed, as the agent reported it: its last failure event, else the note the run ended with. "Codex ended
// with failed: You've hit your usage limit…" reads as "Codex: You've hit your usage limit…".
export function failureOf(events: RunEvent[], notes: string[] = []): string {
  const failed = [...events].reverse().find((event) => event.type === "run_failed");
  const text = failed?.detail || failed?.message || notes.find((note) => / ended with failed: /.test(note)) || "";
  return text.replace(/^(\w[\w ]*?) ended with failed: /, "$1: ").trim();
}

// How long a call took, once it is done: "3s", "1m 05s"; nothing under a second.
export function callTime(call: Call): string {
  if (!call.endedAt) return "";
  const total = Math.round((Date.parse(call.endedAt) - Date.parse(call.startedAt)) / 1000);
  if (!Number.isFinite(total) || total < 1) return "";
  return total >= 60 ? `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, "0")}s` : `${total}s`;
}
