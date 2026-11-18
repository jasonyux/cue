"use client";

import type { LiveSettings } from "@cue/contracts";
import { type SyntheticEvent, useState } from "react";

// How often to suggest, more to the right: the proposer's timer, with the idle trigger's cooldown scaled to it (the
// pause itself stays: it detects a stop, not how often to speak). Uniqueness: the duplicate filter's cosine, high = low.
const OFTEN = [
  { intervalS: 600, cooldownS: 120, label: "rare" },
  { intervalS: 300, cooldownS: 60, label: "occasional" },
  { intervalS: 180, cooldownS: 30, label: "regular" },
  { intervalS: 90, cooldownS: 15, label: "frequent" },
];
const FILTER = [
  { threshold: 0.9, label: "low" },
  { threshold: 0.8, label: "medium" },
  { threshold: 0.65, label: "high" },
];
const nearest = (values: number[], value: number) => values.reduce((best, item, index) => (Math.abs(item - value) < Math.abs(values[best]! - value) ? index : best), 0);
// A level is sent when the knob is let go (or a key released), not on every notch a drag crosses, and only when it moved.
const released = (event: SyntheticEvent<HTMLInputElement>, saved: number, send: (index: number) => void) => {
  const index = Number(event.currentTarget.value);
  if (index !== saved) send(index);
};

type Props = { settings: LiveSettings | null; busy: boolean; onTune: (values: Record<string, number>) => void };

// Two sliders in the top bar, for the running session and as the default. Each input is uncontrolled and keyed by the
// saved level, so it starts over from the saved value whenever that changes. A value set by hand that is none of the
// levels reads "custom" at the nearest notch; a move goes to a level.
export function Tune({ settings, busy, onTune }: Props) {
  const [open, setOpen] = useState(false);
  if (!settings) return null;
  const often = nearest(OFTEN.map((level) => level.intervalS), settings.proposerIntervalS);
  const filter = nearest(FILTER.map((level) => level.threshold), settings.proposerDedupThreshold);
  const oftenLabel = OFTEN[often]!.intervalS === settings.proposerIntervalS ? OFTEN[often]!.label : "custom";
  const filterLabel = FILTER[filter]!.threshold === settings.proposerDedupThreshold ? FILTER[filter]!.label : "custom";
  const pickOften = (index: number) => onTune({ proposerIntervalS: OFTEN[index]!.intervalS, proposerIdleCooldownS: OFTEN[index]!.cooldownS });
  const pickFilter = (index: number) => onTune({ proposerDedupThreshold: FILTER[index]!.threshold });
  return (
    <span className="tune">
      <button type="button" className="icon" aria-label="Tune" title="Tune" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
          <path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4" />
        </svg>
      </button>
      {open ? (
        <div className="panel">
          <h4>Suggestions</h4>
          <label>
            <span>Frequency <b>{oftenLabel}</b></span>
            <input key={often} type="range" min={0} max={OFTEN.length - 1} step={1} defaultValue={often} disabled={busy}
              onPointerUp={(event) => released(event, often, pickOften)} onKeyUp={(event) => released(event, often, pickOften)} />
          </label>
          {settings.proposerDedup ? (
            <label>
              <span>Uniqueness <b>{filterLabel}</b></span>
              <input key={filter} type="range" min={0} max={FILTER.length - 1} step={1} defaultValue={filter} disabled={busy}
                onPointerUp={(event) => released(event, filter, pickFilter)} onKeyUp={(event) => released(event, filter, pickFilter)} />
            </label>
          ) : null}
        </div>
      ) : null}
    </span>
  );
}
