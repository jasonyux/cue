"use client";

import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { RunCard, mergedEvents } from "../live/shared";
import { Banners, TopBar } from "./Shell";
import { useSession } from "./useSession";

type Props = { expectedBackend: "python"; runnerBaseUrl: string };

const DEFAULT_PROMPT = "Look at what I was just doing and finish the obvious next step. Do not send or submit anything.";
const AGENT_SHORT: Record<string, string> = { desktop: "desktop", cmux: "cmux + Claude Code", claude: "Claude Code", codex: "Codex" };

// Temporary runs: one prompt, one run with the session's context, kept out of the conversation, like a private chat.
// A suggestion taken as a temporary run arrives here with the prompt filled in.
export function Temporary({ expectedBackend, runnerBaseUrl }: Props) {
  const s = useSession(runnerBaseUrl, expectedBackend);
  const params = useSearchParams();
  const [prompt, setPrompt] = useState(params.get("prompt") || DEFAULT_PROMPT);
  const proposalsTs = params.get("proposalsTs");
  const proposal = proposalsTs ? { proposalsTs, choice: Number(params.get("choice")) || 0 } : null;
  const agents = s.session?.settings.actorAgents ?? [];
  const [agent, setAgent] = useState<string>(agents.includes("claude") ? "claude" : agents.includes("cmux") ? "cmux" : agents[0] ?? "claude");
  const ids = [...(s.session?.runs ?? [])].filter((id) => !s.session?.chatRuns.includes(id)).reverse();
  const canRun = Boolean(s.session) && !s.session?.paused && s.runningId === null && !s.busy && prompt.trim().length > 0;
  return (
    <div className="v1">
      <TopBar s={s} />
      <Banners s={s} />
      <div className="main collapsed"><div className="center">
        <div className="feed"><div className="col">
          <div className="day">{s.session ? "Runs outside the conversation, this session, newest first. Each sees your recent notes and the latest suggestion." : "Start a session on the main page to run tasks."}</div>
          {ids.length === 0 && s.session ? <div className="empty">No temporary runs yet.</div> : null}
          {ids.map((id) => {
            const detail = s.runs[id];
            if (!detail) return <p key={id} className="muted">{id} · loading…</p>;
            return <RunCard key={id} detail={detail} events={mergedEvents(detail, s.streamed[id])} runnerBaseUrl={runnerBaseUrl} open={detail.run.status === "running"} onStop={s.stopRun} onAnswer={s.answerApproval} />;
          })}
        </div></div>
        <div className="composer"><div className="box">
          <textarea rows={3} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="One task for the agent, outside the conversation" />
          <div className="tools">
            {agents.length > 1 ? <select value={agent} onChange={(event) => setAgent(event.target.value)}>{agents.map((item) => <option key={item} value={item}>{AGENT_SHORT[item] ?? item}</option>)}</select> : <span>{AGENT_SHORT[agents[0] ?? agent] ?? agent}</span>}
            {proposal ? <span>from the suggestion noticed at {proposalsTs?.slice(11, 16)}</span> : null}
            {s.session?.paused ? <span>Paused: resume the session to run.</span> : null}
            <span className="spacer" />
            <button type="button" className="btn primary" disabled={!canRun} onClick={() => void s.startTemporary(prompt, (agents as string[]).includes(agent) ? agent : agents[0] ?? agent, proposal)}>Run</button>
          </div>
        </div></div>
      </div><aside className="side"><div className="strip"><b>Temporary runs stay out of the conversation</b></div></aside></div>
    </div>
  );
}
