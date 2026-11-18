"use client";

import { liveDiskSchema, liveUsageSchema, type LiveDisk, type LiveUsage, type LiveUsageRow } from "@cue/contracts";
import Link from "next/link";
import { useEffect, useState } from "react";

import { callRunner } from "../live/shared";
import { folderName } from "./Chats";
import { Banners, Skel, TopBar } from "./Shell";
import { useSession } from "./useSession";

type Props = { expectedBackend: "python"; runnerBaseUrl: string };
type Part = { label: string; color: string; amount: number; text: string; hint?: string | undefined }; // one share of a total: a segment of the bar, an entry of its legend

const RANGES: [number, string, string][] = [[1, "Today", "today"], [7, "7 days", "last 7 days"], [30, "30 days", "last 30 days"], [0, "All time", "all time"]]; // days (0: everything), the button, the total's label
// How the backend's modules read on the page, and their colour in the bar.
const PURPOSES: [string, string, string][] = [["agent", "Agent runs", "#2f6fed"], ["proposer", "Suggestions", "#7aa2ff"], ["summarizer", "Notes", "#2f9e5f"], ["belief updater", "Tasks", "#8fd1a8"], ["preference updater", "Preferences", "#b9a3f0"]];
const REST = "#d3d9e3"; // in the columns: a day's cost beside its agent runs
// What fills the data folder: its key in the report, its name, its colour in the bar, and what it is (the tooltip).
const KINDS: [string, string, string, string][] = [
  ["screenshots", "Screenshots", "#2f6fed", "The observer's pictures of the screen. History shows them."],
  ["modelCalls", "Model calls", "#7aa2ff", "Each model call in full. Replays and later decisions on old suggestions read them."],
  ["runs", "Agent runs", "#2f9e5f", "The files of the agent's runs: their screenshots and replays."],
  ["screenReads", "Screen reads", "#8fd1a8", "The text read off the screen. The memory keeps a copy."],
  ["memory", "Memory", "#b9a3f0", "Tasks, preferences, conversations and the long record. Keep."],
  ["agentBrowser", "Agent browser", "#e6b566", "The agent's browser profile, with its logins."],
  ["other", "Other", "#c9d0db", "Notes, suggestions and task changes; runs outside a session."],
];
const IN_SESSION = ["screenshots", "modelCalls", "runs", "screenReads", "other"] as const; // what a session's folder splits into; the first four are the table's columns
const FOLD = 8; // sessions listed before "+N more"
const DAY = 86_400_000;

function purposeOf(module: string): string { // a stage's side calls (the feedback digest, the duplicate filter, the lookalike check) count under it
  return module.split(":")[0]!;
}
function cost(rows: LiveUsageRow[]): number {
  return rows.reduce((sum, row) => sum + (row.costUsd ?? 0), 0);
}
// A total as dollars, with a plus when a model in it has no price (its tokens are counted, its cost is not).
function priced(rows: LiveUsageRow[]): string {
  return dollars(cost(rows)) + (rows.some((row) => row.costUsd === undefined) ? "+" : "");
}
function dollars(value: number | null, digits = 2): string {
  return value === null ? "—" : `$${value.toFixed(digits)}`;
}
function byPurpose(rows: LiveUsageRow[]): Record<string, number> {
  const totals: Record<string, number> = {};
  for (const row of rows) totals[purposeOf(row.module)] = (totals[purposeOf(row.module)] ?? 0) + (row.costUsd ?? 0);
  return totals;
}
function size(bytes: number): string { // as Finder counts: a GB is 10^9 bytes
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : bytes >= 1e6 ? `${Math.round(bytes / 1e6)} MB` : `${Math.round(bytes / 1e3)} KB`;
}
function dayLabel(day: string): string {
  return new Date(day).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

// A view's total, and what it is made of as one bar and its legend.
function Summary({ value, label, aside, parts, hint }: { value: string; label: string; aside: string; parts: Part[]; hint?: string | undefined }) {
  const shown = parts.filter((part) => part.amount > 0);
  const total = shown.reduce((sum, part) => sum + part.amount, 0);
  return (
    <div className="panel summary">
      <div className="head"><b title={hint}>{value}</b><span>{label}</span><span className="aside">{aside}</span></div>
      <div className="mix">{shown.map((part) => <i key={part.label} title={`${part.label} ${part.text}`} style={{ flexGrow: (part.amount / total) * 100, background: part.color }} />)}</div>
      <div className="legend">{shown.map((part) => <span key={part.label} title={part.hint}><i style={{ background: part.color }} />{part.label} <b>{part.text}</b></span>)}</div>
    </div>
  );
}

// Cost per UTC day as columns, from the span's first day to today (the days that cost nothing included, so the columns
// are a time axis): the agent's part in its colour, the amount above when there is room for it.
function Days({ byDay, days }: { byDay: LiveUsage["byDay"]; days: number }) {
  const today = Math.floor(Date.now() / DAY);
  const first = days ? today - days : Math.floor(Date.parse(byDay[0]?.day ?? "") / DAY);
  const rows = new Map(byDay.map((day) => [day.day, day.rows]));
  const cols = Array.from({ length: today - first + 1 }, (_, index) => {
    const day = new Date((first + index) * DAY).toISOString().slice(0, 10);
    return { day, total: cost(rows.get(day) ?? []), agent: byPurpose(rows.get(day) ?? []).agent ?? 0 };
  });
  const peak = Math.max(...cols.map((col) => col.total));
  if (!(peak > 0)) return null;
  const few = cols.length <= 14, step = Math.ceil(cols.length / 8);
  return (
    <div className="panel">
      <h3>By day <span className="legend"><span><i style={{ background: PURPOSES[0]![2] }} />Agent runs</span><span><i style={{ background: REST }} />Everything else</span></span></h3>
      <div className="days">
        {cols.map((col, index) => (
          <div key={col.day} title={`${dayLabel(col.day)} · ${dollars(col.total)}`}>
            {few && col.total ? <b style={{ bottom: `${(col.total / peak) * 100}%` }}>{dollars(col.total)}</b> : null}
            <span style={{ height: `${(col.total / peak) * 100}%`, background: REST }}><i style={{ height: `${col.total ? (col.agent / col.total) * 100 : 0}%` }} /></span>
            {few || index % step === 0 ? <small>{dayLabel(col.day)}</small> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

// Tokens and cost per model, split by what it was used for.
function Models({ rows }: { rows: LiveUsageRow[] }) {
  const models = [...new Set(rows.map((row) => row.model))];
  if (!models.length) return null;
  return (
    <div className="panel">
      <h3>By model</h3>
      <table>
        <thead><tr><th>Model</th><th>Used for</th><th className="num">Calls</th><th className="num">Input</th><th className="num">Cached</th><th className="num">Output</th><th className="num">Cost</th></tr></thead>
        <tbody>
          {models.map((model) => {
            const own = rows.filter((row) => row.model === model);
            const sum = (key: keyof LiveUsageRow) => own.reduce((total, row) => total + Number(row[key] ?? 0), 0);
            return [
              <tr key={model} className="model"><td>{model}</td><td>all</td><td className="num">{sum("calls").toLocaleString()}</td><td className="num">{sum("inputTokens").toLocaleString()}</td><td className="num">{sum("cachedInputTokens").toLocaleString()}</td><td className="num">{sum("outputTokens").toLocaleString()}</td><td className="num">{dollars(cost(own), 4)}</td></tr>,
              ...own.map((row) => <tr key={`${model}-${row.module}`} className="subrow"><td /><td>{row.module}</td><td className="num">{row.calls.toLocaleString()}</td><td className="num">{row.inputTokens.toLocaleString()}</td><td className="num">{row.cachedInputTokens.toLocaleString()}</td><td className="num">{row.outputTokens.toLocaleString()}</td><td className="num" title={row.costUsd === undefined ? "No price for this model" : undefined}>{dollars(row.costUsd ?? null, 4)}</td></tr>),
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

// A compressed session's mark: a box (after Lucide's archive).
const PACKED = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label="Compressed">
    <title>Compressed</title>
    <rect width="20" height="5" x="2" y="3" rx="1" />
    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
    <path d="M10 12h4" />
  </svg>
);

// The data folder by what fills it, and its sessions largest first: each opens in History, where it can be deleted.
// The first column ticks sessions (never ``running``, the one in progress), one by one, all at once or those not yet
// compressed that are older than a week or three; the header's buttons compress the ticked ones in place, or write
// the compressed ones among them back. The last column marks a compressed session.
function Disk({ disk, running, onPack }: { disk: LiveDisk; running: string | undefined; onPack: (action: "compress" | "restore", ids: string[]) => void }) {
  const [all, setAll] = useState(false);
  const [picked, setPicked] = useState<ReadonlySet<string>>(new Set());
  const totals: Record<string, number> = { ...disk.rest };
  for (const session of disk.sessions) for (const kind of IN_SESSION) totals[kind] = (totals[kind] ?? 0) + session[kind];
  const parts = KINDS.map(([key, label, color, hint]) => ({ label, color, amount: totals[key] ?? 0, text: size(totals[key] ?? 0), hint }));
  const rows = disk.sessions.map((session) => ({ ...session, total: IN_SESSION.reduce((sum, kind) => sum + session[kind], 0) })).sort((a, b) => b.total - a.total);
  const stored = rows.filter((row) => row.id !== running); // what can be ticked
  const ticked = (compressed: boolean) => stored.filter((row) => row.compressed === compressed && picked.has(row.id)).map((row) => row.id);
  const [plain, packed] = [ticked(false), ticked(true)];
  const job = disk.packing?.running ? disk.packing : null;
  const pick = (ids: string[]) => { setPicked(new Set(ids)); setAll(true); }; // unfolded, so the ticks behind "+N more" show
  const older = (weeks: number) => { // every session not yet compressed that began more than so many weeks ago
    const day = new Date(Date.now() - weeks * 7 * DAY);
    const before = `${day.getFullYear()}${String(day.getMonth() + 1).padStart(2, "0")}${String(day.getDate()).padStart(2, "0")}`; // as a session's id starts: the day it began
    pick(stored.filter((row) => !row.compressed && row.id.slice(0, 8) < before).map((row) => row.id));
  };
  const tick = (id: string, on: boolean) => setPicked((current) => { const next = new Set(current); if (on) next.add(id); else next.delete(id); return next; });
  const start = (action: "compress" | "restore", ids: string[]) => { setPicked(new Set()); onPack(action, ids); };
  return (
    <>
      <Summary value={size(parts.reduce((sum, part) => sum + part.amount, 0))} label="used" aside={`${size(disk.free)} free`} parts={parts} hint={disk.root} />
      {rows.length ? (
        <div className="panel">
          <h3>Sessions
            {job ? <span className="packtools"><span className="packbar"><i style={{ width: `${(job.done / Math.max(1, job.total)) * 100}%` }} /></span>{job.finished} of {job.sessions}</span> : (
              <span className="packtools">
                <button type="button" className="btn sm" onClick={() => older(1)}>Older than 1 week</button>
                <button type="button" className="btn sm" onClick={() => older(3)}>Older than 3 weeks</button>
                <button type="button" className="btn sm" disabled={!packed.length} onClick={() => start("restore", packed)}>Restore{packed.length ? ` ${packed.length}` : ""}</button>
                <button type="button" className="btn sm primary" disabled={!plain.length} onClick={() => start("compress", plain)}>Compress{plain.length ? ` ${plain.length}` : ""}</button>
              </span>
            )}
          </h3>
          {Object.entries(disk.packing?.failed ?? {}).map(([id, why]) => <p key={id} className="packfail">{folderName(id, null)}: {why}</p>)}
          <table>
            <thead>
              <tr>
                <th className="pick"><input type="checkbox" aria-label="Every session" disabled={job !== null || !stored.length} checked={stored.length > 0 && stored.every((row) => picked.has(row.id))} onChange={(event) => pick(event.target.checked ? stored.map((row) => row.id) : [])} /></th>
                <th>Session</th>{KINDS.slice(0, 4).map(([key, label]) => <th key={key} className="num">{label}</th>)}<th className="num">Total</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, all ? rows.length : FOLD).map((row) => (
                <tr key={row.id}>
                  <td className="pick">{row.id === running ? null : <input type="checkbox" aria-label={`Select ${row.id}`} disabled={job !== null} checked={picked.has(row.id)} onChange={(event) => tick(row.id, event.target.checked)} />}</td>
                  <td><Link href={`/v1/history?session=${encodeURIComponent(row.id)}`}>{folderName(row.id, null)}{row.name ? ` · ${row.name}` : ""}</Link></td>
                  {IN_SESSION.slice(0, 4).map((kind) => <td key={kind} className="num">{size(row[kind])}</td>)}<td className="num">{size(row.total)}</td>
                  <td className="state">{row.compressed ? PACKED : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length > FOLD && !all ? <button type="button" className="btn sm ghost" onClick={() => setAll(true)}>+{rows.length - FOLD} more</button> : null}
        </div>
      ) : null}
    </>
  );
}

// The page's panels in outline, while its numbers load.
function Loading() {
  return (
    <>
      <div className="panel summary loading" aria-hidden="true"><div className="head"><Skel w={120} h={26} /></div><Skel w="100%" h={8} /><Skel w="55%" /></div>
      <div className="panel loading" aria-hidden="true"><Skel w={90} /><Skel w="100%" h={120} /></div>
      <div className="panel loading" aria-hidden="true"><Skel w={90} />{[0, 1, 2, 3].map((row) => <Skel key={row} w="100%" h={14} />)}</div>
    </>
  );
}

export function Usage({ expectedBackend, runnerBaseUrl }: Props) {
  const s = useSession(runnerBaseUrl, expectedBackend);
  const [view, setView] = useState<"cost" | "disk">("cost");
  useEffect(() => { // the view last shown, after the first paint (the server's HTML and the first client render must agree): coming back while sessions are compressed shows how far along they are
    try { if (window.localStorage.getItem("v1.usage") === "disk") setView("disk"); } catch { /* no storage: Cost */ }
  }, []);
  const show = (name: "cost" | "disk") => { setView(name); try { window.localStorage.setItem("v1.usage", name); } catch { /* not remembered */ } };
  const [days, setDays] = useState(7); // of RANGES, or -1: the running session
  const [report, setReport] = useState<LiveUsage | null>(null);
  const [disk, setDisk] = useState<LiveDisk | null>(null);
  const [issue, setIssue] = useState<string | null>(null);
  const failed = (fallback: string) => (error: unknown) => setIssue(error instanceof Error ? error.message : fallback);
  const pack = (action: "compress" | "restore", ids: string[]) => { // the backend works through them, a few at a time; the report says how far along
    callRunner(runnerBaseUrl, expectedBackend, `/api/live/disk/${action}`, liveDiskSchema, { method: "POST", body: JSON.stringify({ ids }) }).then(setDisk, failed(`Could not ${action} the sessions.`));
  };
  const packing = disk?.packing?.running ?? false;
  useEffect(() => { // while sessions are compressed or restored, the report is read again every two seconds: its progress, and the rows as they finish
    if (!packing) return;
    const timer = setInterval(() => { callRunner(runnerBaseUrl, expectedBackend, "/api/live/disk", liveDiskSchema).then(setDisk, () => undefined); }, 2000);
    return () => clearInterval(timer);
  }, [runnerBaseUrl, expectedBackend, packing]);
  const asked = days > 0 ? days : days === 0 ? 3650 : 1; // the span the backend is asked for
  useEffect(() => {
    callRunner(runnerBaseUrl, expectedBackend, `/api/live/usage?days=${asked}`, liveUsageSchema).then(setReport, (error: unknown) => setIssue(error instanceof Error ? error.message : "Could not load the usage."));
  }, [runnerBaseUrl, expectedBackend, asked, s.status?.active]);
  useEffect(() => { // summed from the folder's files when the view is opened
    if (view === "disk") callRunner(runnerBaseUrl, expectedBackend, "/api/live/disk", liveDiskSchema).then(setDisk, failed("Could not load the disk use."));
  }, [runnerBaseUrl, expectedBackend, view]);
  const rows = !report ? [] : days === -1 ? report.session ?? [] : days === 0 ? report.all : report.recent;
  const totals = byPurpose(rows);
  const parts = PURPOSES.map(([key, label, color]) => ({ label, color, amount: totals[key] ?? 0, text: dollars(totals[key] ?? 0) }));
  const span = days === -1 ? "this session" : RANGES.find(([value]) => value === days)![2];
  const unpriced = rows.some((row) => row.costUsd === undefined) ? "A model here has no price: its tokens are counted, its cost is not" : undefined; // what the total's plus means
  const aside = report ? [report.session && days !== -1 ? `this session ${priced(report.session)}` : "", days !== 0 ? `all time ${priced(report.all)}` : ""].filter(Boolean).join(" · ") : "";
  return (
    <div className="v1">
      <TopBar s={s} current="Usage" />
      <Banners s={s} />
      {issue ? <div className="issue">{issue}</div> : null}
      <div className="sub usage">
        <span className="segs">{(["cost", "disk"] as const).map((name) => <button key={name} type="button" className={view === name ? "on" : ""} onClick={() => show(name)}>{name === "cost" ? "Cost" : "Disk"}</button>)}</span>
        {view === "cost" ? (
          <span className="segs">
            {report?.session ? <button type="button" className={days === -1 ? "on" : ""} onClick={() => setDays(-1)}>This session</button> : null}
            {RANGES.map(([value, name]) => <button key={value} type="button" className={days === value ? "on" : ""} onClick={() => setDays(value)}>{name}</button>)}
          </span>
        ) : null}
      </div>
      <div className={`page usage${view === "cost" && report && report.days !== asked ? " stale" : ""}`}>
        {view === "disk" ? (disk ? <Disk disk={disk} running={s.status?.session?.id} onPack={pack} /> : issue ? null : <Loading />) : report ? (
          <>
            <Summary value={priced(rows)} label={span} aside={aside} parts={parts} hint={unpriced} />
            {days === 0 || days > 1 ? <Days byDay={report.byDay} days={days} /> : null}
            <Models rows={rows} />
          </>
        ) : issue ? null : <Loading />}
      </div>
    </div>
  );
}
