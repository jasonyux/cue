"use client";

import { agentModelsSchema, type AgentModel, type LiveProposalSet, type LiveSessionStatus, type LiveTaskChange } from "@cue/contracts";
import Link from "next/link";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { AGENT_LABELS, ByHand, ChangeBody, MARKDOWN_LINKS, RunCard, callParts, callRunner, clockOf, decisionLabel, mergedEvents, pendingApproval, proposalsOf, searchLabel, toolRunning, turnsOf } from "../live/shared";
import { AgentScreen } from "./AgentScreen";
import { BrowserMonitor, BrowserSheet } from "./AgentBrowser";
import { Brand } from "./Brand";
import { outcomeOf } from "./Noticed";
import { readOverrides } from "./overrides";
import { useRecords } from "./records";
import { RemoteMonitor, RemoteSheet } from "./RemoteDesktop";
import { Spine, type Mark } from "./Spine";
import { TaskCards } from "./TaskCards";
import { useSession } from "./useSession";

type Props = { expectedBackend: "python"; runnerBaseUrl: string; switcher?: ReactNode };
type Ask = NonNullable<NonNullable<LiveSessionStatus["session"]>["permissionAsks"]>[number];
type Front = { id: string; ts: string; kind: "approval" | "ask" | "suggest"; title: string; tag?: string; answer?: string }; // one thing that waits on you, or did

const AGENT_NAMES: Record<string, string> = { claude: "Claude Code", codex: "Codex", cmux: "cmux + Claude Code", desktop: "Desktop (GUI)" };

function timeLeft(expiresAt: string | null | undefined, now: number): string {
  if (!expiresAt) return "no time limit";
  const minutes = Math.max(0, Math.round((Date.parse(expiresAt) - now) / 60_000));
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} m left` : `${minutes} m left`;
}
// The stored records and the polled ones, the polled winning: the spine reads both, the records every half minute.
function merge<T extends { ts: string }>(stored: T[], fresh: T[]): T[] {
  const map = new Map(stored.map((item) => [item.ts, item]));
  for (const item of fresh) map.set(item.ts, item);
  return [...map.values()];
}
const ago = (ts: string, now: number) => { const minutes = Math.max(0, Math.round((now - Date.parse(ts)) / 60_000)); return minutes < 1 ? "just now" : minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.round(minutes / 60)}h ago` : `${Math.round(minutes / 1440)}d ago`; };
const wants = (item: Ask) => `Cue wants to ${item.source === "agent" && item.level === "use" ? "use" : "look at"} ${item.app}`;
// The Timeline theme of the landing page: the session's stream on the left, every record a mark, and a right side that
// is empty until something needs you. A suggestion or a permission question lands in the middle, the newest first,
// ‹ › through the others; an accept opens the chat, with the agent's work beside it while it runs; the tasks are cards in a tray
// along the bottom. A mark on the spine opens its record and what it was made from.
export function Timeline({ expectedBackend, runnerBaseUrl, switcher }: Props) {
  const s = useSession(runnerBaseUrl, expectedBackend);
  const session = s.session;
  const { records, screenshotUrl } = useRecords(runnerBaseUrl, expectedBackend, session ? null : undefined);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 5_000); return () => clearInterval(timer); }, []);
  const [selected, setSelected] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<Mark | null>(null);
  const [choice, setChoice] = useState(0); // which of a suggestion's proposals the drawer shows: the one whose "why?" was clicked, or the one taken
  const [frontId, setFrontId] = useState<string | null>(null); // the one the person brought to the front
  const [filter, setFilter] = useState<"all" | "ask" | "suggest" | "done">("all"); // only the permission questions, only the suggestions, the answered ones
  const [replied, setReplied] = useState<Front[]>([]); // the permission questions answered on this page: nothing records them once answered
  const [chatOpen, setChatOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const [seen, setSeen] = useState<string[]>([]); // finished runs the person has opened the chat on
  const [declining, setDeclining] = useState<string | null>(null); // "ts:choice" whose Not now reasons show
  const [alternatives, setAlternatives] = useState(false);
  const [view, setView] = useState<"browser" | "remote" | null>(null); // the work pane picked by hand; null follows the agent
  const [browserOpen, setBrowserOpen] = useState(false);
  const [remoteOpen, setRemoteOpen] = useState(false);

  const running = s.runningId ? s.runs[s.runningId] ?? null : null;
  const turns = running ? turnsOf(mergedEvents(running, s.streamed[running.run.id])) : [];
  const asking = running ? pendingApproval(turns) : null;
  const approval = asking ? callParts(asking.detail) : null;
  const asks = session?.permissionAsks ?? [];
  const sets = useMemo(() => merge(records?.proposalSets ?? [], s.proposalSets).filter((set) => !set.error), [records, s.proposalSets]);
  const changes = useMemo(() => merge(records?.changes ?? [], s.changes), [records, s.changes]);
  const open = sets.filter((set) => proposalsOf(set).length > 0 && !set.feedback).sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));
  const state = !session ? "off" : session.paused ? "paused" : asking || asks.length ? "needs" : running ? "working" : open.length ? "noticed" : "watching";
  const titles = Object.fromEntries((s.status?.tasks ?? []).map((task) => [task.id, task.title]));

  // Every record of the session as a mark: the stored screenshots (the looks that kept one), the notes, the task
  // changes, the suggestions with their answers, the pauses that found nothing, your messages, the runs, the questions.
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
      events.forEach((event, index) => { // the agent's own permission requests, from the run's events
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
  const chosen = marks.find((mark) => mark.id === selected);
  const cited = new Set((chosen?.kind === "suggest" || chosen?.kind === "answer" ? proposalsOf(chosen.set)[choice]?.evidence ?? [] : []).map((id) => byRecord.get(id)?.id).filter((id): id is string => Boolean(id)));

  // What waits on you, one in front: the agent's approval first, then the permission questions, then the suggestions,
  // newest first; or the one the person picked, until it is answered.
  const queue: Front[] = [
    ...(running && asking && approval ? [{ id: `approval:${running.run.id}`, ts: asking.createdAt, kind: "approval" as const, title: `Cue wants to use ${approval.name}` }] : []),
    ...asks.map((item) => ({ id: `ask:${item.app}|${item.source ?? ""}`, ts: item.ts, kind: "ask" as const, title: wants(item), tag: item.source === "agent" ? "for the task" : "permission" })),
    ...open.map((set) => ({ id: `set:${set.ts}`, ts: set.ts, kind: "suggest" as const, title: proposalsOf(set)[0]?.task ?? "", tag: set.trigger === "idle" ? "on a pause" : "" })),
  ];
  const done: Front[] = [ // what was answered, newest first: the suggestions by their feedback, the agent's approvals by the run's events, the rest from this page
    ...replied,
    ...marks.filter((mark): mark is Mark & { kind: "ask" } => mark.kind === "ask" && !mark.pending).map((mark) => ({ id: mark.id, ts: mark.ts, kind: "ask" as const, title: mark.title, tag: "for the task", answer: mark.answer ?? "answered" })),
    ...sets.filter((set) => set.feedback && proposalsOf(set).length > 0).map((set) => ({ id: `set:${set.ts}`, ts: set.feedback!.ts, kind: "suggest" as const, title: proposalsOf(set)[0]?.task ?? "" })),
  ].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));
  const has = (kind: "ask" | "suggest" | "done") => kind === "done" ? done.length > 0 : queue.some((item) => (item.kind === "suggest") === (kind === "suggest"));
  const mode = filter !== "all" && has(filter) ? filter : "all"; // the filter, while it has something to show
  const shown = mode === "done" ? done : mode === "all" ? queue : queue.filter((item) => (item.kind === "suggest") === (mode === "suggest"));
  const front = shown.find((item) => item.id === frontId) ?? shown[0] ?? null;
  const at = front ? shown.indexOf(front) : 0;
  const bring = (id: string) => { setFilter("all"); setFrontId(id); }; // to the front, whatever the filter

  // The agent browser and the remote desktop, shown beside the chat while the agent's latest step is on them.
  const browser = session?.agentBrowser ?? null;
  const lastTurn = turns[turns.length - 1];
  const lastCall = lastTurn ? [...lastTurn.events].reverse().find((event) => event.type === "function_call_requested") : undefined;
  const agentOnBrowser = Boolean(browser?.tabs.length) && Boolean(lastCall?.detail?.startsWith("mcp__browser__"));
  const showingBrowser = Boolean(browser?.tabs.length) && (view === null ? agentOnBrowser : view === "browser");
  const agentTab = browser?.tabs.find((tab) => tab.agent);
  const remoteAvailable = Boolean((session?.settings ?? s.status?.defaults)?.actorDesktopSource.includes("remote"));
  const agentOnRemote = remoteAvailable && Boolean(lastCall?.detail?.startsWith("mcp__desktop__exec_py") && lastCall.detail.includes('"target": "remote"'));
  const showingRemote = remoteAvailable && (view === null ? agentOnRemote : view === "remote");
  useEffect(() => { setView(null); }, [running?.run.id]); // a new run: back to following the agent
  const takeControl = showingBrowser && !browserOpen ? () => setBrowserOpen(true) : showingRemote && !remoteOpen ? () => setRemoteOpen(true) : null;
  const agentBusy = Boolean(lastTurn && toolRunning(lastTurn));

  // The chat's agent and its model, as the Zen theme offers them.
  const agents = session?.settings.actorAgents ?? [];
  const [chosenAgent, setChosenAgent] = useState<string | null>(null);
  const agent = s.chat?.agent ?? (chosenAgent && agents.includes(chosenAgent as never) ? chosenAgent : agents.includes("claude") ? "claude" : agents.includes("cmux") ? "cmux" : agents[0] ?? "claude");
  const [modelLists, setModelLists] = useState<Record<string, AgentModel[]>>({});
  const [pickedModel, setPickedModel] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!session || (agent !== "claude" && agent !== "codex") || modelLists[agent]) return;
    callRunner(runnerBaseUrl, expectedBackend, `/api/live/agents/${agent}/models`, agentModelsSchema)
      .then((answer) => setModelLists((lists) => ({ ...lists, [agent]: answer.models })))
      .catch(() => setModelLists((lists) => ({ ...lists, [agent]: [] })));
  }, [agent, session, modelLists, runnerBaseUrl, expectedBackend]);
  const models = modelLists[agent] ?? [];
  const sessionModel = (agent === "claude" ? session?.settings.actorClaudeModel : agent === "codex" ? session?.settings.actorCodexModel : null) ?? null;
  const model = pickedModel[agent] ?? sessionModel ?? models.find((item) => item.default)?.id ?? "";

  useEffect(() => { // "Do it" puts the suggestion in the line and opens the chat
    if (!s.chatSeed) return;
    setDraft(s.chatSeed);
    setChatOpen(true);
    input.current?.focus();
  }, [s.chatSeed]);
  const finished = (session?.runs ?? []).filter((id) => s.runs[id] && s.runs[id]!.run.status !== "running");
  useEffect(() => { if (chatOpen) setSeen((current) => (finished.every((id) => current.includes(id)) ? current : finished)); }, [chatOpen, finished]);
  const lastRun = finished.map((id) => s.runs[id]!).filter((detail) => detail.chat).pop() ?? null;
  const unread = lastRun && !seen.includes(lastRun.run.id) ? lastRun : null;
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setBrowserOpen(false); setRemoteOpen(false); setDrawer(null); setDeclining(null); } };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);
  const msgs = useRef<HTMLDivElement>(null); // the chat sticks to its bottom as it grows, unless the person scrolled up
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

  const pick = (mark: Mark) => { // a mark on the spine: what waits comes to the front, the chat opens for its turns, the rest opens its record
    setSelected(mark.id);
    if (mark.kind === "suggest" || mark.kind === "answer") setChoice(mark.set.feedback?.choice ?? 0); // the proposal taken, else the first
    if ((mark.kind === "suggest" || mark.kind === "ask") && mark.pending) { bring(mark.id); setDrawer(null); return; }
    if (mark.kind === "say" || mark.kind === "run") { setChatOpen(true); return; }
    setDrawer(mark);
  };
  const send = async () => {
    const text = draft.trim();
    if (!text || !session || session.paused || s.runningId) return;
    setDraft("");
    if (!(await s.sendChat(text, agent, models.length ? model : undefined))) setDraft(text);
  };
  const notNow = async (item: LiveProposalSet, reason: string, typed: boolean) => { await s.decide(item, "reject", 0); await s.giveReason(item, reason, typed); };
  const answer = (item: Ask, label: string, level: "block" | "look" | "use" | "deny", scope?: "session" | "once" | "task") => {
    setReplied((list) => [{ id: `replied:${Date.now()}`, ts: new Date().toISOString(), kind: "ask", title: wants(item), tag: item.source === "agent" ? "for the task" : "permission", answer: label }, ...list]);
    void s.answerPermission(item.app, level, scope);
  };
  const answers = (item: Ask) => item.source === "agent" ? <>
    <button type="button" className="btn primary sm" title="For one step; nothing saved" onClick={() => answer(item, "Allow once", item.level ?? "use", "once")}>Allow once</button>
    <button type="button" className="btn sm" title="For the rest of this task; nothing saved" onClick={() => answer(item, "For this task", item.level ?? "use", "task")}>For this task</button>
    <button type="button" className="btn sm" title="A use rule in Settings" onClick={() => answer(item, "Always", item.level ?? "use")}>Always</button>
    <button type="button" className="btn sm ghost" title="For the rest of this task; nothing saved" onClick={() => answer(item, "Deny", "deny")}>Deny</button>
  </> : <>
    <button type="button" className="btn primary sm" onClick={() => answer(item, "Look", "look")}>Look</button>
    <button type="button" className="btn sm" onClick={() => answer(item, "Use", "use")}>Use</button>
    <button type="button" className="btn sm" title="Look until this session ends; nothing saved" onClick={() => answer(item, "This session", "look", "session")}>This session</button>
    <button type="button" className="btn sm ghost" onClick={() => answer(item, "Block", "block")}>Block</button>
  </>;
  const paused = Boolean(session?.paused);
  const actions = (item: LiveProposalSet, choice: number) => {
    const key = `${item.ts}:${choice}`;
    return (
      <div className="acts">
        <button type="button" className="btn primary sm" disabled={s.busy || paused} onClick={() => void s.acceptToChat(item, choice)}>Do it</button>
        <span className="notNow">
          <button type="button" className="btn sm ghost" disabled={s.busy || paused} onClick={() => setDeclining(declining === key ? null : key)}>Not now {declining === key ? "▴" : "▾"}</button>
          {declining === key ? (
            <span className="whyNot" role="menu">
              {(s.status?.rejectReasons ?? []).map((reason) => <button key={reason} type="button" role="menuitem" disabled={s.busy} onClick={() => { setDeclining(null); void notNow(item, reason, false); }}>{reason}</button>)}
              <button type="button" role="menuitem" disabled={s.busy} onClick={() => { const typed = window.prompt("Why not? A few words.")?.trim(); setDeclining(null); if (typed) void notNow(item, typed, true); }}>other…</button>
            </span>
          ) : null}
        </span>
      </div>
    );
  };
  const why = (id: string, index: number) => { // a proposal's record: the drawer on that proposal alone, its citations ringed and linked on the spine
    const mark = marks.find((entry) => entry.id === id);
    if (mark) { setSelected(mark.id); setChoice(index); setDrawer(mark); }
  };
  const card = (item: Front) => {
    if (item.kind === "approval") return running && approval ? (
      <div className="pop ask">
        <div className="top"><span className="kind">for the task</span><time title={clockOf(item.ts)}>{ago(item.ts, now)}</time></div>
        <h4>{item.title}</h4>
        {approval.argument ? <p className="call"><code>{approval.argument}</code></p> : null}{approval.description ? <p>{approval.description}</p> : null}
        <div className="acts"><button type="button" className="btn primary sm" onClick={() => void s.answerApproval(running.run.id, "once")}>Allow once</button><button type="button" className="btn sm ghost" onClick={() => void s.answerApproval(running.run.id, "deny")}>Deny</button></div>
      </div>
    ) : null;
    if (item.kind === "ask") {
      const ask = asks.find((entry) => `ask:${entry.app}|${entry.source ?? ""}` === item.id); // still waiting, or answered
      return (
        <div className="pop ask">
          <div className="top"><span className="kind">{item.tag}</span><time title={clockOf(item.ts)}>{ago(item.ts, now)}</time></div>
          <h4>{item.title}</h4>{ask?.why ? <p>{ask.why}</p> : null}
          {ask ? <div className="acts">{answers(ask)}</div> : <p className="ans">{item.answer}</p>}
        </div>
      );
    }
    const set = sets.find((entry) => `set:${entry.ts}` === item.id);
    if (!set) return null;
    const proposals = proposalsOf(set);
    return (
      <div className="pop">
        <div className="top"><span className="kind soft">{set.trigger === "idle" ? "on a pause" : "on schedule"}</span><time title={clockOf(set.ts)}>{ago(set.ts, now)}</time><button type="button" className="why" onClick={() => why(item.id, 0)}>why?</button></div>
        <h4>{proposals[0]?.task}{proposals[0] ? <ByHand proposal={proposals[0]} /> : null}</h4>
        {proposals[0]?.reason ? <p className="reason">{proposals[0].reason}</p> : null}
        {set.feedback ? <p className="ans">{decisionLabel(set.feedback)} · {clockOf(set.feedback.ts)}{outcomeOf(set, s.runs)}</p> : actions(set, 0)}
        {proposals.length > 1 && !set.feedback ? <button type="button" className="more2" onClick={() => setAlternatives((value) => !value)}>{alternatives ? "hide" : `${proposals.length - 1} more idea${proposals.length === 2 ? "" : "s"}`}</button> : null}
        {alternatives ? <ol className="alts" start={2}>{proposals.slice(1).map((proposal, index) => <li key={index}><b>{proposal.task}</b><ByHand proposal={proposal} /><button type="button" className="why" onClick={() => why(item.id, index + 1)}>why?</button>{actions(set, index + 1)}</li>)}</ol> : null}
      </div>
    );
  };

  // One citation of a proposal, as a chip: a glyph for its kind and its time, the text on hover. A mark of this session
  // opens its record; a search result or a decision from another session links to that session's page; a task shows its title.
  const chip = (id: string, set: LiveProposalSet): ReactNode => {
    const mark = byRecord.get(id);
    if (mark) {
      const text = mark.kind === "note" ? mark.note.summary : mark.kind === "say" ? `you: ${mark.message.text}`
        : mark.kind === "answer" && mark.set.feedback ? `${decisionLabel(mark.set.feedback)}: ${proposalsOf(mark.set)[0]?.task ?? ""}`
        : mark.kind === "suggest" || mark.kind === "check" ? `suggested: ${proposalsOf(mark.set)[0]?.task ?? mark.set.summary}` : "";
      const glyph = mark.kind === "note" ? "●" : mark.kind === "say" ? "💬" : mark.kind === "answer" ? (mark.set.feedback?.decision === "accept" ? "✔" : "✘") : "⚑";
      return <button type="button" className={`chip ${mark.kind}`} title={text} onClick={() => pick(mark)}><i>{glyph}</i>{clockOf(mark.ts).slice(0, 5)}</button>;
    }
    const hit = set.cited?.find((entry) => entry.id === id); // from another session: the Notes page lands on a worklog line, the History page shows the rest
    if (hit) {
      const href = hit.kind === "summary_written" ? `/v1/notes?session=${encodeURIComponent(hit.sessionId)}&id=${encodeURIComponent(hit.id)}` : `/v1/history?session=${encodeURIComponent(hit.sessionId)}`;
      const glyph = hit.kind === "summary_written" ? "●" : hit.text.startsWith("accepted") ? "✔" : hit.text.startsWith("declined") ? "✘" : "⚑";
      return <Link className={`chip far ${hit.kind === "summary_written" ? "note" : "answer"}`} title={hit.text} href={href}><i>{glyph}</i>{new Date(hit.ts).toLocaleDateString(undefined, { month: "short", day: "numeric" })} ↗</Link>;
    }
    if (titles[id]) return <span className="chip task" title={titles[id]}><i>▤</i><span>{titles[id]}</span></span>;
    return <span className="chip" title={id}><i>?</i>{id.slice(0, 6)}</span>;
  };

  // A record and what it was made from: the looks of a note's window, the notes of a change's or a suggestion's window.
  const notesIn = (from: string | null, to: string) => (from ? marks.filter((item): item is Mark & { kind: "note" } => item.kind === "note" && Date.parse(item.ts) > Date.parse(from) && Date.parse(item.ts) <= Date.parse(to)) : []);
  const sources = (from: string | null, to: string) => from ? (
    <><h3>From</h3><ul className="src">
      {notesIn(from, to).map((item) => <li key={item.id}><button type="button" onClick={() => pick(item)}><time>{clockOf(item.ts)}</time>{item.note.summary}</button></li>)}
      {notesIn(from, to).length ? null : <li className="muted">no notes in this window</li>}
    </ul></>
  ) : null;
  const record = (mark: Mark) => {
    switch (mark.kind) {
      case "look": return <><h2>{mark.shot.app}{mark.shot.window ? ` — ${mark.shot.window}` : ""}</h2><time>{clockOf(mark.ts)}</time>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img className="big" src={screenshotUrl(mark.shot.name)} alt={`${mark.shot.app} — ${mark.shot.window}`} /></>;
      case "note": {
        const looks = marks.filter((item): item is Mark & { kind: "look" } => item.kind === "look" && item.ts >= mark.from && item.ts <= mark.ts);
        return <><h2>Note</h2><time>{clockOf(mark.from)} – {clockOf(mark.ts)}{mark.note.model ? ` · ${mark.note.model}` : ""}</time><p>{mark.note.summary}</p>
          {looks.length ? <><h3>From</h3><div className="shots">{looks.map((look) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img key={look.id} src={screenshotUrl(look.shot.name)} alt={look.shot.app} title={`${clockOf(look.ts)} · ${look.shot.app}`} onClick={() => pick(look)} />
          ))}</div></> : null}</>;
      }
      case "change": return <><h2>Task list</h2><time>{clockOf(mark.ts)}{(mark.change.rounds ?? 1) > 1 ? ` · ${mark.change.rounds} rounds` : ""}</time>
        <ChangeBody change={mark.change} titles={titles} />{sources(mark.from, mark.ts)}</>;
      case "suggest": case "answer": {
        const set = mark.set, proposals = proposalsOf(set), proposal = proposals[choice] ?? proposals[0];
        return <><h2>Suggestion</h2><time>{clockOf(set.ts)}{set.trigger === "idle" ? " · on a pause" : ""}{set.model ? ` · ${set.model}` : ""}</time>
          {proposal ? <><p className="big">{proposal.task}<ByHand proposal={proposal} /></p>{proposal.reason ? <p className="muted">{proposal.reason}</p> : null}
            {proposal.evidence?.length ? <div className="chips">{proposal.evidence.map((id) => <Fragment key={id}>{chip(id, set)}</Fragment>)}</div> : null}</> : null}
          {set.feedback ? <p className="ans">{decisionLabel(set.feedback)} · {clockOf(set.feedback.ts)}{outcomeOf(set, s.runs)}</p> : <button type="button" className="btn sm" onClick={() => { bring(`set:${set.ts}`); setDrawer(null); }}>answer it</button>}
          {set.searches?.length ? <><h3>Searched</h3><ul className="searched">{set.searches.map((search, index) => <li key={index}><b>{searchLabel(search)}</b>{search.since || search.until ? <span className="muted"> {search.since ?? "…"} – {search.until ?? "…"}</span> : null} · {search.hits} hit{search.hits === 1 ? "" : "s"}{search.lines.map((line, at) => <small key={at}>{line}</small>)}{search.error ? <small>{search.error}</small> : null}</li>)}</ul></> : null}
          {sources(set.windowStart ?? null, set.ts)}</>;
      }
      case "check": return <><h2>A pause</h2><time>{clockOf(mark.ts)}</time><p>{mark.set.summary || "looked, nothing worth suggesting"}</p></>;
      case "say": return <><h2>You</h2><time>{clockOf(mark.ts)}</time><p>{mark.message.text}</p></>;
      case "run": return <><h2>{AGENT_LABELS[mark.detail.actorAgent ?? ""] ?? "Agent"}</h2><RunCard detail={mark.detail} events={mergedEvents(mark.detail, s.streamed[mark.detail.run.id])} runnerBaseUrl={runnerBaseUrl} open turn /></>;
      case "ask": return <><h2>{mark.title}</h2><time>{clockOf(mark.ts)}</time><p className="muted">{mark.pending ? "waiting for your answer" : "answered"}</p></>;
    }
  };

  const stopping = Boolean(session?.stopReason);
  return (
    <div className="v1 timeline" data-state={state}>
      <header>
        <Brand />
        {session ? (
          <>
            <span className="stat"><span className={`dot${session.paused ? " off" : ""}`} /><b>{{ off: "No session", watching: "Watching", noticed: "Watching", working: "Working", needs: "Needs you", paused: "Paused" }[state]}</b> · {session.paused && session.pausedAt ? `since ${clockOf(session.pausedAt)}` : timeLeft(session.expiresAt, now)}</span>
            <button type="button" className="btn sm" disabled={s.busy || stopping} onClick={() => void s.pauseSession(session.paused)}>{session.paused ? "▶ Resume" : "⏸ Pause"}</button>
            <button type="button" className="btn sm" disabled={s.busy || stopping} onClick={() => void s.stopSession()}>{stopping ? "Stopping…" : "■ Stop"}</button>
          </>
        ) : <span className="stat"><b>No session</b></span>}
        <span className="spacer" />
        {[["/v1/notes", "Notes"], ["/v1/history", "History"], ["/v1/usage", "Usage"], ["/v1/settings", "Settings"]].map(([href, label]) => <Link key={href} className="btn sm ghost" href={href!}>{label}</Link>)}
        {switcher}
      </header>
      {s.waiting ? <div className="wait" role="status"><span className="dot" />{s.waiting}</div> : null}
      {s.issue ?? session?.lastError ? <div className="issue">{s.issue ?? `Last error: ${session?.lastError}`}</div> : null}

      <div className="main">
        {session ? <Spine start={session.startedAt} now={now} paused={session.paused} marks={marks} selected={selected} cited={cited} screenshotUrl={screenshotUrl} onPick={pick} /> : <div className="spinewrap" />}
        <div className="right">
          <div className={`stage${chatOpen && session ? " chat" : ""}${session ? " busy" : ""}${paused ? " paused" : ""}`}>
            {!session ? (
              <div className="empty off"><b>Cue is asleep</b>
                <button type="button" className="btn primary" disabled={s.busy || !s.status} onClick={() => void s.startSession({ ...readOverrides() })}>▶ Start session</button>
                <Link className="muted" href="/v1/settings">Settings</Link></div>
            ) : session.paused ? (
              <div className="empty pausecard"><b>⏸ Paused</b><button type="button" className="btn" disabled={s.busy} onClick={() => void s.pauseSession(true)}>▶ Resume</button></div>
            ) : null}
            {session && !chatOpen ? (
              <div className="alerts">
                <div className="nav">
                  <span className="seg">{(["all", "ask", "suggest", "done"] as const).map((kind) => <button key={kind} type="button" className={mode === kind ? "on" : ""} disabled={kind !== "all" && !has(kind)} onClick={() => { setFilter(kind); setFrontId(null); }}>{{ all: "all", ask: "permissions", suggest: "suggestions", done: "answered" }[kind]}</button>)}</span>
                  <button type="button" aria-label="Older" disabled={at >= shown.length - 1} onClick={() => setFrontId(shown[at + 1]!.id)}>‹</button>
                  <span>{shown.length ? `${shown.length - at}/${shown.length}` : "0"}</span>
                  <button type="button" aria-label="Newer" disabled={at === 0} onClick={() => setFrontId(shown[at - 1]!.id)}>›</button>
                  <button type="button" className="latest" disabled={at === 0} onClick={() => setFrontId(null)}>latest</button>
                </div>
                <div className={`deck n${Math.max(0, Math.min(shown.length - 1, 2))}`}>{front ? <div key={front.id} className="popwrap">{card(front)}</div> : <div className="none"><span className="dot" />Nothing needs you</div>}</div>
              </div>
            ) : null}
            {session && chatOpen ? (
              <div className="session">
                {queue.some((item) => item.kind !== "approval") ? (
                  <div className="waiting">{queue.filter((item) => item.kind !== "approval").map((item) => <button key={item.id} type="button" onClick={() => { setChatOpen(false); bring(item.id); }}><i className={item.kind === "suggest" ? "sg" : "ask"} /><span>{item.title}</span></button>)}</div>
                ) : null}
                <div className={`pair${running ? " split" : ""}`}>
                  {running ? (
                    <div className="work">
                      {showingBrowser || showingRemote ? (
                        <div className="monitor">
                          {showingBrowser ? <BrowserMonitor runnerBaseUrl={runnerBaseUrl} width={900} /> : <RemoteMonitor runnerBaseUrl={runnerBaseUrl} width={900} />}
                          <span className="stamp">● {showingBrowser ? `Cue's browser${agentTab ? ` · ${agentTab.title || agentTab.url}` : ""}` : "Remote desktop"}</span>
                          {takeControl ? <button type="button" className="btn sm takeControl" onClick={takeControl}>Take control ⤢</button> : null}
                        </div>
                      ) : null}
                      <AgentScreen detail={running} events={mergedEvents(running, s.streamed[running.run.id])} runnerBaseUrl={runnerBaseUrl} />
                      {browser?.tabs.length || remoteAvailable ? (
                        <div className="views">
                          {browser?.tabs.length ? <button type="button" className={`btn sm${showingBrowser ? " on" : " ghost"}`} onClick={() => setView(showingBrowser ? null : "browser")}>Cue&apos;s browser</button> : null}
                          {remoteAvailable ? <button type="button" className={`btn sm${showingRemote ? " on" : " ghost"}`} onClick={() => setView(showingRemote ? null : "remote")}>Remote desktop</button> : null}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                  <div className="thread">
                    <div className="hd"><b>Conversation</b><span>{s.chat && s.chat.turn ? `${s.chat.turn} turn${s.chat.turn === 1 ? "" : "s"}` : ""}</span><span className="sp" />
                      {s.chat?.agent || agents.length < 2 ? <span className="muted">{AGENT_NAMES[agent] ?? agent}</span>
                        : <select className="agentPick" value={agent} onChange={(event) => setChosenAgent(event.target.value)} aria-label="Agent">{agents.map((item) => <option key={item} value={item}>{AGENT_NAMES[item] ?? item}</option>)}</select>}
                      {models.length ? (
                        <select className="agentPick" value={model} onChange={(event) => setPickedModel((picked) => ({ ...picked, [agent]: event.target.value }))} aria-label="Model" title={models.find((item) => item.id === model)?.detail}>
                          {sessionModel && !models.some((item) => item.id === sessionModel) ? <option value={sessionModel}>{sessionModel}</option> : null}
                          {models.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                        </select>
                      ) : null}
                      {s.chat?.messages.length && !s.runningId ? <button type="button" className="btn sm ghost" onClick={() => void s.resetChat()}>New</button> : null}
                      <button type="button" className="btn sm ghost" onClick={() => setChatOpen(false)}>Close ✕</button></div>
                    <div className="msgs" ref={msgs} onScroll={(event) => { const el = event.currentTarget; stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 8; }}>
                      {(s.chat?.messages ?? []).map((message) => {
                        const detail = s.runs[message.runId];
                        return message.role === "user" ? (
                          <div key={`${message.runId}-u`} className="msg me">{message.text}<div className="meta">{clockOf(message.ts)}{message.attached ? ` · ${message.attached} note${message.attached === 1 ? "" : "s"} attached` : ""}</div></div>
                        ) : (
                          <div key={`${message.runId}-a`} className="msg ai">
                            {message.text ? <Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_LINKS}>{message.text}</Markdown> : message.status === "running" ? <span className="busy"><span className="dot" />on it…</span> : <span className="muted">(no reply: the turn {message.status === "cancelled" ? "was stopped" : "failed"})</span>}
                            {detail ? <RunCard detail={detail} events={mergedEvents(detail, s.streamed[message.runId])} runnerBaseUrl={runnerBaseUrl} open={message.status === "running"} onStop={s.stopRun} onAnswer={s.answerApproval} turn /> : null}
                          </div>
                        );
                      })}
                      {s.sending ? <><div className="msg me">{s.sending}</div><div className="msg ai"><span className="busy"><span className="dot" />on it…</span></div></> : null}
                      {s.chat?.runningRunId && s.runs[s.chat.runningRunId] && !s.chat.messages.some((message) => message.runId === s.chat?.runningRunId && message.role === "agent") ? (
                        <div className="msg ai"><span className="busy"><span className="dot" />on it…</span>
                          {running && approval ? <div className="askTurn"><b>Cue wants to use {approval.name}</b>{approval.argument ? <p className="call"><code>{approval.argument}</code></p> : null}{approval.description ? <p>{approval.description}</p> : null}
                            <div className="acts"><button type="button" className="btn primary sm" onClick={() => void s.answerApproval(running.run.id, "once")}>Allow once</button><button type="button" className="btn sm ghost" onClick={() => void s.answerApproval(running.run.id, "deny")}>Deny</button></div></div> : null}
                          {asks.filter((item) => item.source === "agent").map((item) => <div key={item.app} className="askTurn"><b>{wants(item)}</b>{item.why ? <p>{item.why}</p> : null}<div className="acts">{answers(item)}</div></div>)}
                          <RunCard detail={s.runs[s.chat.runningRunId]!} events={mergedEvents(s.runs[s.chat.runningRunId]!, s.streamed[s.chat.runningRunId])} runnerBaseUrl={runnerBaseUrl} open onStop={s.stopRun} turn /></div>
                      ) : null}
                      {(s.chat?.messages ?? []).length === 0 && !s.sending ? <p className="muted">Say what you want done.</p> : null}
                    </div>
                    <div className="line">
                      <input ref={input} value={draft} disabled={session.paused} placeholder={session.paused ? "Paused" : "Tell Cue what to do…"} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void send(); }} />
                      {s.runningId && running?.chat ? <button type="button" className="btn sm ghost" onClick={() => void s.stopRun(running.run.id)}>■ Stop</button> : null}
                      <button type="button" className="send" disabled={!draft.trim() || session.paused || Boolean(s.runningId)} onClick={() => void send()} aria-label="Send">↑</button>
                    </div>
                  </div>
                </div>
              </div>
            ) : null}
            {session && !chatOpen && (running || unread) ? (
              <button type="button" className="agentbar" onClick={() => setChatOpen(true)}>
                {running ? <><span className="dot" />{AGENT_LABELS[running.actorAgent ?? ""] ?? "Agent"} · <b>{running.chat ? s.chat?.messages.find((message) => message.runId === running.run.id && message.role === "user")?.text ?? `turn ${running.chat.turn}` : running.run.prompt}</b></>
                  : <>✔ {AGENT_LABELS[unread!.actorAgent ?? ""] ?? "Agent"} · <b>{s.chat?.messages.find((message) => message.runId === unread!.run.id && message.role === "agent")?.text.split("\n")[0] ?? "done"}</b></>} ▸
              </button>
            ) : null}
          </div>
          <TaskCards tasks={s.status?.tasks ?? []} changes={changes} flags={s.status?.taskFlags ?? []} onFlag={{ flag: s.flagTask, reason: s.flagReason, unflag: s.unflag, busy: s.busy }} busy={s.busy} now={now} onEdit={s.editTasks} onPick={(change: LiveTaskChange) => { const mark = marks.find((item) => item.id === `change:${change.ts}`); if (mark) pick(mark); }} />
        </div>
      </div>
      <aside className={`drawer${drawer ? " open" : ""}`}>{drawer ? <><button type="button" className="shut" aria-label="Close" onClick={() => setDrawer(null)}>✕</button>{record(drawer)}</> : null}</aside>
      {remoteOpen ? <RemoteSheet runnerBaseUrl={runnerBaseUrl} backend={expectedBackend} busy={agentBusy} onClose={() => setRemoteOpen(false)} /> : null}
      {browserOpen && browser ? <BrowserSheet runnerBaseUrl={runnerBaseUrl} backend={expectedBackend} browser={browser} busy={agentBusy} onClose={() => setBrowserOpen(false)} /> : null}
    </div>
  );
}
