"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { type Ask, clockOf, mergedEvents, proposalsOf, wants } from "../live/shared";
import { useAgentChoice } from "./agentChoice";
import { Band, YourScreen } from "./Band";
import { Brand } from "./Brand";
import { Conversation } from "./Conversation";
import { Monitor } from "./Monitor";
import { Noticed } from "./Noticed";
import { TaskColumn } from "./TaskColumn";
import { useSession } from "./useSession";

type Props = { expectedBackend: "python"; runnerBaseUrl: string; switcher?: ReactNode }; // switcher: the landing page's theme switch

const TASKS_MIN = 400; // the task column's width by default and its least; dragging its edge widens it, up to 60% of the window

function timeLeft(expiresAt: string | null | undefined, now: number): string {
  if (!expiresAt) return "no time limit";
  const minutes = Math.max(0, Math.round((Date.parse(expiresAt) - now) / 60_000));
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} m left` : `${minutes} m left`;
}

function dollars(value: number | null): string {
  return value === null ? "—" : `$${value.toFixed(2)}`;
}

export function Pilot({ expectedBackend, runnerBaseUrl, switcher }: Props) {
  const s = useSession(runnerBaseUrl, expectedBackend);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { // the time left counts down between polls
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);
  const session = s.session;
  const stopping = Boolean(session?.stopReason);
  const running = s.runningId ? s.runs[s.runningId] ?? null : null;
  const followed = running ?? (s.chat?.messages.length ? s.runs[s.chat.messages[s.chat.messages.length - 1]!.runId] ?? null : null); // the run the monitor follows: the one in flight, else the conversation's latest
  const followedEvents = useMemo(() => followed ? mergedEvents(followed, s.streamed[followed.run.id]) : [], [followed, s.streamed]);
  const [monitor, setMonitor] = useState(false); // the monitor enlarged in the band's place
  const [monitorHeight, setMonitorHeight] = useState<number | null>(null); // dragged taller than its default (the stylesheet keeps it within its bounds)
  const { agents, agent, setAgent, models, model, sessionModel, setModel } = useAgentChoice(session?.settings ?? s.status?.defaults ?? null, s.chat?.agent, runnerBaseUrl, expectedBackend); // before a session: the Settings page's agents, the pick carried into it
  const look = session?.latestSnapshot;
  const mine = session ? { view: <YourScreen session={session} runnerBaseUrl={runnerBaseUrl} />, name: `Your screen${look ? ` · ${look.app ?? ""}${look.window ? ` — ${look.window}` : ""}` : ""}`,
                           state: session.paused ? "○ Paused" : session.observerAlive ? "● Watching" : "○ Observer stopped" } : undefined; // the enlarged monitor's view of your screen
  const asks = session?.permissionAsks ?? [];
  const crowded = asks.length > 0 && s.proposalSets.some((item) => !item.error && proposalsOf(item).length > 0); // a question and a suggestion share the band: both fold their detail behind hover
  const [tasksCollapsed, setTasksCollapsed] = useState(false);
  const [taskWidth, setTaskWidth] = useState(TASKS_MIN);
  useEffect(() => { // the width the person dragged it to last time
    try { setTaskWidth(Math.max(TASKS_MIN, Number(window.localStorage.getItem("v1.taskWidth")) || TASKS_MIN)); } catch { /* storage blocked: the default */ }
  }, []);
  const resize = (width: number) => {
    const next = Math.round(Math.min(Math.max(width, TASKS_MIN), window.innerWidth * 0.6));
    setTaskWidth(next);
    try { window.localStorage.setItem("v1.taskWidth", String(next)); } catch { /* storage blocked: this visit only */ }
  };

  return (
    <div className="v1">
      <header>
        <Brand />
        {session ? (
          <>
            <span className="stat"><span className={`dot${session.paused ? " off" : ""}`} /><b>{stopping ? "Stopping" : session.paused ? "Paused" : "Session running"}</b>{session.paused && session.pausedAt ? ` since ${clockOf(session.pausedAt)}` : ` · ${timeLeft(session.expiresAt, now)}`}</span>
            <span className="stat">Session: <b>{dollars(s.sessionCost)}</b> · {session.runs.length} run{session.runs.length === 1 ? "" : "s"}</span>
            <button type="button" className="btn sm" disabled={s.busy || stopping} onClick={() => void s.pauseSession(session.paused)}>{session.paused ? "▶ Resume" : "⏸ Pause"}</button>
            <button type="button" className="btn sm" disabled={s.busy || stopping} onClick={() => void s.stopSession()}>{stopping ? "Stopping…" : "■ Stop"}</button>
          </>
        ) : (
          <span className="stat"><b>No session</b>{s.sessions[0]?.stoppedAt ? ` · last one ended ${clockOf(s.sessions[0].stoppedAt)}` : ""}</span>
        )}
        <span className="spacer" />
        <Link className="btn sm ghost" href="/v1/temporary">Temporary runs</Link>
        <Link className="btn sm ghost" href="/v1/notes">Notes</Link>
        <Link className="btn sm ghost" href="/v1/history">History</Link>
        <Link className="btn sm ghost" href="/v1/usage">Usage</Link>
        <Link className="btn sm ghost" href="/v1/settings">Settings</Link>
        {switcher}
      </header>
      {s.waiting ? <div className="wait" role="status"><span className="dot" />{s.waiting}</div> : null}
      {s.issue ? <div className="issue">{s.issue}</div> : null}
      {session?.lastError ? <div className="issue">Last error: {session.lastError}</div> : null}

      {monitor && session ? (
        <section className="monitor" style={monitorHeight ? { height: monitorHeight } : undefined}>
          <Monitor session={session} running={running} followed={followed} events={followedEvents} runnerBaseUrl={runnerBaseUrl} backend={expectedBackend} width={1200} screen={mine}>
            <button type="button" onClick={() => setMonitor(false)}>Minimize</button>
          </Monitor>
          <div className="grab" title="Drag to resize" onPointerDown={(event) => { // the bottom edge follows the pointer
            event.preventDefault();
            const top = event.currentTarget.parentElement!.getBoundingClientRect().top;
            const move = (moved: PointerEvent) => setMonitorHeight(Math.round(moved.clientY - top));
            const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
            window.addEventListener("pointermove", move);
            window.addEventListener("pointerup", up);
          }} />
        </section>
      ) : (
        <Band session={session} defaults={s.status?.defaults ?? null} lastSession={s.sessions[0] ?? null} running={running} streamed={running ? s.streamed[running.run.id] ?? [] : []}
          runnerBaseUrl={runnerBaseUrl} busy={s.busy} onStart={s.startSession} onStopRun={s.stopRun} onMonitor={() => setMonitor(true)}>
          <div className="stack">
            {asks.map((item) => <AskCard key={`${item.app}|${item.source ?? ""}`} item={item} busy={s.busy} compact={crowded} onAnswer={s.answerPermission} />)}
            <Noticed proposalSets={session ? s.proposalSets : []} reasons={s.status?.rejectReasons ?? []} runs={s.runs} busy={s.busy || !session || Boolean(session?.paused)} running={s.runningId !== null} compact={crowded}
              onDecide={s.decide} onReason={s.giveReason} onChat={s.acceptToChat} onSend={(item, choice) => s.acceptAndSend(item, choice, agent, model)} />
          </div>
        </Band>
      )}

      <div className={`main${tasksCollapsed ? " collapsed" : ""}`} style={tasksCollapsed ? undefined : { gridTemplateColumns: `minmax(0,1fr) ${taskWidth}px` }}>
        <Conversation chat={s.chat} browsing={s.browsing} runs={s.runs} streamed={s.streamed} runnerBaseUrl={runnerBaseUrl} seed={s.chatSeed}
          agents={agents} agent={agent} onAgent={setAgent} models={models} model={model} sessionModel={sessionModel} onModel={setModel} paused={Boolean(session?.paused)} busy={!session || s.runningId !== null || s.busy} runningId={s.runningId} sending={s.sending}
          onSend={s.sendChat} onStop={s.stopRun} onAnswer={s.answerApproval} onReset={s.resetChat} onBrowse={s.browseChat} />
        <TaskColumn tasks={s.status?.tasks ?? []} trash={s.status?.trash ?? []} flags={s.status?.taskFlags ?? []} change={s.changes[s.changes.length - 1] ?? null}
          busy={s.busy} collapsed={tasksCollapsed} onCollapse={setTasksCollapsed} onResize={resize} hideDays={session?.settings.beliefDoneStepDays ?? s.status?.defaults.beliefDoneStepDays ?? 21}
          onEdit={s.editTasks} onFlag={{ flag: s.flagTask, reason: s.flagReason, unflag: s.unflag, busy: s.busy }} />
      </div>
    </div>
  );
}

// A permission question in the band: an app looked at under ask (Look / Use / This session / Block), or one the agent needs
// for the task (Allow once / For this task / Always / Deny), answered as in Timeline v2. Compact, next to a suggestion: one line, the
// question and its buttons, with what the agent needs it for on hover.
function AskCard({ item, busy, compact, onAnswer }: { item: Ask; busy: boolean; compact: boolean; onAnswer: (app: string, level: "block" | "look" | "use" | "deny", scope?: "session" | "once" | "task") => Promise<void> }) {
  const level = item.level ?? "use";
  return (
    <div className={`ask${compact ? " compact" : ""}`} title={compact ? item.why : undefined}>
      {compact ? null : <div className="kind">{item.source === "agent" ? "For the task" : "Permission"} · {clockOf(item.ts)}</div>}
      <p><b>{wants(item)}</b></p>
      {item.why && !compact ? <p className="why">{item.why}</p> : null}
      <div className="actions">
        {item.source === "agent" ? <>
          <button type="button" className="btn primary sm" disabled={busy} title="For one step; nothing saved" onClick={() => void onAnswer(item.app, level, "once")}>Allow once</button>
          <button type="button" className="btn sm" disabled={busy} title="For the rest of this task; nothing saved" onClick={() => void onAnswer(item.app, level, "task")}>For this task</button>
          <button type="button" className="btn sm" disabled={busy} title="A use rule in Settings" onClick={() => void onAnswer(item.app, level)}>Always</button>
          <button type="button" className="btn sm ghost" disabled={busy} onClick={() => void onAnswer(item.app, "deny")}>Deny</button>
        </> : <>
          <button type="button" className="btn primary sm" disabled={busy} onClick={() => void onAnswer(item.app, "look")}>Look</button>
          <button type="button" className="btn sm" disabled={busy} onClick={() => void onAnswer(item.app, "use")}>Use</button>
          <button type="button" className="btn sm" disabled={busy} title="Look until this session ends; nothing saved" onClick={() => void onAnswer(item.app, "look", "session")}>This session</button>
          <button type="button" className="btn sm ghost" disabled={busy} onClick={() => void onAnswer(item.app, "block")}>Block</button>
        </>}
      </div>
    </div>
  );
}
