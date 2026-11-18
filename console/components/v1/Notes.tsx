"use client";

import { liveSessionListSchema, type LiveSummary } from "@cue/contracts";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { callRunner, clockOf, decisionLabel, proposalsOf, rawJson } from "../live/shared";
import { useRecords } from "./records";
import { Banners, Skel, TopBar } from "./Shell";
import { useSession, type SessionList } from "./useSession";

type Props = { expectedBackend: "python"; runnerBaseUrl: string };

type Entry = { ts: string; kind: "note" | "change" | "noticed"; text: string; note?: LiveSummary; flag?: "wrong" | "ok" | null; range?: string };

// One line a minute about what was on your screen, with the task-list changes and what it noticed on the same line
// of time; the looks behind the selected line on the right. The running session, or a stored one by ``?session=``.
export function Notes({ expectedBackend, runnerBaseUrl }: Props) {
  const s = useSession(runnerBaseUrl, expectedBackend);
  const params = useSearchParams();
  const [sessions, setSessions] = useState<SessionList>([]);
  useEffect(() => { void callRunner(runnerBaseUrl, expectedBackend, "/api/live/sessions?limit=50", liveSessionListSchema).then(setSessions, () => setSessions([])); }, [runnerBaseUrl, expectedBackend]);
  const wanted = params.get("session");
  const wantedId = params.get("id"); // a note to land on: selected and scrolled to once the records are in (a citation from another page)
  // The running session when there is one and none is asked for; else the stored one asked for, or the newest; nothing until the status is known.
  const sessionId = !s.status ? undefined : wanted && wanted !== s.session?.id ? wanted : s.session ? null : sessions[0]?.id;
  const { records, issue, reload, screenshotUrl } = useRecords(runnerBaseUrl, expectedBackend, sessionId);
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    const note = wantedId ? records?.summaries.find((item) => item.id === wantedId) : undefined;
    if (note) { setSelected(note.ts); document.getElementById(`note-${note.id}`)?.scrollIntoView({ block: "center" }); }
  }, [wantedId, records]);
  const entries: Entry[] = records ? [
    ...records.summaries.map((note): Entry => ({ ts: note.ts, kind: "note", text: note.summary, note, flag: note.feedback?.verdict ?? null, range: `${clockOf(note.windowStart)} – ${clockOf(note.windowEnd)}` })),
    ...records.changes.map((change): Entry => ({ ts: change.ts, kind: "change", text: `Task list: ${change.ops.length} change${change.ops.length === 1 ? "" : "s"}` })),
    ...records.proposalSets.filter((set) => proposalsOf(set).length).map((set): Entry => ({ ts: set.ts, kind: "noticed", text: `Noticed: ${proposalsOf(set)[0]?.task ?? ""}${set.feedback ? ` · ${decisionLabel(set.feedback)}` : " · no answer"}` })),
  ].sort((a, b) => b.ts.localeCompare(a.ts)) : [];
  const current = entries.find((entry) => entry.kind === "note" && entry.ts === selected)?.note ?? entries.find((entry) => entry.kind === "note")?.note ?? null;
  const looks = current && records ? records.screenshots.filter((shot) => shot.ts >= current.windowStart && shot.ts <= current.windowEnd) : [];
  const [shown, setShown] = useState<string | null>(null);
  const loading = !records && !issue && !(sessionId === undefined && s.status); // the notes are on their way
  const look = looks.find((shot) => shot.name === shown) ?? looks[looks.length - 1] ?? null;
  const feedbackPath = sessionId ? `/api/live/sessions/${encodeURIComponent(sessionId)}/summaries/feedback` : "/api/live/session/summaries/feedback";
  const flag = async (note: LiveSummary, verdict: "wrong" | "ok") => {
    const comment = verdict === "wrong" ? window.prompt("What did it get wrong? A few words, or leave it blank.", note.feedback?.comment ?? "") : "";
    if (comment === null) return;
    try {
      await callRunner(runnerBaseUrl, expectedBackend, feedbackPath, rawJson, { method: "POST", body: JSON.stringify({ summaryTs: note.ts, verdict, comment }) });
      await reload();
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Could not record the flag.");
    }
  };
  return (
    <div className="v1">
      <TopBar s={s} current="Notes" />
      <Banners s={s} />
      {issue ? <div className="issue">{issue}</div> : null}
      <div className="sub">
        <span className="muted">Session</span>
        <select value={sessionId ?? ""} onChange={(event) => { window.location.search = event.target.value ? `?session=${encodeURIComponent(event.target.value)}` : ""; }}>
          {s.session ? <option value="">{`running · ${clockOf(s.session.startedAt)}`}</option> : null}
          {sessions.filter((item) => item.id !== s.session?.id).map((item) => <option key={item.id} value={item.id}>{item.name ? `${item.name} · ` : ""}{item.startedAt.slice(0, 10)} {clockOf(item.startedAt)}</option>)}
        </select>
        <span className="spacer" />
        <span className="muted">{records ? `${records.screenshots.length} looks · ${records.summaries.length} notes · ${records.changes.length} task changes · ${records.proposalSets.filter((set) => proposalsOf(set).length).length} noticed` : ""}</span>
        {sessionId ? <Link className="btn sm ghost" href={`/v1/history?session=${encodeURIComponent(sessionId)}`}>Open in History</Link> : null}
      </div>
      <div className="page split2 notes">
        <div className="log">
          <p className="muted">One line a minute about what was on your screen. 👎 a line that got it wrong, 👍 one that is right; both teach the notes model.</p>
          {records && entries.length === 0 ? <div className="empty">No notes yet.</div> : sessionId === undefined && s.status ? <div className="empty">No session yet.</div> : null}
          <ul className="tl">
            {loading ? [0, 1, 2, 3, 4].map((row) => <li key={row} className="loading" aria-hidden="true"><time><Skel w={110} h={10} /></time><p><Skel w="94%" /><Skel w="88%" /><Skel w="60%" /></p></li>) : null}
            {entries.map((entry) => (
              <li key={`${entry.kind}-${entry.ts}`} id={entry.note?.id ? `note-${entry.note.id}` : undefined} className={`${entry.kind}${entry.note && entry.note === current ? " sel" : ""}`} onClick={() => entry.note ? setSelected(entry.ts) : undefined}>
                <time>{entry.range ?? clockOf(entry.ts)}</time>
                <p>{entry.text}
                  {entry.note ? <span className="thumbs on"><button type="button" className={entry.flag === "ok" ? "active" : ""} title="This is right" onClick={(event) => { event.stopPropagation(); void flag(entry.note!, "ok"); }}>👍</button><button type="button" className={entry.flag === "wrong" ? "active" : ""} title="This is wrong" onClick={(event) => { event.stopPropagation(); void flag(entry.note!, "wrong"); }}>👎</button></span> : null}
                  {entry.note?.feedback?.comment ? <span className="muted"> · {entry.note.feedback.comment}</span> : null}
                </p>
              </li>
            ))}
          </ul>
        </div>
        <aside className="rail">
          <h3>{current ? `The looks behind ${clockOf(current.windowStart)} – ${clockOf(current.windowEnd)}` : "The looks"}</h3>
          {look ? (
            <>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img className="look" src={screenshotUrl(look.name)} alt={`${look.app} — ${look.window}`} />
              <p className="muted"><b>{look.app}</b>{look.window ? ` — ${look.window}` : ""} · {clockOf(look.ts)}</p>
            </>
          ) : loading ? <Skel w="100%" h={220} /> : <p className="muted">{current ? "No screenshot was stored in this window (screenshots are taken on app switches unless set to every look)." : "Select a note."}</p>}
          {looks.length > 1 ? <div className="strip2">{looks.map((shot) => <button key={shot.name} type="button" className={shot === look ? "on" : ""} onClick={() => setShown(shot.name)}>{clockOf(shot.ts)}</button>)}</div> : null}
        </aside>
      </div>
    </div>
  );
}
