"use client";

import { liveSessionListSchema, liveUsageRowSchema, runDetailSchema, type LiveProposalSet, type LiveSessionRecord, type LiveUsageRow, type RunDetail } from "@cue/contracts";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { MARKDOWN_LINKS, OnPause, RunCard, callRunner, clockOf, decisionLabel, proposalsOf, rawJson } from "../live/shared";
import { useRecords } from "./records";
import { Banners, Skel, TopBar } from "./Shell";
import { useSession, type SessionList } from "./useSession";

type Props = { expectedBackend: "python"; runnerBaseUrl: string };
type Tab = "Runs" | "Noticed" | "Settings used";
type Filter = "All" | "Named" | "Recent";
const PAGE = 20; // sessions a page; ‹ › turn them
const NAV = [300, 380, 600] as const; // the session list's width: least, by default, most; dragged at its edge
const navWidth = (width: number) => Math.round(Math.min(Math.max(width, NAV[0]), NAV[2]));

function length(startedAt: string, stoppedAt: string | null | undefined): string {
  const minutes = Math.round((Date.parse(stoppedAt ?? new Date().toISOString()) - Date.parse(startedAt)) / 60_000);
  return minutes >= 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} m` : `${minutes} m`;
}
function dollars(rows: LiveUsageRow[] | null): string {
  if (!rows || !rows.every((row) => row.costUsd !== undefined)) return "—";
  return `$${rows.reduce((sum, row) => sum + (row.costUsd ?? 0), 0).toFixed(2)}`;
}

// Past sessions a page at a time, the named ones first or either kind alone; the one selected with its runs by
// conversation, what it noticed, and the settings it ran with. Rename and delete are the backend's own; a running session cannot be touched here.
export function History({ expectedBackend, runnerBaseUrl }: Props) {
  const s = useSession(runnerBaseUrl, expectedBackend);
  const params = useSearchParams();
  const [sessions, setSessions] = useState<SessionList>([]);
  const [selected, setSelected] = useState<string | null>(params.get("session"));
  const [tab, setTab] = useState<Tab>("Runs");
  const [usage, setUsage] = useState<LiveUsageRow[] | null>(null);
  const [runs, setRuns] = useState<Record<string, RunDetail>>({});
  const [issue, setIssue] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("All");
  const [page, setPage] = useState(0);
  const [more, setMore] = useState(false); // a session past this page: › goes there
  const [listed, setListed] = useState(false); // the sessions have been asked for and answered, once
  const [nav, setNav] = useState<number>(NAV[1]);
  useEffect(() => { // one more than shown, to know whether › has somewhere to go
    const only = filter === "All" ? "" : `&named=${filter === "Named"}`;
    void callRunner(runnerBaseUrl, expectedBackend, `/api/live/sessions?limit=${PAGE + 1}&offset=${page * PAGE}${only}`, liveSessionListSchema)
      .then((rows) => { setSessions(rows.slice(0, PAGE)); setMore(rows.length > PAGE); setListed(true); }, (error: unknown) => setIssue(error instanceof Error ? error.message : "Could not list the sessions."));
  }, [runnerBaseUrl, expectedBackend, filter, page]);
  useEffect(() => { try { setNav(navWidth(Number(window.localStorage.getItem("v1.historyNav")) || NAV[1])); } catch { /* storage blocked: the default */ } }, []);
  const id = selected ?? sessions[0]?.id ?? null;
  const { records, issue: recordsIssue, reload } = useRecords(runnerBaseUrl, expectedBackend, id ?? undefined);
  const stored: LiveSessionRecord | null = sessions.find((item) => item.id === id) ?? (records?.detail?.session.id === id ? records.detail.session : null); // a link to one past this page: its own record
  useEffect(() => {
    if (!id) return;
    setUsage(null);
    void callRunner(runnerBaseUrl, expectedBackend, `/api/live/sessions/${encodeURIComponent(id)}/usage`, liveUsageRowSchema.array()).then(setUsage, () => setUsage(null));
  }, [runnerBaseUrl, expectedBackend, id]);
  useEffect(() => {
    for (const runId of stored?.runs ?? []) {
      if (runs[runId]) continue;
      void callRunner(runnerBaseUrl, expectedBackend, `/api/runs/${runId}`, runDetailSchema).then((detail) => setRuns((current) => ({ ...current, [runId]: detail })), () => undefined);
    }
  }, [runnerBaseUrl, expectedBackend, stored, runs]);
  const named = sessions.filter((item) => item.name);
  const rest = sessions.filter((item) => !item.name);
  const live = stored?.id === s.session?.id;
  const show = (kind: Filter, at: number) => { setFilter(kind); setPage(at); setSelected(null); };
  const rename = async () => {
    if (!stored) return;
    const name = window.prompt("A name for this session", stored.name ?? "");
    if (name === null) return;
    try {
      await callRunner(runnerBaseUrl, expectedBackend, `/api/live/sessions/${encodeURIComponent(stored.id)}`, rawJson, { method: "PATCH", body: JSON.stringify({ name: name.trim() || null }) });
      setSessions((current) => current.map((item) => (item.id === stored.id ? { ...item, name: name.trim() || null } : item)));
    }
    catch (error) { setIssue(error instanceof Error ? error.message : "Could not rename the session."); }
  };
  const remove = async () => {
    if (!stored || !window.confirm("Delete this session's folder, runs included?")) return;
    try {
      await callRunner(runnerBaseUrl, expectedBackend, `/api/live/sessions/${encodeURIComponent(stored.id)}`, rawJson, { method: "DELETE" });
      setSelected(null);
      setSessions((current) => current.filter((item) => item.id !== stored.id));
    }
    catch (error) { setIssue(error instanceof Error ? error.message : "Could not delete the session."); }
  };
  const decide = async (item: LiveProposalSet, decision: "accept" | "reject", reason?: string) => {
    if (!stored) return;
    const base = `/api/live/sessions/${encodeURIComponent(stored.id)}/proposals/feedback`;
    try {
      await callRunner(runnerBaseUrl, expectedBackend, base, rawJson, { method: "POST", body: JSON.stringify({ proposalsTs: item.ts, decision, choice: decision === "accept" ? 0 : null }) });
      if (reason) await callRunner(runnerBaseUrl, expectedBackend, `${base}/reason`, rawJson, { method: "POST", body: JSON.stringify({ proposalsTs: item.ts, reason, typed: !(s.status?.rejectReasons ?? []).includes(reason) }) });
      await reload();
    } catch (error) { setIssue(error instanceof Error ? error.message : "Could not record the decision."); }
  };
  const conversations = records?.chat?.conversations ?? [];
  const said = Object.fromEntries((records?.chat?.messages ?? []).filter((message) => message.role === "user").map((message) => [message.runId, message.text]));
  const replied = Object.fromEntries((records?.chat?.messages ?? []).filter((message) => message.role === "agent").map((message) => [message.runId, message.text]));
  const row = (item: SessionList[number]) => (
    <button key={item.id} type="button" className={`sess${item.id === id ? " on" : ""}`} onClick={() => setSelected(item.id)}>
      <span className="d"><b>{item.name ?? `${item.startedAt.slice(0, 10)} · ${clockOf(item.startedAt)}`}</b><span className="when">{item.id === s.session?.id ? "● live" : length(item.startedAt, item.stoppedAt)}</span></span>
      <span className="m">{item.name ? `${item.startedAt.slice(0, 10)} · ` : ""}{item.runs.length} run{item.runs.length === 1 ? "" : "s"} · {item.proposalCount} noticed{item.stopReason ? ` · ${item.stopReason}` : ""}</span>
    </button>
  );
  return (
    <div className="v1">
      <TopBar s={s} current="History" />
      <Banners s={s} />
      {issue ?? recordsIssue ? <div className="issue">{issue ?? recordsIssue}</div> : null}
      <div className="page split2" style={{ gridTemplateColumns: `${nav}px minmax(0,1fr)` }}>
        <nav className="sessions">
          <div className="pages">
            {(["All", "Named", "Recent"] as Filter[]).map((kind) => <button key={kind} type="button" aria-pressed={filter === kind} onClick={() => show(kind, 0)}>{kind}</button>)}
            <span className="spacer" />
            <button type="button" aria-label="Newer" disabled={page === 0} onClick={() => show(filter, page - 1)}>‹</button>
            <button type="button" aria-label="Older" disabled={!more} onClick={() => show(filter, page + 1)}>›</button>
          </div>
          {filter === "All" && named.length ? <div className="grp">Named <span>{named.length}</span></div> : null}{named.map(row)}
          {filter === "All" ? <div className="grp">Recent <span>{rest.length}</span></div> : null}{rest.map(row)}
          {!listed && !issue ? [0, 1, 2, 3, 4, 5, 6, 7].map((item) => <div key={item} className="sess loading" aria-hidden="true"><Skel w="62%" /><Skel w="84%" h={10} /></div>)
            : sessions.length === 0 ? <p className="muted" style={{ padding: 18 }}>No sessions.</p> : null}
        </nav>
        <div className="grip" title="Drag to resize" style={{ left: nav - 3 }} onPointerDown={(event) => { // the list's edge follows the pointer; the width is kept for next time
          event.preventDefault();
          const left = event.currentTarget.parentElement!.getBoundingClientRect().left;
          const move = (moved: PointerEvent) => setNav(navWidth(moved.clientX - left));
          const up = (moved: PointerEvent) => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", up);
            try { window.localStorage.setItem("v1.historyNav", String(navWidth(moved.clientX - left))); } catch { /* storage blocked: this visit only */ }
          };
          window.addEventListener("pointermove", move);
          window.addEventListener("pointerup", up);
        }} />
        <div className="detail">
          {stored ? (
            <>
              <div className="top">
                <div><h1>{stored.name ?? `${stored.startedAt.slice(0, 10)} · ${clockOf(stored.startedAt)}`}{live ? null : <button type="button" className="btn sm ghost" onClick={() => void rename()}>rename</button>}</h1>
                  <p className="muted">{stored.startedAt.slice(0, 10)} · {clockOf(stored.startedAt)} – {stored.stoppedAt ? clockOf(stored.stoppedAt) : "now"} · {length(stored.startedAt, stored.stoppedAt)}{stored.stopReason ? ` · ${stored.stopReason}` : ""}{stored.pausedS ? ` · paused ${Math.round(stored.pausedS / 60)} min` : ""} · <code>{stored.id}</code></p></div>
                <span className="spacer" />
                <Link className="btn sm" href={`/v1/notes?session=${encodeURIComponent(stored.id)}`}>Notes</Link>
                {live ? null : <button type="button" className="btn sm ghost" onClick={() => void remove()}>Delete</button>}
              </div>
              <div className="stats">
                <div><b>{dollars(usage)}</b>cost</div>
                <div><b>{stored.runs.length}</b>runs<small>{conversations.length} conversation{conversations.length === 1 ? "" : "s"} · {stored.runs.length - stored.chatRuns.length} temporary</small></div>
                <div><b>{records?.proposalSets.filter((set) => proposalsOf(set).length).length ?? "…"}</b>noticed<small>{records ? `${records.proposalSets.filter((set) => set.feedback?.decision === "accept").length} accepted · ${records.proposalSets.filter((set) => set.feedback?.decision === "reject").length} declined` : ""}</small></div>
                <div><b>{records?.changes.length ?? "…"}</b>task changes</div>
                <div><b>{records?.summaries.length ?? "…"}</b>notes<small>{records ? `${records.screenshots.length} looks stored` : ""}</small></div>
              </div>
              <div className="tabs2">{(["Runs", "Noticed", "Settings used"] as Tab[]).map((name) => <span key={name} className={tab === name ? "on" : ""} onClick={() => setTab(name)}>{name}</span>)}</div>
              {tab === "Runs" ? (
                <>
                  {conversations.map((conversation) => {
                    // The turns that ran in this session: a conversation goes on in later sessions, and those turns are theirs to show.
                    const ids = (records?.chat?.messages ?? []).filter((message) => message.chatId === conversation.id && message.role === "user" && stored.runs.includes(message.runId)).map((message) => message.runId);
                    return (
                      <div key={conversation.id} className="conv">
                        <h3>Conversation · {clockOf(conversation.startedAt)}<small>{ids.length && ids.length < conversation.turns ? `${ids.length} of ` : ""}{conversation.turns} turn{conversation.turns === 1 ? "" : "s"}</small></h3>
                        {ids.map((runId) => runs[runId] ? (
                          <div key={runId} className="turn">
                            <p><b>You</b> · {said[runId]}</p>
                            {replied[runId] ? <details className="reply"><summary><b>Agent</b> · {replied[runId]!.split("\n")[0]!.slice(0, 160)}</summary><Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_LINKS}>{replied[runId]!}</Markdown></details> : null}
                            <RunCard detail={runs[runId]!} events={runs[runId]!.events} runnerBaseUrl={runnerBaseUrl} open={false} turn />
                          </div>
                        ) : <Skel key={runId} w="100%" h={56} />)}
                      </div>
                    );
                  })}
                  {stored.runs.filter((runId) => !stored.chatRuns.includes(runId)).length ? (
                    <div className="conv"><h3>Temporary runs</h3>
                      {stored.runs.filter((runId) => !stored.chatRuns.includes(runId)).map((runId) => runs[runId] ? <RunCard key={runId} detail={runs[runId]!} events={runs[runId]!.events} runnerBaseUrl={runnerBaseUrl} open={false} /> : <Skel key={runId} w="100%" h={56} />)}
                    </div>
                  ) : null}
                  {stored.runs.length === 0 ? <p className="muted">No runs in this session.</p> : null}
                </>
              ) : tab === "Noticed" ? (
                <ul className="noticedList">
                  {(records?.proposalSets ?? []).filter((set) => proposalsOf(set).length).reverse().map((set) => (
                    <li key={set.ts}><time>{clockOf(set.ts)}</time>
                      <div><b>{proposalsOf(set)[0]?.task}</b><OnPause set={set} />{proposalsOf(set)[0]?.reason ? <div className="muted">{proposalsOf(set)[0]?.reason}</div> : null}
                        <div className="actions">{set.feedback ? <span className="decision">{decisionLabel(set.feedback)} · {clockOf(set.feedback.ts)}</span> : <>
                          <button type="button" className="btn sm" onClick={() => void decide(set, "accept")}>Mark accepted</button>
                          {(s.status?.rejectReasons ?? []).map((reason) => <button key={reason} type="button" className="chip" onClick={() => void decide(set, "reject", reason)}>{reason}</button>)}
                          <span className="muted">· labels only; nothing runs from here</span></>}</div>
                      </div>
                    </li>
                  ))}
                  {records && !records.proposalSets.some((set) => proposalsOf(set).length) ? <p className="muted">Nothing was noticed in this session.</p> : null}
                </ul>
              ) : (
                <table><tbody>{Object.entries(records?.detail?.session.settings ?? stored.settings).map(([key, value]) => <tr key={key}><td className="muted">{key}</td><td>{Array.isArray(value) ? value.join(", ") : String(value ?? "")}</td></tr>)}</tbody></table>
              )}
            </>
          ) : !listed && !issue ? (
            <div className="loading" aria-hidden="true">
              <Skel w={260} h={22} />
              <Skel w={340} />
              <div className="stats">{[0, 1, 2, 3, 4].map((box) => <div key={box}><Skel w={48} h={20} /><Skel w={72} h={10} /></div>)}</div>
              {[0, 1, 2].map((card) => <Skel key={card} w="100%" h={72} />)}
            </div>
          ) : <p className="muted">{sessions.length ? "Select a session." : ""}</p>}
        </div>
      </div>
    </div>
  );
}
