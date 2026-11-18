"use client";

import type { AgentModel, LiveChat, RunDetail, RunEvent } from "@cue/contracts";
import { useEffect, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { AGENT_LABELS, MARKDOWN_LINKS, clockOf, mergedEvents } from "../live/shared";
import { Turn } from "./Turn";

type Props = {
  chat: LiveChat | null;
  browsing: boolean; // an earlier conversation: read-only
  runs: Record<string, RunDetail>;
  streamed: Record<string, RunEvent[]>;
  runnerBaseUrl: string;
  seed: string; // text put in the box from outside (Edit first)
  agents: string[]; // the agents the session set up
  agent: string; // the one the next message goes to
  onAgent: (agent: string) => void;
  models: AgentModel[]; // the models the agent lists, if any
  model: string | undefined;
  sessionModel: string | null; // the session's setting, offered when the agent does not list it
  onModel: (id: string) => void;
  paused: boolean;
  busy: boolean; // no session, a run in progress, or the page waiting on the backend
  runningId: string | null;
  sending: string | null; // a message on its way, shown until the conversation carries it
  onSend: (text: string, agent: string, model?: string) => Promise<boolean>;
  onStop: (runId: string) => Promise<void>;
  onAnswer: (runId: string, mode: "once" | "deny") => Promise<void>;
  onReset: () => Promise<void>;
  onBrowse: (chatId: string | null) => Promise<void>;
};

const AGENT_SHORT: Record<string, string> = { desktop: "desktop", cmux: "cmux + Claude Code", claude: "Claude Code", codex: "Codex" };

// The conversation with the agent: your messages, its replies with the run under each, the turn in progress with its
// permission prompt, and the box to type in.
export function Conversation({ chat, browsing, runs, streamed, runnerBaseUrl, seed, agents, agent, onAgent, models, model, sessionModel, onModel, paused, busy, runningId, sending, onSend, onStop, onAnswer, onReset, onBrowse }: Props) {
  const messages = chat?.messages ?? [];
  const running = browsing ? null : (chat?.runningRunId ?? null);
  const fixedAgent = chat?.agent ?? null;
  const [draft, setDraft] = useState(seed);
  useEffect(() => { setDraft(seed); }, [seed]);
  const send = async () => {
    const text = draft;
    setDraft("");
    if (!(await onSend(text, agent, model))) setDraft(text); // not sent: back in the box
  };
  const canSend = !busy && !paused && !browsing && draft.trim().length > 0;
  return (
    <div className="center">
      <div className="feed"><div className="col">
        {chat && chat.conversations.length > 1 ? (
          <div className="day">
            <select value={browsing ? chat.id ?? "" : ""} onChange={(event) => void onBrowse(event.target.value || null)}>
              <option value="">current conversation</option>
              {chat.conversations.filter((item) => item.id !== chat.current).map((item) => <option key={item.id} value={item.id}>{clockOf(item.startedAt)} · {item.turns} turn{item.turns === 1 ? "" : "s"} · {item.firstMessage.slice(0, 50)}</option>)}
            </select>
            {browsing ? <> · read only · <button type="button" className="more" onClick={() => void onBrowse(null)}>back to the current conversation</button></> : null}
          </div>
        ) : null}
        {messages.length === 0 && !sending ? <div className="empty">Nothing said yet. Tell the agent what to do below, or take a suggestion.</div> : <div className="day">Today · conversation with the agent</div>}
        {messages.map((message) => {
          const detail = runs[message.runId];
          return (
            <div key={`${message.runId}-${message.role}`} className="item">
              <time>{clockOf(message.ts)}</time>
              {message.role === "user" ? (
                <div className="card me">
                  <div className="kind">You{message.attached ? <> · <details><summary>{message.attached} note{message.attached === 1 ? "" : "s"} attached</summary><pre>{message.context}</pre></details></> : null}</div>
                  <p>{message.text}</p>
                </div>
              ) : (
                <div className={`card agent${message.status === "running" ? " running" : ""}`}>
                  <div className="kind">Agent{detail?.actorAgent ? ` · ${AGENT_LABELS[detail.actorAgent] ?? detail.actorAgent}` : ""} · turn {message.turn}{message.status !== "completed" ? ` · ${message.status}` : ""}</div>
                  {message.text ? <Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_LINKS}>{message.text}</Markdown> : message.status === "running" ? null : detail && message.status === "failed" ? null : <p className="muted">(no reply: the turn {message.status === "cancelled" ? "was stopped" : "failed"})</p>}
                  {detail ? <Turn detail={detail} events={mergedEvents(detail, streamed[message.runId])} runnerBaseUrl={runnerBaseUrl} finalText={message.text} onAnswer={browsing ? undefined : onAnswer} /> : null}
                </div>
              )}
            </div>
          );
        })}
        {sending ? <div className="item"><time /><div className="card me"><div className="kind">You</div><p>{sending}</p></div></div> : null}
        {running && !messages.some((message) => message.runId === running && message.role === "agent") && runs[running] ? (
          <div className="item"><time>{clockOf(runs[running]?.run.startedAt)}</time>
            <div className="card agent running"><div className="kind"><span className="dot" />Agent · working on turn {chat?.turn}</div>
              <Turn detail={runs[running]!} events={mergedEvents(runs[running]!, streamed[running])} runnerBaseUrl={runnerBaseUrl} onAnswer={onAnswer} /></div></div>
        ) : null}
      </div></div>
      {browsing ? null : (
        <div className="composer"><div className="box">
          <textarea rows={2} value={draft} placeholder={paused ? "Paused: resume the session to send" : "Tell the agent what to do… (⌘↵ to send)"} disabled={paused}
            onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && canSend) void send(); }} />
          <div className="tools">
            {fixedAgent ? <span>{AGENT_SHORT[fixedAgent] ?? fixedAgent}</span> : agents.length > 1 ? (
              <select value={agent} aria-label="Agent" onChange={(event) => onAgent(event.target.value)}>{agents.map((item) => <option key={item} value={item}>{AGENT_SHORT[item] ?? item}</option>)}</select>
            ) : <span>{AGENT_SHORT[agent] ?? agent}</span>}
            {models.length ? (
              <select value={model} aria-label="Model" title={models.find((item) => item.id === model)?.detail} onChange={(event) => onModel(event.target.value)}>
                {sessionModel && !models.some((item) => item.id === sessionModel) ? <option value={sessionModel}>{sessionModel}</option> : null}
                {models.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
              </select>
            ) : null}
            {chat && chat.turn > 0 && chat.pending.length ? <span>{chat.pending.length} note{chat.pending.length === 1 ? "" : "s"} since your last message go with it</span> : null}
            <span className="spacer" />
            {messages.length && !runningId ? <button type="button" className="btn ghost sm" disabled={busy} onClick={() => void onReset()}>New conversation</button> : null}
            {running ? <button type="button" className="btn sm" onClick={() => void onStop(running)}>Stop turn</button> : null}
            <button type="button" className="btn primary" disabled={!canSend} onClick={() => void send()}>Send</button>
          </div>
        </div></div>
      )}
    </div>
  );
}
