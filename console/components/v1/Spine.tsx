"use client";

import type { LiveChatMessage, LiveProposalSet, LiveScreenshot, LiveSummary, LiveTaskChange, RunDetail } from "@cue/contracts";
import { useEffect, useRef } from "react";

import { AGENT_LABELS, clockOf, decisionLabel, proposalsOf } from "../live/shared";

// One record of the session as a mark on the spine: ``id`` names it, ``ts`` places it, and a note, a task change and a
// suggestion also carry the start of the window they read (``from``), shaded when the mark is selected.
export type Mark =
  | { kind: "look"; id: string; ts: string; shot: LiveScreenshot }
  | { kind: "note"; id: string; ts: string; from: string; note: LiveSummary }
  | { kind: "change"; id: string; ts: string; from: string; change: LiveTaskChange }
  | { kind: "suggest"; id: string; ts: string; from: string | null; set: LiveProposalSet; pending: boolean }
  | { kind: "check"; id: string; ts: string; set: LiveProposalSet } // a pause that was looked at and found nothing worth suggesting
  | { kind: "answer"; id: string; ts: string; set: LiveProposalSet }
  | { kind: "say"; id: string; ts: string; message: LiveChatMessage }
  | { kind: "run"; id: string; ts: string; end: string | null; detail: RunDetail }
  | { kind: "ask"; id: string; ts: string; title: string; pending: boolean; answer?: string | undefined };

const PX = 1.2; // pixels per second
const TOP = 34; // under the lane names
const CAP = 60_000; // a stretch with nothing recorded is drawn at most this long, then folded with its length: a pause does not grow the spine
const COLORS = ["#4f7cff", "#e0484b", "#7b3fa0", "#d97b3a", "#2b8a6e", "#b9731c"];
export const appColor = (app: string) => COLORS[[...app].reduce((sum, char) => sum + char.charCodeAt(0), 0) % COLORS.length]!;

export function titleOf(mark: Mark): string {
  switch (mark.kind) {
    case "look": return `${clockOf(mark.ts)} · ${mark.shot.app}${mark.shot.window ? ` — ${mark.shot.window}` : ""}`;
    case "note": return `${clockOf(mark.from)} – ${clockOf(mark.ts)} · ${mark.note.summary.slice(0, 120)}`;
    case "change": return `${clockOf(mark.ts)} · ${mark.change.ops.length} change${mark.change.ops.length === 1 ? "" : "s"} to the task list`;
    case "suggest": return `${clockOf(mark.ts)} · ${proposalsOf(mark.set)[0]?.task ?? ""}`;
    case "check": return `${clockOf(mark.ts)} · a pause${mark.set.summary ? `: ${mark.set.summary}` : ""} · nothing worth suggesting`;
    case "answer": return `${clockOf(mark.ts)} · ${mark.set.feedback ? decisionLabel(mark.set.feedback) : ""}`;
    case "say": return `${clockOf(mark.ts)} · you: ${mark.message.text.slice(0, 120)}`;
    case "run": return `${clockOf(mark.ts)}${mark.end ? ` – ${clockOf(mark.end)}` : ""} · ${AGENT_LABELS[mark.detail.actorAgent ?? ""] ?? "agent"}`;
    case "ask": return `${clockOf(mark.ts)} · ${mark.title}`;
  }
}

type Props = {
  start: string; // the session's start: the top of the spine
  now: number; // ms: where the "now" line is
  paused: boolean;
  marks: Mark[];
  selected: string | null;
  cited: Set<string>; // the marks the selected suggestion cites: ringed
  screenshotUrl: (name: string) => string;
  onPick: (mark: Mark) => void;
};

// The session's stream on its side, time running down and "now" at the bottom: looks, notes, task changes, suggestions
// and pause checks, your answers and the chat, one lane each. It follows "now" until the person scrolls up to read.
export function Spine({ start, now, paused, marks, selected, cited, screenshotUrl, onPick }: Props) {
  const t0 = Date.parse(start);
  const stamps = [t0, ...marks.flatMap((mark) => (mark.kind === "run" && mark.end ? [Date.parse(mark.ts), Date.parse(mark.end)] : [Date.parse(mark.ts)])), now].sort((a, b) => a - b);
  const gaps: { a: number; b: number }[] = [];
  for (let index = 1; index < stamps.length; index++) if (stamps[index]! - stamps[index - 1]! > CAP) gaps.push({ a: stamps[index - 1]!, b: stamps[index]! });
  const y = (ts: string | number) => {
    const t = typeof ts === "number" ? ts : Date.parse(ts);
    let cut = 0;
    for (const gap of gaps) { if (t <= gap.a + CAP) break; cut += Math.min(t, gap.b) - gap.a - CAP; }
    return TOP + (t - t0 - cut) / 1000 * PX;
  };
  const height = y(now) + 160;
  const wrap = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useEffect(() => {
    const el = wrap.current;
    if (el && stick.current) el.scrollTop = Math.max(0, y(now) - el.clientHeight + 100);
  });
  const looks = marks.filter((mark): mark is Mark & { kind: "look" } => mark.kind === "look");
  const chosen = marks.find((mark) => mark.id === selected) ?? null;
  const from = chosen && "from" in chosen ? chosen.from : null;
  const minutes = Math.max(1, Math.ceil((now - t0) / 60_000) + 2), step = minutes > 240 ? 10 : minutes > 120 ? 5 : 1;
  const centre = (item: Mark) => { // where a mark sits, for the links: by its lane (the lefts in v1.css), at its time
    switch (item.kind) {
      case "note": return { x: 132, y: (y(item.from) + y(item.ts)) / 2 };
      case "say": return { x: 274, y: y(item.ts) };
      case "answer": return { x: 256, y: y(item.ts) };
      case "check": return { x: 234, y: y(item.ts) };
      default: return { x: 222, y: y(item.ts) };
    }
  };
  const origin = chosen && (chosen.kind === "suggest" || chosen.kind === "answer") && cited.size ? centre(chosen) : null;
  const mark = (item: Mark, className: string, top: number, style: React.CSSProperties = {}, text = "") => (
    <div key={item.id} className={`m ${className}${item.id === selected ? " sel" : ""}${cited.has(item.id) ? " cited" : ""}`} style={{ top, ...style }} title={titleOf(item)} onClick={() => onPick(item)}>{text}</div>
  );
  return (
    <div className="spinewrap" ref={wrap} onScroll={(event) => { const el = event.currentTarget; stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 160; }}>
      <div className="spine" style={{ height }}>
        <div className="lanehead"><span style={{ left: 4 }}>time</span><span style={{ left: 50 }}>looks</span><span style={{ left: 118 }}>notes</span><span style={{ left: 158 }}>list</span><span style={{ left: 198 }}>ideas</span><span style={{ left: 254 }}>you</span></div>
        {[44, 112, 152, 192, 244].map((left) => <div key={left} className="vline" style={{ left }} />)}
        {Array.from({ length: Math.ceil(minutes / step) }, (_, index) => {
          const at = t0 + index * step * 60_000;
          if (gaps.some((gap) => at > gap.a + CAP && at < gap.b)) return null; // inside a fold
          return <div key={index} className="ruler" style={{ top: y(at) }}><i /><time>{clockOf(new Date(at).toISOString()).slice(0, 5)}</time></div>;
        })}
        {looks.map((look, index) => {
          const next = looks[index + 1];
          const top = y(look.ts), bottom = next ? y(next.ts) : y(now);
          return (
            <div key={`band-${look.id}`}>
              <div className="band" style={{ top, height: Math.max(4, bottom - top), background: appColor(look.shot.app) }} />
              {/* eslint-disable-next-line @next/next/no-img-element */}
              {bottom - top >= 40 ? <img className={`shot${look.id === selected ? " sel" : ""}`} style={{ top: top + 2 }} src={screenshotUrl(look.shot.name)} alt="" loading="lazy" title={titleOf(look)} onClick={() => onPick(look)} />
                : bottom - top >= 14 ? <div className={`appmark${look.id === selected ? " sel" : ""}`} style={{ top: top + 1, background: appColor(look.shot.app) }} title={titleOf(look)} onClick={() => onPick(look)}>{look.shot.app.slice(0, 1)}</div> : null}
            </div>
          );
        })}
        {marks.map((item) => {
          switch (item.kind) {
            case "look": return mark(item, "tick", y(item.ts), { background: appColor(item.shot.app) });
            case "note": return mark(item, "notebar", y(item.from), { height: Math.max(6, y(item.ts) - y(item.from) - 4) });
            case "change": return mark(item, `dia${item.change.ops.length ? "" : " none"}`, y(item.ts) - 6);
            case "suggest": return mark(item, `flag${item.set.trigger === "idle" ? " idle" : ""}${item.pending ? " pending" : ""}${item.set.feedback ? " answered" : ""}`, y(item.ts), {}, String(proposalsOf(item.set).length));
            case "check": return mark(item, "pc", y(item.ts) - 4);
            case "answer": return mark(item, "dc", y(item.ts) - 8, {}, item.set.feedback?.decision === "accept" ? "✔" : "✘");
            case "say": return mark(item, "bubble", y(item.ts) - 8, {}, "💬");
            case "run": return mark(item, `runbar${item.end ? "" : " on"}`, y(item.ts), { height: Math.max(6, (item.end ? y(item.end) : y(now)) - y(item.ts)) });
            case "ask": return mark(item, `askm${item.pending ? " pending" : ""}`, y(item.ts) - 5);
          }
        })}
        {from && chosen ? <div className="window" style={{ top: y(from), height: Math.max(2, y(chosen.ts) - y(from)) }} /> : null}
        {origin ? <svg className="links" width={300} height={height}>{marks.filter((item) => cited.has(item.id)).map((item) => { const to = centre(item); return <line key={item.id} x1={origin.x} y1={origin.y} x2={to.x} y2={to.y} />; })}</svg> : null}
        {gaps.map((gap) => <div key={gap.a} className="gap" style={{ top: y(gap.a + CAP) }}><span>{paused && gap.b === now ? "paused" : ""} {Math.round((gap.b - gap.a) / 60_000)} min</span></div>)}
        <div className="nowline" style={{ top: y(now) }}><span>{paused ? "paused" : "now"} {clockOf(new Date(now).toISOString())}</span></div>
      </div>
    </div>
  );
}
