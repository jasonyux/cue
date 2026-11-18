import type { LiveChatMessage } from "@cue/contracts";

import type { Mark } from "./Spine";

export type Cluster = { id: string; start: number; marks: Mark[] };
export type WorkSegment = { id: string; start: number; end: number; active: boolean; mark: Mark };
export type Span = { id: string; start: number; end: number; active: boolean; mark: Mark; label: string; segments: WorkSegment[] };

// Anchor each window to its first event so a steady stream cannot merge the whole session.
export function clusterMarks(marks: Mark[]): Cluster[] {
  const clusters: Cluster[] = [];
  for (const mark of [...marks].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts))) {
    const start = Date.parse(mark.ts), previous = clusters.at(-1);
    if (previous && start - previous.start < 30_000) previous.marks.push(mark);
    else clusters.push({ id: mark.id, start, marks: [mark] });
  }
  return clusters;
}

export function activitySpans(marks: Mark[], messages: LiveChatMessage[], now: number): Span[] {
  const groups = new Map<string, { messages: LiveChatMessage[]; runs: Extract<Mark, { kind: "run" }>[] }>();
  const chatByRun = new Map<string, string>();
  for (const message of [...messages, ...marks.flatMap((mark) => mark.kind === "say" ? [mark.message] : [])]) {
    const group = groups.get(message.chatId) ?? { messages: [], runs: [] };
    group.messages.push(message); groups.set(message.chatId, group);
    chatByRun.set(message.runId, message.chatId);
  }
  const runs = marks.filter((mark): mark is Extract<Mark, { kind: "run" }> => mark.kind === "run");
  const nativeChats = new Map<string, string>();
  for (const mark of runs) {
    const native = mark.detail.chat?.claudeSessionId ?? mark.detail.chat?.codexThreadId;
    const known = chatByRun.get(mark.detail.run.id);
    if (native && known) nativeChats.set(native, known);
  }
  for (const mark of runs) {
    const native = mark.detail.chat?.claudeSessionId ?? mark.detail.chat?.codexThreadId;
    const id = chatByRun.get(mark.detail.run.id) ?? (native ? nativeChats.get(native) ?? `agent:${native}` : mark.id);
    const group = groups.get(id) ?? { messages: [], runs: [] };
    group.runs.push(mark); groups.set(id, group);
  }
  const spans: Span[] = [];
  for (const [id, group] of groups) {
    const segments: WorkSegment[] = [];
    for (const mark of group.runs) {
      const start = Date.parse(mark.ts), active = mark.detail.run.status === "running" && !mark.end;
      const end = mark.end ? Date.parse(mark.end) : active ? now : start;
      if (end > start) segments.push({ id: mark.id, start, end, active, mark });
    }
    segments.sort((a, b) => a.start - b.start);
    const times = [...group.messages.map((message) => Date.parse(message.ts)), ...segments.flatMap((segment) => [segment.start, segment.end])];
    const start = Math.min(...times), end = Math.max(...times);
    const mark = marks.find((item) => item.kind === "say" && item.message.chatId === id) ?? group.runs[0];
    if (!mark || !(end > start)) continue;
    const conversation = group.messages.length > 0 || group.runs.some((run) => Boolean(run.detail.chat));
    spans.push({ id, start, end, active: segments.some((segment) => segment.active), mark, label: conversation ? "Conversation" : "Agent task", segments });
  }
  return spans;
}

// Concurrent conversations get separate tracks; later ones reuse a finished track.
export function placeSpans(spans: Span[]): (Span & { lane: number; lanes: number })[] {
  const ends: number[] = [];
  const placed = [...spans].sort((a, b) => a.start - b.start).map((span) => {
    let lane = ends.findIndex((end) => end <= span.start);
    if (lane < 0) lane = ends.length;
    ends[lane] = span.end;
    return { ...span, lane };
  });
  return placed.map((span) => ({ ...span, lanes: ends.length }));
}
