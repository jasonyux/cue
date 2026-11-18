"use client";

import type { LiveSessionStatus, LiveSettings, RunDetail, RunEvent } from "@cue/contracts";
import Link from "next/link";
import { useState, type ReactNode } from "react";

import { blockedWhy, clockOf, mergedEvents, runState, turnsOf } from "../live/shared";
import { readOverrides } from "./overrides";
import type { SessionList } from "./useSession";

type LiveSession = NonNullable<LiveSessionStatus["session"]>;

type Props = {
  session: LiveSession | null;
  defaults: LiveSettings | null;
  lastSession: SessionList[number] | null;
  running: RunDetail | null; // the session's run in progress, if any: the band shows its view instead
  streamed: RunEvent[];
  runnerBaseUrl: string;
  busy: boolean;
  onStart: (overrides: Record<string, unknown>) => Promise<void>;
  onStopRun: (runId: string) => Promise<void>;
  onMonitor: () => void; // enlarge the monitor in the band's place
  children: ReactNode; // the permission questions and the Noticed card
};

// The band across the top: your screen as the observer last saw it, or the agent's own view while a run is on, or
// the start card when there is no session; and, on the right, what it noticed.
export function Band({ session, defaults, lastSession, running, streamed, runnerBaseUrl, busy, onStart, onStopRun, onMonitor, children }: Props) {
  const [mine, setMine] = useState(false); // while a run is on: show your screen instead of the agent's view
  return (
    <section className="band">
      {session ? (running && !mine ? <AgentView detail={running} streamed={streamed} runnerBaseUrl={runnerBaseUrl} onMine={() => setMine(true)} onStop={onStopRun} onMonitor={onMonitor} />
        : <Observer session={session} runnerBaseUrl={runnerBaseUrl} agentRunning={running !== null} onAgent={() => setMine(false)} onMonitor={onMonitor} />)
        : <StartCard defaults={defaults} lastSession={lastSession} busy={busy} onStart={onStart} />}
      {session ? children : null}
    </section>
  );
}

// Your screen as the observer last saw it: the screenshot with its time, or the blurred glimpse of a blocked app under a veil.
export function YourScreen({ session, runnerBaseUrl }: { session: LiveSession; runnerBaseUrl: string }) {
  const look = session.latestSnapshot;
  const shot = session.latestScreenshot;
  return (
    <>
      {look?.blocked ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img className="glimpse" src={`${runnerBaseUrl}/api/live/session/screen/blocked?t=${encodeURIComponent(look.ts ?? "")}`} alt={`${look.app}, blurred`} />
      ) : shot ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={`${runnerBaseUrl}/api/live/session/screenshots/${encodeURIComponent(shot.name)}`} alt={`${shot.app} — ${shot.window}`} />
      ) : <span className="blank">{look ? "no screenshot yet" : "first look coming"}</span>}
      {look?.blocked ? <span className="veil">Not watching {look.app}</span> : shot ? <span className="stamp">{clockOf(shot.ts)}</span> : null}
    </>
  );
}

function Observer({ session, runnerBaseUrl, agentRunning, onAgent, onMonitor }: { session: LiveSession; runnerBaseUrl: string; agentRunning: boolean; onAgent: () => void; onMonitor: () => void }) {
  const look = session.latestSnapshot;
  return (
    <>
      <div className={`thumb${session.paused ? " paused" : ""}`}>
        <YourScreen session={session} runnerBaseUrl={runnerBaseUrl} />
        <button type="button" className="enlarge" aria-label="Enlarge the monitor" title="Enlarge the monitor" onClick={onMonitor}>⤢</button>
      </div>
      <div className="seen">
        <div className="label"><span className={`dot${session.paused || !session.observerAlive ? " off" : ""}`} />{session.paused ? "Paused" : session.observerAlive ? "On your screen" : "Observer stopped"}</div>
        <h2>{look ? `${look.app ?? ""}${look.window ? ` — ${look.window}` : ""}` : "Nothing seen yet"}</h2>
        <p>{look?.blocked ? blockedWhy(look.blocked) : look?.visible?.length ? `Also open: ${look.visible.join(" · ")}` : look ? `Last look ${clockOf(look.ts)}` : "The first note comes about a minute after the first look."}</p>
        {agentRunning ? <div className="seg"><button type="button" onClick={onAgent}>Agent's view</button><button type="button" className="on">Your screen</button></div> : null}
      </div>
    </>
  );
}

function AgentView({ detail, streamed, runnerBaseUrl, onMine, onStop, onMonitor }: { detail: RunDetail; streamed: RunEvent[]; runnerBaseUrl: string; onMine: () => void; onStop: (runId: string) => Promise<void>; onMonitor: () => void }) {
  const shots = (detail.browser?.screenshots ?? []).filter((shot) => !shot.placeholder);
  const last = shots[shots.length - 1];
  const turns = turnsOf(mergedEvents(detail, streamed));
  return (
    <>
      <div className="thumb">
        {last ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={`${runnerBaseUrl}${last.url}`} alt={last.label} />
        ) : <span className="blank">no screenshot yet</span>}
        <span className="tag">AGENT</span>
        {last ? <span className="stamp">{last.label}</span> : null}
        <button type="button" className="enlarge" aria-label="Enlarge the monitor" title="Enlarge the monitor" onClick={onMonitor}>⤢</button>
      </div>
      <div className="seen">
        <div className="label"><span className="dot" />Agent working<button type="button" className="btn sm ghost" onClick={() => void onStop(detail.run.id)}>Stop turn</button></div>
        <h2>{detail.chat ? `Turn ${detail.chat.turn} of the conversation` : detail.run.prompt}</h2>
        <p>{runState(turns, true)}</p>
        <div className="seg"><button type="button" className="on">Agent's view</button><button type="button" onClick={onMine}>Your screen</button></div>
      </div>
    </>
  );
}

function StartCard({ defaults, lastSession, busy, onStart }: { defaults: LiveSettings | null; lastSession: SessionList[number] | null; busy: boolean; onStart: (overrides: Record<string, unknown>) => Promise<void> }) {
  const [minutes, setMinutes] = useState<string | null>(null);
  const length = minutes ?? String(defaults?.maxSessionMinutes ?? 120);
  const cost = lastSession?.usage?.costUsd;
  return (
    <>
      <div className="thumb"><span className="blank">no session</span></div>
      <div className="seen">
        <div className="label"><span className="dot off" />Not looking at your screen</div>
        <h2>Start a session to get suggestions and talk to the agent</h2>
        <p>{lastSession ? `Last session: ${clockOf(lastSession.startedAt)} – ${clockOf(lastSession.stoppedAt)}${lastSession.stopReason ? `, ${lastSession.stopReason}` : ""}${cost !== undefined ? ` · $${cost.toFixed(2)}` : ""} · ${lastSession.runs.length} run${lastSession.runs.length === 1 ? "" : "s"}` : "No session stored yet."}</p>
      </div>
      <form className="start" onSubmit={(event) => { event.preventDefault(); void onStart({ ...readOverrides(), maxSessionMinutes: Number(length) || 0 }); }}>
        <label>Session length (minutes, 0 = no limit)<input type="number" min={0} value={length} onChange={(event) => setMinutes(event.target.value)} /></label>
        <div className="go"><button type="submit" className="btn primary" disabled={busy || !defaults}>▶ Start session</button><small className="muted">{Object.keys(readOverrides()).length ? `${Object.keys(readOverrides()).length} settings changed in ` : "Everything else is in "}<Link href="/v1/settings">Settings</Link>.</small></div>
      </form>
    </>
  );
}
