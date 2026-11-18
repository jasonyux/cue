"use client";

import { useEffect, useRef, useState } from "react";


import { callRunner, rawJson } from "../live/shared";

type Tab = { id: string; title: string; url: string; agent: boolean }; // agent: the tab the agent's last step was on
type Browser = { endpoint: string; viewport: { width: number; height: number }; tabs: Tab[] }; // the session's agentBrowser
type Props = { runnerBaseUrl: string; backend: string; browser: Browser; busy: boolean; onClose: () => void };

const NAMED_KEYS = new Set(["Enter", "Backspace", "Tab", "Escape", "Delete", "Home", "End", "PageUp", "PageDown", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Insert"]);

export const post = (runnerBaseUrl: string, backend: string, path: string, body: object) =>
  callRunner(runnerBaseUrl, backend, path, rawJson, { method: "POST", body: JSON.stringify(body) }).catch(() => undefined);

// A live stream the person acts on: pointer events as fractions of the frame, keys as a browser names them (a single
// character is typed as text; a named key or a combination, "Meta+a", is pressed), Escape closes. The box takes the
// frame's own proportions once the first one shows, so a screen of any shape is shown whole, never stretched.
export function Pad({ src, send, onClose }: { src: string; send: (event: object) => void; onClose: () => void }) {
  const pad = useRef<HTMLDivElement>(null);
  const lastMove = useRef(0);
  const [ratio, setRatio] = useState<number | null>(null);
  useEffect(() => { pad.current?.focus(); setRatio(null); }, [src]);
  const at = (event: React.MouseEvent) => {
    const r = event.currentTarget.getBoundingClientRect();
    return { x: (event.clientX - r.left) / r.width, y: (event.clientY - r.top) / r.height };
  };
  const button = (event: React.MouseEvent) => (event.button === 2 ? "right" : event.button === 1 ? "middle" : "left");
  const key = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") { onClose(); return; }
    event.preventDefault();
    const mods = [event.metaKey && "Meta", event.ctrlKey && "Control", event.altKey && "Alt"].filter(Boolean) as string[];
    if (event.key.length === 1 && mods.length === 0) { send({ kind: "text", text: event.key }); return; }
    if (event.key.length !== 1 && !NAMED_KEYS.has(event.key) && !/^F\d+$/.test(event.key)) return; // a bare modifier, or something the page cannot take
    const named = event.key === " " ? "Space" : event.key;
    send({ kind: "key", key: [...mods, ...(event.shiftKey && event.key.length !== 1 ? ["Shift"] : []), named].join("+") });
  };
  return (
    <div ref={pad} className="pad" tabIndex={0} style={ratio ? { aspectRatio: String(ratio), width: `min(100%, calc((100vh - 100px) * ${ratio}))` } : undefined}
      onKeyDown={key} onContextMenu={(event) => event.preventDefault()}
      onPointerMove={(event) => { const now = Date.now(); if (now - lastMove.current > 40) { lastMove.current = now; send({ kind: "move", ...at(event) }); } }}
      onPointerDown={(event) => { event.preventDefault(); pad.current?.focus(); send({ kind: event.button === 0 ? "down" : "click", ...at(event), button: button(event), clicks: event.detail || 1 }); }}
      onPointerUp={(event) => { if (event.button === 0) send({ kind: "up", ...at(event), button: "left" }); }}
      onWheel={(event) => send({ kind: "wheel", ...at(event), dx: event.deltaX, dy: event.deltaY })}>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img key={src} src={src} alt="The live view" draggable={false}
        onLoad={(event) => { const { naturalWidth, naturalHeight } = event.currentTarget; if (naturalWidth && naturalHeight) setRatio(naturalWidth / naturalHeight); }} />
    </div>
  );
}

// The agent browser on the monitor: one tab as video, the one the agent is on. View only.
export function BrowserMonitor({ runnerBaseUrl, width }: { runnerBaseUrl: string; width: number }) {
  // eslint-disable-next-line @next/next/no-img-element
  return <img className="liveBrowser" src={`${runnerBaseUrl}/api/live/session/browser/stream?width=${width}`} alt="The agent's browser" />;
}

// The monitor zoomed in: the agent browser at full size, where the person can click, scroll and type on the pages the
// agent works in, and switch, open or close tabs. It follows the agent's tab until a tab is picked by hand.
export function BrowserSheet({ runnerBaseUrl, backend, browser, busy, onClose }: Props) {
  const [pinned, setPinned] = useState<string | null>(null);
  const tab = pinned && browser.tabs.some((item) => item.id === pinned) ? pinned : null; // a closed tab unpins
  const act = (path: string, body: object) => void post(runnerBaseUrl, backend, path, body);
  const active = tab ?? browser.tabs.find((item) => item.agent)?.id ?? browser.tabs[browser.tabs.length - 1]?.id;
  return (
    <div className="browserSheet">
      <div className="hd">
        <b className="who">Cue&apos;s browser</b>
        <button type="button" className="nav" aria-label="Back" title="Back" onClick={() => act("/api/live/session/browser/tabs", { action: "back", tab })}>‹</button>
        <button type="button" className="nav" aria-label="Forward" title="Forward" onClick={() => act("/api/live/session/browser/tabs", { action: "forward", tab })}>›</button>
        <div className="tabs">
          {browser.tabs.map((item) => (
            <span key={item.id} className={`tab${item.id === active ? " active" : ""}${item.agent ? " agent" : ""}`} title={item.url}>
              <button type="button" className="pick" onClick={() => setPinned(item.id)}>{item.agent ? <i /> : null}{item.title || item.url.replace(/^https?:\/\//, "").slice(0, 40) || "new tab"}</button>
              <button type="button" className="x" aria-label="Close tab" onClick={() => act("/api/live/session/browser/tabs", { action: "close", tab: item.id })}>×</button>
            </span>
          ))}
          <button type="button" className="plus" aria-label="New tab" onClick={() => act("/api/live/session/browser/tabs", { action: "new" })}>+</button>
        </div>
        {tab ? <button type="button" className="b g" onClick={() => setPinned(null)}>Follow agent</button> : <span className="following">following the agent</span>}
        <button type="button" className="b g" onClick={onClose}>Close ✕</button>
      </div>
      <div className={`view${busy ? " busy" : ""}`}>
        <Pad src={`${runnerBaseUrl}/api/live/session/browser/stream?width=1280${tab ? `&tab=${encodeURIComponent(tab)}` : ""}`}
          send={(event) => act("/api/live/session/browser/input", { tab, ...event })} onClose={onClose} />
        {busy ? <div className="veil">Cue is acting on the page<small>your clicks wait until its step is done</small></div> : null}
      </div>
    </div>
  );
}
