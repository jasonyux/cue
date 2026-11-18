"use client";

import type { LiveChatMessage } from "@cue/contracts";
import { type CSSProperties, useEffect, useId, useRef, useState } from "react";

import { clockOf } from "../live/shared";
import { activitySpans, clusterMarks, placeSpans } from "./rail-layout";
import { useScrollEdges } from "./useScrollEdges";
import { appColor, titleOf, type Mark } from "./Spine";

type Props = {
  marks: Mark[];
  now: number;
  width: number; // the sidebar's, in px: a row holds the events that fit
  messages: LiveChatMessage[];
  selected: string | null;
  cited: Set<string>; // the marks the chosen proposal cites: ringed in the accent
  screenshotUrl: (name: string) => string;
  onPick: (mark: Mark) => void;
};

const GLYPH: Record<Mark["kind"], string> = { look: "", note: "≋", change: "◇", suggest: "◆", check: "○", answer: "✔", say: "💬", run: "⌘", ask: "▣" };

// Nearby events share a row; one conversation track connects its individual agent turns.
export function Rail({ marks, now, width, messages, selected, cited, screenshotUrl, onPick }: Props) {
  const legendId = useId();
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const columns = Math.floor((width - 54) / 42); // an event and its gap take 42px; the padding and the conversation track, 54
  const events = columns * 42;
  const clusters = clusterMarks(marks);
  const spans = placeSpans(activitySpans(marks, messages, now));
  let height = 0;
  const rows = clusters.map((cluster) => {
    const open = expanded.has(cluster.id) || cluster.marks.some((mark, index) => index >= columns - 1 && (mark.id === selected || cited.has(mark.id)));
    const visible = open || cluster.marks.length <= columns ? cluster.marks : cluster.marks.slice(0, columns - 1);
    const controls = cluster.marks.length > columns ? 1 : 0;
    const top = height;
    height += 26 + Math.ceil((visible.length + controls) / columns) * 34 + 10;
    return { ...cluster, open, visible, top };
  });
  const latest = Math.max(rows.at(-1)?.start ?? 0, ...spans.map((span) => span.end));
  const last = rows.at(-1);
  const tail = last && latest > Date.parse(last.marks.at(-1)!.ts) + 30_000;
  if (tail) height += 52;
  const anchors = [...rows.map((row) => ({ time: row.start, y: row.top + 38 })), ...(tail ? [{ time: latest, y: height - 20 }] : [])];
  const y = (time: number) => {
    const first = anchors[0]; if (!first) return 0;
    if (time <= first.time) return first.y;
    for (let index = 1; index < anchors.length; index++) {
      const before = anchors[index - 1]!, after = anchors[index]!;
      if (time <= after.time) return before.y + (time - before.time) / (after.time - before.time) * (after.y - before.y);
    }
    return anchors.at(-1)!.y;
  };
  const toggle = (id: string) => {
    stick.current = false;
    setExpanded((previous) => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  };
  const wrap = useRef<HTMLElement>(null);
  const stick = useRef(true);
  const edges = useScrollEdges(wrap, "y", height);
  useEffect(() => {
    const el = wrap.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [height, marks.length]);
  return (
    <div className="rail-shell scroll-hints" data-before={edges.before} data-after={edges.after}>
    <aside className="rail" ref={wrap} tabIndex={0} aria-label="Session timeline" style={{ "--events": `${events}px` } as CSSProperties} onScroll={(event) => { const el = event.currentTarget; stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 80; }}>
      <div className="rail-heading">
        <h3>Today</h3>
        <button type="button" className="legend-toggle" popoverTarget={legendId} aria-label="Timeline legend" title="Timeline legend">?</button>
      </div>
      <div id={legendId} className="rail-legend" popover="auto" aria-label="Timeline legend">
        <header><h4>Timeline legend</h4><button type="button" popoverTarget={legendId} popoverTargetAction="hide" aria-label="Close legend">×</button></header>
        <p>Events within 30 seconds share a row. Their order does not imply causality. Time gaps are compressed.</p>
        <ul>
          <li><span className="legend-thumbnail" aria-hidden="true">▧</span><span>Screenshot</span></li>
          {([
            ["note", "Note"], ["change", "Task update"], ["suggest", "Suggestion"],
            ["check", "Pause"], ["answer", "Accepted"], ["say", "Your message"],
            ["run", "Agent run"], ["ask", "Permission"],
          ] as const).map(([kind, label]) => <li key={kind} className={`legend-entry ${kind}`}><span className="glyph" aria-hidden="true">{GLYPH[kind]}</span><span>{label}</span></li>)}
          <li className="legend-entry answer no"><span className="glyph" aria-hidden="true">✘</span><span>Dismissed</span></li>
        </ul>
        <div className="span-key"><span><i className="work-key" />Conversation: solid segments show agent turns; the active turn pulses.</span><span><i className="gap-key" />Thin connectors join recorded messages and turns. They do not indicate continuous work or typing.</span></div>
      </div>
      <div className="rail-lane-labels" aria-hidden="true"><span>Events</span><span title="Conversation">💬</span></div>
      <div className="clustered-ticks" style={{ height }}>
        {rows.map((row) => (
          <div className="event-cluster" key={row.id} style={{ top: row.top }} role="group" aria-label={`Events near ${clockOf(new Date(row.start).toISOString())}`}>
            <time title={`${clockOf(row.marks[0]!.ts)} – ${clockOf(row.marks.at(-1)!.ts)}`}>{clockOf(row.marks[0]!.ts).slice(0, 5)}{row.marks.length > 1 ? <small> · {row.marks.length}</small> : null}</time>
            <div className="cluster-events">
              {row.visible.map((mark) => (
                <button key={mark.id} type="button" title={titleOf(mark)} aria-label={titleOf(mark)} onClick={() => onPick(mark)}
                  className={`tick ${mark.kind}${mark.id === selected ? " sel" : ""}${cited.has(mark.id) ? " cited" : ""}${"pending" in mark && mark.pending ? " pending" : ""}${mark.kind === "answer" && mark.set.feedback?.decision !== "accept" ? " no" : ""}`}>
                  {mark.kind === "look"
                    // eslint-disable-next-line @next/next/no-img-element
                    ? <img className="mini" src={screenshotUrl(mark.shot.name)} alt="" loading="lazy" style={{ borderColor: appColor(mark.shot.app) }} />
                    : <span className="glyph" aria-hidden="true">{mark.kind === "answer" && mark.set.feedback?.decision !== "accept" ? "✘" : GLYPH[mark.kind]}</span>}
                </button>
              ))}
              {row.marks.length > columns ? <button type="button" className="cluster-more" aria-expanded={row.open} aria-label={`${row.open ? "Collapse" : "Expand"} ${row.marks.length} events near ${clockOf(row.marks[0]!.ts)}`} onClick={() => toggle(row.id)}>{row.open ? "Less" : `+${row.marks.length - row.visible.length}`}</button> : null}
            </div>
          </div>
        ))}
        {tail ? <time className="rail-tail" style={{ top: height - 30 }}>{clockOf(new Date(latest).toISOString()).slice(0, 5)}<small>{spans.some((span) => span.active) ? "now" : "latest"}</small></time> : null}
        {spans.map((span) => {
          const duration = Math.max(1, Math.round((span.end - span.start) / 1000));
          const label = `${span.label} · ${clockOf(new Date(span.start).toISOString())} – ${clockOf(new Date(span.end).toISOString())} · ${duration < 60 ? `${duration}s` : `${Math.floor(duration / 60)}m ${duration % 60}s`}`;
          const top = y(span.start);
          return <div key={span.id} className="conversation-span" style={{ top, height: Math.max(8, y(span.end) - top), left: events + 10 + span.lane * 16 / span.lanes, width: 16 / span.lanes - 2 }}>
            <button type="button" className={`conversation-connector${span.mark.id === selected ? " selected" : ""}`} title={label} aria-label={label} onClick={() => onPick(span.mark)} />
            {span.segments.map((segment) => {
              const segmentLabel = `${span.label} · agent ${segment.active ? "working now" : "turn"} · ${clockOf(new Date(segment.start).toISOString())} – ${clockOf(new Date(segment.end).toISOString())}`;
              return <button key={segment.id} type="button" className={`conversation-segment${segment.active ? " active" : ""}${segment.mark.id === selected ? " selected" : ""}`} style={{ top: y(segment.start) - top, height: Math.max(6, y(segment.end) - y(segment.start)) }} title={segmentLabel} aria-label={segmentLabel} onClick={() => onPick(segment.mark)} />;
            })}
          </div>;
        })}
      </div>
    </aside>
    {edges.after ? <button type="button" className="scroll-latest" aria-label="Jump to latest timeline events" onClick={() => { stick.current = true; wrap.current?.scrollTo({ top: wrap.current.scrollHeight }); }}>↓ Latest</button> : null}
    </div>
  );
}
