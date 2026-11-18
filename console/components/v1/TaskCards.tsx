"use client";

import type { LiveTask, LiveTaskChange, LiveTaskFlag } from "@cue/contracts";
import { useEffect, useRef, useState } from "react";

import { SIDEQUESTS, clockOf, type TaskEdit, type TaskFlagHandlers } from "../live/shared";
import { appColor } from "./Spine";
import { Reasons, Thumbs, flagOf } from "./TaskColumn";

type Layout = { order: string[]; size: Record<string, { w: number; h: number }>; shown: string[]; tray: number; closed: boolean; done: boolean };
const KEY = "v1.timelineCards"; // the cards' order and sizes, which entries are unfolded, the tray's height, whether it is closed, whether the done tasks show
const ROW = 190, GAP = 12; // a card's cell is ROW px tall at least, GAP px from the next
const fits = (rows: number) => 3 + 5 * (rows - 1); // open entries a card of that many rows shows before "+ N more"
const mark = (flag: LiveTaskFlag | undefined) => flag ? <span className="flagMark">{flag.verdict === "up" ? "👍" : "👎"}{flag.reason ? ` ${flag.reason}` : ""}</span> : null; // a flag given, after the text

type Props = {
  tasks: LiveTask[];
  changes: LiveTaskChange[]; // name the update that added an entry (by callId) and count what changed lately
  flags: LiveTaskFlag[];
  busy: boolean;
  now: number;
  preview?: boolean; // the closed row names the first tasks instead of the counts (the Timeline v2 theme's bar)
  onEdit: (edit: TaskEdit) => Promise<void>;
  onPick: (change: LiveTaskChange) => void; // an entry's time tag: that update, on the spine
  onFlag: TaskFlagHandlers;
};

// Your tasks as cards in a tray along the bottom: one per task in a grid that fills the tray, a cell each by default,
// dragged by the head to reorder, pulled by the corner to span more cells; the tray itself pulled taller by its top
// edge. A card shows a few open entries and folds the rest behind "+ N more"; the done ones fold under their count.
export function TaskCards({ tasks, changes, flags, busy, now, preview, onEdit, onPick, onFlag }: Props) {
  const [layout, setLayout] = useState<Layout>({ order: [], size: {}, shown: [], tray: 400, closed: Boolean(preview), done: false }); // a bar starts folded
  useEffect(() => {
    try { const stored: unknown = JSON.parse(window.localStorage.getItem(KEY) ?? "null"); if (stored && typeof stored === "object") setLayout((current) => ({ ...current, ...stored })); } catch { /* storage blocked: the defaults */ }
  }, []);
  const keep = (next: Layout) => { setLayout(next); try { window.localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* storage blocked: this visit only */ } };
  const listed = tasks.filter((task) => task.status !== "done" && !task.deletedAt);
  const finished = tasks.filter((task) => task.status === "done" && !task.deletedAt); // behind the row's "N done", dimmed, each with a way back
  const order = [...layout.order.filter((id) => listed.some((task) => task.id === id)), ...listed.filter((task) => !layout.order.includes(task.id)).map((task) => task.id)];
  const byCall = Object.fromEntries(changes.map((change) => [change.callId, change]));
  const fresh = changes.filter((change) => change.ops.length && now - Date.parse(change.ts) < 180_000).length;
  const projects = listed.filter((task) => task.title !== SIDEQUESTS).length; // the sidequests are one standing list, not a task of your own
  const opened = listed.reduce((sum, task) => sum + task.steps.filter((step) => !step.doneAt).length, 0), closed = listed.reduce((sum, task) => sum + task.steps.filter((step) => step.doneAt).length, 0);
  const dragId = useRef<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const drop = (targetId: string, after: boolean) => {
    const id = dragId.current;
    if (!id || id === targetId) return;
    const next = order.filter((item) => item !== id);
    next.splice(next.indexOf(targetId) + (after ? 1 : 0), 0, id);
    keep({ ...layout, order: next });
  };
  const drag = (event: React.PointerEvent, move: (moved: PointerEvent) => void, done: () => void) => {
    event.preventDefault();
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); done(); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const resize = (event: React.PointerEvent, card: HTMLElement, id: string) => {
    const colW = parseFloat(getComputedStyle(card.parentElement!).gridTemplateColumns) || 280, at = card.getBoundingClientRect(), x0 = event.clientX, y0 = event.clientY;
    let size = layout.size[id] ?? { w: 1, h: 1 };
    card.classList.add("resizing");
    drag(event, (moved) => {
      size = { w: Math.max(1, Math.min(3, Math.round((at.width + moved.clientX - x0 + GAP) / (colW + GAP)))), h: Math.max(1, Math.min(3, Math.round((at.height + moved.clientY - y0 + GAP) / (ROW + GAP)))) };
      card.style.gridColumn = `span ${size.w}`;
      card.style.gridRow = `span ${size.h}`;
    }, () => { card.classList.remove("resizing"); keep({ ...layout, size: { ...layout.size, [id]: size } }); });
  };
  const pullTray = (event: React.PointerEvent) => {
    const h0 = layout.tray, y0 = event.clientY;
    let tray = h0;
    drag(event, (moved) => { tray = Math.max(160, Math.min(720, h0 + y0 - moved.clientY)); setLayout((current) => ({ ...current, tray })); }, () => keep({ ...layout, tray }));
  };
  const ask = (text: string, initial = "") => window.prompt(text, initial)?.trim() || null;
  return (
    <div className={`tray${layout.closed ? "" : " open"}`}>
      <div className="tgrab" title="Drag to resize" onPointerDown={pullTray} />
      <div className="trow">
        <button type="button" className="lbl" onClick={() => keep({ ...layout, closed: !layout.closed })}>Tasks {preview ? <small className="count">{listed.length}</small> : null} {layout.closed ? "▴" : "▾"}</button>
        {preview && layout.closed
          ? <span className="titles">{order.slice(0, 2).map((id) => <span key={id} className="t" title={listed.find((task) => task.id === id)?.title}><i />{listed.find((task) => task.id === id)?.title}</span>)}{order.length > 2 ? <span className="moret">+{order.length - 2} more</span> : null}</span>
          : <span className="n">{preview ? null : <>{projects} task{projects === 1 ? "" : "s"} · </>}{opened} open items · {closed} completed items</span>}
        {fresh ? <span className="new">+{fresh} new</span> : null}
        {!layout.closed && finished.length ? <button type="button" className="done-toggle" aria-pressed={layout.done} title={`${finished.length} completed task${finished.length === 1 ? "" : "s"}`} onClick={() => keep({ ...layout, done: !layout.done })}><span className="done-toggle-track" aria-hidden="true" /><span>{layout.done ? "Hide done" : "Show done"}</span><small>{finished.length}</small></button> : null}
      </div>
      {layout.closed ? null : (
        <div className="tbody" style={{ height: layout.tray }}>
          <div className="tgrid">
            {order.map((id) => {
              const task = listed.find((item) => item.id === id)!;
              const size = layout.size[id] ?? { w: 1, h: 1 }, all = layout.shown.includes(id);
              const entries = task.steps.map((step, index) => ({ step, number: index + 1 })); // numbers are the store's, so an edit names the right entry
              const open = entries.filter((entry) => !entry.step.doneAt), done = entries.filter((entry) => entry.step.doneAt);
              const shown = all ? open : open.slice(0, fits(size.h));
              const row = ({ step, number }: (typeof entries)[number]) => {
                const change = step.callId ? byCall[step.callId] : undefined;
                return (
                  <div key={number} className={`kc${step.doneAt ? " done" : ""}`}>
                    <input type="checkbox" checked={Boolean(step.doneAt)} disabled={busy} aria-label={`Done: ${step.text}`} onChange={(event) => void onEdit({ op: "status", taskId: task.id, step: number, done: event.target.checked })} />
                    <span className="txt">{step.text}{mark(flagOf(task, number, flags))}<Thumbs task={task} step={number} flags={flags} onFlag={onFlag} /></span>
                    {change ? <button type="button" className="prov" title={`Added by the update at ${clockOf(change.ts)}`} onClick={() => onPick(change)}>{clockOf(change.ts).slice(0, 5)}</button> : null}
                    <button type="button" className="x" title="To the trash" disabled={busy} onClick={() => void onEdit({ op: "update", taskId: task.id, remove: [number] })}>✕</button>
                    <Reasons task={task} step={number} flags={flags} onFlag={onFlag} />
                  </div>
                );
              };
              return (
                <div key={id} className={`tc ${task.status}${over === id ? " over" : ""}`} style={{ "--c": appColor(task.title), gridColumn: `span ${size.w}`, gridRow: `span ${size.h}` } as React.CSSProperties}
                  onDragOver={(event) => { if (dragId.current && dragId.current !== id) { event.preventDefault(); setOver(id); } }}
                  onDragLeave={() => setOver((current) => (current === id ? null : current))}
                  onDrop={(event) => { event.preventDefault(); const at = event.currentTarget.getBoundingClientRect(); drop(id, event.clientX > at.left + at.width / 2); setOver(null); }}>
                  <h4 draggable onDragStart={(event) => { dragId.current = id; event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setDragImage(event.currentTarget.parentElement!, 24, 16); }} onDragEnd={() => { dragId.current = null; setOver(null); }}>
                    <span className="grip" title="Drag to move">⠿</span><span className="ttl" title={task.title}>{task.title}{mark(flagOf(task, null, flags))}<Thumbs task={task} step={null} flags={flags} onFlag={onFlag} /></span>
                    {task.status === "paused" ? <span className="st">paused</span> : <span className="cnt">{open.length} open</span>}
                    <button type="button" className="edit" title="Rename" disabled={busy} onClick={() => { const title = ask("New title", task.title); if (title && title !== task.title) void onEdit({ op: "rename", taskId: task.id, title }); }}>✎</button>
                    <button type="button" className="edit" title="Mark the task done" disabled={busy} onClick={() => void onEdit({ op: "status", taskId: task.id, done: true })}>✓</button>
                    <button type="button" className="edit" title="To the trash" disabled={busy} onClick={() => { if (window.confirm(`Move "${task.title}" to the trash?`)) void onEdit({ op: "delete", taskId: task.id }); }}>✕</button>
                  </h4>
                  <Reasons task={task} step={null} flags={flags} onFlag={onFlag} />
                  <div className="bar"><b style={{ width: `${entries.length ? Math.round((done.length / entries.length) * 100) : 0}%` }} /></div>
                  <div className="cards">
                    {shown.map(row)}
                    {shown.length < open.length ? <button type="button" className="more" onClick={() => keep({ ...layout, shown: [...layout.shown, id] })}>+ {open.length - shown.length} more</button>
                      : all && open.length > fits(size.h) ? <button type="button" className="more" onClick={() => keep({ ...layout, shown: layout.shown.filter((item) => item !== id) })}>less</button> : null}
                    {done.length ? <details className="donefold"><summary>{done.length} done</summary>{done.map(row)}</details> : null}
                  </div>
                  <button type="button" className="add" disabled={busy} onClick={() => { const text = ask("A new entry"); if (text) void onEdit({ op: "update", taskId: task.id, steps: [{ text }] }); }}>+ add</button>
                  <span className="rs" title="Drag to resize" onPointerDown={(event) => resize(event, event.currentTarget.parentElement!, id)}>◢</span>
                </div>
              );
            })}
            {layout.done ? finished.map((task) => (
              <div key={task.id} className="tc done" style={{ "--c": appColor(task.title) } as React.CSSProperties}>
                <h4><span className="ttl" title={task.title}>{task.title}</span><span className="st">done</span>
                  <button type="button" className="edit" aria-label="Reopen" title="Reopen" disabled={busy} onClick={() => void onEdit({ op: "status", taskId: task.id, done: false })}>↺</button>
                  <button type="button" className="edit" title="To the trash" disabled={busy} onClick={() => { if (window.confirm(`Move "${task.title}" to the trash?`)) void onEdit({ op: "delete", taskId: task.id }); }}>✕</button></h4>
                <div className="cards">{task.steps.length ? <details className="donefold"><summary>{task.steps.length} entr{task.steps.length === 1 ? "y" : "ies"}</summary>{task.steps.map((step, index) => <div key={index} className="kc done"><span className="txt">{step.text}</span></div>)}</details> : null}</div>
              </div>
            )) : null}
            <button type="button" className="tc new" disabled={busy} onClick={() => { const title = ask("A title for the task"); if (title) void onEdit({ op: "add", taskId: null, title, steps: [] }); }}>+ new task</button>
          </div>
        </div>
      )}
    </div>
  );
}
