"use client";

import type { LiveSessionStatus, RunDetail, RunEvent } from "@cue/contracts";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { toolRunning, turnsOf } from "../live/shared";
import { BrowserMonitor, BrowserSheet } from "./AgentBrowser";
import { NowScreen } from "./NowScreen";
import { RemoteMonitor, RemoteSheet } from "./RemoteDesktop";

type LiveSession = NonNullable<LiveSessionStatus["session"]>;
type Props = {
  session: LiveSession | null; // none while an earlier conversation is read with no session running: the pane then only shows what its runs left
  running: RunDetail | null; // the session's run in flight
  followed: RunDetail | null; // the run the screen follows: the one in flight, else the conversation's latest
  events: RunEvent[]; // the followed run's events, the streamed ones merged in
  runnerBaseUrl: string;
  backend: string;
  width: number; // of the browser's and the remote desktop's stream
  screen?: { view: ReactNode; name: string; state: string } | undefined; // the person's own screen as the observer sees it: what the pane shows with no run in flight, its first view then Your screen instead of Follow agent
  children?: ReactNode; // more buttons on the title bar
};

export const MONITOR = <svg viewBox="0 0 20 20"><rect x="2.5" y="3.5" width="15" height="10" rx="1.5" fill="none" strokeWidth="1.8" /><line x1="7" y1="16.5" x2="13" y2="16.5" strokeWidth="1.8" strokeLinecap="round" /><line x1="10" y1="13.5" x2="10" y2="16.5" strokeWidth="1.8" /></svg>; // the monitor glyph, as Zen's

// The screen pane with nothing on it: the agent browser's blank first tab, or no run, browser or remote desktop to show.
function Blank({ text }: { text: string }) {
  return <div className="blank">{MONITOR}<span>{text}</span></div>;
}

// The monitor: the agent's screen as it works, Cue's browser or the remote desktop, picked on the title bar or followed
// (the agent's latest step says which); Take control opens the browser or the remote desktop full size (Esc closes it).
// Following stops with the run: a finished run shows its own screen (the person's own, given ``screen``), and with nothing
// running the pane waits for a pick.
export function Monitor({ session, running, followed, events, runnerBaseUrl, backend, width, screen, children }: Props) {
  const [view, setView] = useState<"browser" | "remote" | null>(null); // picked by hand; null follows the agent, or shows the person's screen with no run in flight
  const [browserOpen, setBrowserOpen] = useState(false);
  const [remoteOpen, setRemoteOpen] = useState(false);
  useEffect(() => { setView(null); }, [followed?.run.id]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setBrowserOpen(false); setRemoteOpen(false); } };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);
  const turns = useMemo(() => turnsOf(events), [events]);
  const lastTurn = turns[turns.length - 1];
  const lastCall = lastTurn ? [...lastTurn.events].reverse().find((event) => event.type === "function_call_requested") : undefined;
  const browser = session?.agentBrowser ?? null;
  const agentOnBrowser = Boolean(browser?.tabs.length) && Boolean(running) && Boolean(lastCall?.detail?.startsWith("mcp__browser__"));
  const showingBrowser = view === null ? agentOnBrowser : view === "browser"; // picked, it shows even before a run opened the browser (a placeholder then)
  const agentTab = browser?.tabs.find((tab) => tab.agent);
  const blankTab = !(agentTab ?? browser?.tabs[browser.tabs.length - 1])?.url.startsWith("http"); // the streamed tab (the agent's, else the newest) is Chromium's blank page
  const remoteAvailable = session?.settings.actorDesktopSource.includes("remote") ?? false;
  const agentOnRemote = remoteAvailable && Boolean(running) && Boolean(lastCall?.detail?.startsWith("mcp__desktop__exec_py") && lastCall.detail.includes('"target": "remote"'));
  const showingRemote = remoteAvailable && (view === null ? agentOnRemote : view === "remote");
  const showingMine = screen !== undefined && view === null && !running;
  const takeControl = showingBrowser && browser && !browserOpen ? () => setBrowserOpen(true) : showingRemote && !remoteOpen ? () => setRemoteOpen(true) : null; // the browser's, once a run opened it
  const agentBusy = Boolean(lastTurn && toolRunning(lastTurn));
  const name = showingBrowser ? `Cue's browser${agentTab && !blankTab ? ` · ${agentTab.title || agentTab.url}` : ""}` : showingRemote ? "Remote desktop" : showingMine ? screen!.name : "Agent";
  const state = showingMine ? screen!.state : showingBrowser || showingRemote || running ? "● Live" : followed ? "○ Done" : "○ Idle"; // the browser and the remote desktop are always live; a run's own screen only while it runs
  return (
    <div className="screen">
      <header className="chrome"><span className="dots"><i /><i /><i /><span>{state} · {name}</span>
        {takeControl ? <button type="button" className="take" onClick={takeControl}>Take control</button> : null}</span>
        <span className="tools">
          <span className="views" role="group" aria-label="Screen view">
            <button type="button" aria-pressed={view === null} onClick={() => setView(null)}>{screen && !running ? "Your screen" : "Follow agent"}</button>
            <button type="button" aria-pressed={view === "browser"} onClick={() => setView("browser")}>Cue&apos;s browser</button>
            {remoteAvailable ? <button type="button" aria-pressed={view === "remote"} onClick={() => setView("remote")}>Remote desktop</button> : null}
          </span>
          {children}
        </span></header>
      <div className="viewport">
        {showingMine ? screen!.view : showingBrowser ? (blankTab ? <Blank text="No page open yet" /> : <BrowserMonitor runnerBaseUrl={runnerBaseUrl} width={width} />) : showingRemote ? <RemoteMonitor runnerBaseUrl={runnerBaseUrl} width={width} />
          : followed ? <NowScreen detail={followed} events={events} runnerBaseUrl={runnerBaseUrl} /> : <Blank text="Nothing to show yet" />}
      </div>
      {remoteOpen ? <RemoteSheet runnerBaseUrl={runnerBaseUrl} backend={backend} busy={agentBusy} onClose={() => setRemoteOpen(false)} /> : null}
      {browserOpen && browser ? <BrowserSheet runnerBaseUrl={runnerBaseUrl} backend={backend} browser={browser} busy={agentBusy} onClose={() => setBrowserOpen(false)} /> : null}
    </div>
  );
}
