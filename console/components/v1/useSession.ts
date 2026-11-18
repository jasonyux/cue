"use client";

import {
  liveChatListSchema,
  liveChatSchema,
  liveFeedbackResponseSchema,
  liveProposalSetsResponseSchema,
  liveSessionListSchema,
  liveSessionStatusSchema,
  liveTaskChangesSchema,
  liveTaskFlagSchema,
  liveTasksResponseSchema,
  liveUsageSchema,
  runDetailSchema,
  runEventSchema,
  startRunResponseSchema,
  type LiveChat,
  type LiveChatList,
  type LiveChatMessage,
  type LiveProposalSet,
  type LiveSessionStatus,
  type LiveTaskChange,
  type LiveTaskFlag,
  type LiveUsage,
  type RunDetail,
  type RunEvent,
} from "@cue/contracts";
import { createContext, createElement, type ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";

import { callRunner, proposalsOf, rawJson, usePolled, type Parser, type TaskEdit } from "../live/shared";

export type SessionList = ReturnType<typeof liveSessionListSchema.parse>;
export type Decision = "accept" | "reject";
export type ProposalRef = { proposalsTs: string; choice: number }; // which proposal of which set
export type SelectedTask = NonNullable<LiveChatMessage["selectedTasks"]>[number]; // a task entry picked in the tray for the agent
type Queued = { text: string; agent: string; model?: string | undefined; proposal?: ProposalRef | null | undefined; selectedTasks?: SelectedTask[] | undefined }; // a message typed while a turn ran

// The pilot's state: the session status, proposals, chat, latest task-list change and today's cost polled every few
// seconds, every run of the session loaded once and streamed while it runs, and the actions the page offers. One
// EventSource at a time, like the debug console. ``active`` is false for a page whose layout keeps the session: it
// polls nothing.
function useOwnSession(runnerBaseUrl: string, backend: string, active: boolean) {
  const [status, setStatus] = usePolled<LiveSessionStatus | null>(null);
  const [proposalSets, setProposalSets] = usePolled<LiveProposalSet[]>([]);
  const [chat, setChat] = usePolled<LiveChat | null>(null);
  const [changes, setChanges] = usePolled<LiveTaskChange[]>([]);
  const [usage, setUsage] = usePolled<LiveUsage | null>(null);
  const [sessions, setSessions] = usePolled<SessionList>([]);
  const [runs, setRuns] = useState<Record<string, RunDetail>>({});
  const [streamed, setStreamed] = useState<Record<string, RunEvent[]>>({});
  const [waiting, setWaiting] = useState<string | null>(null); // what the page waits on the backend for
  const [issue, setIssue] = useState<string | null>(null);
  const [chatView, setChatView] = useState<LiveChat | null>(null); // an earlier conversation while browsing it
  const [chats, setChats] = useState<LiveChatList | null>(null); // every conversation across sessions, for the sidebar
  const [chatSeed, setChatSeed] = useState(""); // what "Yes, in chat" put in the box; "" again once sent
  const [seedProposal, setSeedProposal] = useState<ProposalRef | null>(null); // the accepted suggestion the next message sends, once Accept put it in the box
  const [sending, setSending] = useState<string | null>(null); // a message on its way: shown until the conversation carries it
  const [queued, setQueued] = useState<Queued[]>([]); // typed while a turn ran: each goes out as its own turn once the running one ends
  const draining = useRef(false);
  const eventSource = useRef<{ runId: string; source: EventSource } | null>(null);
  const runsRef = useRef(runs);
  runsRef.current = runs;

  const call = useCallback(<T,>(path: string, schema: Parser<T>, init?: RequestInit, timeoutMs?: number) =>
    callRunner(runnerBaseUrl, backend, path, schema, init, timeoutMs), [runnerBaseUrl, backend]);

  const closeStream = useCallback(() => {
    eventSource.current?.source.close();
    eventSource.current = null;
  }, []);
  useEffect(() => closeStream, [closeStream]);

  const loadRun = useCallback(async (runId: string) => {
    try {
      const detail = await call(`/api/runs/${runId}`, runDetailSchema);
      setRuns((current) => ({ ...current, [runId]: detail }));
      if (detail.run.status !== "running" && eventSource.current?.runId === runId) closeStream();
      return detail;
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not load the run.");
      return null;
    }
  }, [call, closeStream]);

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
      const next = await call("/api/live/session", liveSessionStatusSchema);
      setStatus(next);
      if (next.session) void ensureRuns(next.session.runs);
      const listed = call("/api/live/chats", liveChatListSchema).catch(() => null); // the sidebar's list, session or none: the page stands without it
      if (next.active) {
        const [sets, conversation, recent] = await Promise.all([
          call("/api/live/session/proposals?limit=60", liveProposalSetsResponseSchema),
          call("/api/live/session/chat", liveChatSchema),
          call("/api/live/session/changes?limit=5", liveTaskChangesSchema),
        ]);
        setProposalSets(sets);
        setChat(conversation);
        void ensureRuns(conversation.messages.map((message) => message.runId)); // a continued conversation's turns ran in earlier sessions
        setChanges(recent.filter((change) => change.ops.length)); // the Tasks panel shows the latest applied change; a tick whose calls were all held or refused applied nothing
      } else {
        setSessions(await call("/api/live/sessions?limit=1", liveSessionListSchema));
      }
      const list = await listed;
      if (list) setChats(list);
      setUsage(await call("/api/live/usage?days=1", liveUsageSchema));
      setIssue(null);
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Runner unavailable.");
    }
  }, [call, ensureRuns, setStatus, setProposalSets, setChat, setChanges, setSessions, setUsage]);

  useEffect(() => {
    if (!active) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 5_000);
    return () => clearInterval(timer);
  }, [refresh, active]);

  // One action at a time against the backend, with the banner text while it runs.
  const act = useCallback(async (text: string | null, work: () => Promise<void>, fallback: string) => {
    if (text) setWaiting(text);
    try {
      await work();
      await refresh();
    } catch (error) {
      setIssue(error instanceof Error ? error.message : fallback);
    } finally {
      if (text) setWaiting(null);
    }
  }, [refresh]);

  const session = status?.session ?? null;
  // The running session's cost so far; a Claude Code turn adds its cost when it ends.
  const sessionCost = usage?.session ? usage.session.reduce((sum, row) => sum + (row.costUsd ?? 0), 0) : null;
  const runningId = session?.runs.find((id) => runs[id]?.run.status === "running") ?? null;

  // One chat turn; ``model``: this turn's, for Claude Code or Codex; ``proposal``: the accepted suggestion the message sends, as
  // written or changed in the box (the one Accept put there, unless given), so the run and the acceptance record it;
  // ``selectedTasks``: the tray entries the text quotes at its top, kept on the message's row.
  const sendChat = async (text: string, agent: string, model?: string, proposal?: ProposalRef, selectedTasks?: SelectedTask[]): Promise<boolean> => {
    const sent = proposal ?? seedProposal;
    if (runningId) { // a turn is running: the message waits, and goes with what is observed up to the moment it is sent
      setQueued((current) => [...current, { text, agent, model, proposal: sent, selectedTasks }]);
      setChatSeed("");
      setSeedProposal(null);
      return true;
    }
    setSending(text);
    try {
      const started = await call("/api/live/session/chat/messages", startRunResponseSchema,
        { method: "POST", body: JSON.stringify({ text, agent, ...(model ? { model } : {}), ...(sent ? { proposal: sent } : {}), ...(selectedTasks?.length ? { selectedTasks } : {}) }) });
      setChatSeed("");
      setSeedProposal(null);
      setRuns((current) => ({ ...current, [started.runId]: started.detail }));
      attachStream(started.runId, started.eventStreamUrl);
      setChat(await call("/api/live/session/chat", liveChatSchema)); // one render with the finally below: the conversation's rows replace the bubbles
      void refresh();
      return true;
    } catch (error) {
      setIssue(error instanceof Error ? error.message : "Could not send the message.");
      return false;
    } finally {
      setSending(null);
    }
  };
  useEffect(() => { // the queue drains one message per turn; a send that fails goes back into the box
    if (runningId || sending || draining.current || !queued.length || session?.paused) return;
    const [next, ...rest] = queued;
    draining.current = true;
    setQueued(rest);
    void sendChat(next!.text, next!.agent, next!.model, next!.proposal ?? undefined, next!.selectedTasks).then((ok) => { if (!ok) setChatSeed(next!.text); }).finally(() => { draining.current = false; });
  }, [runningId, sending, queued, session?.paused]); // eslint-disable-line react-hooks/exhaustive-deps -- sendChat is rebuilt each render
  const accept = (item: LiveProposalSet, choice: number) => act("Recording the decision…",
    async () => { await call("/api/live/session/proposals/feedback", liveFeedbackResponseSchema, { method: "POST", body: JSON.stringify({ proposalsTs: item.ts, decision: "accept", choice }) }); }, "Could not record the decision.");

  return {
    status, session, sessionCost, proposalSets, changes, usage, sessions, runs, streamed, waiting, issue, chatSeed, runningId,
    chat: chatView ?? chat, chats, browsing: chatView !== null, sending, queued,
    unqueue: (index: number) => setQueued((current) => current.filter((_, at) => at !== index)),
    busy: waiting !== null,
    refresh,
    setChatSeed,

    startSession: (overrides: Record<string, unknown>) => act("Starting the session: launching the observer and the model loops…",
      async () => { await call("/api/live/session", rawJson, { method: "POST", body: JSON.stringify(overrides) }, 120_000); }, "Could not start the session."),
    stopSession: () => act("Stopping the session: finishing the last notes and task update, then storing it…",
      async () => { await call("/api/live/session/stop", rawJson, { method: "POST" }, 300_000); }, "Could not stop the session."),
    pauseSession: (paused: boolean) => act(null,
      async () => { await call(`/api/live/session/${paused ? "resume" : "pause"}`, rawJson, { method: "POST" }); }, "Could not pause the session."),
    // The idle trigger: on or off and its pause, for the running session at once and as the next sessions' default.
    setIdleTrigger: (enabled: boolean, idleS: number) => act(null,
      async () => { await call("/api/live/session/idle-trigger", rawJson, { method: "POST", body: JSON.stringify({ enabled, idleS }) }); }, "Could not change the pause suggestions."),
    // The landing page's sliders: a few settings for the running session at once and as the next sessions' default.
    tune: (values: Record<string, number | boolean>) => act(
      null,
      async () => { await call("/api/live/session/tune", rawJson, { method: "POST", body: JSON.stringify(values) }); },
      "Could not change the setting.",
    ),

    // A decision on a proposal set: accept (choice: which proposal) or reject, then the reason after a reject.
    decide: (item: LiveProposalSet, decision: Decision, choice: number) => act("Recording the decision…",
      async () => { await call("/api/live/session/proposals/feedback", liveFeedbackResponseSchema, { method: "POST", body: JSON.stringify({ proposalsTs: item.ts, decision, choice }) }); }, "Could not record the decision."),
    giveReason: (item: LiveProposalSet, reason: string, typed: boolean) => act("Saving the reason…",
      async () => { await call("/api/live/session/proposals/feedback/reason", rawJson, { method: "POST", body: JSON.stringify({ proposalsTs: item.ts, reason, typed }) }); }, "Could not record the reason."),
    acceptToChat: async (item: LiveProposalSet, choice: number) => { // Edit first: the decision, then the suggestion in the box to change
      await accept(item, choice);
      setChatSeed(proposalsOf(item)[choice]?.task ?? "");
      setSeedProposal({ proposalsTs: item.ts, choice });
    },
    acceptAndSend: async (item: LiveProposalSet, choice: number, agent: string, model?: string): Promise<boolean> => { // Send as is: the decision, then the suggestion as the next message
      await accept(item, choice);
      return sendChat(proposalsOf(item)[choice]?.task ?? "", agent, model, { proposalsTs: item.ts, choice });
    },

    sendChat,
    resetChat: () => act(null, async () => { await call("/api/live/session/chat/reset", rawJson, { method: "POST" }); setChatView(null); setSeedProposal(null); }, "Could not start a new conversation."),
    switchChat: (chatId: string) => act(null, async () => { await call("/api/live/session/chat/switch", rawJson, { method: "POST", body: JSON.stringify({ id: chatId }) }); setChatView(null); setSeedProposal(null); }, "Could not continue that conversation."),
    markChat: (chatId: string, mark: { title?: string; pinned?: boolean }) => act(null, async () => { await call(`/api/live/chats/${encodeURIComponent(chatId)}`, rawJson, { method: "PATCH", body: JSON.stringify(mark) }); }, "Could not change the conversation."),
    renameSession: (sessionId: string, name: string) => act(null, async () => { await call(`/api/live/sessions/${encodeURIComponent(sessionId)}`, rawJson, { method: "PATCH", body: JSON.stringify({ name: name.trim() || null }) }); }, "Could not rename the session."),
    browseChat: async (chatId: string | null) => {
      try {
        const shown = chatId ? await call(`/api/live/session/chat?id=${encodeURIComponent(chatId)}`, liveChatSchema) : null;
        setChatView(shown && shown.id !== shown.current ? shown : null);
        if (shown) void ensureRuns(shown.messages.map((message) => message.runId)); // its replies unfold into their runs, as the current conversation's do
      } catch (error) {
        setIssue(error instanceof Error ? error.message : "Could not load that conversation.");
      }
    },
    // A temporary run: one prompt, one run with the session's context, outside the conversation; with ``proposal``, the accepted proposal it carries out.
    startTemporary: async (prompt: string, agent: string, proposal: { proposalsTs: string; choice: number } | null): Promise<boolean> => {
      try {
        const started = await call("/api/runs", startRunResponseSchema, { method: "POST", body: JSON.stringify({ scenarioId: "desktop-free-task", prompt, liveContext: true, agent, ...(proposal ? { proposal } : {}) }) });
        setRuns((current) => ({ ...current, [started.runId]: started.detail }));
        attachStream(started.runId, started.eventStreamUrl);
        await refresh();
        return true;
      } catch (error) {
        setIssue(error instanceof Error ? error.message : "Could not start the run.");
        return false;
      }
    },
    stopRun: (runId: string) => act(null, async () => { await call(`/api/runs/${runId}/stop`, rawJson, { method: "POST" }); await loadRun(runId); }, "Could not stop the run."),
    answerApproval: (runId: string, mode: "once" | "deny") => act(null,
      async () => { await call(`/api/live/session/runs/${runId}/approval`, rawJson, { method: "POST", body: JSON.stringify({ mode }) }); }, "Could not answer the approval request."),
    // An answer to a permission question (an app looked at under ask): block, look or use become its rule in settings.yaml; session: look until the session ends.
    // The observer looks again within half a second of an answer: the status is watched every 0.5s until that look lands (8s at most), then polled as usual.
    answerPermission: (app: string, level: "block" | "look" | "use" | "deny", scope?: "session" | "once" | "task") => act(null,
      async () => {
        const before = status?.session?.latestSnapshot?.ts;
        const agent = status?.session?.permissionAsks?.some((item) => item.app === app && item.source === "agent");
        await call("/api/live/session/permissions", rawJson, { method: "POST", body: JSON.stringify({ app, level, ...(scope ? { scope } : {}) }) });
        for (let tries = 0; tries < 16; tries++) { // the observer looks again at once: wait for that look; an agent's ask just leaves the list
          const next = await call("/api/live/session", liveSessionStatusSchema);
          setStatus(next);
          if (agent ? !next.session?.permissionAsks?.some((item) => item.app === app && item.source === "agent") : next.session?.latestSnapshot?.ts !== before) break;
          await new Promise((resolve) => setTimeout(resolve, 500));
        }
      }, "Could not save the answer."),

    editTasks: (edit: TaskEdit) => act("Updating the task list…", async () => {
      const next = await call("/api/live/tasks", liveTasksResponseSchema, { method: "POST", body: JSON.stringify({ ops: [edit] }) });
      setStatus((current) => (current ? { ...current, tasks: next.tasks, trash: next.trash } : current));
    }, "Could not update the task."),
    flagTask: async (taskId: string, step: number | null, verdict: "up" | "down"): Promise<LiveTaskFlag | null> => {
      try {
        const flag = await call("/api/live/tasks/feedback", liveTaskFlagSchema, { method: "POST", body: JSON.stringify({ taskId, step, verdict }) });
        await refresh();
        return flag;
      } catch (error) {
        setIssue(error instanceof Error ? error.message : "Could not record the flag.");
        return null;
      }
    },
    flagReason: (flagId: string, reason: string) => act(null,
      async () => { await call("/api/live/tasks/feedback/reason", rawJson, { method: "POST", body: JSON.stringify({ flagId, reason }) }); }, "Could not record the reason."),
    unflag: (flagId: string) => act(null, async () => { await call(`/api/live/tasks/feedback/${flagId}`, rawJson, { method: "DELETE" }); }, "Could not take the flag back."),
  };
}

export type Session = ReturnType<typeof useOwnSession>;

// One session for every /v1 page, kept by their layout: it survives a change of page, so the top bar and the page
// start from what is already known instead of from nothing. A page outside the layout (a test) keeps its own.
const Shared = createContext<Session | null>(null);

export function SessionProvider({ runnerBaseUrl, children }: { runnerBaseUrl: string; children: ReactNode }) {
  return createElement(Shared.Provider, { value: useOwnSession(runnerBaseUrl, "python", true) }, children);
}

export function useSession(runnerBaseUrl: string, backend: string): Session {
  const shared = useContext(Shared);
  const own = useOwnSession(runnerBaseUrl, backend, shared === null);
  return shared ?? own;
}
