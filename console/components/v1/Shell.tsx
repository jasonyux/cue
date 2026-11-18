"use client";

import Link from "next/link";
import { type ReactNode, useEffect, useState } from "react";

import { clockOf } from "../live/shared";
import { Brand } from "./Brand";
import { readOverrides } from "./overrides";
import { ThemePicker } from "./theme";
import { Tune } from "./Tune";
import type { Session } from "./useSession";

const glyph = (shape: ReactNode) => <svg viewBox="0 0 24 24" aria-hidden="true">{shape}</svg>;
const PLAY = glyph(<path d="M8 5v14l11-7z" />);
const PAUSE = glyph(<path d="M7 5h4v14H7zM13 5h4v14h-4z" />);
const STOP = glyph(<rect x="6" y="6" width="12" height="12" rx="2" />);

function timeLeft(expiresAt: string | null | undefined, now: number): string {
  if (!expiresAt) return "no time limit";
  const minutes = Math.max(0, Math.round((Date.parse(expiresAt) - now) / 60_000));
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} m left` : `${minutes} m left`;
}

// The session's controls, beside its state: start one when none runs; pause or resume it, and stop it.
function Controls({ s }: { s: Session }) {
  const session = s.session;
  const stopping = Boolean(session?.stopReason);
  if (!session) return <span className="ctl"><button type="button" className="b" disabled={s.busy || !s.status} onClick={() => void s.startSession({ ...readOverrides() })}>{PLAY}Start</button></span>;
  return (
    <span className="ctl">
      <button type="button" className="b" disabled={s.busy || stopping} onClick={() => void s.pauseSession(session.paused)}>{session.paused ? PLAY : PAUSE}{session.paused ? "Resume" : "Pause"}</button>
      <button type="button" className="b stop" disabled={s.busy || stopping} onClick={() => void s.stopSession()}>{STOP}{stopping ? "Stopping…" : "Stop"}</button>
    </span>
  );
}

const PAGES = [["/v1/notes", "Notes"], ["/v1/history", "History"], ["/v1/usage", "Usage"]] as const;

// The bar on top of every /v1 page, as Timeline v2 draws it: the session's state and controls, the page's own buttons
// (children; a page that has none gets Home, the way back to the landing page's home view), the pages, the session's
// tuning, the settings and the theme: the same on every page. ``current`` names the page shown, ``needs`` says a run
// is waiting for an answer.
export function TopBar({ s, current, needs = false, children }: { s: Session; current?: string; needs?: boolean; children?: ReactNode }) {
  const session = s.session;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(timer); }, []);
  const running = s.runningId ? s.runs[s.runningId] ?? null : null;
  const waiting = needs || Boolean(session?.permissionAsks?.length);
  return (
    <header className="top">
      <Brand />
      {session ? (
        <span className="status">
          <i className={`dot${session.paused ? " off" : waiting ? " amber" : ""}`} />
          {session.paused ? "Paused" : waiting ? "Needs you" : running ? (running.chat ? "Running · the conversation" : `Running · ${running.run.prompt}`) : "Watching your screen"}
          <span className="sep">·</span>
          {session.paused && session.pausedAt ? `since ${clockOf(session.pausedAt)}` : timeLeft(session.expiresAt, now)}
        </span>
      ) : <span className="status"><i className="dot off" />No session</span>}
      <Controls s={s} />
      <span className="spacer" />
      {children ?? <><Link className="b" href="/v1" onClick={() => void s.browseChat(null)}>Home</Link><i className="vsep" /></>}
      <nav className="pages" aria-label="Pages">{PAGES.map(([href, label]) => <Link key={href} className="link" href={href} aria-current={label === current ? "page" : undefined}>{label}</Link>)}</nav>
      <i className="vsep" />
      <Tune settings={session?.settings ?? s.status?.defaults ?? null} busy={s.busy} onTune={(values) => void s.tune(values)} />
      <Link className="icon" href="/v1/settings" aria-label="Settings" aria-current={current === "Settings" ? "page" : undefined}>⚙</Link>
      <ThemePicker />
    </header>
  );
}

// A grey block in the shape of something still loading: a line of text unless given a height.
export function Skel({ w, h }: { w: number | string; h?: number }) {
  return <i className="skel" style={{ width: w, height: h }} />;
}

export function Banners({ s }: { s: Session }) {
  return (
    <>
      {s.waiting ? <div className="wait" role="status"><span className="dot" />{s.waiting}</div> : null}
      {s.issue ? <div className="issue">{s.issue}</div> : null}
      {s.session?.lastError ? <div className="issue">Last error: {s.session.lastError}</div> : null}
    </>
  );
}
