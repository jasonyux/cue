"use client";

import type { LiveTask, LiveTaskChange, LiveTaskFlag } from "@cue/contracts";
import { useState } from "react";

import { SIDEQUESTS, TASK_FLAG_REASONS, clockOf, describeOp, type TaskEdit, type TaskFlagHandlers } from "../live/shared";

type Props = {
  tasks: LiveTask[];
  trash: LiveTask[];
  flags: LiveTaskFlag[];
  change: LiveTaskChange | null; // the latest task-list change by the agent
  busy: boolean;
  collapsed: boolean;
  onCollapse: (collapsed: boolean) => void;
  onResize: (width: number) => void;
  hideDays: number; // finished entries older than this wait for "show all" (beliefDoneStepDays)
  seen?: Seen | null; // the list as the person last read it: what differs is marked with a blue dot (the Zen theme)
  onRead?: (taskIds: string[]) => void; // projects the person opened: read from now on
  onEdit: (edit: TaskEdit) => Promise<void>;
  onFlag: TaskFlagHandlers;
};

const DONE_SHOWN = 3; // finished entries shown per project before "show all"; older than the setting's days they wait for "show all" too

// Your tasks as the agent keeps them: projects as rows, each opening to its entries; the sidequests apart below.
export function TaskColumn({ tasks, trash, flags, change, busy, collapsed, onCollapse, onResize, hideDays, seen = null, onRead, onEdit, onFlag }: Props) {
  const [opened, setOpened] = useState<Record<string, boolean>>({});
  const [all, setAll] = useState(false);
  const [editing, setEditing] = useState(false);
  const [questsOpen, setQuestsOpen] = useState(false);
  const order = { active: 0, paused: 1, done: 2 };
  const sorted = [...tasks].sort((a, b) => order[a.status] - order[b.status] || b.lastSeen.localeCompare(a.lastSeen));
  const quests = sorted.find((task) => task.title === SIDEQUESTS) ?? null;
  const projects = sorted.filter((task) => task !== quests && task.status !== "done");
  const done = sorted.filter((task) => task !== quests && task.status === "done");
  const openCount = projects.reduce((sum, task) => sum + task.steps.filter((step) => !step.doneAt).length, 0);
  const titles = Object.fromEntries(tasks.map((task) => [task.id, task.title]));
  if (collapsed) return <aside className="side"><div className="strip" onClick={() => onCollapse(false)}><span>«</span><b>Tasks · {openCount} open</b></div></aside>;
  const edit = editing ? onEdit : undefined;
  return (
    <aside className="side">
      <div className="grip" title="Drag to resize" onPointerDown={(event) => {
        event.preventDefault();
        const move = (moved: PointerEvent) => onResize(window.innerWidth - moved.clientX);
        const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      }} />
      <section>
        <h2>Tasks · {openCount} open
          <span className="tools">
            <button type="button" className="btn sm ghost" onClick={() => { if (!all) onRead?.([...projects, ...(quests ? [quests] : [])].map((task) => task.id)); setAll(!all); }}>{all ? "Collapse all" : "Expand all"}</button>
            <button type="button" className="btn sm ghost" onClick={() => setEditing((value) => !value)}>{editing ? "Done" : "Edit"}</button>
            <button type="button" className="btn sm ghost" title="Collapse the column" onClick={() => onCollapse(true)}>»</button>
          </span>
        </h2>
        {projects.length === 0 ? <p className="muted">No projects yet. The agent adds them as it sees what you work on.</p> : null}
        {projects.map((task) => (
          <Project key={task.id} task={task} flags={flags} open={all || Boolean(opened[task.id])} onToggle={() => { const opening = !(all || opened[task.id]); setOpened({ ...opened, [task.id]: opening }); if (opening) onRead?.([task.id]); }} onEdit={edit} onFlag={onFlag} busy={busy} hideDays={hideDays} seen={seen} />
        ))}
        {done.length ? <details className="trash"><summary>{done.length} project{done.length === 1 ? "" : "s"} done</summary>{done.map((task) => <Project key={task.id} task={task} flags={flags} open={Boolean(opened[task.id])} onToggle={() => { const opening = !opened[task.id]; setOpened({ ...opened, [task.id]: opening }); if (opening) onRead?.([task.id]); }} onEdit={edit} onFlag={onFlag} busy={busy} hideDays={hideDays} seen={seen} />)}</details> : null}
        {change?.ops.length ? <div className="change">{clockOf(change.ts)} · {change.ops.map((op) => describeOp(op, titles)).join("; ")}</div> : null}
        {editing && trash.length ? (
          <details className="trash"><summary>Trash ({trash.length})</summary>
            <ul className="steps">{trash.flatMap((task) => task.deletedAt
              ? [<li key={task.id}><span className="text">{task.title}</span><button type="button" className="btn sm ghost" disabled={busy} onClick={() => void onEdit({ op: "restore", taskId: task.id })}>Restore</button></li>]
              : task.steps.map((step, index) => <li key={`${task.id}-${index}`}><span className="text">{task.title}: {step.text}</span><button type="button" className="btn sm ghost" disabled={busy} onClick={() => void onEdit({ op: "restore", taskId: task.id, step: index + 1 })}>Restore</button></li>))}</ul>
          </details>
        ) : null}
      </section>
      {quests ? (
        <section className="quests">
          <h2 onClick={() => { if (!questsOpen && !all) onRead?.([quests.id]); setQuestsOpen(!questsOpen); }} className={marksOf(quests, seen).task ? "fresh" : undefined}><span className="chev">{all || questsOpen ? "▾" : "▸"}</span>Sidequests · {quests.steps.filter((step) => !step.doneAt).length} open<small className="muted">one-offs, no project</small></h2>
          {all || questsOpen ? <div className="body"><Entries task={quests} flags={flags} onEdit={edit} onFlag={onFlag} busy={busy} hideDays={hideDays} seen={seen} /></div> : null}
        </section>
      ) : null}
    </aside>
  );
}

// The task list as the person last saw it: per task its title, status, and each entry with whether it was done.
export type Seen = Record<string, { title: string; status: string; entries: Record<string, boolean> }>;

export function seenOf(tasks: LiveTask[]): Seen {
  return Object.fromEntries(tasks.map((task) => [task.id, { title: task.title, status: task.status, entries: Object.fromEntries(task.steps.map((step) => [step.text, Boolean(step.doneAt)])) }]));
}

// What changed since the person last saw the list: the entries added or ticked since, and whether the task itself
// is new, renamed, finished or reopened, or holds such an entry. Nothing is marked without a snapshot.
export function marksOf(task: LiveTask, seen: Seen | null): { task: boolean; entries: Set<string> } {
  if (!seen) return { task: false, entries: new Set() };
  const before = seen[task.id];
  const entries = new Set(task.steps.filter((step) => before?.entries[step.text] !== Boolean(step.doneAt)).map((step) => step.text));
  return { task: !before || before.title !== task.title || before.status !== task.status || entries.size > 0, entries };
}

type RowProps = { task: LiveTask; flags: LiveTaskFlag[]; onEdit: ((edit: TaskEdit) => Promise<void>) | undefined; onFlag: TaskFlagHandlers; busy: boolean; hideDays: number; seen: Seen | null };

function Project({ task, flags, open, onToggle, onEdit, onFlag, busy, hideDays, seen }: RowProps & { open: boolean; onToggle: () => void }) {
  const pending = task.steps.filter((step) => !step.doneAt).length;
  return (
    <div className="proj">
      <h4 onClick={onToggle} className={marksOf(task, seen).task ? "fresh" : undefined} title={marksOf(task, seen).task ? "New or changed since you last looked" : undefined}>
        <span className="chev">{open ? "▾" : "▸"}</span>{task.title}
        {task.status !== "active" ? <span className="state">{task.status}</span> : null}
        <Thumbs task={task} step={null} flags={flags} onFlag={onFlag} />
        <span className="count">{pending} open</span>
      </h4>
      {open ? (
        <div className="body">
          <FlagNote task={task} step={null} flags={flags} />
          <Reasons task={task} step={null} flags={flags} onFlag={onFlag} />
          {onEdit ? (
            <div className="edits">
              <button type="button" className="btn sm" disabled={busy} onClick={() => void onEdit({ op: "status", taskId: task.id, done: task.status !== "done" })}>{task.status === "done" ? "Reopen" : "Mark done"}</button>
              <button type="button" className="btn sm" disabled={busy} onClick={() => { const title = window.prompt("New title", task.title)?.trim(); if (title && title !== task.title) void onEdit({ op: "rename", taskId: task.id, title }); }}>Rename</button>
              <button type="button" className="btn sm ghost" disabled={busy} onClick={() => { if (window.confirm(`Move "${task.title}" to the trash?`)) void onEdit({ op: "delete", taskId: task.id }); }}>Trash</button>
            </div>
          ) : null}
          <Entries task={task} flags={flags} onEdit={onEdit} onFlag={onFlag} busy={busy} hideDays={hideDays} seen={seen} />
        </div>
      ) : null}
    </div>
  );
}

function Entries({ task, flags, onEdit, onFlag, busy, hideDays, seen }: RowProps) {
  const fresh = marksOf(task, seen).entries;
  const [showAll, setShowAll] = useState(false);
  const entries = task.steps.map((step, index) => ({ step, number: index + 1 })); // numbers are the store's, so edits refer to the right entry
  const finished = entries.filter((entry) => entry.step.doneAt);
  const recent = finished.filter((entry) => Date.now() - Date.parse(entry.step.doneAt ?? "") <= hideDays * 86_400_000);
  const shown = entries.filter((entry) => !entry.step.doneAt).concat(showAll ? finished : recent.slice(0, DONE_SHOWN));
  const hidden = finished.length - Math.min(recent.length, DONE_SHOWN);
  return (
    <ul className="steps">
      {shown.map(({ step, number }) => (
        <li key={number} className={`${step.doneAt ? "done" : ""}${fresh.has(step.text) ? " fresh" : ""}`}>
          {onEdit ? <input type="checkbox" checked={Boolean(step.doneAt)} disabled={busy} aria-label={`Done: ${step.text}`} onChange={(event) => void onEdit({ op: "status", taskId: task.id, step: number, done: event.target.checked })} /> : <span>{step.doneAt ? "☑" : "☐"}</span>}
          <span className="text">{step.text}</span>
          {onEdit ? <button type="button" className="more" title="To the trash" disabled={busy} onClick={() => void onEdit({ op: "update", taskId: task.id, remove: [number] })}>×</button> : null}
          <Thumbs task={task} step={number} flags={flags} onFlag={onFlag} />
          <FlagNote task={task} step={number} flags={flags} />
          <Reasons task={task} step={number} flags={flags} onFlag={onFlag} />
        </li>
      ))}
      {hidden > 0 ? <li><button type="button" className="more" onClick={() => setShowAll((value) => !value)}>{showAll ? "show fewer" : `+ ${hidden} more done · show`}</button></li> : null}
      {shown.length === 0 && !hidden ? <li className="muted">No entries.</li> : null}
    </ul>
  );
}

// The flag on the task (step null) or on an entry, matched by the entry's text: the numbers shift when the list is reordered.
export function flagOf(task: LiveTask, step: number | null, flags: LiveTaskFlag[]) {
  return flags.find((item) => item.taskId === task.id && (step === null ? item.step === null : item.text === task.steps[step - 1]?.text));
}

const asking = new Map<string, true>(); // flags just thumbed down, until a reason is given or skipped (module state: survives the re-render the flag causes)

export function Thumbs({ task, step, flags, onFlag }: { task: LiveTask; step: number | null; flags: LiveTaskFlag[]; onFlag: TaskFlagHandlers }) {
  const [, rerender] = useState(0);
  const flag = flagOf(task, step, flags);
  const click = (verdict: "up" | "down") => async (event: React.MouseEvent) => {
    event.stopPropagation();
    if (flag?.verdict === verdict) { onFlag.unflag(flag.id); return; } // the active thumb again: taken back
    const made = await onFlag.flag(task.id, step, verdict);
    if (made && verdict === "down") { asking.set(made.id, true); rerender((value) => value + 1); }
  };
  return (
    <span className={`thumbs${flag ? " on" : ""}`}>
      <button type="button" className={flag?.verdict === "up" ? "active" : ""} title="This is right" disabled={onFlag.busy} onClick={(event) => void click("up")(event)}>👍</button>
      <button type="button" className={flag?.verdict === "down" ? "active" : ""} title="This is wrong" disabled={onFlag.busy} onClick={(event) => void click("down")(event)}>👎</button>
    </span>
  );
}

// Why an entry or task was thumbed down, as a line of its own under it.
export function FlagNote({ task, step, flags }: { task: LiveTask; step: number | null; flags: LiveTaskFlag[] }) {
  const flag = flagOf(task, step, flags);
  return flag?.verdict === "down" && flag.reason ? <span className="flagNote">👎 {flag.reason}</span> : null;
}

export function Reasons({ task, step, flags, onFlag }: { task: LiveTask; step: number | null; flags: LiveTaskFlag[]; onFlag: TaskFlagHandlers }) {
  const [, rerender] = useState(0);
  const flag = flagOf(task, step, flags);
  if (!flag || !asking.has(flag.id)) return null;
  const give = (reason: string) => { onFlag.reason(flag.id, reason); asking.delete(flag.id); rerender((value) => value + 1); };
  return (
    <span className="reasons" onClick={(event) => event.stopPropagation()}>
      <span className="muted">Why?</span>
      {TASK_FLAG_REASONS.map((reason) => <button key={reason} type="button" className="chip" disabled={onFlag.busy} onClick={() => give(reason)}>{reason}</button>)}
      <button type="button" className="chip" disabled={onFlag.busy} onClick={() => { const typed = window.prompt("What is wrong with it? A few words.")?.trim(); if (typed) give(typed); }}>other…</button>
      <button type="button" className="chip" onClick={() => { asking.delete(flag.id); rerender((value) => value + 1); }}>skip</button>
    </span>
  );
}
