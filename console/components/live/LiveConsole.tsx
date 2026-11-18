"use client";

import {
  liveFeedbackResponseSchema,
  liveProposalSetsResponseSchema,
  liveSessionDeleteSchema,
  liveSessionDetailSchema,
  liveSessionListSchema,
  liveSessionRecordSchema,
  liveTaskFlagSchema,
  liveTasksResponseSchema,
  liveChatSchema,
  liveUsageSchema,
  liveSessionStatusSchema,
  liveSettingsCheckSchema,
  liveSummariesResponseSchema,
  liveSummaryFeedbackResponseSchema,
  runDetailSchema,
  runEventSchema,
  startRunResponseSchema,
  type LiveProposalSet,
  type LiveSessionDetail,
  type LiveSessionStatus,
  type LiveSettingsCheck,
  type LiveSummary,
  type LiveProvider,
  type LiveTask,
  type LiveTaskFlag,
  type LiveUsage,
  type LiveUsageRow,
  type RunDetail,
  type RunEvent,
  type LiveChat,
  type LiveChatMessage,
} from "@cue/contracts";
import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  AGENT_LABELS, DEFAULT_MAX_TURNS, FALLBACK_NOTE, MARKDOWN_LINKS, SIDEQUESTS, TASK_FLAG_REASONS, AcceptButton, FilteredProposals, ProposalLine, RunCard,
  callRunner, clockOf, decisionLabel, mergedEvents, proposalsOf, rawJson, usageLine, usePolled,
  type Parser, type TaskEdit, type TaskFlagHandlers,
} from "./shared";

type Props = {
  expectedBackend: "python";
  runnerBaseUrl: string;
  nav?: ReactNode; // what sits at the end of the top bar: the landing page's theme switch
};

type ScreenshotMode = "switch" | "always" | "off";
type ProposerSource = "tasks" | "worklog" | "snapshots" | "current_window" | "feedback" | "chat" | "preferences";
const PROPOSER_SOURCES: { value: ProposerSource; label: string }[] = [
  { value: "tasks", label: "the task list (belief, across sessions)" },
  { value: "worklog", label: "the worklog (past summaries)" },
  { value: "snapshots", label: "raw snapshots of this window" },
  { value: "current_window", label: "the current window's tree" },
  { value: "feedback", label: "recent decisions on proposals (accepted and rejected, across sessions)" },
  { value: "chat", label: "what was lately asked of the assistant in the chat, and answered" },
  { value: "preferences", label: "the person's standing preferences (preferences.md)" },
];

// The permission rules as the form holds them, "Slack: look, Code: use", and back.
const rulesText = (rules?: Record<string, string>) => Object.entries(rules ?? {}).map(([name, level]) => `${name}: ${level}`).join(", ");
const rulesFrom = (text: string) => Object.fromEntries(text.split(",").filter((item) => item.trim()).map((item) => { const [name, level = ""] = item.split(":").map((part) => part.trim()); return [name, level]; }));

type SessionForm = {
  summaryModel: string;
  summaryBaseUrl: string;
  summaryProvider: LiveProvider;
  summaryStream: boolean;
  proposerModel: string;
  proposerBaseUrl: string;
  proposerProvider: LiveProvider;
  proposerStream: boolean;
  snapshotIntervalS: string;
  summaryIntervalS: string;
  proposerIntervalS: string;
  maxSessionMinutes: string;
  screenshotMode: ScreenshotMode;
  permissionsApps: string;
  permissionsSites: string;
  summaryThinking: boolean;
  proposerThinking: boolean;
  summaryScreenshots: boolean;
  proposerScreenshots: boolean;
  proposerInputs: ProposerSource[];
  maxProposals: string;
  summaryMaxTokens: string;
  proposerMaxTokens: string;
  proposerFeedbackRejected: string;
  proposerFeedbackAccepted: string;
  proposerFeedbackRecent: string;
  proposerDedup: boolean;
  proposerDedupThreshold: string;
  proposerDedupNotNowHours: string;
  proposerDedupSessionHours: string;
  embedModel: string;
  embedBaseUrl: string;
  notify: boolean;
  beliefModel: string;
  beliefBaseUrl: string;
  beliefProvider: LiveProvider;
  beliefStream: boolean;
  beliefIntervalS: string;
  beliefThinking: boolean;
  beliefMaxTokens: string;
  beliefStaleDays: string;
  beliefDoneStepDays: string;
  beliefTrashDays: string;
  streamLookbackDays: string;
  actorYieldS: string;
  actorContextSummaries: string;
  actorDesktopSource: string;
  desktopRemoteProvider: string;
  desktopRemoteUrl: string;
  actorAgents: string;
  actorCmuxModel: string;
  actorCmuxPermissionMode: string;
  actorCmuxCwd: string;
  actorClaudeModel: string;
  actorClaudePermissionMode: string;
  actorClaudeCwd: string;
  actorCodexModel: string;
  actorCodexSandbox: string;
  actorCodexCwd: string;
};

type SessionList = ReturnType<typeof liveSessionListSchema.parse>;

const LIVE = "live";

type ProposalSetListProps = {
  proposalSets: LiveProposalSet[];
  busy: boolean;
  acceptLabel: string;
  onDecide: (item: LiveProposalSet, decision: "accept" | "reject", choice: number) => void;
  onChat?: ((item: LiveProposalSet, choice: number) => void) | undefined; // accept, and put the proposal in the chat box instead of the run prompt
  onReason: (item: LiveProposalSet, reason: string, typed: boolean) => void; // why a proposal set was rejected, given after the fact; typed: their own words, not a preset
  reasons: string[]; // the one-click reasons, from the backend
};

function ProposalSetList({ proposalSets, busy, acceptLabel, onDecide, onChat, onReason, reasons }: ProposalSetListProps) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [asking, setAsking] = useState<string | null>(null); // the proposal set just rejected, until a reason is given or skipped
  const giveReason = (item: LiveProposalSet, reason: string, typed: boolean) => { onReason(item, reason, typed); setAsking(null); };
  const askOther = (item: LiveProposalSet) => {
    const typed = window.prompt("Why did you reject this proposal? A few words.")?.trim();
    if (typed) giveReason(item, typed, true);
  };
  if (proposalSets.length === 0) return <p className="liveMuted">No proposals yet.</p>;
  return (
    <ol className="liveList">
      {[...proposalSets].reverse().map((item) => {
        const proposals = proposalsOf(item);
        const [top, ...others] = proposals;
        const open = Boolean(expanded[item.ts]);
        return (
          <li key={item.ts} className={top ? "liveProposal" : ""}>
            <span className="liveTime">{clockOf(item.ts)}</span>
            {item.error ? <span className="liveError">error: {item.error}</span> : (
              <>
                {top ? <ProposalLine proposal={top} /> : (
                  <>
                    <strong>none</strong>
                    {item.reason ? <div className="liveMuted">{item.reason}</div> : null}
                  </>
                )}
                {item.policy ? <div className="liveMuted">policy: {item.policy}</div> : null}
                {item.fallback ? <div className="liveMuted">{FALLBACK_NOTE}</div> : null}
                {top ? (
                  <div className="liveActions liveFeedback">
                    <AcceptButton label={acceptLabel} disabled={busy} onRun={() => onDecide(item, "accept", 0)} onChat={onChat ? () => onChat(item, 0) : undefined} />
                    <button type="button" className="secondary liveInline" disabled={busy} onClick={() => { onDecide(item, "reject", 0); setAsking(item.ts); }}>Reject</button>
                    {others.length ? (
                      <button type="button" className="secondary liveInline" onClick={() => setExpanded({ ...expanded, [item.ts]: !open })}>
                        {open ? "Hide other choices" : `See other choices (${others.length})`}
                      </button>
                    ) : null}
                    {item.feedback ? (
                      <span className={`liveDecision ${item.feedback.decision}`}>
                        {decisionLabel(item.feedback)} · {clockOf(item.feedback.ts)}
                        {item.feedback.digestError ? <span className="liveMuted" title={item.feedback.digestError}> · digest failed</span>
                          : item.feedback.digested === false ? <span className="liveMuted"> · <span className="liveDot running" aria-hidden="true" />digesting…</span> : null}
                      </span>
                    ) : null}
                  </div>
                ) : null}
                {asking === item.ts ? (
                  <div className="liveActions liveFeedback">
                    <span className="liveMuted">Why?</span>
                    {reasons.map((reason) => (
                      <button key={reason} type="button" className="secondary liveInline" disabled={busy} onClick={() => giveReason(item, reason, false)}>{reason}</button>
                    ))}
                    <button type="button" className="secondary liveInline" disabled={busy} onClick={() => askOther(item)}>other…</button>
                    <button type="button" className="secondary liveInline" onClick={() => setAsking(null)}>Skip</button>
                  </div>
                ) : null}
                {item.filtered?.length ? <FilteredProposals item={item} allFiltered={!top} /> : null}
                {top && open ? (
                  <ol className="liveAlternatives" start={2}>
                    {others.map((proposal, index) => (
                      <li key={`${item.ts}-${index + 1}`} className={item.feedback?.choice === index + 1 ? "liveChosen" : ""}>
                        <ProposalLine proposal={proposal} />
                        <div className="liveActions liveFeedback">
                          <AcceptButton label={acceptLabel} disabled={busy} onRun={() => onDecide(item, "accept", index + 1)} onChat={onChat ? () => onChat(item, index + 1) : undefined} />
                        </div>
                      </li>
                    ))}
                  </ol>
                ) : null}
              </>
            )}
          </li>
        );
      })}
    </ol>
  );
}

type TaskListProps = { tasks: LiveTask[]; note: string; flags?: LiveTaskFlag[]; onEdit?: ((edit: TaskEdit) => void) | undefined; onFlag?: TaskFlagHandlers | undefined }; // no onEdit: read-only

function TaskList({ tasks, note, flags = [], onEdit, onFlag }: TaskListProps) {
  const order = { active: 0, paused: 1, done: 2 };
  const sorted = [...tasks].sort((a, b) => order[a.status] - order[b.status] || Number(a.title === SIDEQUESTS) - Number(b.title === SIDEQUESTS) || b.lastSeen.localeCompare(a.lastSeen));
  const open = sorted.filter((task) => task.status !== "done");
  const done = sorted.filter((task) => task.status === "done");
  return (
    <>
      <p className="liveMuted">{note}</p>
      {tasks.length === 0 ? <p className="liveMuted">No tasks yet. The belief updater adds them as the worklog grows.</p> : null}
      <ul className="liveTasks">{open.map((task) => <TaskRow key={task.id} task={task} flags={flags} onEdit={onEdit} onFlag={onFlag} />)}</ul>
      {done.length ? (
        <details>
          <summary>{done.length} done</summary>
          <ul className="liveTasks">{done.map((task) => <TaskRow key={task.id} task={task} flags={flags} onEdit={onEdit} onFlag={onFlag} />)}</ul>
        </details>
      ) : null}
    </>
  );
}

function LoopStatus({ nextAt }: { nextAt: string | null }) {
  // A loop is running while its next-tick time is cleared; idle otherwise.
  return nextAt ? (
    <><span className="liveDot idle" aria-hidden="true" />next {clockOf(nextAt)}</>
  ) : (
    <><span className="liveDot running" aria-hidden="true" />running now</>
  );
}

const DONE_SHOWN = 5; // finished entries shown per task before "Show all"

function TaskRow({ task, flags, onEdit, onFlag }: { task: LiveTask; flags: LiveTaskFlag[]; onEdit?: ((edit: TaskEdit) => void) | undefined; onFlag?: TaskFlagHandlers | undefined }) {
  const [showAll, setShowAll] = useState(false);
  const [asking, setAsking] = useState<string | null>(null); // the id of the flag just thumbed down, until a reason is given or skipped
  // The flag on the task (step null) or on an entry. An entry's flag is matched by its text, not its number: the numbers
  // shift whenever the list is reordered (an entry added before it, or one finished).
  const current = (step: number | null) => flags.find((item) => item.taskId === task.id && (step === null ? item.step === null : item.text === task.steps[step - 1]?.text));
  const tag = (step: number | null) => {
    const flag = current(step);
    return flag?.verdict === "down" && flag.reason ? <span className="liveMuted"> {flag.reason}</span> : null;
  };
  const thumbs = (step: number | null) => {
    if (!onFlag) return null;
    const flag = current(step);
    const click = (verdict: "up" | "down") => async () => {
      if (flag?.verdict === verdict) { onFlag.unflag(flag.id); setAsking(null); return; } // the active thumb again: taken back
      const made = await onFlag.flag(task.id, step, verdict);
      setAsking(made && verdict === "down" ? made.id : null);
    };
    return (
      <>
        <button type="button" className={`liveThumb${flag?.verdict === "up" ? " active" : ""}`} title="This is right" disabled={onFlag.busy} onClick={() => void click("up")()}>👍</button>
        <button type="button" className={`liveThumb${flag?.verdict === "down" ? " active" : ""}`} title="This is wrong" disabled={onFlag.busy} onClick={() => void click("down")()}>👎</button>
      </>
    );
  };
  const reasons = (step: number | null) => {
    const flag = current(step);
    if (!onFlag || !flag || asking !== flag.id) return null;
    const give = (reason: string) => { onFlag.reason(flag.id, reason); setAsking(null); };
    return (
      <div className="liveActions liveFeedback">
        <span className="liveMuted">Why?</span>
        {TASK_FLAG_REASONS.map((reason) => <button key={reason} type="button" className="secondary liveInline" disabled={onFlag.busy} onClick={() => give(reason)}>{reason}</button>)}
        <button type="button" className="secondary liveInline" disabled={onFlag.busy} onClick={() => { const typed = window.prompt("What is wrong with it? A few words.")?.trim(); if (typed) give(typed); }}>other…</button>
        <button type="button" className="secondary liveInline" onClick={() => setAsking(null)}>Skip</button>
      </div>
    );
  };
  const entries = task.steps.map((step, index) => ({ step, number: index + 1 })); // numbers are the store's, so edits refer to the right entry
  const doneEntries = entries.filter((entry) => entry.step.doneAt);
  const shown = entries.filter((entry) => !entry.step.doneAt).concat(showAll ? doneEntries : doneEntries.slice(0, DONE_SHOWN)); // pending first, always
  const sidequests = task.title === SIDEQUESTS;
  return (
    <li className={`liveTask ${task.status}${sidequests ? " sidequests" : ""}`}>
      <span className="liveTaskStatus">{sidequests && task.status === "active" ? "one-offs" : task.status}</span>
      <div>
        <strong>{task.title}</strong>{tag(null)}
        {onEdit ? (
          <>
            <button type="button" className="secondary liveTaskDone" onClick={() => onEdit({ op: "status", taskId: task.id, done: task.status !== "done" })}>
              {task.status === "done" ? "Reopen" : "Mark done"}
            </button>
            <button type="button" className="secondary liveTaskDone" onClick={() => { const title = window.prompt("New title", task.title)?.trim(); if (title && title !== task.title) onEdit({ op: "rename", taskId: task.id, title }); }}>Rename</button>
            <button type="button" className="secondary liveTaskDone" onClick={() => { if (window.confirm(`Move "${task.title}" to the trash?`)) onEdit({ op: "delete", taskId: task.id }); }}>Delete</button>
          </>
        ) : null}
        {thumbs(null)}
        {reasons(null)}
        {shown.length ? (
          <ul className="liveSteps">
            {shown.map(({ step, number }) => (
              <li key={number} className={step.doneAt ? "done" : ""}>
                {onEdit ? (
                  <input type="checkbox" checked={Boolean(step.doneAt)} aria-label={`Done: ${step.text}`} onChange={(event) => onEdit({ op: "status", taskId: task.id, step: number, done: event.target.checked })} />
                ) : (step.doneAt ? "☑" : "☐")} {step.text}{tag(number)}
                {onEdit ? <button type="button" className="liveMore" title="To the trash" onClick={() => onEdit({ op: "update", taskId: task.id, remove: [number] })}>×</button> : null}
                {thumbs(number)}
                {reasons(number)}
              </li>
            ))}
            {doneEntries.length > DONE_SHOWN ? (
              <li><button type="button" className="liveMore" onClick={() => setShowAll((value) => !value)}>{showAll ? "Show fewer" : `Show all ${doneEntries.length} done`}</button></li>
            ) : null}
          </ul>
        ) : null}
        <p className="liveMuted">{task.id}{task.lastSeen ? ` · last seen ${clockOf(task.lastSeen)}` : ""}{task.sessionId ? ` · ${task.sessionId}` : ""}</p>
      </div>
    </li>
  );
}

// Deleted tasks and entries, until the trash empties; Restore needs Edit mode like the other corrections.
function TrashList({ trash, onEdit }: { trash: LiveTask[]; onEdit?: ((edit: TaskEdit) => void) | undefined }) {
  if (trash.length === 0) return null;
  const rows = trash.flatMap((task) => task.deletedAt
    ? [{ key: task.id, label: task.title, deletedAt: task.deletedAt, edit: { op: "restore" as const, taskId: task.id } }]
    : task.steps.map((step, index) => ({ key: `${task.id}-${index + 1}`, label: `${task.title}: ${step.text}`, deletedAt: step.deletedAt ?? "", edit: { op: "restore" as const, taskId: task.id, step: index + 1 } })));
  return (
    <details>
      <summary>Trash ({rows.length})</summary>
      <ul className="liveTasks">
        {rows.map((row) => (
          <li key={row.key} className="liveTask">
            <div>
              {row.label} <span className="liveMuted">· deleted {clockOf(row.deletedAt)}</span>
              {onEdit ? <button type="button" className="secondary liveTaskDone" onClick={() => onEdit(row.edit)}>Restore</button> : null}
            </div>
          </li>
        ))}
      </ul>
    </details>
  );
}

type SummaryListProps = {
  summaries: LiveSummary[];
  busy: boolean;
  onFlag: (item: LiveSummary, comment: string, verdict: "wrong" | "ok") => Promise<void>;
};

function SummaryList({ summaries, busy, onFlag }: SummaryListProps) {
  const [editing, setEditing] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  if (summaries.length === 0) return <p className="liveMuted">No summary yet.</p>;
  return (
    <ol className="liveList">
      {[...summaries].reverse().map((item) => (
        <li key={item.ts} className={item.feedback?.verdict === "wrong" ? "liveFlagged" : ""}>
          <span className="liveTime">{clockOf(item.windowStart)}–{clockOf(item.windowEnd)}</span> {item.summary}
          {item.fallback ? <div className="liveMuted">{FALLBACK_NOTE}</div> : null}
          <div className="liveActions liveFeedback">
            {editing === item.ts ? (
              <>
                <textarea
                  className="liveComment"
                  rows={2}
                  placeholder="Optional: what went wrong? e.g. I was reading the ICLR 2027 submission, not ConFit."
                  value={comment}
                  onChange={(event) => setComment(event.target.value)}
                />
                <button type="button" className="primary liveInline" disabled={busy} onClick={() => { void onFlag(item, comment, "wrong").then(() => { setEditing(null); setComment(""); }); }}>Save flag</button>
                <button type="button" className="secondary liveInline" disabled={busy} onClick={() => { setEditing(null); setComment(""); }}>Cancel</button>
              </>
            ) : (
              <>
                <button type="button" className="secondary liveInline" disabled={busy} onClick={() => { setEditing(item.ts); setComment(item.feedback?.comment ?? ""); }}>Flag</button>
                {item.feedback ? (
                  <span className="liveDecision reject">flagged · {clockOf(item.feedback.ts)}{item.feedback.comment ? ` · ${item.feedback.comment}` : ""}</span>
                ) : null}
              </>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}

type RunListProps = {
  ids: string[];
  runs: Record<string, RunDetail>;
  streamed: Record<string, RunEvent[]>;
  runnerBaseUrl: string;
  onStop?: ((runId: string) => void) | undefined;
  onAnswer?: ((runId: string, mode: "once" | "deny") => void) | undefined;
};

// Every task run started from the session, newest first. Cards stay folded so the page stays short; only a run
// that is still in progress is unfolded, so its stream is visible.
function RunList({ ids, runs, streamed, runnerBaseUrl, onStop, onAnswer }: RunListProps) {
  if (ids.length === 0) return <p className="liveMuted">No runs in this session yet.</p>;
  return (
    <div className="liveRuns">
      {[...ids].reverse().map((id) => {
        const detail = runs[id];
        if (!detail) return <p key={id} className="liveMuted">{id} · loading…</p>;
        return <RunCard key={id} detail={detail} events={mergedEvents(detail, streamed[id])} runnerBaseUrl={runnerBaseUrl} open={detail.run.status === "running"} onStop={onStop} onAnswer={onAnswer} />;
      })}
    </div>
  );
}

type ChatPanelProps = {
  chat: LiveChat | null;
  runs: Record<string, RunDetail>;
  streamed: Record<string, RunEvent[]>;
  runnerBaseUrl: string;
  onBrowse: (chatId: string | null) => void; // show an earlier conversation, or null for the current one
  readOnly?: boolean; // a stored session's chat: nothing to send
  seed?: string; // text put in the box from outside (Accept → Chat); typing itself stays inside ChatCompose
  agents?: string[]; // the agents the session set up
  agent?: string;
  onAgent?: (agent: string) => void;
  turns?: string;
  onTurns?: (turns: string) => void;
  busy?: boolean; // no session, or a run (a turn or not) in progress
  paused?: boolean; // the session is paused: nothing is sent until it is resumed
  onSend?: (text: string) => Promise<boolean>; // true once the message is on its way
  onStop?: (runId: string) => void;
  onReset?: () => void;
  onAnswer?: (runId: string, mode: "once" | "deny") => void;
};

// The transcript: the person's messages with the context they carried, the agent's replies as markdown, each reply
// with its run folded under it (steps, screenshots, approvals, cost); the turn in progress streams the same way.
function ChatPanel({ chat, runs, streamed, runnerBaseUrl, onBrowse, readOnly = false, seed = "", agents = [], agent = "", onAgent, turns = "", onTurns, busy = true, paused = false, onSend, onStop, onReset, onAnswer }: ChatPanelProps) {
  const messages = chat?.messages ?? [];
  const browsing = Boolean(chat && chat.id && chat.id !== chat.current); // an earlier conversation: read-only
  const running = browsing ? null : (chat?.runningRunId ?? null);
  const fixedAgent = chat?.agent ?? null;
  const chosen = agents.find((item) => item === agent) ?? agents[0];
  const pending = chat?.pending ?? [];
  const conversations = chat?.conversations ?? [];
  const label = (item: LiveChat["conversations"][number]) => `${clockOf(item.startedAt)} · ${item.turns} turn${item.turns === 1 ? "" : "s"} · ${item.firstMessage.slice(0, 60)}${item.firstMessage.length > 60 ? "…" : ""}`;
  const turnCard = (runId: string, open: boolean) => {
    const detail = runs[runId];
    return detail ? <RunCard detail={detail} events={mergedEvents(detail, streamed[runId])} runnerBaseUrl={runnerBaseUrl} open={open} onStop={readOnly ? undefined : onStop} onAnswer={readOnly ? undefined : onAnswer} /> : <p className="liveMuted">{runId} · loading…</p>;
  };
  const picker = conversations.length > 1 || (conversations.length === 1 && readOnly);
  const canReset = messages.length > 0 && !readOnly && !browsing;  // at the top, away from Send
  return (
    <>
      {picker || canReset ? (
        <div className="liveActions">
          {picker ? (
            <label>Conversation <select value={chat?.id ?? ""} onChange={(event) => onBrowse(event.target.value === chat?.current ? null : event.target.value || null)}>
              {conversations.map((item) => <option key={item.id} value={item.id}>{item.id === chat?.current ? "current · " : ""}{label(item)}</option>)}
            </select></label>
          ) : null}
          {browsing && !readOnly ? <span className="liveMuted">read-only · <button type="button" className="liveMore" onClick={() => onBrowse(null)}>back to the current conversation</button></span> : null}
          {canReset ? <button type="button" className="secondary" disabled={running !== null} onClick={onReset}>New conversation</button> : null}
        </div>
      ) : null}
      {messages.length === 0 && !running ? <p className="liveMuted">No messages yet.</p> : null}
      <ol className="liveChat">
        {messages.map((message: LiveChatMessage) => (
          <li key={`${message.runId}-${message.role}`} className={`liveChatMessage ${message.role}`}>
            <div className="liveMuted liveChatMeta">{message.role === "user" ? "You" : "Agent"} · turn {message.turn} · {clockOf(message.ts)}{message.status && message.status !== "completed" ? ` · ${message.status}` : ""}</div>
            {message.role === "user" ? (
              <>
                <p className="liveChatText">{message.text}</p>
                <details className="liveFiltered">
                  <summary className="liveMuted">{message.attached ? `${message.attached} worklog line${message.attached === 1 ? "" : "s"} attached` : "context attached"}</summary>
                  <pre>{message.context}</pre>
                </details>
              </>
            ) : (
              <>
                {message.text ? <div className="liveMarkdown"><Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_LINKS}>{message.text}</Markdown></div> : <p className="liveMuted">(no reply: the turn {message.status === "cancelled" ? "was stopped" : "failed"})</p>}
                {turnCard(message.runId, false)}
              </>
            )}
          </li>
        ))}
        {running ? (
          <li className="liveChatMessage agent">
            <div className="liveMuted liveChatMeta"><span className="liveDot running" aria-hidden="true" />Agent · working on turn {chat?.turn}</div>
            {turnCard(running, true)}
          </li>
        ) : null}
      </ol>
      {readOnly || browsing || !onSend ? null : (
        <ChatCompose seed={seed} first={!chat || chat.turn === 0} pending={pending} agents={agents} fixedAgent={fixedAgent} chosen={chosen} onAgent={onAgent} turns={turns} onTurns={onTurns} busy={busy} paused={paused} running={running} onSend={onSend} onStop={onStop} />
      )}
    </>
  );
}

type ChatComposeProps = {
  seed: string;
  first: boolean; // no turn yet: the message carries the live context
  pending: LiveChat["pending"];
  agents: string[];
  fixedAgent: string | null;
  chosen: string | undefined;
  onAgent?: ((agent: string) => void) | undefined;
  turns: string;
  onTurns?: ((turns: string) => void) | undefined;
  busy: boolean;
  paused: boolean;
  running: string | null;
  onSend: (text: string) => Promise<boolean>;
  onStop?: ((runId: string) => void) | undefined;
};

// The message box with its controls. The draft is this component's own state, so a keystroke re-renders the box and
// not the whole console with every task and run card in it.
function ChatCompose({ seed, first, pending, agents, fixedAgent, chosen, onAgent, turns, onTurns, busy, paused, running, onSend, onStop }: ChatComposeProps) {
  const [draft, setDraft] = useState(seed);
  useEffect(() => { setDraft(seed); }, [seed]); // Accept → Chat puts a proposal in the box
  const send = async () => {
    const text = draft;
    setDraft("");
    if (!(await onSend(text))) setDraft(text); // not sent: back in the box
  };
  return (
    <>
      <textarea value={draft} rows={3} placeholder="Message the agent…" onChange={(event) => setDraft(event.target.value)} />
      <p className="liveMuted">
        {first
          ? "The first message carries the session's live context (recent worklog and latest proposal)."
          : pending.length ? <>{pending.length} worklog line{pending.length === 1 ? "" : "s"} since your previous message will be attached.</> : "Nothing new was observed since your previous message."}
      </p>
      {pending.length ? (
        <details className="liveFiltered"><summary className="liveMuted">Show them</summary><ul className="liveList">{pending.map((item) => <li key={item.ts}><span className="liveTime">{clockOf(item.windowStart)}–{clockOf(item.windowEnd)}</span> {item.summary}</li>)}</ul></details>
      ) : null}
      <div className="liveActions">
        {fixedAgent ? <span className="liveMuted">Agent: {AGENT_LABELS[fixedAgent] ?? fixedAgent}</span> : agents.length > 1 ? (
          <label>Agent <select value={chosen} onChange={(event) => onAgent?.(event.target.value)}>{agents.map((item) => <option key={item} value={item}>{AGENT_LABELS[item]}</option>)}</select></label>
        ) : null}
        <label>Max turns <input type="number" min={1} max={300} value={turns} onChange={(event) => onTurns?.(event.target.value)} /></label>
        <button type="button" className="primary" disabled={busy || paused || !draft.trim()} onClick={() => void send()}>Send</button>
        {paused ? <span className="liveMuted">Paused: resume the session to send.</span> : null}
        {running ? <button type="button" className="secondary" onClick={() => onStop?.(running)}>Stop turn</button> : null}
      </div>
    </>
  );
}

function dollars(rows: LiveUsageRow[]): string {
  return rows.every((row) => row.costUsd !== undefined) ? `$${rows.reduce((sum, row) => sum + (row.costUsd ?? 0), 0).toFixed(4)}` : "—";
}

type Counter = "calls" | "inputTokens" | "cachedInputTokens" | "outputTokens";
const COUNTERS: Counter[] = ["calls", "inputTokens", "cachedInputTokens", "outputTokens"];

// One block per model: its total across modules, then one line per module (summarizer, belief updater, proposer, agent).
function CostTable({ rows }: { rows: LiveUsageRow[] }) {
  const models = [...new Set(rows.map((row) => row.model))];
  if (rows.length === 0) return <p className="liveMuted">No model calls.</p>;
  return (
    <table className="liveCost">
        <thead><tr><th>Model</th><th>Module</th><th>Calls</th><th>Input</th><th>Cached</th><th>Output</th><th>Cost</th></tr></thead>
        <tbody>
          {models.map((model) => {
            const mine = rows.filter((row) => row.model === model);
            return (
              <Fragment key={model}>
                <tr className="liveCostModel">
                  <td>{model}</td><td>all</td>
                  {COUNTERS.map((key) => <td key={key}>{mine.reduce((sum, row) => sum + row[key], 0).toLocaleString()}</td>)}
                  <td>{dollars(mine)}</td>
                </tr>
                {mine.map((row) => (
                  <tr key={row.module} className="liveMuted">
                    <td /><td>{row.module}</td>
                    {COUNTERS.map((key) => <td key={key}>{row[key].toLocaleString()}</td>)}
                    <td>{dollars([row])}</td>
                  </tr>
                ))}
              </Fragment>
            );
          })}
        </tbody>
      </table>
  );
}

// Folded by default. During a session: this session's tokens so far. Between sessions: the last days or everything stored, one view at a time.
function CostPanel({ usage, active }: { usage: LiveUsage; active: boolean }) {
  const [view, setView] = useState<"recent" | "all">("recent");
  const views = [["recent", `Last ${usage.days} days`], ["all", "All sessions"]] as const;
  return (
    <details className="livePanel liveFold">
      <summary><h2>Cost</h2></summary>
      {active && usage.session ? (
        <>
          <h3>This session so far</h3>
          <CostTable rows={usage.session} />
        </>
      ) : (
        <>
          <div className="liveActions">
            {views.map(([key, label]) => (
              <button key={key} type="button" className={`${view === key ? "primary" : "secondary"} liveInline`} onClick={() => setView(key)}>{label}</button>
            ))}
          </div>
          <CostTable rows={usage[view]} />
        </>
      )}
      <p className="liveMuted">Tokens and estimated cost per model, split by module, from the stored session files. A dash means the price table has no entry for that model.</p>
    </details>
  );
}

export function LiveConsole({ expectedBackend, runnerBaseUrl, nav }: Props) {
  const [status, setStatus] = usePolled<LiveSessionStatus | null>(null);
  const [summaries, setSummaries] = usePolled<LiveSummary[]>([]);
  const [proposalSets, setProposalSets] = usePolled<LiveProposalSet[]>([]);
  const [sessions, setSessions] = usePolled<SessionList>([]);
  const [viewing, setViewing] = useState<string>(LIVE);
  const [history, setHistory] = useState<LiveSessionDetail | null>(null);
  const [nameDraft, setNameDraft] = useState("");
  const [editingTasks, setEditingTasks] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [form, setForm] = useState<SessionForm | null>(null);
  const [waiting, setWaiting] = useState<string | null>(null); // what the page is waiting on the backend for, shown under the banner
  const busy = waiting !== null;
  const [usage, setUsage] = usePolled<LiveUsage | null>(null);
  const [checking, setChecking] = useState(false);
  const [checks, setChecks] = useState<LiveSettingsCheck | null>(null);
  const [issue, setIssue] = useState<string | null>(null);
  const promptBox = useRef<HTMLTextAreaElement>(null); // uncontrolled: typing in it re-renders nothing
  const [maxTurns, setMaxTurns] = useState(String(DEFAULT_MAX_TURNS));
  const [runAgent, setRunAgent] = useState("desktop");
  const [runs, setRuns] = useState<Record<string, RunDetail>>({});
  const [streamed, setStreamed] = useState<Record<string, RunEvent[]>>({});
  const [runIssue, setRunIssue] = useState<string | null>(null);
  const [chatIssue, setChatIssue] = useState<string | null>(null); // the chat's own errors, shown under its messages
  const [chat, setChat] = usePolled<LiveChat | null>(null);
  const [chatSeed, setChatSeed] = useState(""); // what Accept → Chat put in the chat box; "" again once sent
  const [chatTurns, setChatTurns] = useState(String(DEFAULT_MAX_TURNS));
  const [chatAgent, setChatAgent] = useState("cmux");
  const [chatView, setChatView] = useState<LiveChat | null>(null); // an earlier conversation of the live session, while browsing it
  const [storedChat, setStoredChat] = useState<LiveChat | null>(null); // a stored session's chat, in the history view
  const eventSource = useRef<{ runId: string; source: EventSource } | null>(null);
  const runsRef = useRef(runs);
  runsRef.current = runs;

  const call = <T,>(path: string, schema: Parser<T>, init?: RequestInit, timeoutMs?: number) => callRunner(runnerBaseUrl, expectedBackend, path, schema, init, timeoutMs);

  const closeStream = useCallback(() => {
    eventSource.current?.source.close();
    eventSource.current = null;
  }, []);

  useEffect(() => closeStream, [closeStream]);

  const loadRun = useCallback(async (runId: string) => {
    try {
      const detail = await callRunner(runnerBaseUrl, expectedBackend, `/api/runs/${runId}`, runDetailSchema);
      setRuns((current) => ({ ...current, [runId]: detail }));
      if (detail.run.status !== "running" && eventSource.current?.runId === runId) closeStream();
      return detail;
    } catch (error) {
      setRunIssue(error instanceof Error ? error.message : "Could not load the run.");
      return null;
    }
  }, [runnerBaseUrl, expectedBackend, closeStream]);

  const attachStream = useCallback((runId: string, url: string) => {
    if (eventSource.current?.runId === runId) return;
    closeStream();
    const source = new EventSource(`${runnerBaseUrl}${url}`);
    eventSource.current = { runId, source };
    source.onmessage = (message) => {
      const parsed = runEventSchema.safeParse(JSON.parse(message.data));
      if (!parsed.success) return;
      setStreamed((current) => {
        const list = current[runId] ?? [];
        return list.some((item) => item.id === parsed.data.id) ? current : { ...current, [runId]: [...list, parsed.data] };
      });
      if (["run_completed", "run_failed", "run_cancelled", "screenshot_captured"].includes(parsed.data.type)) void loadRun(runId);
    };
    source.onerror = () => void loadRun(runId);
  }, [runnerBaseUrl, closeStream, loadRun]);

  // Every run the session lists is loaded once; a run still in progress (after a page refresh, say) is re-read
  // on each poll and streamed.
  const ensureRuns = useCallback(async (ids: string[]) => {
    for (const id of ids) {
      const known = runsRef.current[id];
      if (known && known.run.status !== "running") continue;
      const detail = await loadRun(id);
      if (detail?.run.status === "running") attachStream(id, detail.eventStreamUrl);
    }
  }, [loadRun, attachStream]);

  const refresh = useCallback(async () => {
    try {
      const next = await callRunner(runnerBaseUrl, expectedBackend, "/api/live/session", liveSessionStatusSchema);
      setStatus(next);
      setIssue(null);
      if (next.session) void ensureRuns(next.session.runs);
      if (next.active) {
        const [nextSummaries, nextProposals, nextChat] = await Promise.all([
          callRunner(runnerBaseUrl, expectedBackend, "/api/live/session/summaries?limit=60", liveSummariesResponseSchema),
          callRunner(runnerBaseUrl, expectedBackend, "/api/live/session/proposals?limit=60", liveProposalSetsResponseSchema),
          callRunner(runnerBaseUrl, expectedBackend, "/api/live/session/chat", liveChatSchema),
        ]);
        setSummaries(nextSummaries);
        setProposalSets(nextProposals);
        setChat(nextChat);
      }
      setSessions(await callRunner(runnerBaseUrl, expectedBackend, "/api/live/sessions?limit=50", liveSessionListSchema));
      setUsage(await callRunner(runnerBaseUrl, expectedBackend, "/api/live/usage?days=3", liveUsageSchema));
      setForm((current) => current ?? {
        summaryModel: next.defaults.summaryModel,
        summaryBaseUrl: next.defaults.summaryBaseUrl ?? "",
        summaryProvider: next.defaults.summaryProvider,
        summaryStream: next.defaults.summaryStream,
        proposerModel: next.defaults.proposerModel,
        proposerBaseUrl: next.defaults.proposerBaseUrl ?? "",
        proposerProvider: next.defaults.proposerProvider,
        proposerStream: next.defaults.proposerStream,
        snapshotIntervalS: String(next.defaults.snapshotIntervalS),
        summaryIntervalS: String(next.defaults.summaryIntervalS),
        proposerIntervalS: String(next.defaults.proposerIntervalS),
        maxSessionMinutes: String(next.defaults.maxSessionMinutes),
        screenshotMode: next.defaults.screenshotMode,
        permissionsApps: rulesText(next.defaults.permissionsApps),
        permissionsSites: rulesText(next.defaults.permissionsSites),
        summaryThinking: next.defaults.summaryThinking,
        proposerThinking: next.defaults.proposerThinking,
        summaryScreenshots: next.defaults.summaryScreenshots,
        proposerScreenshots: next.defaults.proposerScreenshots,
        proposerInputs: next.defaults.proposerInputs,
        maxProposals: String(next.defaults.maxProposals),
        summaryMaxTokens: String(next.defaults.summaryMaxTokens),
        proposerMaxTokens: String(next.defaults.proposerMaxTokens),
        proposerFeedbackRejected: String(next.defaults.proposerFeedbackRejected),
        proposerFeedbackAccepted: String(next.defaults.proposerFeedbackAccepted),
        proposerFeedbackRecent: String(next.defaults.proposerFeedbackRecent),
        proposerDedup: next.defaults.proposerDedup,
        proposerDedupThreshold: String(next.defaults.proposerDedupThreshold),
        proposerDedupNotNowHours: String(next.defaults.proposerDedupNotNowHours),
        proposerDedupSessionHours: String(next.defaults.proposerDedupSessionHours),
        embedModel: next.defaults.embedModel,
        embedBaseUrl: next.defaults.embedBaseUrl ?? "",
        notify: next.defaults.notify,
        beliefModel: next.defaults.beliefModel,
        beliefBaseUrl: next.defaults.beliefBaseUrl ?? "",
        beliefProvider: next.defaults.beliefProvider,
        beliefStream: next.defaults.beliefStream,
        beliefIntervalS: String(next.defaults.beliefIntervalS),
        beliefThinking: next.defaults.beliefThinking,
        beliefMaxTokens: String(next.defaults.beliefMaxTokens),
        beliefStaleDays: String(next.defaults.beliefStaleDays),
        beliefDoneStepDays: String(next.defaults.beliefDoneStepDays),
        beliefTrashDays: String(next.defaults.beliefTrashDays),
        streamLookbackDays: String(next.defaults.streamLookbackDays),
        actorYieldS: String(next.defaults.actorYieldS),
        actorContextSummaries: String(next.defaults.actorContextSummaries),
        actorDesktopSource: next.defaults.actorDesktopSource.join(","),
        desktopRemoteProvider: next.defaults.desktopRemoteProvider ?? "",
        desktopRemoteUrl: next.defaults.desktopRemoteUrl ?? "",
        actorAgents: next.defaults.actorAgents.join(","),
        actorCmuxModel: next.defaults.actorCmuxModel ?? "",
        actorCmuxPermissionMode: next.defaults.actorCmuxPermissionMode,
        actorCmuxCwd: next.defaults.actorCmuxCwd ?? "",
        actorClaudeModel: next.defaults.actorClaudeModel ?? "",
        actorClaudePermissionMode: next.defaults.actorClaudePermissionMode,
        actorClaudeCwd: next.defaults.actorClaudeCwd ?? "",
        actorCodexModel: next.defaults.actorCodexModel ?? "",
        actorCodexSandbox: next.defaults.actorCodexSandbox,
        actorCodexCwd: next.defaults.actorCodexCwd ?? "",
      });
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Runner unavailable.");
    }
  }, [runnerBaseUrl, expectedBackend, ensureRuns, setStatus, setSummaries, setProposalSets, setChat, setSessions, setUsage]); // the setters are stable; listed for the lint rule

  const loadHistory = useCallback(async (sessionId: string) => {
    try {
      const detail = await callRunner(runnerBaseUrl, expectedBackend, `/api/live/sessions/${encodeURIComponent(sessionId)}`, liveSessionDetailSchema);
      setHistory(detail);
      setStoredChat(await callRunner(runnerBaseUrl, expectedBackend, `/api/live/sessions/${encodeURIComponent(sessionId)}/chat`, liveChatSchema));
      setRenaming(false);
      setIssue(null);
      void ensureRuns(detail.session.runs);
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not load that session.");
    }
  }, [runnerBaseUrl, expectedBackend, ensureRuns]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 5_000);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (viewing === LIVE) {
      setHistory(null);
      return;
    }
    void loadHistory(viewing);
  }, [viewing, loadHistory]);

  function settingsBody(form: SessionForm) {
    return {
      summaryModel: form.summaryModel || undefined,
      summaryBaseUrl: form.summaryBaseUrl || undefined,
      summaryProvider: form.summaryProvider,
      summaryStream: form.summaryStream,
      proposerModel: form.proposerModel || undefined,
      proposerBaseUrl: form.proposerBaseUrl || undefined,
      proposerProvider: form.proposerProvider,
      proposerStream: form.proposerStream,
      snapshotIntervalS: Number(form.snapshotIntervalS),
      summaryIntervalS: Number(form.summaryIntervalS),
      proposerIntervalS: Number(form.proposerIntervalS),
      maxSessionMinutes: Number(form.maxSessionMinutes),
      screenshotMode: form.screenshotMode,
      permissionsApps: rulesFrom(form.permissionsApps),
      permissionsSites: rulesFrom(form.permissionsSites),
      summaryThinking: form.summaryThinking,
      proposerThinking: form.proposerThinking,
      summaryScreenshots: form.summaryScreenshots,
      proposerScreenshots: form.proposerScreenshots,
      proposerInputs: form.proposerInputs,
      maxProposals: Number(form.maxProposals) || undefined,
      summaryMaxTokens: Number(form.summaryMaxTokens) || 0,
      proposerMaxTokens: Number(form.proposerMaxTokens) || 0,
      proposerFeedbackRejected: Number(form.proposerFeedbackRejected) || undefined,
      proposerFeedbackAccepted: Number(form.proposerFeedbackAccepted) || undefined,
      proposerFeedbackRecent: Number(form.proposerFeedbackRecent) || undefined,
      proposerDedup: form.proposerDedup,
      proposerDedupThreshold: Number(form.proposerDedupThreshold) || undefined,
      proposerDedupNotNowHours: Number(form.proposerDedupNotNowHours) || undefined,
      proposerDedupSessionHours: Number(form.proposerDedupSessionHours) || undefined,
      embedModel: form.embedModel || undefined,
      embedBaseUrl: form.embedBaseUrl || undefined,
      notify: form.notify,
      beliefModel: form.beliefModel || undefined,
      beliefBaseUrl: form.beliefBaseUrl || undefined,
      beliefProvider: form.beliefProvider,
      beliefStream: form.beliefStream,
      beliefIntervalS: Number(form.beliefIntervalS),
      beliefThinking: form.beliefThinking,
      beliefMaxTokens: Number(form.beliefMaxTokens) || 0,
      beliefStaleDays: Number(form.beliefStaleDays) || undefined,
      beliefDoneStepDays: Number(form.beliefDoneStepDays) || undefined,
      beliefTrashDays: Number(form.beliefTrashDays) || undefined,
      streamLookbackDays: Number(form.streamLookbackDays) || undefined,
      actorYieldS: Number(form.actorYieldS) || undefined,
      actorContextSummaries: Number(form.actorContextSummaries) || undefined,
      actorDesktopSource: form.actorDesktopSource.split(","),
      desktopRemoteProvider: form.desktopRemoteProvider || undefined,
      desktopRemoteUrl: form.desktopRemoteUrl || undefined,
      actorAgents: form.actorAgents.split(","),
      actorCmuxModel: form.actorCmuxModel || undefined,
      actorCmuxPermissionMode: form.actorCmuxPermissionMode,
      actorCmuxCwd: form.actorCmuxCwd || undefined,
      actorClaudeModel: form.actorClaudeModel || undefined,
      actorClaudePermissionMode: form.actorClaudePermissionMode,
      actorClaudeCwd: form.actorClaudeCwd || undefined,
      actorCodexModel: form.actorCodexModel || undefined,
      actorCodexSandbox: form.actorCodexSandbox,
      actorCodexCwd: form.actorCodexCwd || undefined,
    };
  }

  async function startSession() {
    if (!form) return;
    setWaiting("Starting the session: launching the observer and the model loops…");
    try {
      await call("/api/live/session", liveSessionStatusSchema, { method: "POST", body: JSON.stringify(settingsBody(form)) });
      setSummaries([]);
      setProposalSets([]);
      setViewing(LIVE);
      await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not start the session.");
    } finally {
      setWaiting(null);
    }
  }

  async function testSettings() {
    if (!form) return;
    setChecking(true);
    setChecks(null);
    try {
      // Each backend probe gives up after 30 s, so wait longer than that: the probe's own error is the useful result.
      setChecks(await call("/api/live/settings/check", liveSettingsCheckSchema, { method: "POST", body: JSON.stringify(settingsBody(form)) }, 45_000));
    } catch (error) {
      setChecks({ ok: false, checks: [{ role: "runner", label: "Runner", ok: false, error: error instanceof Error ? error.message : "Could not test the configuration." }] });
    } finally {
      setChecking(false);
    }
  }

  async function stopSession() {
    setWaiting("Stopping the session: finishing the last summary and belief update, then storing it…");
    try {
      const stoppedId = status?.session?.id;
      // Stopping ends with a final summary, belief update and proposal round, so this can take a few minutes with thinking models.
      await call("/api/live/session/stop", rawJson, { method: "POST" }, 300_000);
      await refresh();
      if (stoppedId) setViewing(stoppedId); // keep what was just recorded on screen
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not stop the session.");
    } finally {
      setWaiting(null);
    }
  }

  async function pauseSession(paused: boolean) {
    try {
      await call(`/api/live/session/${paused ? "resume" : "pause"}`, rawJson, { method: "POST" });
      await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not pause the session.");
    }
  }

  async function proposeNow() {
    setWaiting("Running the summarizer, the belief updater and the proposer now…");
    try {
      await call("/api/live/session/propose", rawJson, { method: "POST" });
      await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Propose tick failed.");
    } finally {
      setWaiting(null);
    }
  }

  async function sendReason(item: LiveProposalSet, reason: string, typed: boolean, sessionId?: string) {
    setWaiting("Saving the reason…");
    try {
      const path = sessionId ? `/api/live/sessions/${encodeURIComponent(sessionId)}/proposals/feedback/reason` : "/api/live/session/proposals/feedback/reason";
      await call(path, liveFeedbackResponseSchema, { method: "POST", body: JSON.stringify({ proposalsTs: item.ts, reason, typed }) });
      if (sessionId) await loadHistory(sessionId);
      else await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not record the reason.");
    } finally {
      setWaiting(null);
    }
  }

  // Accept a proposal and put it in the chat box, to edit and send as the next message.
  async function sendToChat(item: LiveProposalSet, choice: number) {
    await sendFeedback(item, "accept", choice);
    setChatSeed(proposalsOf(item)[choice]?.task ?? item.task ?? "");
  }

  async function sendFeedback(item: LiveProposalSet, decision: "accept" | "reject", choice: number, sessionId?: string) {
    setWaiting("Recording the decision…");
    try {
      const path = sessionId ? `/api/live/sessions/${encodeURIComponent(sessionId)}/proposals/feedback` : "/api/live/session/proposals/feedback";
      const body = { proposalsTs: item.ts, decision, choice: decision === "accept" ? choice : null };
      await call(path, liveFeedbackResponseSchema, { method: "POST", body: JSON.stringify(body) });
      // Accepting only fills the prompt box with the chosen proposal; nothing runs until the operator says so.
      if (decision === "accept" && !sessionId && promptBox.current) promptBox.current.value = proposalsOf(item)[choice]?.task ?? item.task ?? "";
      if (sessionId) await loadHistory(sessionId);
      else await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not record feedback.");
    } finally {
      setWaiting(null);
    }
  }

  async function flagSummary(item: LiveSummary, comment: string, verdict: "wrong" | "ok", sessionId?: string) {
    setWaiting("Saving the flag…");
    try {
      const path = sessionId ? `/api/live/sessions/${encodeURIComponent(sessionId)}/summaries/feedback` : "/api/live/session/summaries/feedback";
      await call(path, liveSummaryFeedbackResponseSchema, { method: "POST", body: JSON.stringify({ summaryTs: item.ts, comment, verdict }) });
      if (sessionId) await loadHistory(sessionId);
      else await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not record the flag.");
    } finally {
      setWaiting(null);
    }
  }

  async function renameSession(sessionId: string, name: string) {
    setWaiting("Renaming the session…");
    try {
      await call(`/api/live/sessions/${encodeURIComponent(sessionId)}`, liveSessionRecordSchema,
        { method: "PATCH", body: JSON.stringify({ name: name.trim() || null }) });
      await loadHistory(sessionId);
      await refresh(); // the View menu's labels come from the sessions list
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not rename the session.");
    } finally {
      setWaiting(null);
    }
  }

  async function editTasks(edit: TaskEdit) {
    setWaiting("Updating the task list…");
    try {
      const result = await call("/api/live/tasks", liveTasksResponseSchema, { method: "POST", body: JSON.stringify({ ops: [edit] }) });
      setStatus((current) => (current ? { ...current, tasks: result.tasks, trash: result.trash } : current));
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not update the task.");
    } finally {
      setWaiting(null);
    }
  }

  async function flagTask(taskId: string, step: number | null, verdict: "up" | "down"): Promise<LiveTaskFlag | null> {
    setWaiting("Saving the flag…");
    try {
      const flag = await call("/api/live/tasks/feedback", liveTaskFlagSchema, { method: "POST", body: JSON.stringify({ taskId, step, verdict }) });
      await refresh();
      return flag;
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not record the flag.");
      return null;
    } finally {
      setWaiting(null);
    }
  }

  async function flagTaskReason(flagId: string, reason: string) {
    setWaiting("Saving the reason…");
    try {
      await call("/api/live/tasks/feedback/reason", liveTaskFlagSchema, { method: "POST", body: JSON.stringify({ flagId, reason }) });
      await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not record the reason.");
    } finally {
      setWaiting(null);
    }
  }

  async function unflagTask(flagId: string) {
    setWaiting("Removing the flag…");
    try {
      await call(`/api/live/tasks/feedback/${encodeURIComponent(flagId)}`, liveSessionDeleteSchema, { method: "DELETE" });
      await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not take the flag back.");
    } finally {
      setWaiting(null);
    }
  }

  async function deleteSession(sessionId: string) {
    setWaiting("Deleting the session…");
    try {
      await call(`/api/live/sessions/${encodeURIComponent(sessionId)}`, liveSessionDeleteSchema, { method: "DELETE" });
      setViewing(LIVE);
      await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not delete the session.");
    } finally {
      setWaiting(null);
    }
  }

  const runAgents = status?.session?.settings.actorAgents ?? []; // the agents the running session set up
  const agent = runAgents.find((item) => item === runAgent) ?? runAgents[0]; // the one the next run uses

  async function startRun() {
    setRunIssue(null);
    try {
      const started = await call("/api/runs", startRunResponseSchema, {
        method: "POST",
        body: JSON.stringify({ scenarioId: "desktop-free-task", prompt: promptBox.current?.value ?? "", maxResponseTurns: Number(maxTurns) || DEFAULT_MAX_TURNS, liveContext: true, agent }),
      });
      setRuns((current) => ({ ...current, [started.runId]: started.detail }));
      attachStream(started.runId, started.eventStreamUrl);
      await refresh();
    } catch (error) {
      setRunIssue(error instanceof Error ? error.message : "Could not start the run.");
    }
  }

  async function sendChat(text: string): Promise<boolean> {
    setChatIssue(null);
    try {
      const started = await call("/api/live/session/chat/messages", startRunResponseSchema, {
        method: "POST",
        body: JSON.stringify({ text, agent: chat?.agent ?? chatAgent, maxResponseTurns: Number(chatTurns) || DEFAULT_MAX_TURNS }),
      });
      setChatSeed("");
      setRuns((current) => ({ ...current, [started.runId]: started.detail }));
      attachStream(started.runId, started.eventStreamUrl);
      await refresh();
      return true;
    } catch (error) {
      setChatIssue(error instanceof Error ? error.message : "Could not send the message.");
      return false;
    }
  }

  async function browseChat(chatId: string | null, sessionId?: string) {
    try {
      const base = sessionId ? `/api/live/sessions/${encodeURIComponent(sessionId)}/chat` : "/api/live/session/chat";
      const shown = chatId ? await call(`${base}?id=${encodeURIComponent(chatId)}`, liveChatSchema) : null;
      if (sessionId) setStoredChat(shown ?? storedChat);
      else setChatView(shown && shown.id !== shown.current ? shown : null); // back to the current conversation: nothing to hold
    } catch (error) {
      setChatIssue(error instanceof Error ? error.message : "Could not load that conversation.");
    }
  }

  async function resetChat() {
    try {
      setChat(await call("/api/live/session/chat/reset", liveChatSchema, { method: "POST" }));
      setChatView(null);
    } catch (error) {
      setChatIssue(error instanceof Error ? error.message : "Could not start a new conversation.");
    }
  }

  async function answerApproval(runId: string, mode: "once" | "deny") {
    try {
      await call(`/api/live/session/runs/${runId}/approval`, rawJson, { method: "POST", body: JSON.stringify({ mode }) });
    } catch (error) {
      setRunIssue(error instanceof Error ? error.message : "Could not answer the approval request.");
    }
  }

  async function stopRun(runId: string) {
    try {
      await call(`/api/runs/${runId}/stop`, rawJson, { method: "POST" });
      await loadRun(runId);
    } catch (error) {
      setRunIssue(error instanceof Error ? error.message : "Could not stop the run.");
    }
  }

  const session = status?.session ?? null;
  const viewingHistory = viewing !== LIVE;
  const stored = history?.session ?? null;
  const sessionRunIds = session?.runs ?? [];
  const listedRunIds = sessionRunIds.filter((id) => !session?.chatRuns.includes(id)); // chat turns are shown with the chat
  // Runs are sequential: one at a time per runner, chat turns included. The observer and proposer keep working while one runs.
  const runningId = sessionRunIds.find((id) => runs[id]?.run.status === "running") ?? null;

  return (
    <main className="live">
      <header className="liveTopbar">
        <div>
          <h1>Continuous session</h1>
          <p>Observe the screen every few seconds, summarize and proposer every few minutes, then test a run that receives the digest.</p>
        </div>
        <nav className="liveNav">
          <label>
            View
            <select value={viewing} onChange={(event) => setViewing(event.target.value)}>
              <option value={LIVE}>{session ? `Live · ${session.id}` : "Live (no session)"}</option>
              {sessions.filter((item) => item.id !== session?.id).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name ? `${item.name} · ` : ""}{item.id} · {clockOf(item.startedAt)}–{item.stoppedAt ? clockOf(item.stoppedAt) : "…"} · {item.summaryCount} summaries · {item.proposalCount} proposal rounds
                </option>
              ))}
            </select>
          </label>
          {nav}
        </nav>
      </header>

      {issue ? <p className="liveIssue">{issue}</p> : null}
      {waiting ? <p className="liveWaiting" role="status"><span className="liveDot running" aria-hidden="true" />{waiting}</p> : null}

      {viewingHistory ? (
        <>
          <section className="livePanel">
            <h2>Past session {stored?.name ?? viewing}</h2>
            {stored ? (
              <div className="liveStatus">
                <dl>
                  <dt>Folder</dt><dd><code>{stored.folder}</code></dd>
                  <dt>Ran</dt><dd>{clockOf(stored.startedAt)} – {stored.stoppedAt ? clockOf(stored.stoppedAt) : "still running"}{stored.stopReason ? ` · ${stored.stopReason}` : ""}</dd>
                  <dt>Observed</dt><dd>{stored.snapshotCount} distinct snapshots · {stored.screenshotCount} screenshots</dd>
                  <dt>Summarizer</dt><dd>{stored.settings.summaryModel}{stored.settings.summaryThinking ? " (thinking)" : " (non-thinking)"}{stored.settings.summaryScreenshots ? " + screenshots" : ""} · every {stored.settings.summaryIntervalS}s</dd>
                  <dt>Belief</dt><dd>{stored.settings.beliefModel}{stored.settings.beliefThinking ? " (thinking)" : " (non-thinking)"} · every {stored.settings.beliefIntervalS}s</dd>
                  <dt>Proposer</dt><dd>{stored.settings.proposerModel}{stored.settings.proposerThinking ? " (thinking)" : " (non-thinking)"}{stored.settings.proposerScreenshots ? " + screenshots" : ""}{stored.settings.proposerInputs.length < PROPOSER_SOURCES.length ? ` · sees ${stored.settings.proposerInputs.join(" + ")}` : ""} · every {stored.settings.proposerIntervalS}s · up to {stored.settings.maxProposals} proposals</dd>
                  <dt>Usage</dt><dd>{usageLine(stored.usage)}</dd>
                  <dt>Runs</dt><dd>{stored.runs.length ? stored.runs.join(", ") : "none"}</dd>
                  {stored.lastError ? (<><dt>Last error</dt><dd className="liveError">{stored.lastError}</dd></>) : null}
                </dl>
                <p className="liveMuted">Read-only view. Flags on summaries and Accept/Reject on proposals still record labels for this session; nothing runs from here.</p>
                {stored.active ? null : (
                  <div className="liveActions">
                    {renaming ? (
                      <>
                        <input value={nameDraft} placeholder="Session name" aria-label="Session name" maxLength={80} onChange={(event) => setNameDraft(event.target.value)} />
                        <button type="button" className="primary" disabled={busy} onClick={() => void renameSession(stored.id, nameDraft)}>Save name</button>
                        <button type="button" className="secondary" disabled={busy} onClick={() => setRenaming(false)}>Cancel</button>
                      </>
                    ) : (
                      <>
                        <button type="button" className="secondary" disabled={busy} onClick={() => { setNameDraft(stored.name ?? ""); setRenaming(true); }}>Rename session</button>
                        <button type="button" className="secondary" disabled={busy} onClick={() => { if (window.confirm("Delete this session's folder, runs included?")) void deleteSession(stored.id); }}>Delete session</button>
                      </>
                    )}
                  </div>
                )}
              </div>
            ) : <p>Loading…</p>}
          </section>
          {history ? (
            <section className="livePanel">
              <h2>Tasks</h2>
              <TaskList tasks={history.tasks} note="The task list as this session's last belief update left it." />
            </section>
          ) : null}
          {history ? (
            <section className="liveColumns">
              <div className="livePanel">
                <h2>Summaries</h2>
                <SummaryList summaries={history.summaries} busy={busy} onFlag={(item, comment, verdict) => flagSummary(item, comment, verdict, viewing)} />
              </div>
              <div className="livePanel">
                <h2>Proposals</h2>
                <ProposalSetList proposalSets={history.proposals} busy={busy} acceptLabel="Accept" onDecide={(item, decision, choice) => void sendFeedback(item, decision, choice, viewing)} onReason={(item, reason, typed) => void sendReason(item, reason, typed, viewing)} reasons={status?.rejectReasons ?? []} />
              </div>
            </section>
          ) : null}
          <section className="livePanel">
            <h2>Runs in this session</h2>
            <p className="liveMuted">Read-only. Each run&apos;s events and screenshots are stored inside the session folder. Switch the view back to Live to start a new run.</p>
            <RunList ids={(stored?.runs ?? []).filter((id) => !stored?.chatRuns.includes(id))} runs={runs} streamed={streamed} runnerBaseUrl={runnerBaseUrl} />
          </section>
          {storedChat && storedChat.conversations.length ? (
            <section className="livePanel">
              <h2>Chat with the agent</h2>
              <p className="liveMuted">Read-only. Each reply unfolds into the run that produced it.</p>
              <ChatPanel chat={storedChat} runs={runs} streamed={streamed} runnerBaseUrl={runnerBaseUrl} readOnly onBrowse={(id) => void browseChat(id, viewing)} />
            </section>
          ) : null}
        </>
      ) : (
        <>
          <section className="livePanel">
            <h2>Session</h2>
            {session ? (
              <div className="liveStatus">
                <dl>
                  <dt>Folder</dt><dd><code>{session.folder}</code></dd>
                  <dt>Started</dt><dd>{clockOf(session.startedAt)}{session.expiresAt ? ` · auto-stops at ${clockOf(session.expiresAt)}` : " · no time limit"}</dd>
                  {session.stopReason ? (<><dt>Stopping</dt><dd>{session.stopReason} · finishing the last summary and belief update, then the session is stored</dd></>) : null}
                  <dt>Observer</dt><dd>{session.observerAlive ? (session.paused ? "paused" : "recording") : "stopped"} · screenshots: {session.settings.effectiveScreenshotMode}</dd>
                  <dt>Snapshots</dt><dd>{session.snapshotCount} distinct / {session.tickCount} ticks · {session.screenshotCount} screenshots</dd>
                  <dt>Latest</dt><dd>{session.latestSnapshot ? `${clockOf(session.latestSnapshot.ts)} · ${session.latestSnapshot.app ?? ""} · ${session.latestSnapshot.window ?? ""}` : "—"}</dd>
                  <dt>Summarizer</dt><dd><LoopStatus nextAt={session.nextSummaryAt} /> · {session.settings.summaryModel}{session.settings.summaryThinking ? " (thinking)" : " (non-thinking)"}{session.settings.summaryScreenshots ? " + screenshots" : ""} · every {session.settings.summaryIntervalS}s · {session.summaryTicks} ticks</dd>
                  <dt>Belief</dt><dd><LoopStatus nextAt={session.nextBeliefAt} /> · {session.settings.beliefModel}{session.settings.beliefThinking ? " (thinking)" : " (non-thinking)"} · every {session.settings.beliefIntervalS}s · {session.beliefTicks} ticks</dd>
                  <dt>Proposer</dt><dd><LoopStatus nextAt={session.nextProposalAt} /> · {session.settings.proposerModel}{session.settings.proposerThinking ? " (thinking)" : " (non-thinking)"}{session.settings.proposerScreenshots ? " + screenshots" : ""}{session.settings.proposerInputs.length < PROPOSER_SOURCES.length ? ` · sees ${session.settings.proposerInputs.join(" + ")}` : ""} · every {session.settings.proposerIntervalS}s · up to {session.settings.maxProposals} proposals · {session.proposerTicks} ticks</dd>
                  <dt>Usage</dt><dd>{usageLine(session.usage)}</dd>
                  {session.lastError ? (<><dt>Last error</dt><dd className="liveError">{session.lastError}</dd></>) : null}
                </dl>
                <div className="liveActions">
                  <button type="button" className="primary" disabled={busy || session.nextProposalAt === null} title={session.nextProposalAt === null ? "The proposer is already running" : undefined} onClick={() => void proposeNow()}>Propose now</button>
                  <button type="button" className="secondary" disabled={busy || Boolean(session.stopReason)} onClick={() => void pauseSession(session.paused)}>{session.paused ? "Resume" : "Pause"}</button>
                  <button type="button" className="secondary" disabled={busy || Boolean(session.stopReason)} onClick={() => void stopSession()}>{session.stopReason ? "Stopping…" : "Stop session"}</button>
                </div>
              </div>
            ) : form ? (
              <form onSubmit={(event) => { event.preventDefault(); void startSession(); }}>
                <details className="liveFold">
                  <summary><h3>Configuration</h3></summary>
                  <div className="liveForm">
                    <label>Snapshot every (s)<input type="number" min={2} value={form.snapshotIntervalS} onChange={(event) => setForm({ ...form, snapshotIntervalS: event.target.value })} /></label>
                    <label>Max session length (minutes, 0 = no limit)<input type="number" min={0} value={form.maxSessionMinutes} onChange={(event) => setForm({ ...form, maxSessionMinutes: event.target.value })} /></label>
                    <label>
                      Observer screenshots
                      <select value={form.screenshotMode} onChange={(event) => setForm({ ...form, screenshotMode: event.target.value as ScreenshotMode })}>
                      <option value="switch">on app switch</option>
                      <option value="always">every distinct snapshot</option>
                      <option value="off">off</option>
                      </select>
                    </label>
                    <label>App permissions (name or bundle id: block, look or use)<input value={form.permissionsApps} placeholder="Slack: look, Code: use, 1Password*: block" onChange={(event) => setForm({ ...form, permissionsApps: event.target.value })} /></label>
                    <label>Site permissions (in any browser)<input value={form.permissionsSites} placeholder="chase.com: block, *bank*: block" onChange={(event) => setForm({ ...form, permissionsSites: event.target.value })} /></label>
                    <fieldset className="liveGroup">
                      <legend>Summarizer</legend>
                      <div className="liveFields">
                        <label>Model (required; never the task agent's model)<input value={form.summaryModel} placeholder="e.g. qwen3.8-27b" onChange={(event) => setForm({ ...form, summaryModel: event.target.value })} /></label>
                        <label>Base URL (blank = task agent endpoint)<input value={form.summaryBaseUrl} placeholder="http://host/v1" onChange={(event) => setForm({ ...form, summaryBaseUrl: event.target.value })} /></label>
                        <label>Endpoint type<select value={form.summaryProvider} onChange={(event) => setForm({ ...form, summaryProvider: event.target.value as LiveProvider })}><option value="vllm">vLLM (VLLM_* key)</option><option value="openai">OpenAI (OPENAI_* key)</option><option value="anthropic">Anthropic (ANTHROPIC_* key, or behind a proxy)</option><option value="openrouter">OpenRouter (OPENROUTER_* key)</option><option value="litellm">LiteLLM proxy (LITELLM_* key)</option></select></label>
                        <label>Summarize every (s)<input type="number" min={10} value={form.summaryIntervalS} onChange={(event) => setForm({ ...form, summaryIntervalS: event.target.value })} /></label>
                        <label>Output tokens per call (0 = automatic)<input type="number" min={0} value={form.summaryMaxTokens} onChange={(event) => setForm({ ...form, summaryMaxTokens: event.target.value })} /></label>
                      </div>
                      <label className="liveCheck"><input type="checkbox" checked={form.summaryThinking} onChange={(event) => setForm({ ...form, summaryThinking: event.target.checked })} /> Thinks before answering (slower)</label>
                      <label className="liveCheck"><input type="checkbox" checked={form.summaryStream} onChange={(event) => setForm({ ...form, summaryStream: event.target.checked })} /> Streams the reply (keeps a long answer alive through a proxy; off if the endpoint cannot stream)</label>
                      <label className="liveCheck"><input type="checkbox" checked={form.summaryScreenshots} onChange={(event) => setForm({ ...form, summaryScreenshots: event.target.checked })} /> Sees screenshots (multimodal model; forces a screenshot on every snapshot)</label>
                    </fieldset>
                    <fieldset className="liveGroup">
                      <legend>Belief updater</legend>
                      <div className="liveFields">
                        <label>Model (required)<input value={form.beliefModel} placeholder="e.g. qwen3.8-27b" onChange={(event) => setForm({ ...form, beliefModel: event.target.value })} /></label>
                        <label>Base URL (blank = task agent endpoint)<input value={form.beliefBaseUrl} placeholder="http://host/v1" onChange={(event) => setForm({ ...form, beliefBaseUrl: event.target.value })} /></label>
                        <label>Endpoint type<select value={form.beliefProvider} onChange={(event) => setForm({ ...form, beliefProvider: event.target.value as LiveProvider })}><option value="vllm">vLLM (VLLM_* key)</option><option value="openai">OpenAI (OPENAI_* key)</option><option value="anthropic">Anthropic (ANTHROPIC_* key, or behind a proxy)</option><option value="openrouter">OpenRouter (OPENROUTER_* key)</option><option value="litellm">LiteLLM proxy (LITELLM_* key)</option></select></label>
                        <label>Update every (s)<input type="number" min={15} value={form.beliefIntervalS} onChange={(event) => setForm({ ...form, beliefIntervalS: event.target.value })} /></label>
                        <label>Output tokens per call (0 = automatic)<input type="number" min={0} value={form.beliefMaxTokens} onChange={(event) => setForm({ ...form, beliefMaxTokens: event.target.value })} /></label>
                        <label>Pause a task unseen for (days)<input type="number" min={1} value={form.beliefStaleDays} onChange={(event) => setForm({ ...form, beliefStaleDays: event.target.value })} /></label>
                        <label>Drop finished checklist steps after (days)<input type="number" min={1} value={form.beliefDoneStepDays} onChange={(event) => setForm({ ...form, beliefDoneStepDays: event.target.value })} /></label>
                        <label>Deleted tasks and entries kept (days)<input type="number" min={1} value={form.beliefTrashDays} onChange={(event) => setForm({ ...form, beliefTrashDays: event.target.value })} /></label>
                        <label>Stream lookback for a window (days)<input type="number" min={1} value={form.streamLookbackDays} onChange={(event) => setForm({ ...form, streamLookbackDays: event.target.value })} /></label>
                      </div>
                      <label className="liveCheck"><input type="checkbox" checked={form.beliefThinking} onChange={(event) => setForm({ ...form, beliefThinking: event.target.checked })} /> Thinks before answering</label>
                      <label className="liveCheck"><input type="checkbox" checked={form.beliefStream} onChange={(event) => setForm({ ...form, beliefStream: event.target.checked })} /> Streams the reply</label>
                    </fieldset>
                    <fieldset className="liveGroup">
                      <legend>Proposer</legend>
                      <div className="liveFields">
                        <label>Model (required)<input value={form.proposerModel} placeholder="e.g. qwen3.8-27b" onChange={(event) => setForm({ ...form, proposerModel: event.target.value })} /></label>
                        <label>Base URL (blank = task agent endpoint)<input value={form.proposerBaseUrl} placeholder="http://host/v1" onChange={(event) => setForm({ ...form, proposerBaseUrl: event.target.value })} /></label>
                        <label>Endpoint type<select value={form.proposerProvider} onChange={(event) => setForm({ ...form, proposerProvider: event.target.value as LiveProvider })}><option value="vllm">vLLM (VLLM_* key)</option><option value="openai">OpenAI (OPENAI_* key)</option><option value="anthropic">Anthropic (ANTHROPIC_* key, or behind a proxy)</option><option value="openrouter">OpenRouter (OPENROUTER_* key)</option><option value="litellm">LiteLLM proxy (LITELLM_* key)</option></select></label>
                        <label>Propose every (s)<input type="number" min={15} value={form.proposerIntervalS} onChange={(event) => setForm({ ...form, proposerIntervalS: event.target.value })} /></label>
                        <label>Proposals per round (1-{status?.limits.maxProposals ?? ""}, first shown by default)<input type="number" min={1} max={status?.limits.maxProposals} value={form.maxProposals} onChange={(event) => setForm({ ...form, maxProposals: event.target.value })} /></label>
                        <label>Output tokens per call (0 = automatic)<input type="number" min={0} value={form.proposerMaxTokens} onChange={(event) => setForm({ ...form, proposerMaxTokens: event.target.value })} /></label>
                        <label>Rejected decisions shown (last N, typed reasons first)<input type="number" min={1} value={form.proposerFeedbackRejected} onChange={(event) => setForm({ ...form, proposerFeedbackRejected: event.target.value })} /></label>
                        <label>Accepted decisions shown (last N)<input type="number" min={1} value={form.proposerFeedbackAccepted} onChange={(event) => setForm({ ...form, proposerFeedbackAccepted: event.target.value })} /></label>
                        <label>Unanswered rounds shown (the top proposal of the last N of this session)<input type="number" min={1} value={form.proposerFeedbackRecent} onChange={(event) => setForm({ ...form, proposerFeedbackRecent: event.target.value })} /></label>
                      </div>
                      <label className="liveCheck"><input type="checkbox" checked={form.proposerThinking} onChange={(event) => setForm({ ...form, proposerThinking: event.target.checked })} /> Thinks before answering</label>
                      <label className="liveCheck"><input type="checkbox" checked={form.proposerStream} onChange={(event) => setForm({ ...form, proposerStream: event.target.checked })} /> Streams the reply</label>
                      {PROPOSER_SOURCES.map((source) => (
                      <label key={source.value} className="liveCheck"><input type="checkbox" checked={form.proposerInputs.includes(source.value)} onChange={(event) => setForm({ ...form, proposerInputs: PROPOSER_SOURCES.map((item) => item.value).filter((value) => (value === source.value ? event.target.checked : form.proposerInputs.includes(value))) })} /> Sees {source.label}</label>
                      ))}
                      <label className="liveCheck"><input type="checkbox" checked={form.proposerScreenshots} onChange={(event) => setForm({ ...form, proposerScreenshots: event.target.checked })} /> Sees screenshots of the window (multimodal model; forces a screenshot on every snapshot)</label>
                      <label className="liveCheck"><input type="checkbox" checked={form.proposerDedup} onChange={(event) => setForm({ ...form, proposerDedup: event.target.checked })} /> Filters duplicates after generation (embeddings; a proposal too close to one already decided or shown is held back)</label>
                      {form.proposerDedup ? (
                        <div className="liveFields">
                          <label>Embeddings model (required)<input value={form.embedModel} placeholder="e.g. openai/text-embedding-3-small" onChange={(event) => setForm({ ...form, embedModel: event.target.value })} /></label>
                          <label>Embeddings base URL (blank = task agent endpoint)<input value={form.embedBaseUrl} placeholder="https://host/v1" onChange={(event) => setForm({ ...form, embedBaseUrl: event.target.value })} /></label>
                          <label>Duplicate at cosine ≥<input type="number" min={0.05} max={1} step={0.01} value={form.proposerDedupThreshold} onChange={(event) => setForm({ ...form, proposerDedupThreshold: event.target.value })} /></label>
                          <label>"Not now" rejections count for (hours)<input type="number" min={1} value={form.proposerDedupNotNowHours} onChange={(event) => setForm({ ...form, proposerDedupNotNowHours: event.target.value })} /></label>
                          <label>This session's proposals count for (hours)<input type="number" min={1} value={form.proposerDedupSessionHours} onChange={(event) => setForm({ ...form, proposerDedupSessionHours: event.target.value })} /></label>
                        </div>
                      ) : null}
                      <label className="liveCheck"><input type="checkbox" checked={form.notify} onChange={(event) => setForm({ ...form, notify: event.target.checked })} /> macOS notification on proposals</label>
                    </fieldset>
                    <fieldset className="liveGroup">
                      <legend>Actor</legend>
                      <div className="liveFields">
                        <label>Agents to set up<select value={form.actorAgents} onChange={(event) => setForm({ ...form, actorAgents: event.target.value })}><option value="desktop">Desktop agent (OpenAI model, exec_py)</option><option value="cmux">cmux agent (Claude Code in a cmux workspace)</option><option value="claude">Claude Code (started by the backend)</option><option value="codex">Codex (started by the backend)</option><option value="desktop,cmux,claude,codex">all (each run picks one)</option></select></label>
                        <label>Summaries a run is given as context<input type="number" min={1} value={form.actorContextSummaries} onChange={(event) => setForm({ ...form, actorContextSummaries: event.target.value })} /></label>
                      </div>
                      {form.actorAgents.includes("cmux") ? (
                        <div className="liveFields">
                          <label>Claude model (blank = Claude Code&apos;s default)<input value={form.actorCmuxModel} placeholder="sonnet, opus, or a model id" onChange={(event) => setForm({ ...form, actorCmuxModel: event.target.value })} /></label>
                          <label>Claude permission mode (a headless run cannot answer prompts)<select value={form.actorCmuxPermissionMode} onChange={(event) => setForm({ ...form, actorCmuxPermissionMode: event.target.value })}><option value="auto">auto</option><option value="acceptEdits">acceptEdits</option><option value="bypassPermissions">bypassPermissions</option><option value="dontAsk">dontAsk</option><option value="manual">manual</option><option value="plan">plan</option></select></label>
                          <label>Folder Claude starts in (blank = the run&apos;s workspace)<input value={form.actorCmuxCwd} placeholder="~/projects/app" onChange={(event) => setForm({ ...form, actorCmuxCwd: event.target.value })} /></label>
                        </div>
                      ) : null}
                      {form.actorAgents.includes("claude") ? (
                        <div className="liveFields">
                          <label>Claude Code model (blank = Claude Code&apos;s default)<input value={form.actorClaudeModel} placeholder="sonnet, opus, or a model id" onChange={(event) => setForm({ ...form, actorClaudeModel: event.target.value })} /></label>
                          <label>Claude Code permission mode (what it holds back is answered on the run card)<select value={form.actorClaudePermissionMode} onChange={(event) => setForm({ ...form, actorClaudePermissionMode: event.target.value })}><option value="auto">auto</option><option value="acceptEdits">acceptEdits</option><option value="bypassPermissions">bypassPermissions</option><option value="dontAsk">dontAsk</option><option value="manual">manual</option><option value="plan">plan</option></select></label>
                          <label>Folder Claude Code starts in (blank = the run&apos;s workspace)<input value={form.actorClaudeCwd} placeholder="~/projects/app" onChange={(event) => setForm({ ...form, actorClaudeCwd: event.target.value })} /></label>
                        </div>
                      ) : null}
                      {form.actorAgents.includes("codex") ? (
                        <div className="liveFields">
                          <label>Codex model (blank = Codex&apos;s default)<input value={form.actorCodexModel} placeholder="a model id" onChange={(event) => setForm({ ...form, actorCodexModel: event.target.value })} /></label>
                          <label>Codex sandbox (a step beyond it is judged by Codex&apos;s own reviewer)<select value={form.actorCodexSandbox} onChange={(event) => setForm({ ...form, actorCodexSandbox: event.target.value })}><option value="workspace-write">workspace-write</option><option value="read-only">read-only</option><option value="full-access">full-access</option></select></label>
                          <label>Folder Codex starts in (blank = the run&apos;s workspace)<input value={form.actorCodexCwd} placeholder="~/projects/app" onChange={(event) => setForm({ ...form, actorCodexCwd: event.target.value })} /></label>
                        </div>
                      ) : null}
                      {form.actorAgents.includes("desktop") || form.actorAgents.includes("claude") || form.actorAgents.includes("codex") ? (
                        <div className="liveFields">
                          <label>Yield a running exec_py after (s)<input type="number" min={1} value={form.actorYieldS} onChange={(event) => setForm({ ...form, actorYieldS: event.target.value })} /></label>
                          <label>Desktops the agent gets<select value={form.actorDesktopSource} onChange={(event) => setForm({ ...form, actorDesktopSource: event.target.value })}><option value="local">local (this Mac)</option><option value="local,remote">local + remote (exec_py picks a target)</option><option value="remote">remote only</option></select></label>
                          <label>Remote desktop provider<select value={form.desktopRemoteProvider} onChange={(event) => setForm({ ...form, desktopRemoteProvider: event.target.value })}><option value="">none</option><option value="cua">cua (Ubuntu container)</option></select></label>
                          <label>Remote desktop URL (token from CUA_DESKTOP_REMOTE_TOKEN)<input value={form.desktopRemoteUrl} placeholder="wss://host" onChange={(event) => setForm({ ...form, desktopRemoteUrl: event.target.value })} /></label>
                        </div>
                      ) : null}
                    </fieldset>
                  </div>
                </details>
                <div className="liveActions">
                  <button type="submit" className="primary" disabled={busy || checking}>Start session</button>
                  <button type="button" className="secondary" disabled={busy || checking} onClick={() => void testSettings()}>{checking ? "Testing…" : "Test configuration"}</button>
                </div>
                {checks ? (
                  <ul className="liveChecks">
                    {checks.checks.map((check) => (
                      <li key={check.role} className={check.ok ? "ok" : "fail"}>
                        <strong>{check.ok ? "✓" : "✗"} {check.label}</strong>
                        {check.model ? ` · ${check.model}` : ""}{check.baseUrl ? ` @ ${check.baseUrl}` : ""}
                        {check.ok ? ` · ${check.latencyMs ?? "?"} ms${check.detail ? ` · "${check.detail}"` : ""}` : ` · ${check.error ?? "failed"}`}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </form>
            ) : <p>Loading…</p>}
          </section>

          {usage ? <CostPanel usage={usage} active={Boolean(status?.active)} /> : null}

          {status ? (
            <section className="livePanel">
              <div className="livePanelHead">
                <h2>Tasks</h2>
                <button type="button" className="secondary" onClick={() => setEditingTasks((editing) => !editing)}>{editingTasks ? "Done editing" : "Edit"}</button>
              </div>
              <TaskList tasks={status.tasks} flags={status.taskFlags} note={editingTasks ? "Tick or untick entries, mark tasks done or reopen them, rename them, or move a task or an entry to the trash. Your changes are logged as operator corrections." : "What the belief updater believes you are working on, across sessions. Thumbs up or down records what it got right or wrong; nothing changes."} onEdit={editingTasks ? (edit) => void editTasks(edit) : undefined} onFlag={{ flag: flagTask, reason: (flagId, reason) => void flagTaskReason(flagId, reason), unflag: (flagId) => void unflagTask(flagId), busy }} />
              <TrashList trash={status.trash} onEdit={editingTasks ? (edit) => void editTasks(edit) : undefined} />
            </section>
          ) : null}
          {session ? (
            <section className="liveColumns">
              <div className="livePanel">
                <h2>Summaries</h2>
                <SummaryList summaries={summaries} busy={busy} onFlag={(item, comment, verdict) => flagSummary(item, comment, verdict)} />
              </div>
              <div className="livePanel">
                <h2>Proposals</h2>
                <ProposalSetList proposalSets={proposalSets} busy={busy} acceptLabel="Accept" onDecide={(item, decision, choice) => void sendFeedback(item, decision, choice)} onChat={(item, choice) => void sendToChat(item, choice)} onReason={(item, reason, typed) => void sendReason(item, reason, typed)} reasons={status?.rejectReasons ?? []} />
              </div>
            </section>
          ) : null}

          <section className="livePanel">
            <h2>One-shot task</h2>
            <p className="liveMuted">One prompt, one run, with the session&apos;s recent worklog and latest proposal attached. Runs go one at a time.</p>
            <textarea ref={promptBox} rows={3} defaultValue="Look at what I was just doing and finish the obvious next step. Do not send or submit anything." />
            <div className="liveActions">
              {runAgents.length > 1 ? (
                <label>Agent <select value={agent} onChange={(event) => setRunAgent(event.target.value)}>{runAgents.map((item) => <option key={item} value={item}>{AGENT_LABELS[item]}</option>)}</select></label>
              ) : null}
              <label>Max turns <input type="number" min={1} max={300} value={maxTurns} onChange={(event) => setMaxTurns(event.target.value)} /></label>
              <button type="button" className="primary" disabled={!session || session.paused || runningId !== null} onClick={() => void startRun()}>Run task</button>
              {runningId ? <button type="button" className="secondary" onClick={() => void stopRun(runningId)}>Stop run</button> : null}
            </div>
            {runIssue ? <p className="liveIssue">{runIssue}</p> : null}
            <h3>Runs in this session</h3>
            <RunList ids={listedRunIds} runs={runs} streamed={streamed} runnerBaseUrl={runnerBaseUrl} onStop={(id) => void stopRun(id)} onAnswer={(id, mode) => void answerApproval(id, mode)} />
          </section>

          <section className="livePanel">
            <h2>Chat with the agent</h2>
            <p className="liveMuted">Each message is one run; it carries what was observed since your previous message. The agent is fixed by the first message.</p>
            <ChatPanel chat={chatView ?? chat} runs={runs} streamed={streamed} runnerBaseUrl={runnerBaseUrl} seed={chatSeed} agents={runAgents} agent={chatAgent} onAgent={setChatAgent} turns={chatTurns} onTurns={setChatTurns} busy={!session || runningId !== null} paused={Boolean(session?.paused)} onSend={sendChat} onStop={(id) => void stopRun(id)} onReset={() => void resetChat()} onAnswer={(id, mode) => void answerApproval(id, mode)} onBrowse={(id) => void browseChat(id)} />
            {chatIssue ? <p className="liveIssue">{chatIssue}</p> : null}
          </section>
        </>
      )}
    </main>
  );
}
