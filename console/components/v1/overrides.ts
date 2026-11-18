import type { LiveSettings } from "@cue/contracts";

// The settings page's unsaved changes, kept in this browser until Save writes them to .env and settings.yaml
// (POST /api/live/settings).
export const STORAGE_KEY = "v1.settings";

export type Overrides = Partial<Record<keyof LiveSettings, unknown>>;

export function readOverrides(): Overrides {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== "object") return {};
    // Only settings that still exist: one saved by an earlier version and since removed would make the start fail.
    return Object.fromEntries(Object.entries(parsed).filter(([key]) => FIELDS.some((field) => field.key === key && field.kind !== "rules"))) as Overrides;
  } catch {
    return {}; // storage blocked or unreadable: the defaults
  }
}

export function writeOverrides(overrides: Overrides): void {
  try {
    if (Object.keys(overrides).length) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(overrides));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage blocked: the page keeps them for this visit only
  }
}

type Kind = "number" | "text" | "bool" | "select" | "set" | "list" | "rules" | "secret"; // set: any of the options, as checkboxes; rules: rows of pattern → level, each saved by its own ✓, never among the overrides; secret: a key, typed but never shown back
export type Field = { key: keyof LiveSettings; label: string; kind: Kind; group: string; options?: [string, string][]; unit?: string; hint?: string; short?: string; placeholder?: string }; // short: the word before the box in the simple view, where a row holds two

export const LEVELS: [string, string][] = [["block", "block"], ["look", "look"], ["use", "use"]]; // what a permission rule gives
const PROVIDERS: [string, string][] = [["vllm", "vLLM"], ["openai", "OpenAI"], ["anthropic", "Anthropic"], ["openrouter", "OpenRouter"], ["litellm", "LiteLLM"]];

// Every setting the page shows, grouped as it shows them (the deprecated cmux and desktop agents' settings stay out of it). ``list`` fields are comma-separated in the box.
export const FIELDS: Field[] = [
  { key: "maxSessionMinutes", label: "Session length", kind: "number", group: "Session", unit: "minutes · 0 for no limit" },
  { key: "actorAgents", label: "Agents for chat and temporary runs", kind: "set", group: "Session", options: [["claude", "Claude Code"], ["codex", "Codex"]], hint: "with both, each message picks" },
  { key: "notify", label: "macOS notification on a suggestion", kind: "bool", group: "Session" },

  { key: "permissionsDefault", label: "Apps not listed", kind: "select", group: "Permissions", options: [LEVELS[0]!, ["ask", "ask"], ...LEVELS.slice(1)], hint: "ask: each new app is asked about once, on the Zen page" },
  { key: "permissionsApps", label: "Apps", kind: "rules", group: "Permissions", hint: "app name or bundle id · * matches anything · the strictest match wins", placeholder: "Slack, com.apple.mail, 1Password*" },
  { key: "permissionsSites", label: "Sites", kind: "rules", group: "Permissions", hint: "in any browser · a domain covers its subdomains · * matches anything", placeholder: "chase.com, *bank*" },
  { key: "snapshotIntervalS", label: "A look every", kind: "number", group: "Looking", unit: "seconds" },
  { key: "summaryIntervalS", label: "A note every", kind: "number", group: "Looking", unit: "seconds" },
  { key: "beliefIntervalS", label: "The task list updated every", kind: "number", group: "Looking", unit: "seconds" },
  { key: "proposerIntervalS", label: "A suggestion every", kind: "number", group: "Looking", unit: "seconds", hint: "when there is something worth suggesting" },
  { key: "proposerIdleTrigger", label: "Suggest when I pause", kind: "bool", group: "Looking", hint: "one quick suggestion when you stop typing and moving the mouse" },
  { key: "proposerIdleS", label: "Pause before a suggestion", kind: "number", group: "Looking", unit: "seconds" },
  { key: "proposerIdleCooldownS", label: "At least between two pause suggestions", kind: "number", group: "Advanced", unit: "seconds" },
  { key: "proposerIdleAwayS", label: "Away after a pause suggestion", kind: "number", group: "Advanced", unit: "seconds" },
  { key: "proposerIdleContextS", label: "A pause suggestion reads the last", kind: "number", group: "Advanced", unit: "seconds" },
  { key: "proposerIdleProposals", label: "Suggestions per pause", kind: "number", group: "Advanced" },
  { key: "proposerIdleThinking", label: "Pause suggestions think first", kind: "bool", group: "Advanced", hint: "off is faster, but some models refuse it" },
  { key: "screenshotMode", label: "Screenshots", kind: "select", group: "Looking", options: [["switch", "on app switch"], ["always", "every distinct look"], ["off", "off"]] },

  { key: "summaryModel", label: "Notes model", kind: "text", group: "Models" },
  { key: "summaryProvider", label: "Notes endpoint type", kind: "select", group: "Models", options: PROVIDERS },
  { key: "summaryBaseUrl", label: "Notes base URL", kind: "text", group: "Models", hint: "blank: the task agent's endpoint" },
  { key: "beliefModel", label: "Tasks model", kind: "text", group: "Models" },
  { key: "beliefProvider", label: "Tasks endpoint type", kind: "select", group: "Models", options: PROVIDERS },
  { key: "beliefBaseUrl", label: "Tasks base URL", kind: "text", group: "Models" },
  { key: "preferenceModel", label: "Preferences model", kind: "text", group: "Models" },
  { key: "preferenceProvider", label: "Preferences endpoint type", kind: "select", group: "Models", options: PROVIDERS },
  { key: "preferenceBaseUrl", label: "Preferences base URL", kind: "text", group: "Models" },
  { key: "proposerModel", label: "Suggestions model", kind: "text", group: "Models" },
  { key: "proposerProvider", label: "Suggestions endpoint type", kind: "select", group: "Models", options: PROVIDERS },
  { key: "proposerBaseUrl", label: "Suggestions base URL", kind: "text", group: "Models" },
  { key: "proposerDedup", label: "Filter repeated suggestions", kind: "bool", short: "dedup", group: "Models", hint: "holds back a suggestion you already answered; needs the embedding model" },
  { key: "embedProvider", label: "Embeddings endpoint type", kind: "select", group: "Models", options: PROVIDERS.filter(([provider]) => provider !== "anthropic") },
  { key: "embedModel", label: "Embeddings model", kind: "text", group: "Models", hint: "shared by the repeat filter and the task list's lookalike check" },
  { key: "embedBaseUrl", label: "Embeddings base URL", kind: "text", group: "Models" },

  { key: "actorClaudeModel", label: "Claude Code model", kind: "text", group: "Agent", hint: "blank: Claude Code's default" },
  { key: "actorClaudePermissionMode", label: "Claude Code permission mode", kind: "select", group: "Agent", options: ["auto", "acceptEdits", "bypassPermissions", "dontAsk", "manual", "plan"].map((mode) => [mode, mode]), hint: "what it holds back is answered on the run card" },
  { key: "actorClaudeCwd", label: "Folder Claude Code starts in", kind: "text", group: "Agent", hint: "blank: the run's workspace" },
  { key: "actorCodexModel", label: "Codex model", kind: "text", group: "Agent", hint: "blank: Codex's default" },
  { key: "actorCodexSandbox", label: "Codex sandbox", kind: "select", group: "Agent", options: ["workspace-write", "read-only", "full-access"].map((mode) => [mode, mode]), hint: "a step beyond it is judged by Codex's own reviewer" },
  { key: "actorCodexCwd", label: "Folder Codex starts in", kind: "text", group: "Agent", hint: "blank: the run's workspace" },
  { key: "actorContextSummaries", label: "Notes a run is given as context", kind: "number", group: "Agent" },
  { key: "actorDesktopSource", label: "Desktops", kind: "set", group: "Agent", options: [["local", "This Mac"], ["remote", "Remote desktop"]] },
  { key: "desktopRemoteProvider", label: "Remote desktop provider", kind: "select", group: "Agent", options: [["", "none"], ["cua", "cua (Ubuntu container)"]] },
  { key: "desktopRemoteUrl", label: "Remote desktop URL", kind: "text", short: "URL", group: "Agent", hint: "ws:// or wss:// of the container's bridge" },
  { key: "desktopRemoteToken", label: "Remote desktop token", kind: "secret", short: "token", group: "Agent", hint: "CUA_DESKTOP_REMOTE_TOKEN in .env" },

  { key: "openaiApiKey", label: "OpenAI key", kind: "secret", short: "key", group: "Providers", hint: "for every model on OpenAI without a key of its own" },
  { key: "anthropicApiKey", label: "Anthropic key", kind: "secret", short: "key", group: "Providers", hint: "for every model on Anthropic; Claude Code never uses it" },
  { key: "openrouterApiKey", label: "OpenRouter key", kind: "secret", short: "key", group: "Providers", hint: "for every model on OpenRouter" },
  { key: "litellmApiKey", label: "LiteLLM key", kind: "secret", short: "key", group: "Providers", hint: "for every model on your LiteLLM proxy" },
  { key: "vllmApiKey", label: "vLLM key", kind: "secret", short: "key", group: "Providers", hint: "only if your vLLM server was started with --api-key" },
  { key: "openaiBaseUrl", label: "OpenAI base URL", kind: "text", short: "URL", group: "Providers" },
  { key: "anthropicBaseUrl", label: "Anthropic base URL", kind: "text", short: "URL", group: "Providers" },
  { key: "openrouterBaseUrl", label: "OpenRouter base URL", kind: "text", short: "URL", group: "Providers" },
  { key: "litellmBaseUrl", label: "LiteLLM base URL", kind: "text", short: "URL", group: "Providers", hint: "your LiteLLM proxy" },
  { key: "vllmBaseUrl", label: "vLLM base URL", kind: "text", short: "URL", group: "Providers", hint: "your vLLM server" },

  { key: "summaryThinking", label: "Notes: thinks before answering", kind: "bool", group: "Advanced" },
  { key: "summaryStream", label: "Notes: streams the reply", kind: "bool", group: "Advanced" },
  { key: "summaryScreenshots", label: "Notes: sees screenshots", kind: "bool", group: "Advanced" },
  { key: "summaryMaxTokens", label: "Notes: output tokens per call", kind: "number", group: "Advanced", unit: "0 = automatic" },
  { key: "proposerContextSummaries", label: "Suggestions: notes given as context", kind: "number", group: "Advanced" },
  { key: "beliefThinking", label: "Tasks: thinks before answering", kind: "bool", group: "Advanced" },
  { key: "beliefStream", label: "Tasks: streams the reply", kind: "bool", group: "Advanced" },
  { key: "beliefMaxTokens", label: "Tasks: output tokens per call", kind: "number", group: "Advanced", unit: "0 = automatic" },
  { key: "beliefStaleDays", label: "Tasks: pause a project untouched for", kind: "number", group: "Advanced", unit: "days" },
  { key: "beliefDoneStepDays", label: "Tasks: hide finished entries after", kind: "number", group: "Advanced", unit: "days" },
  { key: "beliefTrashDays", label: "Tasks: keep the trash for", kind: "number", group: "Advanced", unit: "days" },
  { key: "beliefContextSummaries", label: "Tasks: earlier worklog lines shown", kind: "number", group: "Advanced" },
  { key: "beliefSimilarThreshold", label: "Tasks: lookalike entry at cosine ≥", kind: "number", group: "Advanced", unit: "0 = off" },
  { key: "preferenceIntervalS", label: "Preferences: an update every", kind: "number", group: "Advanced", unit: "seconds" },
  { key: "preferenceMinFeedback", label: "Preferences: reactions an update needs", kind: "number", group: "Advanced" },
  { key: "preferenceThinking", label: "Preferences: thinks before answering", kind: "bool", group: "Advanced" },
  { key: "preferenceStream", label: "Preferences: streams the reply", kind: "bool", group: "Advanced" },
  { key: "preferenceMaxTokens", label: "Preferences: output tokens per call", kind: "number", group: "Advanced", unit: "0 = automatic" },
  { key: "preferenceCapTokens", label: "Preferences: file cap", kind: "number", group: "Advanced", unit: "tokens" },
  { key: "preferenceContextSummaries", label: "Preferences: worklog lines shown", kind: "number", group: "Advanced" },
  { key: "preferenceContextDecisions", label: "Preferences: earlier decisions shown", kind: "number", group: "Advanced" },
  { key: "preferenceSearchRounds", label: "Preferences: history searches per update", kind: "number", group: "Advanced", unit: "0 = off" },
  { key: "streamLookbackDays", label: "Stream lookback for a window", kind: "number", group: "Advanced", unit: "days" },
  { key: "proposerThinking", label: "Suggestions: thinks before answering", kind: "bool", group: "Advanced" },
  { key: "proposerStream", label: "Suggestions: streams the reply", kind: "bool", group: "Advanced" },
  { key: "proposerScreenshots", label: "Suggestions: sees screenshots", kind: "bool", group: "Advanced" },
  { key: "proposerMaxTokens", label: "Suggestions: output tokens per call", kind: "number", group: "Advanced", unit: "0 = automatic" },
  { key: "maxProposals", label: "Suggestions per round", kind: "number", group: "Advanced" },
  { key: "proposerInputs", label: "Suggestions: what it is shown", kind: "list", group: "Advanced", hint: "among tasks, worklog, snapshots, current_window, feedback, chat, preferences" },
  { key: "proposerFeedbackRejected", label: "Suggestions: rejected decisions shown", kind: "number", group: "Advanced" },
  { key: "proposerFeedbackAccepted", label: "Suggestions: accepted decisions shown", kind: "number", group: "Advanced" },
  { key: "proposerFeedbackRecent", label: "Suggestions: unanswered rounds shown", kind: "number", group: "Advanced", hint: "the top proposal of each" },
  { key: "proposerSearchRounds", label: "Suggestions: history searches per round", kind: "number", group: "Advanced", unit: "0 = off" },
  { key: "proposerDedupThreshold", label: "Filter: duplicate at cosine ≥", kind: "number", group: "Advanced" },
  { key: "proposerDedupNotNowHours", label: "Filter: a \"not now\" counts for", kind: "number", group: "Advanced", unit: "hours" },
  { key: "proposerDedupSessionHours", label: "Filter: this session's suggestions count for", kind: "number", group: "Advanced", unit: "hours" },
  { key: "maxElements", label: "Accessibility elements per look", kind: "number", group: "Advanced" },
  { key: "screenshotMaxSide", label: "Screenshots sent to models, long side", kind: "number", group: "Advanced", unit: "pixels" },
];

export const GROUPS = ["Session", "Looking", "Permissions", "Models", "Providers", "Agent", "Advanced"];

// The simple view: who does the work, the providers' keys (and a local one's address), each background model's provider and name,
// the session's timing and the remote desktop; the rest (permissions included) is in the advanced view. Each row is one setting, or a few side by side.
export const SIMPLE: { title: string; rows: { label: string; hint?: string; keys: (keyof LiveSettings)[] }[] }[] = [
  { title: "Agent", rows: [{ label: "Who does the work", hint: "on your own Claude Code or Codex login", keys: ["actorAgents"] }] },
  { title: "Providers", rows: [
    { label: "OpenAI", keys: ["openaiApiKey"] }, { label: "Anthropic", keys: ["anthropicApiKey"] }, { label: "OpenRouter", keys: ["openrouterApiKey"] },
    { label: "LiteLLM", keys: ["litellmBaseUrl", "litellmApiKey"] }, { label: "vLLM", keys: ["vllmBaseUrl", "vllmApiKey"] },
  ] },
  { title: "Models", rows: [
    { label: "Notes", hint: "a note about every minute of what you did", keys: ["summaryProvider", "summaryModel"] },
    { label: "Tasks", hint: "keeps your task list", keys: ["beliefProvider", "beliefModel"] },
    { label: "Suggestions", hint: "proposes what to do next", keys: ["proposerProvider", "proposerModel", "proposerDedup"] },
    { label: "Preferences", hint: "learns what help you want from your reactions", keys: ["preferenceProvider", "preferenceModel"] },
    { label: "Semantic dedup", hint: "embeddings that hold back repeats", keys: ["embedProvider", "embedModel"] },
  ] },
  { title: "Session", rows: [
    { label: "Session length", keys: ["maxSessionMinutes"] },
    { label: "A suggestion every", keys: ["proposerIntervalS"] },
    { label: "Suggest when I pause", hint: "one quick suggestion after this many seconds without typing or moving the mouse", keys: ["proposerIdleTrigger", "proposerIdleS"] },
    { label: "\"Not now\" lasts", hint: "a suggestion declined for now stays away this long", keys: ["proposerDedupNotNowHours"] },
  ] },
  { title: "Remote desktop", rows: [
    { label: "Desktops", hint: "where the agent may work", keys: ["actorDesktopSource"] },
    { label: "Bridge", hint: "the container's, see the remote desktop guide", keys: ["desktopRemoteProvider", "desktopRemoteUrl", "desktopRemoteToken"] },
  ] },
];

// What a field holds, as text in its box.
export function shown(field: Field, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (field.kind === "set" && Array.isArray(value)) return (field.options ?? []).map(([option]) => option).filter((option) => value.includes(option)).join(","); // in the options' order, whatever order the backend keeps
  return Array.isArray(value) ? value.join(",") : String(value);
}

// What a box's text becomes in the request: numbers as numbers, lists as arrays, blanks as null for the nullable URLs.
export function parsed(field: Field, text: string): unknown {
  if (field.kind === "number") return Number(text) || 0;
  if (field.kind === "list") return text.split(",").map((item) => item.trim()).filter(Boolean);
  if (field.kind === "set") return text.split(",").filter(Boolean);
  return text === "" ? null : text;
}
