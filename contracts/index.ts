import { z } from "zod";
import defaults from "./defaults.json" with { type: "json" };

export const DEFAULT_MODEL = defaults.model;
export const DEFAULT_MODEL_DISPLAY_NAME = defaults.modelDisplayName;

// "desktop" scenarios come only from the Python backend: they operate local apps instead of a lab page.
const labIdSchema = z.enum(["kanban", "paint", "booking", "desktop"]);

const categorySchema = z.enum(["productivity", "creativity", "commerce"]);

const browserModeSchema = z.enum(["headless", "headful"]);
export type BrowserMode = z.infer<typeof browserModeSchema>;

const responseTurnBudgetSchema = z.number().int().positive().max(300);
export type ResponseTurnBudget = z.infer<typeof responseTurnBudgetSchema>;

export const scenarioManifestSchema = z.object({
  id: z.string().min(1),
  labId: labIdSchema,
  category: categorySchema,
  title: z.string().min(1),
  description: z.string().min(1),
  defaultPrompt: z.string().min(1),
  workspaceTemplatePath: z.string().min(1),
  tags: z.array(z.string().min(1)).min(1),
});
export type ScenarioManifest = z.infer<typeof scenarioManifestSchema>;

const runSummarySchema = z.object({
  stepCount: z.number().int().nonnegative(),
  screenshotCount: z.number().int().nonnegative(),
  notes: z.array(z.string()),
});
export type RunSummary = z.infer<typeof runSummarySchema>;

const runStatusSchema = z.enum([
  "running",
  "completed",
  "failed",
  "cancelled",
]);

const runEventLevelSchema = z.enum(["ok", "pending", "warn", "error"]);
export type RunEventLevel = z.infer<typeof runEventLevelSchema>;

const runEventTypeSchema = z.enum([
  "run_started",
  "workspace_prepared",
  "lab_started",
  "browser_session_started",
  "browser_navigated",
  "function_call_requested",
  "function_call_completed",
  "tool_progress", // a line of output from a still-running exec_py cell (the live actor)
  "screenshot_captured",
  "run_progress",
  "run_completed",
  "run_failed",
  "run_cancelled",
]);
export type RunEventType = z.infer<typeof runEventTypeSchema>;

export const runEventSchema = z.object({
  id: z.string().min(1),
  runId: z.string().min(1),
  sequence: z.number().int().nonnegative(),
  type: runEventTypeSchema,
  level: runEventLevelSchema,
  message: z.string().min(1),
  detail: z.string().optional(),
  createdAt: z.string().datetime(),
});
export type RunEvent = z.infer<typeof runEventSchema>;

const tokenCountsSchema = z.object({
  calls: z.number().int().nonnegative(),
  inputTokens: z.number().int().nonnegative(),
  cachedInputTokens: z.number().int().nonnegative(),
  cacheWriteTokens: z.number().int().nonnegative(),
  outputTokens: z.number().int().nonnegative(),
  reasoningTokens: z.number().int().nonnegative(),
  totalTokens: z.number().int().nonnegative(),
  costUsd: z.number().nonnegative().optional(),
});
// Populated by the Python backend: per-model token counts and cost for the run's model calls.
const runUsageSchema = tokenCountsSchema.extend({
  byModel: z.array(tokenCountsSchema.extend({ model: z.string().min(1) })),
});
export type RunUsage = z.infer<typeof runUsageSchema>;

export const runRecordSchema = z.object({
  id: z.string().min(1),
  scenarioId: z.string().min(1),
  labId: labIdSchema,
  browserMode: browserModeSchema,
  model: z.string().min(1),
  maxResponseTurns: responseTurnBudgetSchema,
  prompt: z.string().min(1),
  status: runStatusSchema,
  startedAt: z.string().datetime(),
  completedAt: z.string().datetime().optional(),
  durationMs: z.number().int().nonnegative().optional(),
  summary: runSummarySchema.optional(),
  usage: runUsageSchema.optional(),
  proposal: z.object({ proposalsTs: z.string(), choice: z.number().int().nonnegative() }).optional(), // the accepted proposal the run carries out
});
export type RunRecord = z.infer<typeof runRecordSchema>;

const browserViewportSchema = z.object({
  height: z.number().int().positive(),
  width: z.number().int().positive(),
});
export type BrowserViewport = z.infer<typeof browserViewportSchema>;

export const browserScreenshotArtifactSchema = z.object({
  source: z.enum(["browser_preview", "code_tool"]).optional(),
  placeholder: z.boolean().optional(), // desktop runs: a stand-in frame captured before any window was observed
  imageWidth: z.number().int().positive().optional(),
  imageHeight: z.number().int().positive().optional(),
  capturedAt: z.string().datetime(),
  id: z.string().min(1),
  label: z.string().min(1),
  mimeType: z.literal("image/png"),
  pageTitle: z.string().min(1).optional(),
  pageUrl: z.string().min(1),
  path: z.string().min(1),
  url: z.string().min(1),
});
export type BrowserScreenshotArtifact = z.infer<
  typeof browserScreenshotArtifactSchema
>;

export const browserStateSchema = z.object({
  currentUrl: z.string().min(1),
  mode: browserModeSchema,
  pageTitle: z.string().min(1).optional(),
  screenshots: z.array(browserScreenshotArtifactSchema),
  targetLabel: z.string().min(1),
  viewport: browserViewportSchema,
});
export type BrowserState = z.infer<typeof browserStateSchema>;

export const startRunRequestSchema = z
  .object({
    scenarioId: z.string().min(1),
    browserMode: browserModeSchema.optional(),
    maxResponseTurns: responseTurnBudgetSchema.optional(),
    prompt: z.string().min(1),
    model: z.string().min(1).optional(),
    liveContext: z.boolean().optional(),
    agent: z.enum(["desktop", "cmux", "claude", "codex"]).optional(), // with liveContext: which of the session's agents runs it (desktop when omitted)
  })
  .strict();
export type StartRunRequest = z.infer<typeof startRunRequestSchema>;

export const runDetailSchema = z.object({
  run: runRecordSchema,
  scenario: scenarioManifestSchema,
  workspacePath: z.string().min(1),
  eventStreamUrl: z.string().min(1),
  replayUrl: z.string().min(1),
  browser: browserStateSchema.optional(),
  liveContext: z.string().optional(),
  actorYieldS: z.number().positive().optional(), // with liveContext: the live actor's exec_py yield time for this run
  actorDesktopSource: z.array(z.string()).optional(), // with liveContext: the desktops this run had, local and/or remote
  actorSessionFolder: z.string().optional(), // with liveContext, for the agents with desktops: the session's folder, where the worker reads the permissions and writes its asks
  desktopRemoteProvider: z.string().optional(), // with liveContext, when the source includes remote: exec_py's remote target
  desktopRemoteUrl: z.string().optional(),
  actorAgent: z.string().optional(), // with liveContext: the agent that ran it: desktop, cmux or claude
  actorCmuxPermissionMode: z.string().optional(), // a cmux run: claude's --permission-mode
  actorCmuxModel: z.string().optional(), // and, when the session named them, its --model
  actorCmuxCwd: z.string().optional(), // and the folder Claude Code started in
  actorCmuxBrowserWorkspace: z.string().optional(), // and the session's agent browser workspace in cmux
  actorClaudeModel: z.string().optional(), // a claude run, when the session named them: claude's --model
  actorClaudePermissionMode: z.string().optional(), // and its --permission-mode
  actorClaudeCwd: z.string().optional(), // and the folder Claude Code started in
  actorCodexModel: z.string().optional(), // a codex run, when the session named them: Codex's model
  actorCodexSandbox: z.string().optional(), // and its sandbox
  actorCodexCwd: z.string().optional(), // and the folder Codex started in
  actorBrowserEndpoint: z.string().optional(), // and the session's agent browser, by its CDP address
  chat: z // set for a turn of the session's chat: which turn, the agent conversation it resumed and left, and its reply
    .object({ turn: z.number().int().positive(), resume: z.string().optional(), resumeTotals: z.record(z.string(), z.unknown()).optional(), claudeSessionId: z.string().optional(), claudeTotals: z.record(z.string(), z.unknown()).optional(), codexThreadId: z.string().optional(), reply: z.string().optional() })
    .optional(),
  events: z.array(runEventSchema),
});
export type RunDetail = z.infer<typeof runDetailSchema>;

// The models an agent offers the signed-in user, as its own SDK lists them (GET /api/live/agents/{agent}/models).
export const agentModelsSchema = z.object({
  models: z.array(z.object({ id: z.string(), label: z.string(), detail: z.string(), default: z.boolean() })), // default: the agent's own choice
});
export type AgentModel = z.infer<typeof agentModelsSchema>["models"][number];

export const startRunResponseSchema = z.object({
  runId: z.string().min(1),
  status: z.literal("running"),
  eventStreamUrl: z.string().min(1),
  replayUrl: z.string().min(1),
  detail: runDetailSchema,
});
export type StartRunResponse = z.infer<typeof startRunResponseSchema>;

export const scenarioResetResponseSchema = z.object({
  scenarioId: z.string().min(1),
  resetAt: z.string().datetime(),
  cancelledRunId: z.string().min(1).optional(),
});
export type ScenarioResetResponse = z.infer<typeof scenarioResetResponseSchema>;

export const scenariosResponseSchema = z.array(scenarioManifestSchema);

export const runnerErrorResponseSchema = z.object({
  code: z.string().min(1),
  error: z.string().min(1),
  hint: z.string().min(1).optional(),
});
export type RunnerErrorResponse = z.infer<typeof runnerErrorResponseSchema>;

// The Python backend runs exec_py with a visible browser only.
export const backendCapabilitiesSchema = z.object({
  backendId: z.literal("python"),
  instanceId: z.string().min(1),
  codeTool: z.literal("exec_py"),
  browserModes: z.tuple([z.literal("headful")]),
  defaults: z.object({
    browserMode: z.literal("headful"),
    model: z.string().min(1),
    maxResponseTurns: responseTurnBudgetSchema,
  }),
});
export type BackendCapabilities = z.infer<typeof backendCapabilitiesSchema>;

export const replayBundleSchema = z.object({
  version: z.literal(3),
  run: runRecordSchema,
  scenario: scenarioManifestSchema,
  events: z.array(runEventSchema),
  browser: browserStateSchema.optional(),
  artifacts: z.object({
    eventsPath: z.string().min(1),
    replayPath: z.string().min(1),
    runPath: z.string().min(1),
    screenshotsDirectory: z.string().min(1),
    workspacePath: z.string().min(1),
  }),
});
export type ReplayBundle = z.infer<typeof replayBundleSchema>;

// ----- Continuous sessions (Python backend only): observer + summary/proposer loop -----

// How a stage endpoint takes its parameters: vllm sends a temperature and Qwen's thinking flag, openai sends reasoning_effort and no temperature.
export const liveProviderSchema = z.enum(["vllm", "openai", "anthropic", "openrouter", "litellm"]); // how an endpoint takes its parameters, and (but vllm) whose key set it reads
export type LiveProvider = z.infer<typeof liveProviderSchema>;

// A held key, abbreviated; a session stored before that said only whether one was held.
const keyHintSchema = z.union([z.string(), z.boolean().transform((held) => (held ? "•••" : ""))]);

// The idle trigger's state in a running session (python-app/app/live/proposer/idle.py).
export const liveIdleTriggerSchema = z.object({
  enabled: z.boolean(),
  state: z.enum(["off", "armed", "firing", "shown", "away"]),
  idleS: z.number(),
  thresholdS: z.number(), // idleS, lengthened while its proposals go unanswered
  inputIdleS: z.number().nullable(),
  firedAt: z.string().nullable(),
});
export type LiveIdleTrigger = z.infer<typeof liveIdleTriggerSchema>;

export const liveSettingsSchema = z.object({
  summaryModel: z.string(), // empty when summary.model is unset; a session cannot start without it
  summaryBaseUrl: z.string().nullable().optional(),
  summaryProvider: liveProviderSchema,
  summaryStream: z.boolean(), // stream the reply and assemble it; off for an endpoint that cannot stream
  proposerModel: z.string(),
  proposerBaseUrl: z.string().nullable().optional(),
  proposerProvider: liveProviderSchema,
  proposerStream: z.boolean(),
  beliefModel: z.string(), // empty when belief.model is unset; a session cannot start without it
  beliefBaseUrl: z.string().nullable().optional(),
  beliefProvider: liveProviderSchema,
  beliefStream: z.boolean(),
  preferenceModel: z.string().optional(), // the preference updater's endpoint (sessions stored before it lack these); empty when unset, a session cannot start without it
  preferenceBaseUrl: z.string().nullable().optional(),
  preferenceProvider: liveProviderSchema.optional(),
  preferenceStream: z.boolean().optional(),
  embedModel: z.string(), // the embeddings endpoint behind the proposer's duplicate filter; empty when unset
  embedBaseUrl: z.string().nullable().optional(),
  embedProvider: liveProviderSchema.optional(), // the embedder's provider: whose key set it reads (sessions before it have none)
  providerKeys: z.object({ openai: keyHintSchema, anthropic: keyHintSchema, openrouter: keyHintSchema, litellm: keyHintSchema, vllm: keyHintSchema }).optional(), // the providers' keys the backend's .env holds, each abbreviated to its first and last characters ("" when none)
  openaiApiKey: z.string().optional(), // a provider's key from the settings page: sent with a start, never sent back
  anthropicApiKey: z.string().optional(),
  openrouterApiKey: z.string().optional(),
  litellmApiKey: z.string().optional(),
  vllmApiKey: z.string().optional(),
  openaiBaseUrl: z.string().nullable().optional(), // a provider's base URL: its key set's, else the provider's own; the form's for a start or a save
  anthropicBaseUrl: z.string().nullable().optional(),
  openrouterBaseUrl: z.string().nullable().optional(),
  litellmBaseUrl: z.string().nullable().optional(),
  vllmBaseUrl: z.string().nullable().optional(),
  snapshotIntervalS: z.number().positive(),
  summaryIntervalS: z.number().positive(),
  proposerIntervalS: z.number().positive(),
  summaryThinking: z.boolean(),
  proposerThinking: z.boolean(),
  summaryScreenshots: z.boolean(),
  proposerScreenshots: z.boolean(),
  proposerInputs: z.array(z.enum(["tasks", "worklog", "snapshots", "current_window", "feedback", "chat", "preferences"])).min(1), // what the proposer is shown; screenshots have their own flag
  beliefIntervalS: z.number().positive(),
  beliefThinking: z.boolean(),
  beliefMaxTokens: z.number().int().nonnegative(),
  beliefStaleDays: z.number().int().positive(),
  beliefDoneStepDays: z.number().int().positive(),
  beliefTrashDays: z.number().int().positive(),
  beliefContextSummaries: z.number().int().nonnegative().optional(), // earlier worklog lines the belief updater sees before the new ones
  beliefSimilarThreshold: z.number().min(0).max(1).optional(), // an added entry this close (cosine) to one on the list is held back once; 0 = off
  preferenceThinking: z.boolean().optional(), // the preference updater: on its timer, the model free to leave the file as it is
  preferenceIntervalS: z.number().positive().optional(), // a tick this often while a session runs, and one at its stop
  preferenceMinFeedback: z.number().int().positive().optional(), // reactions a tick needs; fewer wait for the next
  preferenceMaxTokens: z.number().int().nonnegative().optional(),
  preferenceCapTokens: z.number().int().positive().optional(), // the preferences file's size cap, in tokens
  preferenceContextSummaries: z.number().int().positive().optional(), // worklog lines it is shown
  preferenceContextDecisions: z.number().int().positive().optional(), // earlier decisions shown before the new reactions
  preferenceSearchRounds: z.number().int().nonnegative().optional(), // history searches a tick may make; 0 leaves the tool out
  streamLookbackDays: z.number().int().positive(), // how far back a stage's window reads the stream for what stood at its end
  actorYieldS: z.number().positive(),
  actorContextSummaries: z.number().int().positive(), // how many of the session's last worklog lines a run is given as context
  actorDesktopSource: z.array(z.enum(["local", "remote"])).min(1), // the desktops a run gets: local, remote, or both (then exec_py takes a target)
  desktopRemoteProvider: z.string().nullable().optional(), // the actor's remote desktop, by provider name ("cua"); null: none
  desktopRemoteUrl: z.string().nullable().optional(), // where it is reached
  desktopRemoteToken: z.string().optional(), // its token from the settings page: sent with a save or a start, never sent back
  desktopRemoteTokenHint: z.string().optional(), // the one the backend's .env holds, abbreviated ("" when none)
  actorAgents: z.array(z.enum(["desktop", "cmux", "claude", "codex"])).min(1), // the agents runs can use: the desktop agent, Claude Code in cmux, Claude Code or Codex on their own
  actorCmuxModel: z.string().nullable().optional(), // the cmux agent's claude --model; null: Claude Code's default
  actorCmuxPermissionMode: z.string(), // its claude --permission-mode, auto unless changed
  actorCmuxCwd: z.string().nullable().optional(), // the folder Claude Code starts in; null: the run's workspace folder
  actorClaudeModel: z.string().nullable().optional(), // the claude agent's claude --model; null: Claude Code's default
  actorClaudePermissionMode: z.string(), // its claude --permission-mode, auto unless changed; what it holds back is answered on the run card
  actorClaudeCwd: z.string().nullable().optional(), // the folder Claude Code starts in; null: the run's workspace folder
  actorCodexModel: z.string().nullable().optional(), // the codex agent's model; null: Codex's default
  actorCodexSandbox: z.string(), // its sandbox: read-only, workspace-write (the default) or full-access
  actorCodexCwd: z.string().nullable().optional(), // the folder Codex starts in; null: the run's workspace folder
  maxProposals: z.number().int().min(1), // the backend enforces its cap and reports it in status.limits
  summaryMaxTokens: z.number().int().nonnegative(), // 0 = automatic
  proposerMaxTokens: z.number().int().nonnegative(),
  proposerFeedbackRejected: z.number().int().positive(), // the feedback source: the last N rejected proposals, typed reasons first,
  proposerFeedbackAccepted: z.number().int().positive(), // the last N accepted,
  proposerFeedbackRecent: z.number().int().positive(), // and the top proposal of the last N rounds of this session nobody has answered yet
  proposerSearchRounds: z.number().int().nonnegative(), // the history search tool: searches a round at most; 0 leaves the tool out
  proposerDedup: z.boolean(), // the guardrail after generation: drop proposals that embed too close to decided or already shown ones
  proposerDedupThreshold: z.number().positive().max(1),
  proposerDedupNotNowHours: z.number().positive(), // a "not now" rejection stays a reference this long
  proposerDedupSessionHours: z.number().positive(), // this session's earlier proposals stay references this long
  // The idle trigger: one quick proposal when the person pauses after working (optional: sessions stored before it lack them)
  proposerIdleTrigger: z.boolean().optional(),
  proposerIdleS: z.number().positive().optional(), // seconds without keyboard or mouse input that fire it
  proposerIdleCooldownS: z.number().positive().optional(),
  proposerIdleAwayS: z.number().positive().optional(),
  proposerIdleContextS: z.number().positive().optional(),
  proposerIdleProposals: z.number().int().min(1).optional(),
  proposerIdleThinking: z.boolean().optional(),
  screenshotMode: z.enum(["switch", "always", "off"]),
  effectiveScreenshotMode: z.enum(["switch", "always", "off"]),
  screenshotMaxSide: z.number().int().positive(),
  maxSessionMinutes: z.number().nonnegative(),
  permissionsDefault: z.enum(["block", "ask", "look", "use"]).optional(), // what may be read of an app no rule names (ask: blocked and asked about, once per app); optional: sessions stored before it
  permissionsApps: z.record(z.string(), z.enum(["block", "look", "use"])).optional(), // app name or bundle id (globs allowed) → level
  permissionsSites: z.record(z.string(), z.enum(["block", "look", "use"])).optional(), // a domain or a glob on the address → level, in any browser
  maxElements: z.number().int().positive(),
  notify: z.boolean(),
  proposerContextSummaries: z.number().int().positive(), // the worklog source: the session's last N worklog lines, as actorContextSummaries is a run's
});
export type LiveSettings = z.infer<typeof liveSettingsSchema>;

export const liveSummarySchema = z.object({
  id: z.string().optional(), // the record's id, the same as its stream event's; absent on rows from before ids were written
  ts: z.string(),
  windowStart: z.string(),
  windowEnd: z.string(),
  summary: z.string(),
  model: z.string().optional(),
  images: z.number().int().optional(),
  fallback: z.string().optional(), // "thinking_truncated": reasoning hit the token limit, answered without thinking
  feedback: z.object({ verdict: z.enum(["wrong", "ok"]), comment: z.string(), ts: z.string() }).optional(),
});
export type LiveSummary = z.infer<typeof liveSummarySchema>;

// The session's chat with the agent (python-app live/actor/chat.py): each turn is a run, listed under the chat.
export const liveChatMessageSchema = z.object({
  id: z.string().optional(), // the record's id, the same as its stream event's; absent on rows from before ids were written
  ts: z.string(),
  chatId: z.string(),
  turn: z.number().int().positive(),
  role: z.enum(["user", "agent"]),
  agent: z.string().nullable().optional(), // the conversation's agent
  text: z.string(),
  context: z.string().optional(), // user: the observed context prepended to the message
  attached: z.number().int().optional(), // user: how many worklog lines that context carried
  selectedTasks: z.array(z.object({ taskId: z.string(), title: z.string(), text: z.string() })).optional(), // user: the task entries picked in the tray (Ask Pilot), which the text quotes at its top
  status: z.string().optional(), // agent: how the turn's run ended (completed, failed, cancelled)
  runId: z.string(),
  sessionId: z.string().optional(), // the session the turn ran in (a conversation can span several); absent on rows from before
});
export type LiveChatMessage = z.infer<typeof liveChatMessageSchema>;

// One conversation, as the store lists them (python-app/app/live/actor/chat.py): kept across sessions in data/memory/chats.
export const liveConversationSchema = z.object({
  id: z.string(),
  agent: z.string().nullable(),
  turns: z.number().int(),
  startedAt: z.string(),
  firstMessage: z.string(),
  lastAt: z.string().optional(), // when it last moved
  sessions: z.array(z.string()).optional(), // the sessions it took part in
  legacy: z.boolean().optional(), // kept in a session's own folder, from before the store
  viewOnly: z.boolean().optional(), // readable, not continued
  title: z.string().nullable().optional(), // a name the person gave it, shown in place of its first message
  pinned: z.boolean().optional(), // kept at the top of the sidebar
});
export type LiveConversation = z.infer<typeof liveConversationSchema>;
export const liveChatListSchema = z.object({
  conversations: z.array(liveConversationSchema),
  current: z.string().nullable(),
  sessions: z.record(z.string().nullable()).optional(), // the sessions the conversations were in, by id: the name given, else null
});
export type LiveChatList = z.infer<typeof liveChatListSchema>;

export const liveChatSchema = z.object({
  id: z.string().nullable(), // the conversation shown: the current one, or an earlier one asked for by id
  current: z.string().nullable(), // the conversation the next message continues (none in a stored session)
  agent: z.string().nullable(), // fixed by the first message of the conversation
  turn: z.number().int().nonnegative(),
  runningRunId: z.string().nullable(),
  messages: z.array(liveChatMessageSchema),
  pending: z.array(liveSummarySchema), // the worklog lines the next message will carry (empty for an earlier conversation)
  conversations: z.array(liveConversationSchema), // every conversation, latest first
});
export type LiveChat = z.infer<typeof liveChatSchema>;

// One ranked suggestion from the proposer. "explain" and "lookup" are answered in words; "act" needs the agent's hands.
// action/reversible only appear on records from the first ranked-proposal version; nothing runs from a proposalSet.
export const liveProposalSchema = z.object({
  kind: z.enum(["explain", "lookup", "act"]),
  task: z.string(),
  reason: z.string().optional(),
  confidence: z.number().optional(), // 1 (a long shot) to 5 (sure); 0 or absent when the model gave none
  humanSeconds: z.number().nullable().optional(), // the proposer's rough estimate of the person's own time to do it; absent on older records
  evidence: z.array(z.string()).optional(), // the ids of the records it rests on, as the prompt showed them: worklog lines, decisions, chat turns, search results, tasks
  action: z.enum(["propose", "do"]).optional(),
  reversible: z.boolean().optional(),
});
export type LiveProposal = z.infer<typeof liveProposalSchema>;

// The flat action/task/reason/confidence fields mirror proposals[0]; "none" means the list is empty.
export const liveProposalSetSchema = z
  .object({
    id: z.string().optional(), // the record's id, the same as its stream event's; absent on rows from before ids were written
    ts: z.string(),
    windowStart: z.string().optional(),
    windowEnd: z.string().optional(),
    summary: z.string().optional(),
    kind: z.enum(["explain", "lookup", "act"]).optional(),
    action: z.enum(["none", "propose", "do"]).optional(),
    task: z.string().nullable().optional(),
    reason: z.string().optional(),
    reversible: z.boolean().optional(),
    confidence: z.number().optional(),
    proposals: z.array(liveProposalSchema).optional(),
    filtered: z // what the duplicate filter removed, with the score and the reference it matched
      .array(liveProposalSchema.extend({ score: z.number(), matched: z.string(), source: z.enum(["decision", "not_now", "session", "stale"]), note: z.string() }))
      .optional(),
    effect: z.string().optional(),
    trigger: z.enum(["idle"]).optional(), // the idle trigger made it; absent for the timer's rounds
    cited: z // the search results the proposals cite, so one from another session can be shown
      .array(z.object({ id: z.string(), ts: z.string(), kind: z.string(), sessionId: z.string(), text: z.string(), line: z.string() }))
      .optional(),
    searches: z // the history searches the model made before answering: the query, regex and kinds, the slice, how many hits, the first few lines; or why it was rejected
      .array(z.object({ query: z.string(), regex: z.string().nullable().optional(), kinds: z.array(z.string()).optional(), since: z.string().nullable().optional(), until: z.string().nullable().optional(), hits: z.number().int(), lines: z.array(z.string()), error: z.string().optional() }))
      .optional(),
    policy: z.string().optional(),
    model: z.string().optional(),
    reasoning: z.string().optional(),
    fallback: z.string().optional(),
    error: z.string().optional(),
    feedback: z
      .object({
        id: z.string().optional(), // the decision's id, the same as its stream event's
        decision: z.enum(["accept", "reject"]),
        ts: z.string(),
        choice: z.number().int().nullable().optional(),
        rejectReason: z.string().optional(),
        modified: z.boolean().optional(), // accepted and sent to the chat with its text changed first (the row keeps the text sent)
        digested: z.boolean().optional(), // the digest of what the proposer saw is made after the decision, in the background
        digestError: z.string().optional(),
      })
      .optional(),
  })
  .passthrough();
export type LiveProposalSet = z.infer<typeof liveProposalSetSchema>;

// Fields written to session.json; the same shape comes back for stored sessions in the history view.
export const liveSessionRecordSchema = z.object({
  id: z.string(),
  name: z.string().nullable().optional(), // operator-given display name; the folder keeps the timestamp id
  folder: z.string(),
  startedAt: z.string(),
  stoppedAt: z.string().nullable().optional(),
  stopReason: z.string().nullable().optional(),
  expiresAt: z.string().nullable().optional(),
  pausedAt: z.string().nullable().optional(), // set while the session is paused
  pausedS: z.number().optional(), // how long it was paused in all; sessions from before pausing carry none
  settings: liveSettingsSchema,
  usage: runUsageSchema.nullable().optional(),
  runs: z.array(z.string()),
  chatRuns: z.array(z.string()), // the runs among them that are chat turns: shown with the chat, not in the Runs list
  lastError: z.string().nullable().optional(),
});
export type LiveSessionRecord = z.infer<typeof liveSessionRecordSchema>;

// One task the belief updater believes the person is working on; tasks.json across sessions, beliefs.jsonl per session.
export const liveTaskSchema = z.object({
  id: z.string(),
  title: z.string(),
  status: z.enum(["active", "done", "paused"]),
  steps: z.array(z.object({ text: z.string(), doneAt: z.string().nullable().optional(), deletedAt: z.string().nullable().optional(), callId: z.string().optional(), addedAt: z.string().optional() })), // the checklist; callId: the call that added the entry, addedAt: when
  lastSeen: z.string(),
  sessionId: z.string(),
  evidence: z.array(z.string()),
  deletedAt: z.string().nullable().optional(), // set on a task listed in the trash
  callId: z.string().optional(), // the model call (or operator edit) that added the task
});
export type LiveTask = z.infer<typeof liveTaskSchema>;
// After operator corrections: the visible list, and the trash (deleted tasks whole; other tasks with only their deleted entries).
export const liveTasksResponseSchema = z.object({ tasks: z.array(liveTaskSchema), trash: z.array(liveTaskSchema) });
// POST /api/live/tasks/feedback: the operator's thumbs up or down on a task (step null) or one of its entries; the reason comes after a thumbs down.
export const liveTaskFlagSchema = z.object({
  id: z.string(),
  ts: z.string(),
  sessionId: z.string(),
  taskId: z.string(),
  title: z.string(),
  step: z.number().int().nullable(),
  text: z.string(),
  verdict: z.enum(["up", "down"]),
  reason: z.string(),
  callId: z.string().optional(), // copied from the task or entry the flag is on
});
export type LiveTaskFlag = z.infer<typeof liveTaskFlagSchema>;

export const liveSessionStatusSchema = z.object({
  active: z.boolean(),
  defaults: liveSettingsSchema,
  rejectReasons: z.array(z.string()), // the one-click reasons after a Reject, in order; the backend owns the list
  limits: z.object({ maxProposals: z.number().int().positive() }),
  tasks: z.array(liveTaskSchema),
  trash: z.array(liveTaskSchema),
  taskFlags: z.array(liveTaskFlagSchema), // the latest flag per task and entry
  session: liveSessionRecordSchema
    .extend({
      observerAlive: z.boolean(),
      paused: z.boolean(), // nothing runs: no looks, notes, task updates, suggestions or runs until resumed
      snapshotCount: z.number().int(),
      tickCount: z.number().int(),
      screenshotCount: z.number().int(),
      latestSnapshot: z
        .object({ ts: z.string().nullable().optional(), app: z.string().nullable().optional(), window: z.string().nullable().optional(), visible: z.array(z.string()).nullable().optional(),
          blocked: z.enum(["app", "site", "default", "ask"]).nullable().optional() }) // what blocked the look when its content was not read: an app rule, a site rule, the default, a question not answered yet
        .nullable(),
      latestScreenshot: z.object({ ts: z.string(), name: z.string(), app: z.string(), window: z.string() }).nullable(), // the newest stored screenshot, fetched by name
      permissionAsks: z // the permission questions with no answer yet, newest first (POST /api/live/session/permissions answers one): the agent's asks
        .array(z.object({ app: z.string(), bundle: z.string(), ts: z.string(), source: z.literal("agent").optional(), level: z.enum(["look", "use"]).optional(), why: z.string().optional() })) // (source agent: the level it wants and why), then the apps looked at under ask
        .optional(),
      agentBrowser: z // the claude agent's browser, once a run opened it: its tabs (agent: the one the agent is on), streamed and driven from the monitor
        .object({ endpoint: z.string(), viewport: z.object({ width: z.number(), height: z.number() }), tabs: z.array(z.object({ id: z.string(), title: z.string(), url: z.string(), agent: z.boolean() })) })
        .nullable(),
      summaryTicks: z.number().int(),
      beliefTicks: z.number().int(),
      proposerTicks: z.number().int(),
      latestProposals: liveProposalSetSchema.nullable(),
      nextSummaryAt: z.string().nullable(),
      nextBeliefAt: z.string().nullable(),
      nextProposalAt: z.string().nullable(),
      idleTrigger: liveIdleTriggerSchema.optional(),
    })
    .nullable(),
});
export type LiveSessionStatus = z.infer<typeof liveSessionStatusSchema>;

export const liveSummariesResponseSchema = z.array(liveSummarySchema);
export const liveProposalSetsResponseSchema = z.array(liveProposalSetSchema);
export const liveProposeNowResponseSchema = z.object({
  summary: liveSummarySchema.nullable().optional(),
  proposals: liveProposalSetSchema.nullable().optional(),
});
export type LiveProposeNowResponse = z.infer<typeof liveProposeNowResponseSchema>;

export const liveFeedbackResponseSchema = z.object({
  ts: z.string(),
  proposalsTs: z.string(),
  decision: z.enum(["accept", "reject"]),
  choice: z.number().int().nullable().optional(),
}).passthrough();
export type LiveFeedbackResponse = z.infer<typeof liveFeedbackResponseSchema>;

// POST /api/live/settings/check: one probe per model endpoint plus the observer's permissions, before a session starts.
export const liveCheckResultSchema = z.object({
  role: z.string(),
  label: z.string(),
  ok: z.boolean(),
  model: z.string().optional(),
  baseUrl: z.string().nullable().optional(),
  latencyMs: z.number().optional(),
  detail: z.string().optional(),
  error: z.string().optional(),
});
export const liveSettingsCheckSchema = z.object({ ok: z.boolean(), checks: z.array(liveCheckResultSchema) });
export type LiveSettingsCheck = z.infer<typeof liveSettingsCheckSchema>;

export const liveSummaryFeedbackResponseSchema = z.object({
  ts: z.string(),
  summaryTs: z.string(),
  verdict: z.enum(["wrong", "ok"]),
  comment: z.string(),
}).passthrough();

export const liveSessionListSchema = z.array(
  liveSessionRecordSchema.extend({ summaryCount: z.number().int(), proposalCount: z.number().int() }),
);
export const liveSessionDetailSchema = z.object({
  tasks: z.array(liveTaskSchema),
  session: liveSessionRecordSchema.extend({ snapshotCount: z.number().int(), screenshotCount: z.number().int(), active: z.boolean() }),
  summaries: liveSummariesResponseSchema,
  proposals: liveProposalSetsResponseSchema,
});
export type LiveSessionDetail = z.infer<typeof liveSessionDetailSchema>;
export const liveSessionDeleteSchema = z.object({ id: z.string(), deleted: z.literal(true) });

// GET /api/live/usage: token counts and estimated cost per model and module (summarizer, belief updater, proposer, agent).
export const liveUsageRowSchema = tokenCountsSchema.extend({ model: z.string().min(1), module: z.string().min(1) });
export const liveUsageSchema = z.object({
  all: z.array(liveUsageRowSchema), // every stored session
  recent: z.array(liveUsageRowSchema), // the last `days` days
  byDay: z.array(z.object({ day: z.string(), rows: z.array(liveUsageRowSchema) })), // the same span split by UTC day, oldest first
  days: z.number().positive(),
  session: z.array(liveUsageRowSchema).nullable(), // the running session, null when none
});
export type LiveUsageRow = z.infer<typeof liveUsageRowSchema>;
export type LiveUsage = z.infer<typeof liveUsageSchema>;

// GET /api/live/disk: the data folder's bytes, each session by what fills it and the rest by what it is, so the parts
// add up to the folder and the console can show what deleting would free. POST /api/live/disk/compress and
// POST /api/live/disk/restore ({ids}) start a job and answer with the same report: a compressed session keeps its screenshots and
// model-call log packed in place, still readable (python-app/app/live/storage/pack.py).
const diskBytes = z.number().int().nonnegative();
export const liveDiskSchema = z.object({
  root: z.string(), // the data folder
  free: diskBytes, // left on its volume
  sessions: z.array(z.object({ id: z.string(), name: z.string().nullable(), compressed: z.boolean(), screenshots: diskBytes, modelCalls: diskBytes, runs: diskBytes, screenReads: diskBytes, other: diskBytes })), // oldest first
  rest: z.object({ memory: diskBytes, agentBrowser: diskBytes, other: diskBytes }), // the folder beside its sessions
  packing: z.object({ // the sessions being compressed or restored, or the last ones; null before any
    total: diskBytes, // to read, of all of them
    done: diskBytes, // read so far
    sessions: z.number().int(),
    finished: z.number().int(), // sessions through, the failed ones included
    failed: z.record(z.string()), // session id → why it was left as it was
    running: z.boolean(),
  }).nullable(),
});
export type LiveDisk = z.infer<typeof liveDiskSchema>;

// The task-list changes and the stored screenshots of a session (GET /api/live/session/changes and /screenshots, and
// the same under /api/live/sessions/{id}/), read by the pilot console.
export const liveTaskOpSchema = z.object({
  op: z.enum(["add", "update", "status", "rename", "delete", "restore"]),
  taskId: z.string().nullable().optional(),
  title: z.string().nullable().optional(),
  steps: z.array(z.object({ text: z.string(), done: z.boolean().optional() })).optional(),
  remove: z.array(z.number().int()).optional(),
  reword: z.array(z.object({ step: z.number().int(), text: z.string() })).optional(), // update: open entries given new text
  step: z.number().int().nullable().optional(),
  done: z.boolean().nullable().optional(),
});
// One tool call of a belief tick: the round, the op it asked for (absent when not understood), whether it was applied, the result the model read.
export const liveBeliefCallSchema = z.object({ round: z.number().int(), tool: z.string(), op: liveTaskOpSchema.optional(), applied: z.boolean(), result: z.string() });
// One belief tick that did something: the ops it applied, and the results of the calls the lookalike check held (warned) or the
// check refused (rejected), as the model read them; rounds is how many replies the tick took.
export const liveTaskChangeSchema = z.object({
  ts: z.string(),
  windowStart: z.string(),
  windowEnd: z.string(),
  ops: z.array(liveTaskOpSchema),
  callId: z.string(),
  rounds: z.number().int().optional(),
  rejected: z.array(z.string()).optional(),
  warned: z.array(z.string()).optional(),
  calls: z.array(liveBeliefCallSchema).optional(), // every call in order; ticks from before the log have none
});
export const liveTaskChangesSchema = z.array(liveTaskChangeSchema);
export type LiveTaskOp = z.infer<typeof liveTaskOpSchema>;
export type LiveTaskChange = z.infer<typeof liveTaskChangeSchema>;
export const liveScreenshotSchema = z.object({ ts: z.string(), name: z.string(), app: z.string(), window: z.string() });
export const liveScreenshotsSchema = z.array(liveScreenshotSchema);
export type LiveScreenshot = z.infer<typeof liveScreenshotSchema>;
