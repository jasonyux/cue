"use client";

import { useState } from "react";

import { Pad, post } from "./AgentBrowser";

const GONE = "The remote desktop could not be reached. A container built before the viewer role answers busy while Cue works there: rebuild it.";

// The remote desktop on the monitor, between runs: screenshots a few times a second, view only.
export function RemoteMonitor({ runnerBaseUrl, width }: { runnerBaseUrl: string; width: number }) {
  const [gone, setGone] = useState(false);
  if (gone) return <div className="blank">{GONE}</div>;
  // eslint-disable-next-line @next/next/no-img-element
  return <img className="liveBrowser" src={`${runnerBaseUrl}/api/live/session/remote/stream?width=${width}`} alt="The remote desktop" onError={() => setGone(true)} />;
}

// The monitor zoomed in on the remote desktop, where the person's clicks, scrolling and typing go to it.
export function RemoteSheet({ runnerBaseUrl, backend, busy, onClose }: { runnerBaseUrl: string; backend: string; busy: boolean; onClose: () => void }) {
  return (
    <div className="browserSheet remote">
      <div className="hd">
        <b className="who">Remote desktop</b><span className="following">Cue and you share it; your clicks wait for its step</span><span className="sp" />
        <button type="button" className="b g" onClick={onClose}>Close \u2715</button>
      </div>
      <div className={`view${busy ? " busy" : ""}`}>
        <Pad src={`${runnerBaseUrl}/api/live/session/remote/stream?width=1280`} send={(event) => void post(runnerBaseUrl, backend, "/api/live/session/remote/input", event)} onClose={onClose} />
        {busy ? <div className="veil">Cue is acting on the remote desktop<small>your clicks wait until its step is done</small></div> : null}
      </div>
    </div>
  );
}
