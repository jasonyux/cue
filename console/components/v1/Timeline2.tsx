"use client";

import { type LiveChatMessage, type LiveConversation, type LiveProposalSet, type LiveTaskChange } from "@cue/contracts";
import Link from "next/link";
import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { AGENT_LABELS, type Ask, ByHand, ChangeBody, MARKDOWN_LINKS, RunCard, callParts, clockOf, decisionLabel, mergedEvents, pendingApproval, proposalsOf, searchLabel, turnsOf, wants } from "../live/shared";
import { useAgentChoice } from "./agentChoice";
import { Chats } from "./Chats";
import { MONITOR, Monitor } from "./Monitor";
import { outcomeOf } from "./Noticed";
import { readOverrides } from "./overrides";
import { Rail } from "./Rail";
import { useRecords } from "./records";
import { TopBar } from "./Shell";
import { type Mark } from "./Spine";
import { TaskList, type TaskPick, X, quote } from "./TaskList";
import { Starting, Turn } from "./Turn";
import { useScrollEdges } from "./useScrollEdges";
import { useSession } from "./useSession";

// The sidebar's panels, picked from the pair of toggles at its top; the choice and the width are this browser's.
const PANELS = {
  chats: { label: "Conversations", icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20 12a8 8 0 0 1-11.6 7.1L4 20l1-4.2A8 8 0 1 1 20 12z" /></svg> },
  timeline: { label: "Timeline", icon: <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true"><path d="M12 3v18M12 7h7M12 12h5M12 17h8M5 7h3M7 12h1M4 17h4" /></svg> },
} as const;
type Panel = keyof typeof PANELS;
const remembered = (key: string) => { try { return window.localStorage.getItem(key); } catch { return null; } };
const remember = (key: string, value: string) => { try { window.localStorage.setItem(key, value); } catch { /* a private window: not kept */ } };

type Props = { expectedBackend: "python"; runnerBaseUrl: string };
type Front = { id: string; ts: string; kind: "approval" | "ask" | "suggest"; title: string; tag?: string; answer?: string }; // one thing that waits on you, or did

const AGENT_NAMES: Record<string, string> = { claude: "Claude Code", codex: "Codex", cmux: "cmux + Claude Code", desktop: "Desktop (GUI)" };
const KIND_LABEL: Record<Mark["kind"], string> = { look: "Screenshot", note: "Note", change: "Task list", suggest: "Suggestion", check: "Pause", answer: "Answer", say: "You", run: "Run", ask: "Permission" };
const GLYPH: Record<Mark["kind"], string> = { look: "◎", note: "≋", change: "◇", suggest: "◆", check: "○", answer: "✔", say: "💬", run: "⌘", ask: "▣" };

// The stored records and the polled ones, the polled winning: the rail reads both, the records every half minute.
function merge<T extends { ts: string }>(stored: T[], fresh: T[]): T[] {
  const map = new Map(stored.map((item) => [item.ts, item]));
  for (const item of fresh) map.set(item.ts, item);
  return [...map.values()];
}
// A mark in a line of text, for a source card or a tick's title.
function describe(mark: Mark): string {
  switch (mark.kind) {
    case "look": return `${mark.shot.app}${mark.shot.window ? ` — ${mark.shot.window}` : ""}`;
    case "note": return mark.note.summary;
    case "change": return `${mark.change.ops.length} change${mark.change.ops.length === 1 ? "" : "s"} to the task list`;
    case "suggest": case "check": return proposalsOf(mark.set)[0]?.task ?? mark.set.summary ?? "";
    case "answer": return mark.set.feedback ? `${decisionLabel(mark.set.feedback)}: ${proposalsOf(mark.set)[0]?.task ?? ""}` : "";
    case "say": return mark.message.text;
    case "run": return mark.detail.run.prompt ?? "a run";
    case "ask": return mark.title;
  }
}

// The Timeline v2 theme of the landing page: the stacked-cards look. A slim timeline of thumbnails and glyphs on the
// left; in the middle one card for what needs you, the others stacked behind it; "Why?" opens a page on that one
// proposal's stated reason and the records it cites; an accept opens the conversation beside the agent's screen; the
// tasks sit in a bar along the bottom that unfolds into a list, a section per task. A tick on the timeline opens its record on the right.
// The chat box keeps its text to itself, so a keystroke re-renders the box and not the page around it. ``initial`` is the text
// kept from before the conversation was closed; ``seed`` is what Accept puts in, focused; ``onChange`` keeps the page's copy.
const Composer = memo(function Composer({ initial, seed, picks, onDrop, disabled, placeholder, canSend, onChange, onSend, onStop }: {
  initial: string; seed: string; picks: TaskPick[]; onDrop: (index: number) => void; disabled: boolean; placeholder: string; canSend: boolean; onChange: (text: string) => void; onSend: (text: string, picks: TaskPick[]) => Promise<boolean>;
  onStop?: (() => void) | undefined; // a turn is running: with the box empty, the round button stops it; typed text turns it back into Send, which queues
}) {
  const [draft, setDraft] = useState(initial);
  const input = useRef<HTMLTextAreaElement>(null);
  const set = (text: string) => { setDraft(text); onChange(text); };
  useEffect(() => { if (seed) { set(seed); input.current?.focus(); } }, [seed]); // eslint-disable-line react-hooks/exhaustive-deps -- on a new seed only
  useEffect(() => { if (picks.length) input.current?.focus(); }, [picks.length]); // the tray's Attach: the cursor after the chips
  useLayoutEffect(() => { // the box grows with the text (CSS field-sizing); measured here only where the browser cannot, since measuring re-lays out the page on every keystroke
    const el = input.current;
    if (!el || "fieldSizing" in el.style) return;
    el.style.height = "auto";
    const cap = window.innerHeight * 0.4;
    el.style.height = `${Math.min(el.scrollHeight, cap)}px`;
    el.style.overflowY = el.scrollHeight > cap ? "auto" : "hidden";
  }, [draft]);
  const send = async () => {
    const text = draft.trim();
    if ((!text && !picks.length) || !canSend) return;
    set("");
    if (!(await onSend(picks.length ? [quote(picks), text].filter(Boolean).join("\n\n") : text, picks))) set(text);
  };
  return (
    <form className="composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
      {picks.length ? <div className="picks">{picks.map((pick, index) => <span key={`${pick.taskId}:${pick.text}`} className="pickchip" title={`${pick.title}: ${pick.text}`}><i /><span>{pick.text}</span><button type="button" className="x" aria-label="Drop" onClick={() => onDrop(index)}>{X}</button></span>)}</div> : null}
      <div className="boxline">
      <textarea ref={input} value={draft} disabled={disabled} placeholder={placeholder} aria-label="Message Cue" onChange={(event) => set(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} />
      {onStop && !draft.trim() && !picks.length ? <button type="button" className="send halt" aria-label="Stop the turn" title="Stop the current turn" onClick={onStop}><i /></button>
        : <button type="submit" className="send" disabled={(!draft.trim() && !picks.length) || !canSend} aria-label="Send">↑</button>}
      </div>
    </form>
  );
});

export function Timeline2({ expectedBackend, runnerBaseUrl }: Props) {
  const s = useSession(runnerBaseUrl, expectedBackend);
  const session = s.session;
  const { records, screenshotUrl } = useRecords(runnerBaseUrl, expectedBackend, session ? null : undefined);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 5_000); return () => clearInterval(timer); }, []);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Mark | null>(null); // a tick's record, in the panel on the right
  const [why, setWhy] = useState<{ id: string; choice: number } | null>(null); // the Why panel: a card and which of its proposals
  const whySource = useRef<HTMLElement>(null);
  const [source, setSource] = useState<string | null>(null); // the cited record the Why page shows on its side
  const [frontId, setFrontId] = useState<string | null>(null); // the one the person brought to the front
  const [filter, setFilter] = useState<"all" | "ask" | "suggest" | "done">("all");
  const [replied, setReplied] = useState<Front[]>([]); // the permission questions answered on this page: nothing records them once answered
  const [chatOpen, setChatOpen] = useState(false);
  const draft = useRef(""); // the chat box's text, kept here only for while the conversation is closed
  const [seen, setSeen] = useState<string[]>([]); // finished runs the person has opened the chat on
  const [declining, setDeclining] = useState<string | null>(null); // "ts:choice" whose Not now reasons show
  const [declinePosition, setDeclinePosition] = useState({ left: 0, bottom: 0, maxHeight: 0 });
  const [alternatives, setAlternatives] = useState(false);
  const [expandedCards, setExpandedCards] = useState<Set<string>>(() => new Set());
  const [paneOn, setPaneOn] = useState(true); // the screen pane beside the chat, on or off by hand
  const [share, setShare] = useState(58); // the screen pane's share of the workspace, in percent
  const [opened, setOpened] = useState<Set<string>>(() => new Set()); // in the chat's log: folds shown ("fold:ts") and folded suggestions opened as cards ("set:ts")
  const [panel, setPanel] = useState<Panel>("chats");
  const [sideWidth, setSideWidth] = useState(232);
  useEffect(() => { // the last visit's choice and width, after the first paint: the server's HTML and the first client render must agree
    if (remembered("v1.side") === "timeline") setPanel("timeline");
    const width = Number(remembered("v1.sideWidth"));
    if (width) setSideWidth(Math.max(160, Math.min(420, width)));
  }, []);
  const showPanel = (name: Panel) => { setPanel(name); remember("v1.side", name); };
  const resizeSide = (event: React.PointerEvent) => {
    const start = event.clientX, from = sideWidth;
    event.preventDefault();
    const move = (moved: PointerEvent) => setSideWidth(Math.max(160, Math.min(420, from + moved.clientX - start)));
    const up = (moved: PointerEvent) => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); remember("v1.sideWidth", String(Math.max(160, Math.min(420, from + moved.clientX - start)))); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const [jump, setJump] = useState<string | null>(null); // an element of the log to scroll to once rendered

  const running = s.runningId ? s.runs[s.runningId] ?? null : null;
  const followed = running ?? (s.chat?.messages.length ? s.runs[s.chat.messages[s.chat.messages.length - 1]!.runId] ?? null : null); // the run the screen pane follows: the one in flight, else the conversation's latest
  const followedEvents = useMemo(() => followed ? mergedEvents(followed, s.streamed[followed.run.id]) : [], [followed, s.streamed]); // once per change of the run, not per keystroke in the chat box
  const turns = useMemo(() => turnsOf(followedEvents), [followedEvents]);
  const asking = running ? pendingApproval(turns) : null;
  const approval = asking ? callParts(asking.detail) : null;
  const asks = session?.permissionAsks ?? [];
  const sets = useMemo(() => merge(records?.proposalSets ?? [], s.proposalSets).filter((set) => !set.error), [records, s.proposalSets]);
  const changes = useMemo(() => merge(records?.changes ?? [], s.changes), [records, s.changes]);
  const open = sets.filter((set) => proposalsOf(set).length > 0 && !set.feedback).sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));
  const state = !session ? "off" : session.paused ? "paused" : asking || asks.length ? "needs" : running ? "working" : open.length ? "noticed" : "watching";
  const titles = Object.fromEntries((s.status?.tasks ?? []).map((task) => [task.id, task.title]));

  // Every record of the session as a mark, as the Timeline theme builds them.
  const marks = useMemo<Mark[]>(() => {
    if (!session) return [];
    const list: Mark[] = [];
    const stored = records?.screenshots ?? [], latest = session.latestScreenshot;
    for (const shot of latest && !stored.some((item) => item.name === latest.name) ? [...stored, latest] : stored) list.push({ kind: "look", id: `look:${shot.name}`, ts: shot.ts, shot });
    for (const note of records?.summaries ?? []) list.push({ kind: "note", id: `note:${note.ts}`, ts: note.ts, from: note.windowStart, note });
    for (const change of changes) list.push({ kind: "change", id: `change:${change.ts}`, ts: change.ts, from: change.windowStart, change });
    for (const set of sets) {
      if (proposalsOf(set).length === 0) { if (set.trigger === "idle") list.push({ kind: "check", id: `check:${set.ts}`, ts: set.ts, set }); continue; }
      list.push({ kind: "suggest", id: `set:${set.ts}`, ts: set.ts, from: set.windowStart ?? null, set, pending: !set.feedback });
      if (set.feedback) list.push({ kind: "answer", id: `answer:${set.ts}`, ts: set.feedback.ts, set });
    }
    for (const message of s.chat?.messages ?? []) if (message.role === "user") list.push({ kind: "say", id: `say:${message.runId}`, ts: message.ts, message });
    for (const runId of session.runs) {
      const detail = s.runs[runId];
      if (!detail) continue;
      list.push({ kind: "run", id: `run:${runId}`, ts: detail.run.startedAt, end: detail.run.completedAt ?? null, detail });
      const events = mergedEvents(detail, s.streamed[runId]);
      events.forEach((event, index) => {
        if (!event.message.startsWith("Claude is waiting for your approval")) return;
        const answered = events.slice(index + 1).some((later) => later.type === "function_call_completed" || later.message.startsWith("Approval ")) || detail.run.status !== "running";
        const reply = events.slice(index + 1).find((later) => later.message.startsWith("Approval "))?.message.replace(/^Approval answered: /, "").replace(/\.$/, "");
        list.push({ kind: "ask", id: answered ? `approval:${event.id}` : `approval:${runId}`, ts: event.createdAt, title: `Cue wants to use ${callParts(event.detail).name}`, pending: !answered, answer: reply });
      });
    }
    for (const ask of session.permissionAsks ?? []) list.push({ kind: "ask", id: `ask:${ask.app}|${ask.source ?? ""}`, ts: ask.ts, title: wants(ask), pending: true });
    return list.sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  }, [session, records, changes, sets, s.chat, s.runs, s.streamed]);

  // What a proposal can cite, by the record's id: this session's notes, chat turns, suggestions and answers, as marks.
  const byRecord = useMemo(() => {
    const map = new Map<string, Mark>();
    for (const mark of marks) {
      if (mark.kind === "note" && mark.note.id) map.set(mark.note.id, mark);
      else if (mark.kind === "say" && mark.message.id) map.set(mark.message.id, mark);
      else if ((mark.kind === "suggest" || mark.kind === "check") && mark.set.id) map.set(mark.set.id, mark);
      else if (mark.kind === "answer" && mark.set.feedback?.id) map.set(mark.set.feedback.id, mark);
    }
    return map;
  }, [marks]);

  // What waits on you, one in front: the agent's approval first, then the permission questions, then the suggestions, newest first.
  const queue: Front[] = [
    ...(running && asking && approval ? [{ id: `approval:${running.run.id}`, ts: asking.createdAt, kind: "approval" as const, title: `Cue wants to use ${approval.name}` }] : []),
    ...asks.map((item) => ({ id: `ask:${item.app}|${item.source ?? ""}`, ts: item.ts, kind: "ask" as const, title: wants(item), tag: item.source === "agent" ? "for the task" : "permission" })),
    ...open.map((set) => ({ id: `set:${set.ts}`, ts: set.ts, kind: "suggest" as const, title: proposalsOf(set)[0]?.task ?? "", tag: set.trigger === "idle" ? "on a pause" : "" })),
  ];
  const done: Front[] = [
    ...replied,
    ...sets.filter((set) => set.feedback && proposalsOf(set).length > 0).map((set) => ({ id: `set:${set.ts}`, ts: set.feedback!.ts, kind: "suggest" as const, title: proposalsOf(set)[0]?.task ?? "" })),
  ].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));
  const has = (kind: "ask" | "suggest" | "done") => kind === "done" ? done.length > 0 : queue.some((item) => (item.kind === "suggest") === (kind === "suggest"));
  const mode = filter !== "all" && has(filter) ? filter : "all";
  const shown = mode === "done" ? done : mode === "all" ? queue : queue.filter((item) => (item.kind === "suggest") === (mode === "suggest"));
  const front = shown.find((item) => item.id === frontId) ?? shown[0] ?? null;
  const at = front ? shown.indexOf(front) : 0;
  const rear = shown.filter((item) => item !== front).slice(0, 2);
  const bring = (id: string) => { setFilter("all"); setFrontId(id); setWhy(null); };
  const setOf = (id: string) => sets.find((entry) => `set:${entry.ts}` === id);

  // The proposal the Why page is on, and the marks it cites: ringed on the rail while the page is open.
  const whySet = why ? setOf(why.id) : undefined;
  const whyProposal = whySet ? proposalsOf(whySet)[why!.choice] ?? proposalsOf(whySet)[0] : undefined;
  const cited = new Set((whyProposal?.evidence ?? []).map((id) => byRecord.get(id)?.id).filter((id): id is string => Boolean(id)));


  const { agents, agent, setAgent, models, model, sessionModel, setModel } = useAgentChoice(session?.settings ?? null, s.chat?.agent, runnerBaseUrl, expectedBackend); // the chat's agent and its model
  // The Home card's "Send to": the current conversation unless picked otherwise, another one, or "new". Nothing happens
  // before Accept, which switches or resets first (aim) and then sends. Its agent and model are the card's own choice, a
  // second use of the hook, so the chat's stays the conversation's.
  const [target, setTarget] = useState<string | null>(null);
  const conversations = (s.chat?.conversations ?? []).filter((item) => !item.viewOnly).slice(0, 8); // latest first; the ones a message can go to
  const current = s.chats?.current ?? null; // the conversation the next message goes to; none without a session
  const sendTarget = target ?? current ?? "new";
  const targetAgent = sendTarget === "new" ? null : sendTarget === current ? s.chat?.agent ?? null : conversations.find((item) => item.id === sendTarget)?.agent ?? null;
  const sender = useAgentChoice(session?.settings ?? null, targetAgent, runnerBaseUrl, expectedBackend);
  const aim = async () => {
    if (sendTarget === "new") { if (current) await s.resetChat(); } else if (sendTarget !== current) await s.switchChat(sendTarget);
    setTarget(null);
  };
  const [picks, setPicks] = useState<TaskPick[]>([]); // entries picked in the tray, held in the box until sent
  const attach = async (list: TaskPick[]) => { // the tray's Attach: to the picked conversation, as the card's Accept, with its agent and model
    await aim();
    if (!targetAgent) setAgent(sender.agent);
    if (sender.model) setModel(sender.model, sender.agent);
    setPicks((current) => [...current, ...list]);
    setChatOpen(true); setWhy(null); setDetail(null);
  };

  useEffect(() => { // Accept puts the suggestion in the box (Composer) and opens the conversation
    if (!s.chatSeed) return;
    setChatOpen(true);
    setWhy(null);
  }, [s.chatSeed]);
  const finished = (session?.runs ?? []).filter((id) => s.runs[id] && s.runs[id]!.run.status !== "running");
  useEffect(() => { if (chatOpen) setSeen((current) => (finished.every((id) => current.includes(id)) ? current : finished)); }, [chatOpen, finished]);
  const lastRun = finished.map((id) => s.runs[id]!).filter((detail) => detail.chat).pop() ?? null;
  const unread = lastRun && !seen.includes(lastRun.run.id) ? lastRun : null;
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setDetail(null); setDeclining(null); setWhy(null); } };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);
  const msgs = useRef<HTMLDivElement>(null);
  const chatEdges = useScrollEdges(msgs, "y", `${chatOpen}:${s.chat?.id}:${s.chat?.messages.length}:${s.sending}`);
  const stick = useRef(true);
  useEffect(() => {
    const el = msgs.current;
    if (!el) return;
    stick.current = true;
    el.scrollTop = el.scrollHeight;
    const follow = new MutationObserver(() => { if (stick.current) el.scrollTop = el.scrollHeight; });
    follow.observe(el, { childList: true, subtree: true, characterData: true });
    return () => follow.disconnect();
  }, [chatOpen]);

  useEffect(() => { if (source) whySource.current?.scrollIntoView?.({ block: "nearest" }); }, [source]);
  useEffect(() => { if (!jump) return; document.getElementById(jump)?.scrollIntoView?.({ block: "center" }); setJump(null); }, [jump]);

  const pick = (mark: Mark) => { // a tick: what waits comes to the front, the chat opens for its turns, the rest opens its record on the right
    setSelected(mark.id);
    setWhy(null);
    if ((mark.kind === "suggest" || mark.kind === "ask") && mark.pending) {
      if (chatOpen && mark.kind === "suggest") { setOpened((current) => new Set(current).add(`set:${mark.set.ts}`)); setJump(`sg-${mark.set.ts}`); return; } // in the chat: its card, in place
      bring(mark.id); setChatOpen(false); setDetail(null); return;
    }
    if (mark.kind === "say" || mark.kind === "run") { setChatOpen(true); setDetail(null); return; }
    setDetail(mark);
  };
  const notNow = async (item: LiveProposalSet, reason: string, typed: boolean) => { await s.decide(item, "reject", 0); await s.giveReason(item, reason, typed); };
  const answer = (item: Ask, label: string, level: "block" | "look" | "use" | "deny", scope?: "session" | "once" | "task") => {
    setReplied((list) => [{ id: `replied:${Date.now()}`, ts: new Date().toISOString(), kind: "ask", title: wants(item), tag: item.source === "agent" ? "for the task" : "permission", answer: label }, ...list]);
    void s.answerPermission(item.app, level, scope);
  };
  const answers = (item: Ask) => item.source === "agent" ? <>
    <button type="button" className="b primary" title="For one step; nothing saved" onClick={() => answer(item, "Allow once", item.level ?? "use", "once")}>Allow once</button>
    <button type="button" className="b" title="For the rest of this task; nothing saved" onClick={() => answer(item, "For this task", item.level ?? "use", "task")}>For this task</button>
    <button type="button" className="b" title="A use rule in Settings" onClick={() => answer(item, "Always", item.level ?? "use")}>Always</button>
    <button type="button" className="b" onClick={() => answer(item, "Deny", "deny")}>Deny</button>
  </> : <>
    <button type="button" className="b blue" onClick={() => answer(item, "Look", "look")}>Look</button>
    <button type="button" className="b" onClick={() => answer(item, "Use", "use")}>Use</button>
    <button type="button" className="b" title="Look until this session ends; nothing saved" onClick={() => answer(item, "This session", "look", "session")}>This session</button>
    <button type="button" className="b" onClick={() => answer(item, "Block", "block")}>Block</button>
  </>;
  const paused = Boolean(session?.paused);
  const reading = !session && s.browsing; // an earlier conversation open to read while no session runs
  const actions = (item: LiveProposalSet, choice: number, home = false) => { // home: the Home card, sending where its "Send to" says; else the chat's conversation
    const key = `${item.ts}:${choice}`;
    return (
      <div className="acts">
        <span className="accept"><span className="face" aria-hidden="true">→ Accept</span>{/* under the pointer it comes apart: send as is, or edit first */}
          <button type="button" className="b primary go" disabled={s.busy || paused} title="Send it to the chat as it is" onClick={() => { setChatOpen(true); setWhy(null); void (home ? aim().then(() => s.acceptAndSend(item, choice, sender.agent, sender.model)) : s.acceptAndSend(item, choice, agent, model)); }}>→ Do it</button>
          <button type="button" className="b primary pen" disabled={s.busy || paused} aria-label="Edit first" title="Change it in the chat box first" onClick={() => { setChatOpen(true); setWhy(null); void (home ? aim() : Promise.resolve()).then(() => s.acceptToChat(item, choice)); }}>✎</button>
        </span>
        <span className="notNow">
          <button type="button" className="b" disabled={s.busy || paused} aria-haspopup="menu" aria-expanded={declining === key} onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); setDeclinePosition({ left: Math.max(16, Math.min(rect.left, window.innerWidth - 190)), bottom: window.innerHeight - rect.top + 8, maxHeight: Math.max(48, rect.top - 24) }); setDeclining(declining === key ? null : key); }}>Not now ▾</button>
          {declining === key ? (
            <span className="menu" role="menu" popover="manual" ref={(element) => { element?.showPopover?.(); }} style={{ position: "fixed", inset: "auto", left: declinePosition.left, bottom: declinePosition.bottom, maxHeight: declinePosition.maxHeight, overflowY: "auto", margin: 0 }}>
              {(s.status?.rejectReasons ?? []).map((reason) => <button key={reason} type="button" role="menuitem" disabled={s.busy} onClick={() => { setDeclining(null); void notNow(item, reason, false); }}>{reason}</button>)}
              <button type="button" role="menuitem" disabled={s.busy} onClick={() => { const typed = window.prompt("Why not? A few words.")?.trim(); setDeclining(null); if (typed) void notNow(item, typed, true); }}>other…</button>
            </span>
          ) : null}
        </span>
      </div>
    );
  };
  // The card's "Send to", in steps: one of the session's conversations, newest first, or a new one; for one without an
  // agent yet, the agent beside it; then the model, for agents that list any (the chat header's pickers, the same hook).
  const sendTo = () => <>
    {conversations.length ? <span className="pick"><select className="agentPick" aria-label="Send to" value={sendTarget === "new" ? "new" : `conv:${sendTarget}`} onChange={(event) => setTarget(event.target.value === "new" ? "new" : event.target.value.slice(5))}>
      {conversations.map((item) => <option key={item.id} value={`conv:${item.id}`}>{AGENT_NAMES[item.agent ?? ""] ?? item.agent ?? "?"} · {(item.title ?? item.firstMessage).slice(0, 28) || clockOf(item.startedAt)}</option>)}
      <option value="new">New conversation</option>
    </select></span> : null}
    {targetAgent ? null : sender.agents.length < 2 ? AGENT_NAMES[sender.agent] ?? sender.agent : <span className="pick"><select className="agentPick" aria-label="Agent" value={sender.agent} onChange={(event) => sender.setAgent(event.target.value)}>{sender.agents.map((item) => <option key={item} value={item}>{AGENT_NAMES[item] ?? item}</option>)}</select></span>}
    {sender.models.length ? <span className="pick"><select className="agentPick" aria-label="Model" value={sender.model} onChange={(event) => sender.setModel(event.target.value)} title={sender.models.find((item) => item.id === sender.model)?.detail}>
      {sender.sessionModel && !sender.models.some((item) => item.id === sender.sessionModel) ? <option value={sender.sessionModel}>{sender.sessionModel}</option> : null}
      {sender.models.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
    </select></span> : null}
  </>;
  const openWhy = (id: string, choice: number) => { setWhy({ id, choice }); setSource(null); setDetail(null); };
  const openConversation = (item: LiveConversation) => { // from the sidebar: continue it, or read it when it cannot be continued (or nothing runs to continue it in)
    if (item.viewOnly || !session) void s.browseChat(item.id);
    else if (item.id !== current || s.browsing) void (item.id !== current ? s.switchChat(item.id) : s.browseChat(null));
    setChatOpen(true); setWhy(null); setDetail(null);
  };

  // A citation as a chip on the card: a glyph and a time, the text on hover; a click opens the Why page on it.
  const chip = (id: string, set: LiveProposalSet, choice: number): ReactNode => {
    const mark = byRecord.get(id);
    if (mark) return <button key={id} type="button" className={`ctx ${mark.kind}`} title={describe(mark)} onClick={() => { openWhy(`set:${set.ts}`, choice); setSource(id); }}><i>{mark.kind === "answer" && mark.set.feedback?.decision !== "accept" ? "✘" : GLYPH[mark.kind]}</i><span>{KIND_LABEL[mark.kind]}<small>{clockOf(mark.ts).slice(0, 5)}</small></span></button>;
    const hit = set.cited?.find((entry) => entry.id === id);
    if (hit) return <button key={id} type="button" className="ctx far" title={hit.text} onClick={() => { openWhy(`set:${set.ts}`, choice); setSource(id); }}><i>{hit.kind === "summary_written" ? "≋" : "✔"}</i><span>Earlier<small>{new Date(hit.ts).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</small></span></button>;
    if (titles[id]) return <span key={id} className="ctx task" title={titles[id]}><i>▤</i><span>{titles[id]}<small>task</small></span></span>;
    return null;
  };
  const card = (item: Front, inFront: boolean) => {
    if (!inFront) return <button type="button" className={`rear ${item.kind === "suggest" ? "" : "permission"}`} onClick={() => setFrontId(item.id)}><span className="rmeta">{item.kind === "suggest" ? "◆ Suggestion" : "▣ Permission"} <time>{clockOf(item.ts).slice(0, 5)}</time></span><span className="rtitle">{item.title}</span>{item.answer ? <span className="rwhy">Answered</span> : null}</button>;
    if (item.kind === "approval") return running && approval ? (
      <section className="card permission">
        <div className="meta"><span className="kind"><i className="diamond" />Permission</span><time>{clockOf(item.ts)}</time></div>
        <h1>{item.title}</h1>
        {approval.argument ? <p className="description"><code>{approval.argument}</code></p> : null}{approval.description ? <p className="description">{approval.description}</p> : null}
        <div className="acts"><button type="button" className="b blue" onClick={() => void s.answerApproval(running.run.id, "once")}>Allow once</button><button type="button" className="b" onClick={() => void s.answerApproval(running.run.id, "deny")}>Deny</button></div>
      </section>
    ) : null;
    if (item.kind === "ask") {
      const ask = asks.find((entry) => `ask:${entry.app}|${entry.source ?? ""}` === item.id);
      return (
        <section className="card permission">
          <div className="meta"><span className="kind"><i className="diamond" />{item.tag === "for the task" ? "For the task" : "Permission"}</span><time>{clockOf(item.ts)}</time></div>
          <h1>{item.title}</h1>{ask?.why ? <p className="description">{ask.why}</p> : null}
          {ask ? <div className="acts">{answers(ask)}</div> : <div className="receipt"><span className="check">✓</span><span><strong>{item.answer}</strong><small>Answered {clockOf(item.ts).slice(0, 5)}</small></span></div>}
        </section>
      );
    }
    const set = setOf(item.id);
    if (!set) return null;
    const proposals = proposalsOf(set), first = proposals[0];
    const expanded = expandedCards.has(item.id), detailsId = `proposal-details-${encodeURIComponent(item.id)}`;
    return (
      <section className={`card suggestion-card${expanded ? " expanded" : " compact"}`}>
        <div className="meta"><span className="kind"><i className="diamond" />Suggestion</span>{expanded ? <time>{clockOf(set.ts)}</time> : null}{expanded && set.trigger === "idle" ? <span className="tag">on a pause</span> : null}
          <span className="agentMenu">{sendTo()}</span>{/* where it goes, at the row's end: a conversation of the session, or a new one with an agent and a model */}
        </div>
        <h1>{first?.task}{expanded && first ? <ByHand proposal={first} /> : null}</h1>
        {expanded ? <div className="proposal-details" id={detailsId}>
          {first?.reason ? <p className="description">{first.reason}</p> : null}
          {first?.evidence?.length ? <><div className="srclabel">Rests on</div><div className="evidence-chips">{first.evidence.map((id) => chip(id, set, 0))}</div></> : null}
          {proposals.length > 1 && !set.feedback ? <button type="button" className="more" onClick={() => setAlternatives((value) => !value)}>{alternatives ? "hide" : `${proposals.length - 1} more idea${proposals.length === 2 ? "" : "s"}`}</button> : null}
          {alternatives && !set.feedback ? <ol className="alts" start={2}>{proposals.slice(1).map((proposal, index) => <li key={index}><b>{proposal.task}</b><ByHand proposal={proposal} /><button type="button" className="why" onClick={() => openWhy(item.id, index + 1)}>Why? ›</button>{actions(set, index + 1, true)}</li>)}</ol> : null}
        </div> : null}
        <div className="card-footer">
          {set.feedback ? <div className="receipt"><span className="check">✓</span><span><strong>{decisionLabel(set.feedback)}</strong><small>Answered {clockOf(set.feedback.ts).slice(0, 5)}{outcomeOf(set, s.runs)}</small></span></div> : actions(set, 0, true)}
          <div className="card-secondary-actions">
          <button type="button" className="card-expand" aria-expanded={expanded} aria-controls={detailsId} onClick={() => setExpandedCards((current) => { const next = new Set(current); if (next.has(item.id)) next.delete(item.id); else next.add(item.id); return next; })}>{expanded ? "− Less" : "+ Details"}</button>
            <button type="button" className="why" onClick={() => openWhy(item.id, 0)}>Why? ›</button>
          </div>
        </div>
      </section>
    );
  };

  // The chat's log: the conversation's messages and the session's suggestions in one order of time. A suggestion
  // answered from the chat is the message it became, captioned; another answered one is a receipt line; the unanswered
  // ones of a stretch between messages fold to one line. Only the stretch after the last message holds an open card,
  // the newest: once you send or accept something, what came before it folds.
  const became = useMemo(() => new Map(Object.values(s.runs).filter((detail) => detail.run.proposal).map((detail) => [detail.run.id, detail.run.proposal!.proposalsTs])), [s.runs]); // run → the suggestion it carries out
  const messages = s.chat?.messages ?? [];
  const carried = new Set(messages.map((message) => became.get(message.runId)));
  const entries: ({ ts: string; message: LiveChatMessage } | { ts: string; set: LiveProposalSet })[] = [
    ...messages.map((message) => ({ ts: message.ts, message })),
    ...(s.browsing ? [] : sets.filter((set) => proposalsOf(set).length > 0 && !carried.has(set.ts)).map((set) => ({ ts: set.ts, set }))),
  ].sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
  const toggle = (key: string) => setOpened((current) => { const next = new Set(current); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  const hide = (key: string, group: LiveProposalSet[]) => setOpened((current) => { const next = new Set(current); next.delete(key); for (const set of group) next.delete(`set:${set.ts}`); return next; }); // back to one line, cards opened from it included
  const suggestion = (set: LiveProposalSet, row: boolean) => { // answered: a receipt; waiting: a card, or (row) one line that opens into it
    const proposals = proposalsOf(set), first = proposals[0], task = first?.task ?? "";
    if (set.feedback) return <div key={set.ts} className="rc" id={`sg-${set.ts}`}><i>◆</i><time>{clockOf(set.ts).slice(0, 5)}</time><b title={task}>{task}</b><span className={`st${set.feedback.decision === "accept" ? " ok" : ""}`}>{set.feedback.decision === "accept" ? "✓ " : "✘ "}{decisionLabel(set.feedback)}</span></div>;
    if (row) return <button key={set.ts} type="button" className="rc row" id={`sg-${set.ts}`} onClick={() => toggle(`set:${set.ts}`)}><i>◆</i><time>{clockOf(set.ts).slice(0, 5)}</time><b title={task}>{task}</b></button>;
    const key = `set:${set.ts}`, expanded = expandedCards.has(key), detailsId = `chat-details-${encodeURIComponent(set.ts)}`;
    return (
      <section key={set.ts} className="sg" id={`sg-${set.ts}`}>
        <div className="meta"><span className="k">Suggestion</span><time>{clockOf(set.ts)}</time>{set.trigger === "idle" ? <span className="tag">on a pause</span> : null}
          <button type="button" className="card-expand" aria-expanded={expanded} aria-controls={detailsId} onClick={() => setExpandedCards((current) => { const next = new Set(current); if (next.has(key)) next.delete(key); else next.add(key); return next; })}>{expanded ? "− Less" : "+ Details"}</button>
        </div>
        <h4>{task}</h4>
        {expanded ? <div className="proposal-details" id={detailsId}>{/* the reason, what it rests on, the other ideas with their own buttons: as the Home card's details */}
          {first?.reason ? <p className="description">{first.reason}</p> : null}
          {first?.evidence?.length ? <><div className="srclabel">Rests on</div><div className="evidence-chips">{first.evidence.map((id) => chip(id, set, 0))}</div></> : null}
          {proposals.length > 1 ? <ol className="alts" start={2}>{proposals.slice(1).map((proposal, index) => <li key={index}><b>{proposal.task}</b>{actions(set, index + 1)}</li>)}</ol> : null}
        </div> : null}
        {actions(set, 0)}
      </section>
    );
  };
  const stretch = (group: LiveProposalSet[], last: boolean) => { // suggestions between two messages, in order; the folded ones are one line where the first of them was
    const folded = group.filter((set) => !set.feedback && !(last && set.ts === open[0]?.ts) && !opened.has(`set:${set.ts}`));
    const key = `fold:${group[0]!.ts}`, shown = opened.has(key);
    const card = last ? group.find((set) => set.ts === open[0]?.ts) : undefined; // the open card closes its stretch: what is answered or folded reads as the past, above it
    return [...group.filter((set) => set !== card), ...(card ? [card] : [])].flatMap((set) => {
      if (!folded.includes(set)) return [suggestion(set, false)];
      const line = set === folded[0] ? [<button key={key} type="button" className="fold" onClick={() => (shown ? hide(key, group) : toggle(key))}><i>◆</i>{folded.length} earlier suggestion{folded.length === 1 ? "" : "s"}<span className="n">{shown ? "hide" : "show"}</span></button>] : [];
      return shown ? [...line, suggestion(set, true)] : line;
    });
  };
  const said = (text: string) => { // the tray's lines, quoted at the top of a message, drawn as a quotation
    const lines = text.split("\n");
    let n = 0;
    while (n < lines.length && lines[n]!.startsWith("> ")) n++;
    return n ? <><blockquote>{lines.slice(0, n).map((line) => line.slice(2)).join("\n")}</blockquote>{lines.slice(n).join("\n").trimStart()}</> : text;
  };
  const bubble = (message: LiveChatMessage) => {
    const detail = s.runs[message.runId];
                      return message.role === "user" ? (
                        <div key={`${message.runId}-u`} className="me">{said(message.text)}<small>{became.has(message.runId) ? "◆ suggestion · " : ""}{clockOf(message.ts)}{message.attached ? ` · ${message.attached} note${message.attached === 1 ? "" : "s"} attached` : ""}</small></div>
                      ) : (
                        <div key={`${message.runId}-a`} className="ai">
                          {message.text ? <div className="text"><Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_LINKS}>{message.text}</Markdown></div>
                            : message.status === "running" || (detail && message.status === "failed") ? null /* the turn bar below says it is working, or why it failed */
                              : <div className="text"><span className="muted">(no reply: the turn {message.status === "cancelled" ? "was stopped" : "failed"})</span></div>}
                          {detail ? <Turn detail={detail} events={mergedEvents(detail, s.streamed[message.runId])} runnerBaseUrl={runnerBaseUrl} finalText={message.text} /> : null}
                        </div>
                      );
  };
  const logItems = () => {
    const out: ReactNode[] = [];
    let group: LiveProposalSet[] = [];
    const flush = (last: boolean) => { if (group.length) { out.push(...stretch(group, last)); group = []; } };
    for (const entry of entries) {
      if ("set" in entry) group.push(entry.set);
      else { flush(false); out.push(bubble(entry.message)); }
    }
    flush(true);
    return out;
  };

  // A record, for the detail panel and the Why page's side: what it is, when, and what it was made from.
  const notesIn = (from: string | null, to: string) => (from ? marks.filter((item): item is Mark & { kind: "note" } => item.kind === "note" && Date.parse(item.ts) > Date.parse(from) && Date.parse(item.ts) <= Date.parse(to)) : []);
  const nearby = (from: string | null, to: string) => from && notesIn(from, to).length ? (
    <><h3>Nearby records</h3>{notesIn(from, to).map((item) => <button key={item.id} type="button" className="source" onClick={() => pick(item)}><span className="sym">≋</span><span><time>{clockOf(item.ts)} · Note</time><p>{item.note.summary}</p></span></button>)}</>
  ) : null;
  const record = (mark: Mark) => {
    switch (mark.kind) {
      case "look": return <><time>{clockOf(mark.ts)}</time>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img className="big" src={screenshotUrl(mark.shot.name)} alt={describe(mark)} /><h4>{describe(mark)}</h4></>;
      case "note": {
        const looks = marks.filter((item): item is Mark & { kind: "look" } => item.kind === "look" && item.ts >= mark.from && item.ts <= mark.ts);
        return <><time>{clockOf(mark.from)} – {clockOf(mark.ts)}{mark.note.model ? ` · ${mark.note.model}` : ""}</time><p>{mark.note.summary}</p>
          {looks.length ? <div className="shots">{looks.map((look) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img key={look.id} src={screenshotUrl(look.shot.name)} alt={look.shot.app} title={`${clockOf(look.ts)} · ${look.shot.app}`} onClick={() => pick(look)} />
          ))}</div> : null}</>;
      }
      case "change": return <><time>{clockOf(mark.ts)}{(mark.change.rounds ?? 1) > 1 ? ` · ${mark.change.rounds} rounds` : ""}</time>
        <ChangeBody change={mark.change} titles={titles} />{nearby(mark.from, mark.ts)}</>;
      case "suggest": case "answer": {
        const set = mark.set, proposals = proposalsOf(set);
        return <><time>{clockOf(set.ts)}{set.trigger === "idle" ? " · on a pause" : ""}</time>
          <ol className="ideas">{proposals.map((proposal, index) => <li key={index}><b>{proposal.task}</b><ByHand proposal={proposal} />{proposal.reason ? <p>{proposal.reason}</p> : null}<button type="button" className="link" onClick={() => { setDetail(null); openWhy(`set:${set.ts}`, index); }}>Why? ›</button></li>)}</ol>
          {set.feedback ? <p className="ans">{decisionLabel(set.feedback)} · {clockOf(set.feedback.ts)}{outcomeOf(set, s.runs)}</p> : <button type="button" className="b" onClick={() => { bring(`set:${set.ts}`); setDetail(null); setChatOpen(false); }}>Answer it</button>}</>;
      }
      case "check": return <><time>{clockOf(mark.ts)}</time><p>{mark.set.summary || "looked, nothing worth suggesting"}</p></>;
      case "say": return <><time>{clockOf(mark.ts)}</time><p>{mark.message.text}</p></>;
      case "run": return <RunCard detail={mark.detail} events={mergedEvents(mark.detail, s.streamed[mark.detail.run.id])} runnerBaseUrl={runnerBaseUrl} open turn />;
      case "ask": return <><time>{clockOf(mark.ts)}</time><p className="muted">{mark.pending ? "waiting for your answer" : mark.answer ?? "answered"}</p></>;
    }
  };

  // The Why drawer keeps the workspace visible alongside the reason and cited records.
  const whyPanel = () => {
    if (!why) return null;
    const set = whySet, ask = asks.find((entry) => `ask:${entry.app}|${entry.source ?? ""}` === why.id);
    const proposal = whyProposal;
    const sourceMark = source ? byRecord.get(source) : undefined;
    const sourceHit = source && set ? set.cited?.find((entry) => entry.id === source) : undefined;
    const sourceOf = (id: string): ReactNode => {
      const mark = byRecord.get(id);
      if (mark) return <button key={id} type="button" className={`source${source === id ? " sel" : ""}`} onClick={() => setSource(id)}><span className="sym">{GLYPH[mark.kind]}</span><span><time>{clockOf(mark.ts)} · {KIND_LABEL[mark.kind]}</time><h3>{describe(mark)}</h3></span></button>;
      const hit = set?.cited?.find((entry) => entry.id === id);
      if (hit) return <button key={id} type="button" className={`source far${source === id ? " sel" : ""}`} onClick={() => setSource(id)}><span className="sym">{hit.kind === "summary_written" ? "≋" : "✔"}</span><span><time>{hit.ts.slice(0, 10)} {clockOf(hit.ts).slice(0, 5)} · earlier session</time><h3>{hit.text}</h3></span></button>;
      if (titles[id]) return <div key={id} className="source"><span className="sym">▤</span><span><time>Task</time><h3>{titles[id]}</h3><p>{(s.status?.tasks ?? []).find((task) => task.id === id)?.steps.filter((step) => !step.doneAt).map((step) => step.text).join(" · ")}</p></span></div>;
      return <div key={id} className="source muted"><span className="sym">?</span><span><h3>{id}</h3></span></div>;
    };
    return (
      <aside className="detail why-panel" aria-label={set ? "Why this suggestion" : "Why this permission request"}>
        <header><h3>{set ? "Why this suggestion?" : "Why this permission request?"}</h3><button type="button" className="icon" aria-label="Close Why panel" onClick={() => setWhy(null)}>×</button></header>
        <div className="body">
        <div className="whymain">
          {proposal ? <p className="small">{proposal.task}</p> : null}
          <div className="reasonbox"><div className="label">{set ? "Stated reason" : "What asked"}</div><p>{proposal?.reason || ask?.why || (set ? "No reason given." : "An app came into view that your rules ask about.")}</p></div>
          {proposal?.evidence?.length ? <><div className="label">Rests on</div>{proposal.evidence.map(sourceOf)}</> : set ? <p className="small muted">No records cited: it rests on the screen alone.</p> : null}
          {set?.searches?.length ? <><div className="label">Searched</div><ul className="searched">{set.searches.map((search, index) => <li key={index}><b>{searchLabel(search)}</b> · {search.hits} hit{search.hits === 1 ? "" : "s"}{search.error ? <> · {search.error}</> : null}</li>)}</ul></> : null}
        </div>
        <section className="whyside" ref={whySource}>
          {sourceMark ? <><h3>{KIND_LABEL[sourceMark.kind]}</h3>{record(sourceMark)}</>
            : sourceHit ? <><h3>{sourceHit.kind === "summary_written" ? "Note" : "Decision"} · earlier session</h3><time>{sourceHit.ts.slice(0, 10)} {clockOf(sourceHit.ts)}</time><p>{sourceHit.text}</p>
              <Link className="b" href={sourceHit.kind === "summary_written" ? `/v1/notes?session=${encodeURIComponent(sourceHit.sessionId)}&id=${encodeURIComponent(sourceHit.id)}` : `/v1/history?session=${encodeURIComponent(sourceHit.sessionId)}`}>Open that session ↗</Link></>
            : <p className="small muted">{proposal?.evidence?.length ? "Pick a record to see it here." : ""}</p>}
        </section>
        </div>
      </aside>
    );
  };

  return (
    <div className="v1 timelinev2" data-state={state}>
      <TopBar s={s} needs={Boolean(asking)}>
        {session ? <button type="button" className="b" aria-expanded={chatOpen} aria-controls="timelinev2-conversation" onClick={() => { setChatOpen((value) => !value); setWhy(null); setDetail(null); }}>{chatOpen ? "Home" : "Chat"}</button> : null}
        {reading ? <button type="button" className="b" onClick={() => void s.browseChat(null)}>Home</button> : null}
        {session ? <i className="vsep" /> : null}
      </TopBar>
      {s.waiting ? <div className="wait" role="status"><span className="dot" />{s.waiting}</div> : null}
      {s.issue ?? session?.lastError ? <div className="issue">{s.issue ?? `Last error: ${session?.lastError}`}</div> : null}

      <div className="main">
        <div className="side" style={{ width: sideWidth }}>
          <nav className="side-tabs" aria-label="Sidebar">
            {(Object.keys(PANELS) as Panel[]).map((name) => <button key={name} type="button" className={panel === name ? "on" : ""} aria-pressed={panel === name} aria-label={PANELS[name].label} title={PANELS[name].label} onClick={() => showPanel(name)}>{PANELS[name].icon}</button>)}
          </nav>
          {panel === "timeline" ? <Rail now={now} width={sideWidth} messages={session ? s.chat?.messages ?? [] : []} marks={marks} selected={selected} cited={cited} screenshotUrl={screenshotUrl} onPick={pick} />
            : <Chats list={s.chats} current={current} shown={s.browsing ? s.chat?.id ?? null : null} session={session?.id ?? null} working={running?.chat ? current : null} busy={s.busy} onOpen={openConversation} onNew={session ? () => { void s.resetChat(); setChatOpen(true); setWhy(null); setDetail(null); } : undefined} onMark={(id, mark) => void s.markChat(id, mark)} onRenameSession={(id, name) => void s.renameSession(id, name)} />}
          <div className="side-grip" role="separator" aria-orientation="vertical" aria-label="Resize the sidebar" onPointerDown={resizeSide} />
        </div>
        <div className="stage">
          <div className={`stage-content${why ? " with-why" : ""}`}>
          {!session && !reading ? (
            <div className="curtain"><div className="pausecard"><h2>Cue is asleep</h2><p>Start a session to watch your screen.</p>
              <button type="button" className="b primary" disabled={s.busy || !s.status} onClick={() => void s.startSession({ ...readOverrides() })}>▶ Start session</button></div></div>
          ) : null}
          {session && !chatOpen ? (
            <div className={`deckwrap${front ? "" : " quiet"}`}>
              <nav className="decknav">
                <div className="filters">{(["all", "ask", "suggest", "done"] as const).map((kind) => <button key={kind} type="button" className={mode === kind ? "sel" : ""} disabled={kind !== "all" && !has(kind)} onClick={() => { setFilter(kind); setFrontId(null); }}>{{ all: "all", ask: "permissions", suggest: "suggestions", done: "answered" }[kind]}</button>)}</div>
                <div className="paging">
                  <button type="button" aria-label="Older" disabled={at >= shown.length - 1} onClick={() => setFrontId(shown[at + 1]!.id)}>‹</button>
                  <span>{shown.length ? `${shown.length - at}/${shown.length}` : "0"}</span>
                  <button type="button" aria-label="Newer" disabled={at === 0} onClick={() => setFrontId(shown[at - 1]!.id)}>›</button>
                  <button type="button" className="latest" disabled={at === 0} onClick={() => setFrontId(null)}>latest</button>
                </div>
              </nav>
              {front ? (
                <div className="deck">
                  {rear.slice().reverse().map((item, index) => <div key={item.id} className={`rearwrap level${rear.length - index}`}>{card(item, false)}</div>)}
                  <div key={front.id} className="frontwrap">{card(front, true)}</div>
                </div>
              ) : (
                <div className="quietbox"><div className="orb">⌁</div><h2>Nothing needs you right now.</h2><p>Cue is following your activity.</p><button type="button" className="b" onClick={() => setChatOpen(true)}>Ask Cue</button></div>
              )}
            </div>
          ) : null}
          {(session && chatOpen) || reading ? (
            <section id="timelinev2-conversation" className="execution" style={{ "--share": `${share}%` } as React.CSSProperties}>
              <div className="convnav">
                <button type="button" className="monitor-toggle" aria-pressed={paneOn} aria-label="Monitor" title={paneOn ? "Hide the monitor" : "Show the monitor"} onClick={() => setPaneOn((on) => !on)}>
                  {MONITOR}
                </button>
              </div>
              <div className={`workspace${paneOn ? "" : " alone"}`}>
                {paneOn ? (
                  <>
                    <div className="screenpane">
                      <Monitor session={session} running={running} followed={followed} events={followedEvents} runnerBaseUrl={runnerBaseUrl} backend={expectedBackend} width={900} />
                    </div>
                    <div className="divider" role="separator" aria-orientation="vertical" aria-valuenow={share} onPointerDown={(event) => {
                      const pane = event.currentTarget.parentElement!.getBoundingClientRect(); event.preventDefault();
                      const move = (moved: PointerEvent) => setShare(Math.max(35, Math.min(75, Math.round((moved.clientX - pane.left) / pane.width * 100))));
                      const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
                      window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
                    }} />
                  </>
                ) : null}
                <div className="conversation">
                  <div className="hd">
                    {s.chat?.agent || agents.length < 2 ? <span className="muted">{AGENT_NAMES[agent] ?? agent}</span>
                      : <select className="agentPick" value={agent} onChange={(event) => setAgent(event.target.value)} aria-label="Agent">{agents.map((item) => <option key={item} value={item}>{AGENT_NAMES[item] ?? item}</option>)}</select>}
                    {models.length ? (
                      <select className="agentPick" value={model} onChange={(event) => setModel(event.target.value)} aria-label="Model" title={models.find((item) => item.id === model)?.detail}>
                        {sessionModel && !models.some((item) => item.id === sessionModel) ? <option value={sessionModel}>{sessionModel}</option> : null}
                        {models.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                      </select>
                    ) : null}
                    <span className="spacer" />
                    {s.chat?.messages.length && !s.runningId && !s.browsing ? <button type="button" className="link" onClick={() => void s.resetChat()}>New conversation</button> : null}
                  </div>
                  {s.browsing ? <div className="viewonly"><span>{session ? `From the session of ${(s.chat?.messages[0]?.ts ?? "").slice(0, 10)}` : "No session"} · view only</span><button type="button" className="link" onClick={() => void s.browseChat(null)}>Back</button></div> : null}
                  <div className="chat-scroll-area scroll-hints" data-before={chatEdges.before} data-after={chatEdges.after}><div className="log" ref={msgs} tabIndex={0} aria-label="Conversation messages" onScroll={(event) => { const el = event.currentTarget; stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 8; }}>
                    {logItems()}
                    {s.sending && !s.browsing ? <><div className="me">{s.sending}</div><div className="ai"><Starting /></div></> : null}
                    {!s.browsing && s.chat?.runningRunId && s.runs[s.chat.runningRunId] && !s.chat.messages.some((message) => message.runId === s.chat?.runningRunId && message.role === "agent") ? (
                      <div className="ai">
                        <Turn detail={s.runs[s.chat.runningRunId]!} events={mergedEvents(s.runs[s.chat.runningRunId]!, s.streamed[s.chat.runningRunId])} runnerBaseUrl={runnerBaseUrl} /></div>
                    ) : null}
                    {s.browsing ? null : s.queued.map((item, index) => <div key={`queued-${index}`} className="me queued">{item.text}<small>queued · <button type="button" className="link" onClick={() => s.unqueue(index)}>remove</button></small></div>)}
                    {(s.chat?.messages ?? []).length === 0 && !s.sending ? <div className="draftnote">Send a message to begin, or accept a suggestion.</div> : null}
                  </div>
                  {chatEdges.after ? <button type="button" className="scroll-latest" aria-label="Jump to latest messages" onClick={() => { stick.current = true; msgs.current?.scrollTo({ top: msgs.current.scrollHeight }); }}>↓ Latest</button> : null}
                  </div>
                  {/* What waits on you sits by the box, in view however long the log: Claude's permission request, the agent's ask to look at an app. */}
                  {!s.browsing && running && approval ? <div className="askturn"><h3>Cue wants to use {approval.name}</h3>{approval.argument ? <p><code>{approval.argument}</code></p> : null}{approval.description ? <p>{approval.description}</p> : null}
                    <div className="acts"><button type="button" className="b blue" onClick={() => void s.answerApproval(running.run.id, "once")}>Allow once</button><button type="button" className="b" onClick={() => void s.answerApproval(running.run.id, "deny")}>Deny</button></div></div> : null}
                  {!s.browsing && running ? asks.filter((item) => item.source === "agent").map((item) => <div key={item.app} className="askturn"><h3>{wants(item)}</h3>{item.why ? <p>{item.why}</p> : null}<div className="acts">{answers(item)}</div></div>) : null}
                  <Composer initial={draft.current} seed={s.chatSeed} picks={picks} onDrop={(index) => setPicks((current) => current.filter((_, at) => at !== index))} disabled={paused || s.browsing} placeholder={s.browsing ? "Earlier conversation — read only" : paused ? "Session paused. Resume it to message Cue." : "Message Cue…"}
                    canSend={!paused && !s.browsing} onChange={(text) => { draft.current = text; }} onSend={async (text, sent) => { const ok = await s.sendChat(text, agent, model, undefined, sent); if (ok) setPicks([]); return ok; }}
                    onStop={!s.browsing && s.runningId && running?.chat ? () => void s.stopRun(running.run.id) : undefined} />
                </div>
              </div>
            </section>
          ) : null}
          {session && !chatOpen && (running || unread) ? (
            <button type="button" className="agentbar" onClick={() => setChatOpen(true)}>
              {running ? <><span className="agentbar-label"><span className="dot" />{AGENT_LABELS[running.actorAgent ?? ""] ?? "Agent"} ·</span><b>{running.chat ? s.chat?.messages.find((message) => message.runId === running.run.id && message.role === "user")?.text ?? `turn ${running.chat.turn}` : running.run.prompt}</b></>
                : <><span className="agentbar-label">✔ {AGENT_LABELS[unread!.actorAgent ?? ""] ?? "Agent"} ·</span><b>{s.chat?.messages.find((message) => message.runId === unread!.run.id && message.role === "agent")?.text.split("\n")[0] ?? "done"}</b></>}<span className="agentbar-chevron" aria-hidden="true">›</span>
            </button>
          ) : null}
          {session && paused && !chatOpen ? <div className="curtain"><div className="pausecard"><h2>Session paused</h2><p>Screen, notes, suggestions and the agent are held.</p><button type="button" className="b primary" disabled={s.busy} onClick={() => void s.pauseSession(true)}>Resume session</button></div></div> : null}
          </div>
          {session && why ? whyPanel() : null}
          {detail ? (
            <aside className="detail">
              <header><h3>{KIND_LABEL[detail.kind]}</h3><button type="button" className="icon" aria-label="Close detail" onClick={() => setDetail(null)}>×</button></header>
              <div className="body">{record(detail)}{detail.kind === "note" ? nearby(detail.from, detail.ts) : detail.kind === "suggest" || detail.kind === "answer" ? nearby(detail.set.windowStart ?? null, detail.set.ts) : null}</div>
            </aside>
          ) : null}
        </div>
      </div>
      <TaskList tasks={s.status?.tasks ?? []} changes={changes} flags={s.status?.taskFlags ?? []} onFlag={{ flag: s.flagTask, reason: s.flagReason, unflag: s.unflag, busy: s.busy }} busy={s.busy} now={now} onEdit={s.editTasks} sendTo={sendTo()} onAttach={session ? attach : undefined} onPick={(change: LiveTaskChange) => { const mark = marks.find((item) => item.id === `change:${change.ts}`); if (mark) pick(mark); }} />
    </div>
  );
}
