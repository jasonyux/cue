"use client";

import { agentModelsSchema, type AgentModel, type LiveProposalSet } from "@cue/contracts";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ByHand, MARKDOWN_LINKS, OnPause, RunCard, SIDEQUESTS, type TaskEdit, blockedWhy, callParts, callRunner, clockOf, decisionLabel, mergedEvents, pendingApproval, proposalsOf, runState, toolRunning, turnsOf } from "../live/shared";
import { useDismiss } from "./dismiss";
import { IdleSwitch } from "./IdleSwitch";
import { readOverrides } from "./overrides";
import { BrowserMonitor, BrowserSheet } from "./AgentBrowser";
import { RemoteMonitor, RemoteSheet } from "./RemoteDesktop";
import { AgentScreen } from "./AgentScreen";
import { outcomeOf } from "./Noticed";
import { Pet } from "./Pet";
import { TaskColumn, marksOf, seenOf, type Seen } from "./TaskColumn";
import { useSession } from "./useSession";

type Props = { expectedBackend: "python"; runnerBaseUrl: string; switcher?: ReactNode };
type ZenState = "off" | "watching" | "noticed" | "working" | "needs" | "paused";

const AGENT_NAMES: Record<string, string> = { claude: "Claude Code", codex: "Codex", cmux: "cmux + Claude Code", desktop: "Desktop (GUI)" };
const BUBBLE_MIN = 320; // Pilot's speech bubble: its width by default and its least
const SHEET_MIN = 380; // the task sheet's width by default and its least; its right edge drags it wider
const CHAT_MIN = 680; // the conversation's width by default and its least; either edge drags it wider, on both sides

// A width the person drags, kept in this browser under ``key``: it starts at ``least`` and never goes below it.
// ``widthAt`` turns where the pointer is into the width, already capped to what fits.
function useDragWidth(key: string, least: number) {
  const [width, setWidth] = useState(least);
  useEffect(() => { // the width the person dragged it to last time
    try { setWidth(Math.max(least, Number(window.localStorage.getItem(key)) || least)); } catch { /* storage blocked: the default */ }
  }, [key, least]);
  const drag = (event: React.PointerEvent, widthAt: (x: number) => number) => {
    event.preventDefault();
    const move = (moved: PointerEvent) => {
      const next = Math.round(Math.max(widthAt(moved.clientX), least));
      setWidth(next);
      try { window.localStorage.setItem(key, String(next)); } catch { /* storage blocked: this visit only */ }
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  return [width, drag] as const;
}

function timeLeft(expiresAt: string | null | undefined): string {
  if (!expiresAt) return "no time limit";
  const minutes = Math.max(0, Math.round((Date.parse(expiresAt) - Date.now()) / 60_000));
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} m left` : `${minutes} m left`;
}

// The Zen theme of the landing page: your screen on a monitor on a desk, Pilot beside it saying what matters, and
// nothing else until asked for. The lamp pauses and resumes watching, the clipboard opens your tasks, the mug is
// today's cost; the conversation is one line that opens upward.
export function Zen({ expectedBackend, runnerBaseUrl, switcher }: Props) {
  const s = useSession(runnerBaseUrl, expectedBackend);
  const router = useRouter();
  const [chatOpen, setChatOpen] = useState(false);
  const [tasksOpen, setTasksOpen] = useState(false);
  const [asksOpen, setAsksOpen] = useState(false); // the permission questions, listed
  const [askHidden, setAskHidden] = useState<string | null>(null); // the question card folded away (by app) until another app asks
  // What the monitor shows: null follows the agent (its browser or the remote desktop while it works there, else the
  // person's screen); the chin's buttons pick one by hand. Take control then zooms in on it and takes the person's input.
  const [view, setView] = useState<"browser" | "remote" | "screen" | null>(null);
  const [browserOpen, setBrowserOpen] = useState(false); // zoomed in on the agent browser
  const [remoteOpen, setRemoteOpen] = useState(false); // zoomed in on the remote desktop
  // The task list as the person last read it, kept in this browser: what differs now gets a blue dot. Opening a project
  // reads it: its dots stay while the list is open, and are gone the next time. An edit of the person's own reads its
  // task at once. The first visit takes the whole list as read.
  const tasks = s.status?.tasks ?? null;
  const [seen, setSeen] = useState<Seen | null>(null);
  const [shownSeen, setShownSeen] = useState<Seen | null>(null); // what the open list marks against: as read when it opened
  const keep = (next: Seen) => { setSeen(next); try { window.localStorage.setItem("v1.tasksSeen", JSON.stringify(next)); } catch { /* storage blocked: this visit only */ } };
  useEffect(() => {
    if (!tasks || seen) return;
    let stored: unknown = null;
    try { stored = JSON.parse(window.localStorage.getItem("v1.tasksSeen") ?? "null"); } catch { /* unreadable: start from now */ }
    if (stored && typeof stored === "object") setSeen(stored as Seen); else keep(seenOf(tasks));
  }, [tasks, seen]);  
  const read = (ids: string[], shownToo = false) => {
    if (!tasks || !seen) return;
    const now = seenOf(tasks.filter((task) => ids.includes(task.id)));
    keep({ ...seen, ...now });
    if (shownToo) setShownSeen((shown) => (shown ? { ...shown, ...now } : shown));
  };
  const updated = Boolean(tasks?.some((task) => marksOf(task, seen).task));
  const readAfterEdit = useRef<string | null>(null);
  const editTasks = async (edit: TaskEdit) => { await s.editTasks(edit); readAfterEdit.current = edit.taskId; };
  useEffect(() => { // the person's own edit: no dot for it, now or later
    if (!readAfterEdit.current || !tasks) return;
    const id = readAfterEdit.current;
    readAfterEdit.current = null;
    read([id], true);
  }, [tasks]); // eslint-disable-line react-hooks/exhaustive-deps -- read with the list as the edit left it
  const toggleTasks = () => {
    if (!tasksOpen) setShownSeen(seen);
    setTasksOpen(!tasksOpen);
  };
  const sheet = useRef<HTMLDivElement>(null);
  const glasses = useRef<SVGGElement>(null);
  const askCard = useRef<HTMLDivElement>(null);
  const approvalCard = useRef<HTMLDivElement>(null); // the agent's own tool approval (Claude Code's, Codex's), shown the same way
  const asksSheet = useRef<HTMLDivElement>(null);
  const [sheetWidth, dragSheet] = useDragWidth("v1.zenTaskWidth", SHEET_MIN);
  const widen = (event: React.PointerEvent) => {
    const left = sheet.current?.getBoundingClientRect().left ?? 0;
    dragSheet(event, (x) => Math.min(x - left, window.innerWidth - left - 24));
  };
  const [chatWidth, dragChat] = useDragWidth("v1.zenChatWidth", CHAT_MIN);
  // The conversation is centred on the window, so it widens by twice the pointer's distance from the middle.
  const widenChat = (event: React.PointerEvent) => dragChat(event, (x) => Math.min(2 * Math.abs(x - window.innerWidth / 2), window.innerWidth - 48));
  const [menuOpen, setMenuOpen] = useState(false);
  const menu = useRef<HTMLDivElement>(null);
  const [alternatives, setAlternatives] = useState(false);
  const [why, setWhy] = useState(false);
  const [hiddenKey, setHiddenKey] = useState<string | null>(null); // what Pilot was saying when it was told to hush
  const [listOpen, setListOpen] = useState(false); // the session's suggestions, all of them
  const [rowOpen, setRowOpen] = useState<string | null>(null); // a listed, answered suggestion being answered again
  // The reasons menu of a listed suggestion. It hangs from the button that opened it but is drawn on the stage, outside
  // the list, so the list's scrolling does not clip it; it closes when the list scrolls.
  const stage = useRef<HTMLDivElement>(null);
  const whyMenu = useRef<HTMLSpanElement>(null);
  const [whyAt, setWhyAt] = useState<{ item: LiveProposalSet; left: number; top: number } | null>(null);
  const openWhy = (item: LiveProposalSet, button: HTMLElement) => {
    const at = button.getBoundingClientRect(), origin = stage.current?.getBoundingClientRect();
    if (origin) setWhyAt({ item, left: at.left - origin.left, top: at.bottom - origin.top });
  };
  const leaveWhy = (event: React.MouseEvent) => { if (!whyMenu.current?.contains(event.relatedTarget as Node | null)) setWhyAt(null); };
  const [declining, setDeclining] = useState<number | null>(null); // the proposal whose "Not now" reasons are showing
  const bubble = useRef<HTMLDivElement>(null);
  const [bubbleWidth, dragBubble] = useDragWidth("v1.zenBubbleWidth", BUBBLE_MIN);
  // The bubble hangs from Pilot on its right, so its left edge is the one that drags it wider.
  const widenBubble = (event: React.PointerEvent) => {
    const right = bubble.current?.getBoundingClientRect().right ?? window.innerWidth;
    dragBubble(event, (x) => Math.min(right - x, right - 24));
  };
  const grip = <div className="bgrip" title="Drag to widen" onPointerDown={widenBubble} />;
  const [draft, setDraft] = useState("");
  const input = useRef<HTMLInputElement>(null);
  // The conversation sticks to its bottom as it grows, unless the person scrolled up to read; scrolling back to the
  // bottom makes it stick again.
  const msgs = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const atBottom = (el: HTMLDivElement) => el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
  useEffect(() => {
    const el = msgs.current;
    if (!el) return;
    stick.current = true;
    el.scrollTop = el.scrollHeight;
    const follow = new MutationObserver(() => { if (stick.current) el.scrollTop = el.scrollHeight; });
    follow.observe(el, { childList: true, subtree: true, characterData: true });
    return () => follow.disconnect();
  }, [chatOpen]);
  const session = s.session;
  // The idle trigger: the running session's switch when there is one, else the default the next session starts with.
  const idleOn = session?.idleTrigger?.enabled ?? s.status?.defaults.proposerIdleTrigger ?? false;
  const idleS = session?.idleTrigger?.idleS ?? s.status?.defaults.proposerIdleS ?? 10;
  const running = s.runningId ? s.runs[s.runningId] ?? null : null;
  const turns = running ? turnsOf(mergedEvents(running, s.streamed[running.run.id])) : [];
  const asking = running ? pendingApproval(turns) : null;
  const approval = asking ? callParts(asking.detail) : null; // the call it waits on
  const approvalDetail = approval ? <>{approval.argument ? <p className="call"><code>{approval.argument}</code></p> : null}{approval.description ? <p>{approval.description}</p> : null}</> : null;
  const approvalActs = running ? <div className="acts"><button type="button" className="b p" onClick={() => void s.answerApproval(running.run.id, "once")}>Allow once</button><button type="button" className="b r" onClick={() => void s.answerApproval(running.run.id, "deny")}>Deny</button></div> : null;
  const [current] = [...s.proposalSets].filter((item) => !item.error && proposalsOf(item).length > 0).reverse();
  const state: ZenState = !session ? "off" : session.paused ? "paused" : asking ? "needs" : running ? "working" : current && !current.feedback ? "noticed" : "watching";
  const agents = session?.settings.actorAgents ?? [];
  // The agent browser, once a Claude Code run opened it. While the agent's latest step is on a web page the monitor
  // shows that tab live; the zoom button opens it full size, where the person can act on it too.
  const browser = session?.agentBrowser ?? null;
  const lastTurn = turns[turns.length - 1];
  const lastCall = lastTurn ? [...lastTurn.events].reverse().find((event) => event.type === "function_call_requested") : undefined;
  const agentOnBrowser = Boolean(browser?.tabs.length) && Boolean(lastCall?.detail?.startsWith("mcp__browser__"));
  const showingBrowser = Boolean(browser?.tabs.length) && (view === null ? agentOnBrowser : view === "browser");
  const agentTab = browser?.tabs.find((tab) => tab.agent);
  const remoteAvailable = Boolean((session?.settings ?? s.status?.defaults)?.actorDesktopSource.includes("remote")); // a desktop the person can watch and use too; asleep, as the settings say
  const agentOnRemote = remoteAvailable && Boolean(lastCall?.detail?.startsWith("mcp__desktop__exec_py") && lastCall.detail.includes('"target": "remote"'));
  const showingRemote = remoteAvailable && (view === null ? agentOnRemote : view === "remote");
  useEffect(() => { setView(null); }, [running?.run.id]); // a new run: back to following the agent
  const takeControl = showingBrowser && !browserOpen ? () => setBrowserOpen(true) : showingRemote && !remoteOpen ? () => setRemoteOpen(true) : null;
  const agentBusy = Boolean(lastTurn && toolRunning(lastTurn));
  const [chosenAgent, setChosenAgent] = useState<string | null>(null);
  const agent = s.chat?.agent ?? (chosenAgent && agents.includes(chosenAgent as never) ? chosenAgent : agents.includes("claude") ? "claude" : agents.includes("cmux") ? "cmux" : agents[0] ?? "claude"); // a conversation keeps the agent it began with
  // The models the chat's agent offers (Claude Code and Codex list theirs; the backend asks them once), and the one the
  // next message uses: the person's pick for that agent, else the session's setting, else the agent's own default.
  const [modelLists, setModelLists] = useState<Record<string, AgentModel[]>>({});
  const [pickedModel, setPickedModel] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!session || (agent !== "claude" && agent !== "codex") || modelLists[agent]) return;
    callRunner(runnerBaseUrl, expectedBackend, `/api/live/agents/${agent}/models`, agentModelsSchema)
      .then((answer) => setModelLists((lists) => ({ ...lists, [agent]: answer.models })))
      .catch(() => setModelLists((lists) => ({ ...lists, [agent]: [] }))); // no list: no picker, and the session's model is used
  }, [agent, session, modelLists, runnerBaseUrl, expectedBackend]);
  const models = modelLists[agent] ?? [];
  const sessionModel = (agent === "claude" ? session?.settings.actorClaudeModel : agent === "codex" ? session?.settings.actorCodexModel : null) ?? null;
  const model = pickedModel[agent] ?? sessionModel ?? models.find((item) => item.default)?.id ?? "";
  const mine = session?.latestScreenshot;
  const look = session?.latestSnapshot;
  // Permission questions with no answer yet: the agent's asks (its run waits on them) and the apps looked at under ask. The newest
  // shows in a card on the monitor; Pilot's glasses list them all; a running turn shows the agent's in the conversation too.
  const asks = session?.permissionAsks ?? [];
  const ask = asks[0] && asks[0].app !== askHidden ? asks[0] : null;
  const agentAsks = asks.filter((item) => item.source === "agent");
  // Allow's three answers hang from the Allow ▾ that opened them but are drawn over the page (a portal), so neither the
  // glasses list nor the conversation clips them; they close when the pointer leaves them or the button.
  const allowMenu = useRef<HTMLSpanElement>(null);
  const [allowAt, setAllowAt] = useState<{ item: (typeof asks)[number]; left: number; top: number } | null>(null);
  const openAllow = (item: (typeof asks)[number], button: HTMLElement) => { const at = button.getBoundingClientRect(); setAllowAt({ item, left: at.left, top: at.bottom }); };
  const leaveAllow = (event: React.MouseEvent) => { if (!allowMenu.current?.contains(event.relatedTarget as Node | null)) setAllowAt(null); };
  const answer = (app: string, level: "block" | "look" | "use" | "deny", scope?: "session" | "once" | "task") => { setAskHidden(null); void s.answerPermission(app, level, scope); };
  const answers = (item: (typeof asks)[number]) => item.source === "agent" ? <>
    <button type="button" className="b p" aria-haspopup="menu" onMouseEnter={(event) => openAllow(item, event.currentTarget)} onMouseLeave={leaveAllow} onClick={(event) => openAllow(item, event.currentTarget)}>Allow ▾</button>
    <button type="button" className="b r" title="For the rest of this task; nothing saved" onClick={() => answer(item.app, "deny")}>Deny</button>
  </> : <>
    <button type="button" className="b r" onClick={() => answer(item.app, "block")}>Block</button>
    <button type="button" className="b p" onClick={() => answer(item.app, "look")}>Look</button>
    <button type="button" className="b p" onClick={() => answer(item.app, "use")}>Use</button>
    <button type="button" className="b" title="Look until this session ends; nothing saved" onClick={() => answer(item.app, "look", "session")}>This session</button>
  </>;
  const wants = (item: (typeof asks)[number]) => `Pilot wants to ${item.source === "agent" && item.level === "use" ? "use" : "look at"} ${item.app}`;

  useEffect(() => { // "Do it" puts the suggestion in the line and opens the conversation
    if (!s.chatSeed) return;
    setDraft(s.chatSeed);
    setChatOpen(true);
    input.current?.focus();
  }, [s.chatSeed]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setChatOpen(false); setTasksOpen(false); setMenuOpen(false); setBrowserOpen(false); setRemoteOpen(false); setAsksOpen(false); } };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);

  const stopping = Boolean(session?.stopReason);
  const writing = Boolean(session && !session.paused && session.nextBeliefAt === null); // the task model is at work: its next tick is unset while one runs
  const power = () => {
    if (!session) void s.startSession({ ...readOverrides() });
    else if (window.confirm("Stop the session? Pilot stops watching and the session is stored.")) void s.stopSession();
  };
  // What Pilot says now: a suggestion, or the turn at work (or its permission request). The bubble shows while the
  // conversation is closed (the conversation shows the same, so only one is open, and only the person switches); a
  // click on Pilot hushes it, until there is something new or the next click. A hushed turn still shows as a small
  // progress mark by Pilot: everything folded is not nothing.
  const bubbleKey = `${state}:${current?.ts ?? ""}:${running?.run.id ?? ""}`;
  const speaking = state === "noticed" || state === "working" || state === "needs";
  const folded = !chatOpen && !listOpen; // neither the conversation nor the suggestions list is open
  const saying = folded && state === "noticed" && hiddenKey !== bubbleKey;
  const atWork = folded && (state === "working" || state === "needs") && hiddenKey !== bubbleKey;
  const hushedAtWork = folded && (state === "working" || state === "needs") && hiddenKey === bubbleKey;
  const suggestions = [...s.proposalSets].filter((item) => !item.error && proposalsOf(item).length > 0).reverse(); // newest first
  const pokePilot = () => {
    if (listOpen) { setListOpen(false); return; }
    if (chatOpen) { setChatOpen(false); setHiddenKey(null); return; } // the conversation folds away; Pilot says it instead
    if (saying || atWork) { setHiddenKey(bubbleKey); return; } // hush
    if (speaking) setHiddenKey(null); // say it again
    else setListOpen(true); // nothing new: the session's suggestions
  };
  // A press anywhere else that is not a control closes what is open: the task sheet, the menu, and whatever Pilot shows
  // (the list closes; a suggestion or the turn at work is hushed, as a click on Pilot would).
  useDismiss(tasksOpen, [sheet], () => setTasksOpen(false));
  useDismiss(asksOpen, [asksSheet, glasses], () => setAsksOpen(false));
  useDismiss(Boolean(ask) && !asksOpen, [askCard, glasses, allowMenu], () => { if (ask) setAskHidden(ask.app); });
  useDismiss(menuOpen, [menu], () => setMenuOpen(false));
  useDismiss(saying || atWork || (listOpen && !chatOpen), [bubble, whyMenu, approvalCard], () => {
    setDeclining(null);
    if (listOpen) { setListOpen(false); setWhyAt(null); } else setHiddenKey(bubbleKey);
  });
  const send = async () => {
    const text = draft.trim();
    if (!text || !session || session.paused || s.runningId) return;
    setDraft("");
    if (!(await s.sendChat(text, agent, models.length ? model : undefined))) setDraft(text);
    // The sheet stays as it was: the turn shows there while it is open, in Pilot's bubble once the person closes it.
  };
  const notNow = async (item: LiveProposalSet, reason: string, typed: boolean) => { await s.decide(item, "reject", 0); await s.giveReason(item, reason, typed); };
  const temporary = async (item: LiveProposalSet, choice: number) => {
    await s.decide(item, "accept", choice);
    router.push(`/v1/temporary?prompt=${encodeURIComponent(proposalsOf(item)[choice]?.task ?? "")}&proposalsTs=${encodeURIComponent(item.ts)}&choice=${choice}`);
  };
  const actions = (item: LiveProposalSet, choice: number) => (
    <div className="acts">
      <button type="button" className="b p" disabled={s.busy} onClick={() => void s.acceptToChat(item, choice)}>Do it</button>
      <button type="button" className="b" disabled={s.busy} onClick={() => void temporary(item, choice)}>Temporary run</button>
      <span className="notNow">
        <button type="button" className="b g" disabled={s.busy} onMouseEnter={() => setDeclining(choice)} onClick={() => setDeclining(declining === choice ? null : choice)}>Not now {declining === choice ? "▴" : "▾"}</button>
        {declining === choice ? (
          <span className="whyNot" role="menu">
            {(s.status?.rejectReasons ?? []).map((reason) => <button key={reason} type="button" role="menuitem" disabled={s.busy} onClick={() => { setDeclining(null); void notNow(item, reason, false); }}>{reason}</button>)}
            <button type="button" role="menuitem" disabled={s.busy} onClick={() => { const typed = window.prompt("Why not? A few words.")?.trim(); setDeclining(null); if (typed) void notNow(item, typed, true); }}>other…</button>
          </span>
        ) : null}
      </span>
    </div>
  );
  const proposals = current ? proposalsOf(current) : [];

  return (
    <div className="v1 zen" data-state={state}>
      <div className="top">
        <span className="chip"><span className="sdot" /><b>{{ off: "No session", watching: "Watching", noticed: "Watching", working: "Working", needs: "Needs you", paused: "Paused" }[state]}</b>{session ? ` · ${session.paused && session.pausedAt ? `since ${clockOf(session.pausedAt)}` : timeLeft(session.expiresAt)}` : ""}</span>
        {s.waiting ? <span className="chip wait"><span className="sdot" />{s.waiting}</span> : null}
        {s.issue ?? session?.lastError ? <span className="chip bad">{s.issue ?? `Last error: ${session?.lastError}`}</span> : null}
        <div className="more">
          <Link href="/v1/settings" className="round" aria-label="Settings" title="Settings">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
          </Link>
          <button type="button" className="round" onClick={() => setMenuOpen((value) => !value)} aria-label="Menu">⋯</button>
          {menuOpen ? (
            <div className="menu" ref={menu}>
              {session ? <>
                <button type="button" disabled={s.busy || Boolean(session.stopReason)} onClick={() => { setMenuOpen(false); void s.pauseSession(session.paused); }}>{session.paused ? "▶ Resume watching" : "⏸ Pause watching"}</button>
                <button type="button" disabled={s.busy || Boolean(session.stopReason)} onClick={() => { setMenuOpen(false); void s.stopSession(); }}>■ Stop session</button>
                <hr />
              </> : null}
              <IdleSwitch key={idleS} trigger={session?.idleTrigger ?? null} enabled={idleOn} idleS={idleS} busy={s.busy} onChange={(on, seconds) => void s.setIdleTrigger(on, seconds)} />
              <hr />
              <Link href="/v1/temporary">Temporary runs</Link><Link href="/v1/notes">Notes</Link><Link href="/v1/history">History</Link><Link href="/v1/usage">Usage</Link>
              <hr />
              <div className="menuRow">{switcher}</div>
            </div>
          ) : null}
        </div>
      </div>

      <div className="surface" />
      <div className="stage" ref={stage}>
        <div className="bezel"><div className="screen">
          {showingBrowser && !browserOpen ? <BrowserMonitor runnerBaseUrl={runnerBaseUrl} width={900} />
          : showingRemote && !remoteOpen ? <RemoteMonitor runnerBaseUrl={runnerBaseUrl} width={900} />
          : (state === "working" || state === "needs") && running ? <AgentScreen detail={running} events={mergedEvents(running, s.streamed[running.run.id])} runnerBaseUrl={runnerBaseUrl} />
          : look?.blocked ? <>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img className="glimpse" src={`${runnerBaseUrl}/api/live/session/screen/blocked?t=${encodeURIComponent(look.ts ?? "")}`} alt={`${look.app}, blurred`} />
            <div className="veil blocked">Not watching {look.app}<small>{blockedWhy(look.blocked)}</small></div>
          </> : mine ? <>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={`${runnerBaseUrl}/api/live/session/screenshots/${encodeURIComponent(mine.name)}`} alt={`${mine.app} — ${mine.window}`} />
          </> : <div className="blank">{session ? "The first look is on its way." : ""}</div>}
          {takeControl ? <button type="button" className="b takeControl" onClick={takeControl}>Take control ⤢</button> : null}
          {showingBrowser && !browserOpen ? <span className="stamp">● Pilot&apos;s browser{agentTab ? ` — ${agentTab.title || agentTab.url}` : ""}{agentOnBrowser ? " · Pilot is working there" : ""}</span>
            : showingRemote && !remoteOpen ? <span className="stamp">● Remote desktop{agentOnRemote ? " · Pilot is working there" : ""}</span>
            : state === "working" || state === "needs" ? null
            : mine && !look?.blocked ? <span className="stamp">● {mine.app}{mine.window ? ` — ${mine.window}` : ""} · {clockOf(mine.ts)}</span> : null}
          {state === "paused" ? (
            <div className="veil">⏸ Paused · not looking at your screen<small>notes, tasks and suggestions wait too</small>
              <button type="button" className="b" disabled={s.busy} onClick={() => void s.pauseSession(true)}>▶ Resume</button></div>
          ) : null}
          {state === "off" ? (
            <div className="veil off">Pilot is asleep<small>{s.sessions[0]?.stoppedAt ? `the last session ended ${clockOf(s.sessions[0].stoppedAt)}` : "start a session and it looks at your screen"}</small>
              <button type="button" className="b p" disabled={s.busy || !s.status} onClick={() => void s.startSession({ ...readOverrides() })}>▶ Start session</button>
              <Link className="settingsLink" href="/v1/settings">Settings</Link></div>
          ) : null}
        </div>
          <div className="chin">
            <span className="mark">ComputerPilot</span>
            {remoteAvailable ? (
              <button type="button" className={`hw remote${showingRemote ? " on" : ""}`} onClick={() => setView(showingRemote ? "screen" : "remote")} aria-label="Remote desktop" title={showingRemote ? "Back to your screen" : "Show the remote desktop on the monitor"}>
                <svg viewBox="0 0 20 20"><rect x="2.5" y="3.5" width="15" height="10" rx="1.5" fill="none" strokeWidth="1.8" /><line x1="7" y1="16.5" x2="13" y2="16.5" strokeWidth="1.8" strokeLinecap="round" /><line x1="10" y1="13.5" x2="10" y2="16.5" strokeWidth="1.8" /></svg>
              </button>
            ) : null}
            <button type="button" className={`hw browser${showingBrowser ? " on" : ""}`} disabled={!browser?.tabs.length} onClick={() => setView(showingBrowser ? "screen" : "browser")} aria-label="Pilot's browser"
              title={!browser?.tabs.length ? "Pilot's browser opens with its first task on the web" : showingBrowser ? "Back to your screen" : "Show Pilot's browser on the monitor"}>
              <svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="7" fill="none" strokeWidth="1.8" /><ellipse cx="10" cy="10" rx="3" ry="7" fill="none" strokeWidth="1.4" /><line x1="3" y1="10" x2="17" y2="10" strokeWidth="1.4" /><line x1="4" y1="6.5" x2="16" y2="6.5" strokeWidth="1.1" /><line x1="4" y1="13.5" x2="16" y2="13.5" strokeWidth="1.1" /></svg>
            </button>
            <span className="sp" />
            <span className="led" title={{ off: "No session", watching: "Watching", noticed: "Watching", working: "Agent working", needs: "Waiting for you", paused: "Paused" }[state]} />
            {session ? (
              <button type="button" className="hw" disabled={s.busy || stopping} onClick={() => void s.pauseSession(session.paused)} aria-label={session.paused ? "Resume" : "Pause"} title={session.paused ? "Resume watching" : "Pause watching"}>
                {session.paused ? <svg viewBox="0 0 20 20"><path d="M7 5 L15 10 L7 15 Z" /></svg> : <svg viewBox="0 0 20 20"><rect x="5.5" y="5" width="3" height="10" rx="1" /><rect x="11.5" y="5" width="3" height="10" rx="1" /></svg>}
              </button>
            ) : null}
            <button type="button" className={`hw power${session ? " on" : ""}`} disabled={s.busy || stopping || !s.status} onClick={power} aria-label={session ? "Stop session" : "Start session"} title={session ? "Stop the session" : "Start a session"}>
              <svg viewBox="0 0 20 20"><path d="M6.2 6.2 A5.4 5.4 0 1 0 13.8 6.2" fill="none" strokeWidth="2" strokeLinecap="round" /><line x1="10" y1="3.6" x2="10" y2="9.6" strokeWidth="2" strokeLinecap="round" /></svg>
            </button>
          </div>
        </div>
        <div className="neck" /><div className="base" />

        <span className="obj lamp" aria-hidden="true">
          <svg viewBox="0 0 120 150"><polygon className="cone" points="78,52 40,148 140,148" fill="#ffe9a8" opacity=".45" /><rect x="18" y="138" width="56" height="10" rx="5" fill="#59606d" /><path d="M46 138 L38 76 L80 46" stroke="#59606d" strokeWidth="7" fill="none" strokeLinecap="round" /><path d="M66 30 L104 50 L88 66 Z" fill="#3c424d" /><circle className="bulbl" cx="94" cy="62" r="6" fill="#ffd56a" /></svg>
        </span>
        <button type="button" className={`obj clip${writing ? " writing" : ""}${updated && !tasksOpen ? " updated" : ""}`} onClick={toggleTasks} aria-label="Your tasks">
          <span className="tip">{writing ? "Pilot is updating your tasks…" : updated ? "Your tasks · updated since you last looked" : "Your tasks"}</span>
          <svg viewBox="0 0 76 96"><rect x="4" y="8" width="68" height="86" rx="6" fill="#c08a52" /><rect x="11" y="16" width="54" height="72" rx="2" fill="#fffdf6" /><rect x="24" y="2" width="28" height="14" rx="4" fill="#9aa0aa" /><g stroke="#c9c2b3" strokeWidth="3" strokeLinecap="round"><line x1="18" y1="32" x2="58" y2="32" /><line x1="18" y1="44" x2="52" y2="44" /><line x1="18" y1="56" x2="56" y2="56" /><line x1="18" y1="68" x2="44" y2="68" /></g>
            <g className="pencil"><g transform="rotate(38 50 60)"><rect x="46" y="30" width="8" height="34" rx="1.5" fill="#ffc94d" /><rect x="46" y="30" width="8" height="5" fill="#e7808a" /><polygon points="46,64 54,64 50,72" fill="#f3d9b1" /><polygon points="48.6,68.8 51.4,68.8 50,72" fill="#3a3f49" /></g></g></svg>
          {updated && !tasksOpen ? <span className="newMark">updated</span> : null}
          {(s.status?.tasks ?? []).length ? <span className="badge">{(s.status?.tasks ?? []).reduce((sum, task) => sum + (task.status === "done" || task.title === SIDEQUESTS ? 0 : task.steps.filter((step) => !step.doneAt).length), 0)}</span> : null}
        </button>
        <Link className="obj mug" href="/v1/usage" aria-label="Usage">
          <span className="tip">This session's cost · Usage</span>
          <svg viewBox="0 0 64 70"><path d="M8 14 h40 v40 a10 10 0 0 1 -10 10 h-20 a10 10 0 0 1 -10 -10 Z" fill="#fff" stroke="#d6cdbb" strokeWidth="2" /><path d="M48 24 h6 a8 8 0 0 1 0 16 h-6" stroke="#d6cdbb" strokeWidth="4" fill="none" /><text x="28" y="44" textAnchor="middle" fontSize="11" fontWeight="700" fill="#2f6fed" fontFamily="-apple-system,sans-serif">{s.sessionCost === null ? "$—" : `$${s.sessionCost.toFixed(2)}`}</text></svg>
        </Link>
        {atWork && state === "needs" && approval ? (
          <div className="ask" ref={approvalCard}>
            <button type="button" className="shut" aria-label="Put away" onClick={() => setHiddenKey(bubbleKey)}>✕</button>
            <div className="k">For the task</div>
            <h4>Pilot wants to use {approval.name}</h4>
            {approvalDetail}{approvalActs}
          </div>
        ) : ask && !asksOpen ? (
          <div className="ask" ref={askCard}>
            <button type="button" className="shut" aria-label="Put away" onClick={() => setAskHidden(ask.app)}>✕</button>
            <div className="k">{ask.source === "agent" ? "For the task" : "Permission"}</div>
            <h4>{wants(ask)}</h4>
            {ask.why ? <p>{ask.why}</p> : null}
            <div className="acts">{answers(ask)}</div>
          </div>
        ) : null}
        <button type="button" className="petSpot" onClick={pokePilot} title={saying || atWork || listOpen ? "Hide what Pilot says" : hushedAtWork ? "Show what Pilot is doing" : "Show what Pilot says, or the session's suggestions"} aria-label="Pilot"><Pet glasses={session ? (look?.blocked ? "dark" : "clear") : null} asking={asks.length} onGlasses={() => { setAsksOpen(!asksOpen); setAskHidden(null); }} glassesRef={glasses} /><span className="zzz">z z</span></button>

        {saying && state === "noticed" && current ? (
          <div className="bubble" ref={bubble} style={{ width: bubbleWidth }} title={`Noticed at ${clockOf(current.ts)}`} onMouseLeave={() => setDeclining(null)}>
            {grip}
            <h4>{proposals[0]?.task}{proposals[0] ? <ByHand proposal={proposals[0]} /> : null}<OnPause set={current} /></h4>
            {proposals[0]?.reason ? (why ? <p>{proposals[0].reason} <button type="button" className="more2" onClick={() => setWhy(false)}>hide</button></p> : <button type="button" className="more2" onClick={() => setWhy(true)}>why?</button>) : null}
            {actions(current, 0)}
            {proposals.length > 1 ? <button type="button" className="more2" onClick={() => setAlternatives((value) => !value)}>{alternatives ? "hide" : `${proposals.length - 1} more idea${proposals.length === 2 ? "" : "s"}`}</button> : null}
            {alternatives ? <ol className="alts" start={2}>{proposals.slice(1).map((proposal, index) => <li key={index}><b>{proposal.task}</b><ByHand proposal={proposal} />{actions(current, index + 1)}</li>)}</ol> : null}
            {suggestions.length > 1 ? <button type="button" className="more2 all" onClick={() => setListOpen(true)}>all suggestions this session · {suggestions.length}</button> : null}
          </div>
        ) : null}
        {listOpen && !chatOpen ? (
          <div className="bubble list" ref={bubble} style={{ width: bubbleWidth }} onScroll={() => setWhyAt(null)}>
            {grip}
            <div className="listHead"><b>{session ? `Suggestions this session · ${suggestions.length}` : "Pilot is asleep"}</b>
              {speaking ? <button type="button" className="more2" onClick={() => { setListOpen(false); setHiddenKey(null); }}>‹ back</button> : null}</div>
            {!session ? <p>Press the power button under the screen to start a session.</p>
              : suggestions.length === 0 ? <p>Nothing suggested yet. Pilot speaks up when it has seen enough.</p>
              : <ul className="sugg">{suggestions.map((item) => {
                const first = proposalsOf(item)[0];
                return (
                  <li key={item.ts} className={item.feedback ? item.feedback.decision : "open"}>
                    <time>{clockOf(item.ts)}</time>
                    <div><span className="task">{first?.task}</span>
                      <span className="state">{item.feedback ? `${decisionLabel(item.feedback)}${outcomeOf(item, s.runs)}` : "no answer yet"}{first ? <ByHand proposal={first} /> : null}</span>
                      {!item.feedback || rowOpen === item.ts ? (
                        <span className="rowActs">
                          <button type="button" className="b p" disabled={s.busy || !session || session.paused} onClick={() => { setListOpen(false); setRowOpen(null); void s.acceptToChat(item, 0); }}>Do it</button>
                          <button type="button" className="b" disabled={s.busy} onClick={() => void temporary(item, 0)}>Temporary run</button>
                          <button type="button" className="b g" disabled={s.busy} onMouseEnter={(event) => openWhy(item, event.currentTarget)} onMouseLeave={leaveWhy} onClick={(event) => openWhy(item, event.currentTarget)}>Not now {whyAt?.item === item ? "▴" : "▾"}</button>
                        </span>
                      ) : (
                        <span className="rowActs">
                          {item.feedback.decision === "reject" && !item.feedback.rejectReason ? <button type="button" className="b g" onMouseEnter={(event) => openWhy(item, event.currentTarget)} onMouseLeave={leaveWhy} onClick={(event) => openWhy(item, event.currentTarget)}>add a reason {whyAt?.item === item ? "▴" : "▾"}</button> : null}
                          <button type="button" className="b g" onClick={() => setRowOpen(item.ts)}>change answer</button>
                        </span>
                      )}
                    </div>
                  </li>
                );
              })}</ul>}
          </div>
        ) : null}
        {listOpen && !chatOpen && whyAt ? (
          <span className="whyNot onStage" role="menu" ref={whyMenu} style={{ left: whyAt.left, top: whyAt.top }} onMouseLeave={() => setWhyAt(null)}>
            {(s.status?.rejectReasons ?? []).map((reason) => <button key={reason} type="button" role="menuitem" disabled={s.busy} onClick={() => { const item = whyAt.item; setWhyAt(null); setRowOpen(null); void notNow(item, reason, false); }}>{reason}</button>)}
            <button type="button" role="menuitem" disabled={s.busy} onClick={() => { const item = whyAt.item; const typed = window.prompt("Why not? A few words.")?.trim(); setWhyAt(null); setRowOpen(null); if (typed) void notNow(item, typed, true); }}>other…</button>
          </span>
        ) : null}
        {hushedAtWork && running ? (
          <div className="petMark" title={state === "needs" ? "Pilot is waiting for your answer; click it to see" : "Pilot is working; click it to see"}>
            {state === "needs" ? <b>✋</b> : null}<span className="prog"><i /></span>
          </div>
        ) : null}
        {atWork && state === "working" && running ? (
          <div className="bubble" ref={bubble} style={{ width: bubbleWidth }}>
            {grip}
            <div className="k blue">On it{running.chat ? ` · turn ${running.chat.turn}` : " · temporary run"}</div>
            <h4>{running.chat ? (s.chat?.messages.find((message) => message.runId === running.run.id && message.role === "user")?.text ?? `Turn ${running.chat.turn}`) : running.run.prompt}</h4>
            <p>{runState(turns, true)}</p>
            <div className="prog"><i /></div>
            <div className="acts"><button type="button" className="b" onClick={() => setChatOpen(true)}>Show conversation</button><button type="button" className="b g" onClick={() => void s.stopRun(running.run.id)}>■ Stop</button></div>
          </div>
        ) : null}
      </div>

      {tasksOpen ? (
        <div className="tasks" ref={sheet} style={{ width: sheetWidth }}>
          <div className="zgrip" title="Drag to widen" onPointerDown={widen} />
          <TaskColumn tasks={s.status?.tasks ?? []} trash={s.status?.trash ?? []} flags={s.status?.taskFlags ?? []} change={s.changes[s.changes.length - 1] ?? null}
            busy={s.busy} collapsed={false} onCollapse={() => setTasksOpen(false)} onResize={() => undefined} seen={shownSeen ?? seen} onRead={read}
            hideDays={session?.settings.beliefDoneStepDays ?? s.status?.defaults.beliefDoneStepDays ?? 21}
            onEdit={editTasks} onFlag={{ flag: s.flagTask, reason: s.flagReason, unflag: s.unflag, busy: s.busy }} />
        </div>
      ) : null}

      {asksOpen ? (
        <div className="asks" ref={asksSheet}>
          <div className="hd"><b>Permission questions</b>{asks.length ? null : <span>none waiting</span>}<button type="button" className="shut" aria-label="Close" onClick={() => setAsksOpen(false)}>✕</button></div>
          {asks.map((item, index) => (
            <div key={`${item.app}|${item.bundle}|${item.source ?? ""}`} className={`askRow${index && asks[index - 1]?.source !== item.source ? " sep" : ""}`}>
              <b>{item.app}{item.source === "agent" ? <span> · for the task, to {item.level}</span> : null}</b>
              {item.why ? <p>{item.why}</p> : null}
              <div className="acts">{answers(item)}</div>
            </div>
          ))}
        </div>
      ) : null}
      {remoteOpen ? <RemoteSheet runnerBaseUrl={runnerBaseUrl} backend={expectedBackend} busy={agentBusy} onClose={() => setRemoteOpen(false)} /> : null}
      {browserOpen && browser ? <BrowserSheet runnerBaseUrl={runnerBaseUrl} backend={expectedBackend} browser={browser} busy={agentBusy} onClose={() => setBrowserOpen(false)} /> : null}

      <div className="chatwrap">
        {chatOpen ? (
          <div className="sheet" style={{ width: chatWidth }}>
            <div className="cgrip l" title="Drag to widen" onPointerDown={widenChat} /><div className="cgrip r" title="Drag to widen" onPointerDown={widenChat} />
            <div className="hd"><b>Conversation</b><span>{s.chat && s.chat.turn ? `${s.chat.turn} turn${s.chat.turn === 1 ? "" : "s"}` : "nothing said yet"}</span><span className="sp" />
              {s.chat?.agent || agents.length < 2 ? <span className="agentName" title={s.chat?.agent ? "A conversation keeps the agent it began with; New starts one with another" : undefined}>{AGENT_NAMES[agent] ?? agent}</span>
                : <select className="agentPick" value={agent} onChange={(event) => setChosenAgent(event.target.value)} aria-label="Agent">{agents.map((item) => <option key={item} value={item}>{AGENT_NAMES[item] ?? item}</option>)}</select>}
              {models.length ? (
                <select className="agentPick" value={model} onChange={(event) => setPickedModel((picked) => ({ ...picked, [agent]: event.target.value }))} aria-label="Model"
                  title={models.find((item) => item.id === model)?.detail}>
                  {sessionModel && !models.some((item) => item.id === sessionModel) ? <option value={sessionModel}>{sessionModel}</option> : null}
                  {models.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                </select>
              ) : null}
              {s.chat?.messages.length && !s.runningId ? <button type="button" className="b g" onClick={() => void s.resetChat()}>New</button> : null}
              <button type="button" className="b g" onClick={() => setChatOpen(false)}>Close ✕</button></div>
            <div className="msgs" ref={msgs} onScroll={(event) => { stick.current = atBottom(event.currentTarget); }}>
              {(s.chat?.messages ?? []).map((message) => {
                const detail = s.runs[message.runId];
                return message.role === "user" ? (
                  <div key={`${message.runId}-u`} className="msg me">{message.text}<div className="meta">{clockOf(message.ts)}{message.attached ? ` · ${message.attached} note${message.attached === 1 ? "" : "s"} attached` : ""}</div></div>
                ) : (
                  <div key={`${message.runId}-a`} className="msg ai">
                    {message.text ? <Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_LINKS}>{message.text}</Markdown> : message.status === "running" ? <span className="busy"><span className="sdot" />on it…</span> : <span className="faint">(no reply: the turn {message.status === "cancelled" ? "was stopped" : "failed"})</span>}
                    {detail ? <RunCard detail={detail} events={mergedEvents(detail, s.streamed[message.runId])} runnerBaseUrl={runnerBaseUrl} open={message.status === "running"} onStop={s.stopRun} onAnswer={s.answerApproval} turn /> : null}
                  </div>
                );
              })}
              {s.sending ? <><div className="msg me">{s.sending}</div><div className="msg ai"><span className="busy"><span className="sdot" />on it…</span></div></> : null}
              {/* The turn at work: its reply row exists only once it ends, so until then its run card stands in. */}
              {s.chat?.runningRunId && s.runs[s.chat.runningRunId] && !s.chat.messages.some((message) => message.runId === s.chat?.runningRunId && message.role === "agent") ? (
                <div className="msg ai">
                  <span className="busy"><span className="sdot" />on it…</span>
                  <RunCard detail={s.runs[s.chat.runningRunId]!} events={mergedEvents(s.runs[s.chat.runningRunId]!, s.streamed[s.chat.runningRunId])} runnerBaseUrl={runnerBaseUrl} open onStop={s.stopRun} turn />
                  {approval ? <div className="askTurn"><b>Pilot wants to use {approval.name}</b>{approvalDetail}{approvalActs}</div> : null}
                  {agentAsks.map((item) => (
                    <div key={item.app} className="askTurn"><b>{wants(item)}</b>{item.why ? <p>{item.why}</p> : null}<div className="acts">{answers(item)}</div></div>
                  ))}
                </div>
              ) : null}
              {(s.chat?.messages ?? []).length === 0 && !s.sending ? <p className="faint">Say what you want done. Pilot sees what you have been doing, so a few words are enough.</p> : null}
            </div>
          </div>
        ) : null}
        <label className="chatline">
          <button type="button" className="face" onClick={() => setChatOpen(!chatOpen)} aria-label={chatOpen ? "Hide conversation" : "Show conversation"} />
          <input ref={input} value={draft} disabled={!session || session.paused} placeholder={!session ? "Start a session to talk to Pilot" : session.paused ? "Paused: resume to talk to Pilot" : "Tell Pilot what to do…"}
            onFocus={() => setChatOpen(true)} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void send(); }} />
          {s.runningId && running?.chat ? <button type="button" className="b g" onClick={() => void s.stopRun(running.run.id)}>■ Stop turn</button> : null}
          <button type="button" className="send" disabled={!draft.trim() || !session || session.paused || Boolean(s.runningId)} onClick={() => void send()} aria-label="Send">↑</button>
        </label>
      </div>
      {allowAt ? createPortal(
        <span className="allowMenu" role="menu" ref={allowMenu} style={{ left: allowAt.left, top: allowAt.top }} onMouseLeave={() => setAllowAt(null)}>
          <button type="button" role="menuitem" title="For one step; nothing saved" onClick={() => { setAllowAt(null); answer(allowAt.item.app, allowAt.item.level ?? "use", "once"); }}>Allow once</button>
          <button type="button" role="menuitem" title="For the rest of this task; nothing saved" onClick={() => { setAllowAt(null); answer(allowAt.item.app, allowAt.item.level ?? "use", "task"); }}>Allow for this task</button>
          <button type="button" role="menuitem" title="A use rule in Settings" onClick={() => { setAllowAt(null); answer(allowAt.item.app, allowAt.item.level ?? "use"); }}>Always allow</button>
        </span>, document.body) : null}
    </div>
  );
}
