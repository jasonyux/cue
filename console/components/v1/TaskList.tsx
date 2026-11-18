"use client";

import type { LiveTask, LiveTaskChange, LiveTaskFlag } from "@cue/contracts";
import { type PointerEvent as ReactPointerEvent, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";

import { SIDEQUESTS, clockOf, type TaskEdit, type TaskFlagHandlers } from "../live/shared";
import { useMore } from "./more";
import { Reasons, Thumbs, flagOf } from "./TaskColumn";
import type { SelectedTask } from "./useSession";

type Layout = { order: string[]; tray: number; closed: boolean; done: boolean };
const KEY = "v1.tasks"; // the sections' order, the tray's height, whether it is closed, whether the done tasks show
const FOLD = 5; // open entries a task shows before "+ N more"
const SHARE = 0.5; // of the folded bar's width the titles may take before "+N more"
const CHEVRON = <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M2.5 7.5L6 4l3.5 3.5" /></svg>;
const ARROW = <svg viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M1.5 5h7M5.5 2l3 3-3 3" /></svg>;
export type TaskPick = SelectedTask; // an entry picked for the agent: its task's id and title, and its text
export const quote = (picks: TaskPick[]) => { // the tray's lines for the message, quoted: each task's title over its entries
  const lines: string[] = [];
  for (const id of new Set(picks.map((pick) => pick.taskId))) {
    const own = picks.filter((pick) => pick.taskId === id);
    lines.push(`> ${own[0]!.title}:`, ...own.map((pick) => `>   • ${pick.text}`));
  }
  return lines.join("\n");
};
export const X = <svg viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="M1 1l8 8M9 1l-8 8" /></svg>;
const mark = (flag: LiveTaskFlag | undefined) => <span className="flagMark" title={flag?.reason || undefined}>{flag ? flag.verdict === "up" ? "👍" : "👎" : null}</span>; // a slot on every line, so the marks line up

type Props = {
  tasks: LiveTask[];
  changes: LiveTaskChange[]; // name the update that added an entry (by callId) and count what changed lately
  flags: LiveTaskFlag[];
  busy: boolean;
  now: number;
  onEdit: (edit: TaskEdit) => Promise<void>;
  onPick: (change: LiveTaskChange) => void; // an entry's time: that update, on the spine
  onFlag: TaskFlagHandlers;
  sendTo?: ReactNode; // the proposal card's "Send to" pickers, for the picking bar
  onAttach?: ((picks: TaskPick[]) => void) | undefined; // with a session: Ask Cue picks entries for the chat box (the button waits, disabled, without one)
};

// Your tasks in a tray along the bottom, a section per task in equal columns: a header line (its name, the open count,
// a ⋯ menu to rename, fold, finish or trash it), then its open entries one line each, cut with an ellipsis; a click on
// an entry unfolds it to its full text, another folds it back. Past the fold and the done entries wait behind a quiet
// footer line. A header dragged onto another section moves the task there. The Sidequests list (the one-offs the
// belief model files under that exact title) is always last, a ring for a dot, with no rename, finish or trash: any
// of those would make the model start another. The closed tray is a bar naming the first tasks; its top edge pulls it taller.
export function TaskList({ tasks, changes, flags, busy, now, onEdit, onPick, onFlag, sendTo, onAttach }: Props) {
  const [layout, setLayout] = useState<Layout>({ order: [], tray: 400, closed: true, done: false });
  useEffect(() => {
    try { const stored: unknown = JSON.parse(window.localStorage.getItem(KEY) ?? "null"); if (stored && typeof stored === "object") setLayout((current) => ({ ...current, ...stored })); } catch { /* storage blocked: the defaults */ }
  }, []);
  const keep = (next: Layout) => { setLayout(next); try { window.localStorage.setItem(KEY, JSON.stringify(next)); } catch { /* storage blocked: this visit only */ } };
  const [unfolded, setUnfolded] = useState<ReadonlySet<string>>(new Set()); // "<task>:<entry>" read in full, "<task>:more" past the fold, "<task>:done" with the done ones listed
  const toggle = (key: string) => setUnfolded((current) => { const next = new Set(current); if (!next.delete(key)) next.add(key); return next; });
  const fold = (taskId: string) => setUnfolded((current) => new Set([...current].filter((key) => !key.startsWith(`${taskId}:`))));
  const { more } = useMore();
  const [picking, setPicking] = useState(false); // Ask Cue: a click on an entry picks it, on a task's name all of it
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set());
  const pick = (keys: string[], on: boolean) => setPicked((current) => { const next = new Set(current); for (const key of keys) if (on) next.add(key); else next.delete(key); return next; });
  const stopPicking = () => { setPicking(false); setPicked(new Set()); };
  const listed = tasks.filter((task) => task.status !== "done" && !task.deletedAt);
  const finished = tasks.filter((task) => task.status === "done" && !task.deletedAt); // behind the row's "Show done", struck through, each with a way back
  const quests = listed.find((task) => task.title === SIDEQUESTS);
  const order = [...layout.order.filter((id) => listed.some((task) => task.id === id && task !== quests)), ...listed.filter((task) => task !== quests && !layout.order.includes(task.id)).map((task) => task.id)];
  const dragId = useRef<string | null>(null);
  const [over, setOver] = useState<string | null>(null); // the section a dragged header is above
  const drop = (targetId: string, after: boolean) => {
    const id = dragId.current;
    if (!id || id === targetId) return;
    const next = order.filter((item) => item !== id);
    next.splice(next.indexOf(targetId) + (after ? 1 : 0), 0, id);
    keep({ ...layout, order: next });
  };
  const byCall = Object.fromEntries(changes.map((change) => [change.callId, change]));
  const fresh = changes.filter((change) => change.ops.length && now - Date.parse(change.ts) < 180_000).length;
  const opened = listed.reduce((sum, task) => sum + task.steps.filter((step) => !step.doneAt).length, 0), closed = listed.reduce((sum, task) => sum + task.steps.filter((step) => step.doneAt).length, 0);
  const pullTray = (event: ReactPointerEvent) => {
    event.preventDefault();
    const h0 = layout.tray, y0 = event.clientY;
    let tray = h0;
    const move = (moved: PointerEvent) => { tray = Math.max(160, Math.min(720, h0 + y0 - moved.clientY)); setLayout((current) => ({ ...current, tray })); };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); keep({ ...layout, tray }); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };
  const ask = (text: string, initial = "") => window.prompt(text, initial)?.trim() || null;
  const trash = (task: LiveTask) => { if (window.confirm(`Move "${task.title}" to the trash?`)) void onEdit({ op: "delete", taskId: task.id }); };
  const entry = (task: LiveTask, step: LiveTask["steps"][number], number: number) => {
    const key = `${task.id}:${number}`, change = step.callId ? byCall[step.callId] : undefined;
    if (picking) {
      const on = picked.has(key);
      return (
        <div key={number} className={`entry${step.doneAt ? " done" : ""}${on ? " picked" : ""}`} onClick={step.doneAt ? undefined : () => pick([key], !on)}>
          <input type="checkbox" checked={on} readOnly tabIndex={-1} aria-label={`Pick: ${step.text}`} />
          <span className="txt" title={step.text}>{step.text}</span>
          {change ? <span className="prov">{clockOf(change.ts).slice(0, 5)}</span> : <span className="prov" />}
        </div>
      );
    }
    return (
      <div key={number} className={`entry${step.doneAt ? " done" : ""}${unfolded.has(key) ? " open" : ""}`}>
        <input type="checkbox" checked={Boolean(step.doneAt)} disabled={busy} aria-label={`Done: ${step.text}`} onChange={(event) => void onEdit({ op: "status", taskId: task.id, step: number, done: event.target.checked })} />
        <button type="button" className="txt" title={step.text} onClick={() => toggle(key)}>{step.text}</button>
        {mark(flagOf(task, number, flags))}
        {change ? <button type="button" className="prov" title={`Added by the update at ${clockOf(change.ts)}`} onClick={() => onPick(change)}>{clockOf(change.ts).slice(0, 5)}</button> : <span className="prov" />}
        <button type="button" className="x" title="To the trash" disabled={busy} onClick={() => void onEdit({ op: "update", taskId: task.id, remove: [number] })}>{X}</button>
        <Thumbs task={task} step={number} flags={flags} onFlag={onFlag} />
        <Reasons task={task} step={number} flags={flags} onFlag={onFlag} />
      </div>
    );
  };
  const section = (task: LiveTask) => {
    const entries = task.steps.map((step, index) => ({ step, number: index + 1 })); // numbers are the store's, so an edit names the right entry
    const open = entries.filter((item) => !item.step.doneAt), done = entries.filter((item) => item.step.doneAt);
    const all = unfolded.has(`${task.id}:more`), shown = all ? open : open.slice(0, FOLD);
    const ended = task.status === "done", standing = task === quests;
    const menu: [string, () => void][] = standing ? [["Fold all", () => fold(task.id)]] : ended ? [["Reopen", () => void onEdit({ op: "status", taskId: task.id, done: false })], ["Trash", () => trash(task)]] : [
      ["Rename", () => { const title = ask("New title", task.title); if (title && title !== task.title) void onEdit({ op: "rename", taskId: task.id, title }); }],
      ["Fold all", () => fold(task.id)],
      ["Mark done", () => void onEdit({ op: "status", taskId: task.id, done: true })],
      ["Trash", () => trash(task)],
    ];
    return (
      <section key={task.id} className={`task ${task.status}${standing ? " standing" : ""}${over === task.id ? " over" : ""}`}
        onDragOver={(event) => { if (dragId.current && dragId.current !== task.id && !standing) { event.preventDefault(); setOver(task.id); } }}
        onDragLeave={() => setOver((current) => (current === task.id ? null : current))}
        onDrop={(event) => { event.preventDefault(); const at = event.currentTarget.getBoundingClientRect(); drop(task.id, event.clientX > at.left + at.width / 2); setOver(null); }}>
        <div className="thd" draggable={!ended && !standing && !picking} onClick={picking && !ended ? () => { const keys = open.map((item) => `${task.id}:${item.number}`); pick(keys, !keys.every((key) => picked.has(key))); } : undefined} onDragStart={(event) => { dragId.current = task.id; event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setDragImage(event.currentTarget.parentElement!, 24, 16); }} onDragEnd={() => { dragId.current = null; setOver(null); }}>
          <span className="ttl" title={task.title}>{task.title}</span>
          {mark(flagOf(task, null, flags))}
          <span className="cnt">{ended ? "done" : standing ? "one-offs" : task.status === "paused" ? "paused" : `${open.length} open`}</span>
          {picking ? null : more(task.id, menu)}
          {picking ? null : <Thumbs task={task} step={null} flags={flags} onFlag={onFlag} />}
        </div>
        <Reasons task={task} step={null} flags={flags} onFlag={onFlag} />
        {shown.map(({ step, number }) => entry(task, step, number))}
        {unfolded.has(`${task.id}:done`) ? done.map(({ step, number }) => entry(task, step, number)) : null}
        <div className="tft">
          {open.length > FOLD ? <button type="button" className="unfold" onClick={() => toggle(`${task.id}:more`)}>{all ? "less" : `+ ${open.length - FOLD} more`}</button> : null}
          {done.length ? <button type="button" className="unfold" aria-pressed={unfolded.has(`${task.id}:done`)} onClick={() => toggle(`${task.id}:done`)}>{done.length} done</button> : null}
          {ended || picking ? null : <button type="button" className="add" disabled={busy} onClick={() => { const text = ask("A new entry"); if (text) void onEdit({ op: "update", taskId: task.id, steps: [{ text }] }); }}>+ add</button>}
        </div>
      </section>
    );
  };
  const ordered = [...order.map((id) => listed.find((task) => task.id === id)!), ...(quests ? [quests] : [])];
  const flip = () => keep({ ...layout, closed: !layout.closed });
  const attach = () => { // the picks in the tray's order, to the box; the tray folds
    onAttach?.(ordered.flatMap((task) => task.steps.flatMap((step, index) => (picked.has(`${task.id}:${index + 1}`) ? [{ taskId: task.id, title: task.title, text: step.text }] : []))));
    stopPicking();
    keep({ ...layout, closed: true });
  };
  // Folded, the bar names the tasks that fit whole in SHARE of its width, the rest as "+N more". Every title is laid
  // out (the ones past the fit invisible) so a resize can measure them again.
  const bar = useRef<HTMLButtonElement>(null), strip = useRef<HTMLSpanElement>(null);
  const [fit, setFit] = useState<{ n: number; width?: number }>({ n: ordered.length });
  const titles = ordered.map((task) => task.title).join("\n");
  useLayoutEffect(() => {
    if (!bar.current || !strip.current) return;
    const place = () => {
      const limit = bar.current!.clientWidth * SHARE, left = strip.current!.getBoundingClientRect().left;
      let n = 0, width = 0;
      for (const title of strip.current!.children) {
        const right = title.getBoundingClientRect().right - left;
        if (right > limit) break;
        n++;
        width = right;
      }
      setFit({ n, width });
    };
    const watch = new ResizeObserver(place);
    watch.observe(bar.current);
    return () => watch.disconnect();
  }, [layout.closed, titles]);
  return (
    <div className={`tray${layout.closed ? "" : " open"}${picking ? " picking" : ""}`}>
      {layout.closed ? (
        <button type="button" className="trow fold" ref={bar} onClick={flip}>
          <span className="lbl">Tasks <small className="count">{listed.length}</small></span>
          <span className="titles" ref={strip} style={{ width: fit.width }}>{ordered.map((task, index) => <span key={task.id} className={`t${task.status === "paused" ? " paused" : ""}`} style={index < fit.n ? undefined : { visibility: "hidden" }} title={task.title}><i />{task.title}</span>)}</span>
          {ordered.length > fit.n ? <span className="moret">+{ordered.length - fit.n} more</span> : null}
          {fresh ? <span className="new">+{fresh} new</span> : null}
          <span className="chev">{CHEVRON}</span>
        </button>
      ) : (
        <>
          <div className="tgrab" title="Drag to resize" onPointerDown={pullTray} />
          {picking ? (
            <div className="trow">
              <span className="lbl asking">Ask Cue <small className="count">{picked.size}</small></span>
              <button type="button" className="cancel" onClick={stopPicking}>Cancel</button>
              <span className="goes">{sendTo}<button type="button" className="attach" disabled={!picked.size} onClick={attach}>Attach {ARROW}</button></span>
            </div>
          ) : (
          <div className="trow">
            <button type="button" className="lbl" onClick={flip}>Tasks <small className="count">{listed.length}</small></button>
            <span className="n">{opened} open · {closed} done</span>
            {fresh ? <span className="new">+{fresh} new</span> : null}
            {finished.length ? <button type="button" className="done-toggle" aria-pressed={layout.done} title={`${finished.length} completed task${finished.length === 1 ? "" : "s"}`} onClick={() => keep({ ...layout, done: !layout.done })}><span className="done-toggle-track" aria-hidden="true" /><span>{layout.done ? "Hide done" : "Show done"}</span><small>{finished.length}</small></button> : null}
            <button type="button" className="newtask" disabled={busy} onClick={() => { const title = ask("A title for the task"); if (title) void onEdit({ op: "add", taskId: null, title, steps: [] }); }}>+ New task</button>
            <button type="button" className="askb" disabled={!onAttach} title={onAttach ? undefined : "Start a session first"} onClick={() => setPicking(true)}>{ARROW} Ask Cue</button>
            <button type="button" className="chev" aria-label="Fold" onClick={flip}>{CHEVRON}</button>
          </div>
          )}
          <div className="tbody" style={{ height: layout.tray }}>
            <div className="tlist">
              {ordered.map(section)}
              {layout.done ? finished.map(section) : null}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
