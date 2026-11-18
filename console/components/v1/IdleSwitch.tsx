"use client";

import type { LiveIdleTrigger } from "@cue/contracts";
import { useState } from "react";

export const IDLE_S_RANGE = [3, 600] as const; // the backend's bounds on the pause (config.py IDLE_S_RANGE)

const STATES: Record<LiveIdleTrigger["state"], string> = {
  off: "", armed: "watching for a pause", firing: "looking…", shown: "waiting for you", away: "you seem away",
};

type Props = {
  trigger: LiveIdleTrigger | null; // the running session's, for its state
  enabled: boolean;
  idleS: number;
  busy: boolean;
  onChange: (enabled: boolean, idleS: number) => void;
};

// The idle trigger's switch in the menu: on or off and the pause, for the running session and as the default.
// Rendered with key={idleS}, so the seconds field starts over from the saved value whenever that changes.
export function IdleSwitch({ trigger, enabled, idleS, busy, onChange }: Props) {
  const [draft, setDraft] = useState(String(idleS));
  const commit = () => {
    const seconds = Number(draft);
    if (Number.isFinite(seconds) && seconds >= IDLE_S_RANGE[0] && seconds <= IDLE_S_RANGE[1] && seconds !== idleS) onChange(enabled, seconds);
    else setDraft(String(idleS));
  };
  const state = enabled && trigger ? STATES[trigger.state] : "";
  return (
    <div className="menuRow idle">
      <label title="One quick suggestion on the screen in front when you stop typing and moving the mouse for a while">
        <input type="checkbox" checked={enabled} disabled={busy} onChange={(event) => onChange(event.target.checked, idleS)} /> Suggest when I pause
      </label>
      <label className="after">
        after <input type="number" min={IDLE_S_RANGE[0]} max={IDLE_S_RANGE[1]} step={1} value={draft} disabled={busy} aria-label="Seconds of pause"
          onChange={(event) => setDraft(event.target.value)} onBlur={commit} onKeyDown={(event) => { if (event.key === "Enter") commit(); }} /> s
        {state ? <small> · {state}</small> : null}
      </label>
    </div>
  );
}
