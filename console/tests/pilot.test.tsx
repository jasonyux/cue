import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { LiveDisk, LiveSessionStatus, LiveSettings } from "@cue/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Pilot } from "../components/v1/Pilot";
import { Settings } from "../components/v1/Settings";
import { Usage } from "../components/v1/Usage";
import { Zen } from "../components/v1/Zen";

const nav = vi.hoisted(() => ({ router: { push: vi.fn(), replace: vi.fn() }, query: "" })); // the page's address, as a test sets it
vi.mock("next/navigation", () => ({ useRouter: () => nav.router, useSearchParams: () => new URLSearchParams(nav.query) }));

const settings: LiveSettings = {
  summaryModel: "qwen", summaryProvider: "vllm", summaryStream: true, proposerModel: "sonnet", proposerProvider: "anthropic", proposerStream: true,
  beliefModel: "qwen", beliefProvider: "vllm", beliefStream: true, embedModel: "", snapshotIntervalS: 10, summaryIntervalS: 60, proposerIntervalS: 180,
  summaryThinking: false, proposerThinking: true, summaryScreenshots: false, proposerScreenshots: false, proposerInputs: ["tasks", "worklog"],
  beliefIntervalS: 300, beliefThinking: true, beliefMaxTokens: 0, beliefStaleDays: 3, beliefDoneStepDays: 7, beliefTrashDays: 7, streamLookbackDays: 30,
  actorYieldS: 20, actorContextSummaries: 12, actorDesktopSource: ["local"], actorAgents: ["cmux"], actorCmuxPermissionMode: "auto", actorClaudePermissionMode: "auto", actorCodexSandbox: "workspace-write",
  maxProposals: 3, summaryMaxTokens: 0, proposerMaxTokens: 0, proposerFeedbackRejected: 6, proposerFeedbackAccepted: 3, proposerFeedbackRecent: 2, proposerSearchRounds: 5, proposerDedup: false,
  proposerDedupThreshold: 0.8, proposerDedupNotNowHours: 24, proposerDedupSessionHours: 24, screenshotMode: "switch", effectiveScreenshotMode: "switch",
  screenshotMaxSide: 1024, maxSessionMinutes: 120, maxElements: 400, notify: false, proposerContextSummaries: 20,
};
const tasks = [
  { id: "t1", title: "ICLR 2027 paper", status: "active" as const, steps: [{ text: "Revise the introduction" }, { text: "Send the draft", doneAt: new Date(Date.now() - 3_600_000).toISOString() }], lastSeen: "2026-10-07T10:00:00Z", sessionId: "s1", evidence: [] },
  { id: "t2", title: "Sidequests", status: "active" as const, steps: [{ text: "Attend the colloquium" }], lastSeen: "2026-10-07T09:00:00Z", sessionId: "s1", evidence: [] },
];
const base = { defaults: settings, rejectReasons: ["not now", "already done"], limits: { maxProposals: 3 }, tasks, trash: [], taskFlags: [] };
const idle: LiveSessionStatus = { ...base, active: false, session: null };
const live: LiveSessionStatus = { ...base, active: true, session: {
  id: "20261007-135300", folder: "/data/sessions/20261007-135300", startedAt: "2026-10-07T13:53:00Z", expiresAt: "2026-10-07T15:53:00Z", settings, runs: [], chatRuns: [],
  observerAlive: true, paused: false, snapshotCount: 4, tickCount: 8, screenshotCount: 1, summaryTicks: 1, beliefTicks: 0, proposerTicks: 1, latestProposals: null,
  latestSnapshot: { ts: "2026-10-07T14:13:40Z", app: "VS Code", window: "prompts.py", visible: ["Slack — general"] },
  latestScreenshot: { ts: "2026-10-07T14:13:40Z", name: "shot-1.png", app: "VS Code", window: "prompts.py" },
  agentBrowser: null,
  nextSummaryAt: "2026-10-07T14:14:40Z", nextBeliefAt: "2026-10-07T14:18:00Z", nextProposalAt: "2026-10-07T14:16:00Z",
} };
const proposals = [{ ts: "2026-10-07T14:12:00Z", proposals: [{ kind: "act" as const, task: "Register for NeurIPS 2026 before early-bird ends", reason: "Registration opened today." }, { kind: "lookup" as const, task: "Reply to Wei" }] }];
const chat = { id: "c1", current: "c1", agent: null, turn: 0, runningRunId: null, messages: [], pending: [], conversations: [] };
const spent = { model: "sonnet", module: "proposer", calls: 2, inputTokens: 10, cachedInputTokens: 0, cacheWriteTokens: 0, outputTokens: 5, reasoningTokens: 0, totalTokens: 15, costUsd: 0.5 };
const usage = { all: [], recent: [spent], byDay: [{ day: new Date().toISOString().slice(0, 10), rows: [spent] }], days: 7, session: [spent] }; // one call, today
const disk = { // ten sessions, each larger than the one before: 27.5 GB of screenshots in all
  root: "/data",
  free: 468e9,
  sessions: Array.from({ length: 10 }, (_, index) => ({ id: `202610${String(index + 1).padStart(2, "0")}-100000`, name: index === 9 ? "Deck week" : null, compressed: false, screenshots: (index + 1) * 500e6, modelCalls: 100e6, runs: 0, screenReads: 20e6, other: 1e6 })),
  rest: { memory: 600e6, agentBrowser: 400e6, other: 90e6 },
  packing: null,
};

function backend(status: LiveSessionStatus, sets: unknown = proposals) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.includes("/api/live/session/proposals") ? sets : url.includes("/api/live/session/chat") ? chat : url.includes("/api/live/session/changes") ? []
      : url.includes("/api/live/sessions") ? [] : url.includes("/api/live/usage") ? usage : url.includes("/api/live/disk") ? disk : url.includes("/api/live/session") || url.endsWith("/api/live/settings") ? status : null;
    return { ok: body !== null, status: body === null ? 404 : 200, json: async () => body ?? { error: `no stub for ${url}` } } as Response;
  });
}

describe("the pilot console", () => {
  beforeEach(() => { vi.stubGlobal("EventSource", vi.fn(() => ({ close: vi.fn() }))); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.localStorage.clear(); nav.query = ""; nav.router.push.mockClear(); });

  it("offers to start a session and shows the tasks when none is running", async () => {
    vi.stubGlobal("fetch", backend(idle));
    render(<Pilot expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Start a session to get suggestions and talk to the agent");
    expect(screen.getByRole("button", { name: "▶ Start session" }).hasAttribute("disabled")).toBe(false);
    expect(screen.getByText(/Tasks · 1 open/)).toBeTruthy(); // the project's open entry; the sidequest is counted apart
    expect(screen.getByText(/Sidequests · 1 open/)).toBeTruthy();
    expect(screen.getByText("ICLR 2027 paper")).toBeTruthy();
    expect(screen.getByText("Nothing said yet. Tell the agent what to do below, or take a suggestion.")).toBeTruthy();
    expect(screen.getByText("cmux + Claude Code")).toBeTruthy(); // the chat box already names the Settings page's agent
  });

  it("shows the screen, the suggestion and the composer during a session", async () => {
    vi.stubGlobal("fetch", backend(live));
    render(<Pilot expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("VS Code — prompts.py");
    expect(screen.getByText("Also open: Slack — general")).toBeTruthy();
    expect(screen.getByRole("img", { name: "VS Code — prompts.py" }).getAttribute("src")).toBe("http://runner/api/live/session/screenshots/shot-1.png");
    expect(screen.getByText("Register for NeurIPS 2026 before early-bird ends")).toBeTruthy();
    expect(screen.getByRole("button", { name: "→ Do it" }).hasAttribute("disabled")).toBe(false);
    expect(screen.getByRole("button", { name: "Edit first" }).hasAttribute("disabled")).toBe(false);
    expect(screen.queryByRole("button", { name: "Temporary run" })).toBeNull();
    expect(screen.getByRole("button", { name: "1 alternative" })).toBeTruthy();
    expect(screen.getByText(/Session running/)).toBeTruthy();
    expect(screen.getByText(/Session:/).textContent).toContain("$0.50");
    expect(screen.getByRole("button", { name: "⏸ Pause" }).hasAttribute("disabled")).toBe(false);
    expect(screen.getByPlaceholderText("Tell the agent what to do… (⌘↵ to send)")).toBeTruthy();
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" }).hasAttribute("disabled")).toBe(true)); // nothing typed yet
  });

  it("Do it accepts the suggestion and sends it as written; the pen puts it in the chat box instead", async () => {
    const calls: string[] = [];
    const stub = backend(live);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/proposals/feedback")) { calls.push(`feedback ${init?.body}`); return { ok: true, status: 200, json: async () => ({}) } as Response; }
      if (url.endsWith("/chat/messages")) { calls.push(`message ${init?.body}`); return { ok: false, status: 500, json: async () => ({ error: "not in this test" }) } as Response; }
      return stub(input);
    }));
    render(<Pilot expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "→ Do it" }));
    await waitFor(() => expect(calls).toEqual([
      'feedback {"proposalsTs":"2026-10-07T14:12:00Z","decision":"accept","choice":0}',
      'message {"text":"Register for NeurIPS 2026 before early-bird ends","agent":"cmux","proposal":{"proposalsTs":"2026-10-07T14:12:00Z","choice":0}}',
    ]));
    fireEvent.click(screen.getByRole("button", { name: "Edit first" }));
    await waitFor(() => expect((screen.getByPlaceholderText("Tell the agent what to do… (⌘↵ to send)") as HTMLTextAreaElement).value).toBe("Register for NeurIPS 2026 before early-bird ends"));
    expect(calls.length).toBe(3); // the decision again; nothing sent
  });

  it("the composer offers the agent's models, as the agent lists them, and the next message carries the pick", async () => {
    const codex = { ...live, session: { ...live.session!, settings: { ...settings, actorAgents: ["claude", "codex"] as ("claude" | "codex")[] } } };
    const models = { claude: [{ id: "default", label: "Default (recommended)", detail: "Opus 5.5", default: true }, { id: "opus", label: "Opus 5.5", detail: "", default: false }],
                     codex: [{ id: "gpt-6-luna", label: "GPT-6-Luna", detail: "Fast", default: true }, { id: "gpt-5.6-terra", label: "GPT-5.6-Terra", detail: "Balanced", default: false }] };
    const stub = backend(codex);
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const listed = /\/api\/live\/agents\/(\w+)\/models/.exec(String(input));
      if (listed) return { ok: true, status: 200, json: async () => ({ models: models[listed[1] as "claude" | "codex"] }) } as Response;
      if (String(input).endsWith("/api/live/session/chat/messages")) return { ok: false, status: 500, json: async () => ({ error: "not in this test" }) } as Response;
      return stub(input);
    });
    vi.stubGlobal("fetch", fetch);
    render(<Pilot expectedBackend="python" runnerBaseUrl="http://runner" />);
    const line = await screen.findByPlaceholderText("Tell the agent what to do… (⌘↵ to send)");
    const model = await screen.findByRole("combobox", { name: "Model" }) as HTMLSelectElement;
    expect([...model.options].map((option) => option.text)).toEqual(["Default (recommended)", "Opus 5.5"]);
    expect(model.value).toBe("default"); // the agent's own default
    fireEvent.change(screen.getByRole("combobox", { name: "Agent" }), { target: { value: "codex" } });
    await waitFor(() => expect([...(screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement).options].map((option) => option.text)).toEqual(["GPT-6-Luna", "GPT-5.6-Terra"]));
    fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "gpt-5.6-terra" } });
    fireEvent.change(line, { target: { value: "Tidy the notes" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(fetch.mock.calls.some(([url]) => String(url).endsWith("/api/live/session/chat/messages"))).toBe(true));
    const sent = (fetch.mock.calls as unknown as [RequestInfo | URL, RequestInit][]).find(([url]) => String(url).endsWith("/api/live/session/chat/messages"))!;
    expect(JSON.parse(String(sent[1].body))).toEqual({ text: "Tidy the notes", agent: "codex", model: "gpt-5.6-terra" });
  });

  it("a permission question sits in the band above the suggestion, and its answer is posted", async () => {
    const asked = { ...live, session: { ...live.session!, permissionAsks: [{ app: "Slack", bundle: "com.tinyspeck.slackmacgap", ts: "2026-10-07T14:14:00Z" },
      { app: "Mail", bundle: "com.apple.mail", ts: "2026-10-07T14:14:30Z", source: "agent" as const, level: "use" as const, why: "send the reply you drafted" }] } };
    const posted: unknown[] = [];
    const base = backend(asked);
    const answered = backend(live); // the asks left the list
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/api/live/session/permissions")) { posted.push(JSON.parse(String(init?.body))); return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response; }
      return posted.length === 2 ? answered(input) : base(input);
    }));
    render(<Pilot expectedBackend="python" runnerBaseUrl="http://runner" />);
    expect(await screen.findByText("Cue wants to look at Slack")).toBeTruthy();
    expect(screen.getByText("Cue wants to use Mail").closest(".ask")!.getAttribute("title")).toBe("send the reply you drafted"); // next to a suggestion the cards are compact: what for on hover
    expect(screen.getByText("Register for NeurIPS 2026 before early-bird ends").closest("p")!.getAttribute("title")).toBe("Registration opened today."); // the suggestion stays under them, its reason on hover
    expect(screen.queryByText("Registration opened today.")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Look" }));
    fireEvent.click(screen.getByRole("button", { name: "For this task" }));
    await waitFor(() => expect(posted).toEqual([{ app: "Slack", level: "look" }, { app: "Mail", level: "use", scope: "task" }]));
    await waitFor(() => expect(screen.queryByText("Cue wants to look at Slack")).toBeNull());
  });

  it("the ⤢ on the band's screen enlarges the monitor in its place, with the views and Take control; it drags taller, and Minimize brings the band back", async () => {
    const browsing = { ...live, session: { ...live.session!, agentBrowser: { endpoint: "http://127.0.0.1:9222", viewport: { width: 1280, height: 800 }, tabs: [{ id: "t1", title: "Maps", url: "https://maps.example", agent: true }] } } };
    vi.stubGlobal("fetch", backend(browsing));
    const { container } = render(<Pilot expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Enlarge the monitor" }));
    expect(screen.queryByText("On your screen")).toBeNull(); // the band is gone, suggestion included
    expect(screen.queryByText("Register for NeurIPS 2026 before early-bird ends")).toBeNull();
    expect(screen.getByRole("group", { name: "Screen view" }).textContent).toBe("Your screenCue's browser"); // nothing runs: your screen in place of Follow agent; no remote desktop in the settings
    expect(screen.getByText("● Watching · Your screen · VS Code — prompts.py")).toBeTruthy(); // as the band showed it
    expect(screen.getByRole("img", { name: "VS Code — prompts.py" }).getAttribute("src")).toBe("http://runner/api/live/session/screenshots/shot-1.png");
    fireEvent.click(screen.getByRole("button", { name: "Cue's browser" }));
    expect(screen.getByText("● Live · Cue's browser · Maps")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Take control" })).toBeTruthy();
    const monitor = container.querySelector<HTMLElement>(".monitor")!;
    expect(monitor.style.height).toBe(""); // the stylesheet's default, its least
    fireEvent(monitor.querySelector(".grab")!, new MouseEvent("pointerdown", { bubbles: true, clientY: 400 })); // jsdom has no PointerEvent
    fireEvent(window, new MouseEvent("pointermove", { clientY: 720 }));
    expect(monitor.style.height).toBe("720px");
    fireEvent(window, new MouseEvent("pointerup", { clientY: 720 }));
    fireEvent.click(screen.getByRole("button", { name: "Minimize" }));
    expect(await screen.findByText("On your screen")).toBeTruthy();
    expect(screen.getByText("Register for NeurIPS 2026 before early-bird ends")).toBeTruthy();
  });

  it("in the Zen theme, shows what it noticed from the pet and keeps the rest tucked away", async () => {
    vi.stubGlobal("fetch", backend(live));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    expect(document.querySelector(".v1.zen")?.getAttribute("data-state")).toBe("noticed");
    expect(screen.getByRole("button", { name: "Do it" }).hasAttribute("disabled")).toBe(false);
    expect(screen.getByRole("button", { name: "1 more idea" })).toBeTruthy();
    expect(screen.getByRole("img", { name: "VS Code — prompts.py" }).getAttribute("src")).toBe("http://runner/api/live/session/screenshots/shot-1.png");
    expect(document.querySelector(".v1.zen .clip .badge")?.textContent).toBe("1"); // the project's open entry; sidequests apart, as in the task list
    expect(screen.queryByText("Conversation")).toBeNull(); // the conversation and the tasks open only when asked for
    expect(document.querySelector(".v1.zen .tasks")).toBeNull();
    expect(screen.getByRole("button", { name: "Pause" }).hasAttribute("disabled")).toBe(false); // the monitor's pause button
    expect(screen.getByRole("button", { name: "Stop session" }).hasAttribute("disabled")).toBe(false);
  });

  it("in the Zen theme, sleeps and offers to start when there is no session", async () => {
    vi.stubGlobal("fetch", backend(idle));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Pilot is asleep");
    expect(document.querySelector(".v1.zen")?.getAttribute("data-state")).toBe("off");
    expect(screen.getByRole("button", { name: "▶ Start session" }).hasAttribute("disabled")).toBe(false);
    expect(screen.getByRole("button", { name: "Start session" }).hasAttribute("disabled")).toBe(false); // the power button
    expect(screen.getByPlaceholderText("Start a session to talk to Pilot").hasAttribute("disabled")).toBe(true);
  });

  it("in the Zen theme, the conversation offers the session's agents", async () => {
    const both = { ...live, session: { ...live.session!, settings: { ...settings, actorAgents: ["desktop", "cmux"] as ("desktop" | "cmux")[] } } };
    vi.stubGlobal("fetch", backend(both));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    const line = await screen.findByPlaceholderText("Tell Pilot what to do…");
    line.focus();
    const agent = await screen.findByRole("combobox", { name: "Agent" });
    expect([...(agent as HTMLSelectElement).options].map((option) => option.text)).toEqual(["Desktop (GUI)", "cmux + Claude Code"]);
    expect((agent as HTMLSelectElement).value).toBe("cmux");
    expect(screen.queryByRole("spinbutton")).toBeNull(); // no turn cap: the agent stops when it is done, or when the person stops it
  });

  it("in the Zen theme, the conversation offers the agent's models, as the agent lists them, and the next message carries the pick", async () => {
    const codex = { ...live, session: { ...live.session!, settings: { ...settings, actorAgents: ["claude", "codex"] as ("claude" | "codex")[] } } };
    const models = { claude: [{ id: "default", label: "Default (recommended)", detail: "Opus 5.5", default: true }, { id: "opus", label: "Opus 5.5", detail: "", default: false }],
                     codex: [{ id: "gpt-6-luna", label: "GPT-6-Luna", detail: "Fast", default: true }, { id: "gpt-5.6-terra", label: "GPT-5.6-Terra", detail: "Balanced", default: false }] };
    const stub = backend(codex);
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const listed = /\/api\/live\/agents\/(\w+)\/models/.exec(String(input));
      if (listed) return { ok: true, status: 200, json: async () => ({ models: models[listed[1] as "claude" | "codex"] }) } as Response;
      if (String(input).endsWith("/api/live/session/chat/messages")) return { ok: false, status: 500, json: async () => ({ error: "not in this test" }) } as Response;
      return stub(input);
    });
    vi.stubGlobal("fetch", fetch);
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    const line = await screen.findByPlaceholderText("Tell Pilot what to do…");
    line.focus();
    const model = await screen.findByRole("combobox", { name: "Model" }) as HTMLSelectElement;
    expect([...model.options].map((option) => option.text)).toEqual(["Default (recommended)", "Opus 5.5"]);
    expect(model.value).toBe("default"); // the agent's own default
    fireEvent.change(screen.getByRole("combobox", { name: "Agent" }), { target: { value: "codex" } });
    await waitFor(() => expect([...(screen.getByRole("combobox", { name: "Model" }) as HTMLSelectElement).options].map((option) => option.text)).toEqual(["GPT-6-Luna", "GPT-5.6-Terra"]));
    fireEvent.change(screen.getByRole("combobox", { name: "Model" }), { target: { value: "gpt-5.6-terra" } });
    fireEvent.change(line, { target: { value: "Tidy the notes" } });
    fireEvent.keyDown(line, { key: "Enter" });
    await waitFor(() => expect(fetch.mock.calls.some(([url]) => String(url).endsWith("/api/live/session/chat/messages"))).toBe(true));
    const sent = (fetch.mock.calls as unknown as [RequestInfo | URL, RequestInit][]).find(([url]) => String(url).endsWith("/api/live/session/chat/messages"))!;
    expect(JSON.parse(String(sent[1].body))).toEqual({ text: "Tidy the notes", agent: "codex", model: "gpt-5.6-terra" });
  });

  it("in the Zen theme, clicking Pilot hides and shows what it says, and lists the session's suggestions", async () => {
    vi.stubGlobal("fetch", backend(live));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    const pilot = screen.getByRole("button", { name: "Pilot" });
    fireEvent.click(pilot);
    expect(screen.queryByText("Register for NeurIPS 2026 before early-bird ends")).toBeNull(); // hushed
    fireEvent.click(pilot);
    expect(screen.getByText("Register for NeurIPS 2026 before early-bird ends")).toBeTruthy(); // said again
  });

  it("in the Zen theme, the session's suggestions can be answered, told why not, and answered again", async () => {
    const answered = [{ ...proposals[0], ts: "2026-10-07T14:00:00Z", feedback: { decision: "reject" as const, ts: "2026-10-07T14:01:00Z" } }, ...proposals];
    const calls: string[] = [];
    const base = backend(live);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/proposals/feedback")) { calls.push(`${url.split("/api/live/session/")[1]} ${init?.body}`); return { ok: true, status: 200, json: async () => ({}) } as Response; }
      if (url.includes("/api/live/session/proposals")) return { ok: true, status: 200, json: async () => answered } as Response;
      return base(input);
    }));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: /all suggestions this session/ }));
    expect(screen.getByText("Suggestions this session · 2")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /add a reason/ })); // the earlier one, rejected without a reason
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.click(screen.getByRole("menuitem", { name: "already done" }));
    await waitFor(() => expect(calls).toContain('proposals/feedback/reason {"proposalsTs":"2026-10-07T14:00:00Z","reason":"already done","typed":false}'));
    fireEvent.click(screen.getByRole("button", { name: "change answer" }));
    expect(screen.getAllByRole("button", { name: /Not now/ }).length).toBeGreaterThan(0);
  });

  it("in the Zen theme, the clipboard shows the task model writing, and blue dots mark what changed until the project is opened", async () => {
    const writing = { ...live, session: { ...live.session!, nextBeliefAt: null } };
    vi.stubGlobal("fetch", backend(writing));
    window.localStorage.setItem("v1.tasksSeen", JSON.stringify({ t1: { title: "ICLR 2027 paper", status: "active", entries: { "Send the draft": true } }, t2: { title: "Sidequests", status: "active", entries: {} } }));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("updated");
    const clip = screen.getByRole("button", { name: "Your tasks" });
    expect(clip.className).toContain("writing");
    fireEvent.click(clip);
    expect(screen.getByText("ICLR 2027 paper").closest("h4")?.className).toContain("fresh"); // a dot on the closed project
    fireEvent.click(screen.getByText("ICLR 2027 paper")); // opened: read
    expect(screen.getByText("Revise the introduction").closest("li")?.className).toContain("fresh"); // its dots stay while the list is open
    expect(screen.getByText("Send the draft").closest("li")?.className).not.toContain("fresh");
    expect(JSON.parse(window.localStorage.getItem("v1.tasksSeen") ?? "{}").t1.entries).toEqual({ "Revise the introduction": false, "Send the draft": true });
    fireEvent.click(clip);
    expect(screen.getByText("updated")).toBeTruthy(); // the sidequest added since was not opened
    fireEvent.click(clip);
    expect(screen.getByText("ICLR 2027 paper").closest("h4")?.className ?? "").not.toContain("fresh"); // read last time: no dot now
  });

  it("in the Zen theme, the monitor zooms into the agent browser, where clicks and keys go to the page", async () => {
    const browsing: LiveSessionStatus = { ...live, session: { ...live.session!, agentBrowser: { endpoint: "http://127.0.0.1:1", viewport: { width: 1280, height: 800 }, tabs: [
      { id: "t1", title: "Example Domain", url: "https://example.com/", agent: true }, { id: "t2", title: "", url: "https://login.example.org/account", agent: false }] } } };
    const fetch = backend(browsing);
    vi.stubGlobal("fetch", fetch);
    const { container } = render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Pilot's browser" }));
    expect((container.querySelector(".screen img") as HTMLImageElement).src).toContain("/api/live/session/browser/stream"); // the monitor shows it, view only
    fireEvent.click(screen.getByText("Take control ⤢"));
    expect(screen.getByText("Example Domain")).toBeTruthy();
    expect(screen.getByText("login.example.org/account")).toBeTruthy();
    expect(screen.getByText("following the agent")).toBeTruthy(); // the stream follows the agent's tab until one is picked
    const pad = container.querySelector(".browserSheet .pad") as HTMLDivElement;
    pad.getBoundingClientRect = () => ({ left: 0, top: 0, width: 1280, height: 800, right: 1280, bottom: 800, x: 0, y: 0, toJSON: () => ({}) });
    vi.stubGlobal("PointerEvent", class extends MouseEvent {}); // jsdom has none; the sheet reads the pointer's clientX and clientY
    fireEvent.pointerDown(pad, { clientX: 320, clientY: 400, button: 0, detail: 1 });
    fireEvent.keyDown(pad, { key: "a" });
    fireEvent.keyDown(pad, { key: "Enter" });
    fireEvent.keyDown(pad, { key: "a", metaKey: true });
    fireEvent.keyDown(pad, { key: "Shift" }); // a bare modifier: nothing to send
    const calls = fetch.mock.calls as unknown as [RequestInfo | URL, RequestInit][];
    const sent = calls.filter(([url]) => String(url).endsWith("/api/live/session/browser/input")).map(([, init]) => JSON.parse(String(init.body)));
    expect(sent).toEqual([
      { tab: null, kind: "down", x: 0.25, y: 0.5, button: "left", clicks: 1 },
      { tab: null, kind: "text", text: "a" }, { tab: null, kind: "key", key: "Enter" }, { tab: null, kind: "key", key: "Meta+a" },
    ]);
    fireEvent.click(screen.getByText("login.example.org/account")); // picking a tab pins the view to it
    expect(screen.getByRole("button", { name: "Follow agent" })).toBeTruthy();
    expect((container.querySelector(".browserSheet .pad img") as HTMLImageElement).src).toContain("tab=t2");
    fireEvent.click(screen.getByRole("button", { name: "Back" })); // the pinned tab's history
    await waitFor(() => expect(calls.filter(([url]) => String(url).endsWith("/api/live/session/browser/tabs")).map(([, init]) => JSON.parse(String(init.body)))).toEqual([{ action: "back", tab: "t2" }]));
    fireEvent.keyDown(container.querySelector(".browserSheet .pad") as HTMLDivElement, { key: "Escape" });
    expect(container.querySelector(".browserSheet")).toBeNull();
  });

  it("in the Zen theme, a remote desktop can be shown on the monitor and taken control of", async () => {
    const withRemote: LiveSessionStatus = { ...live, session: { ...live.session!, settings: { ...settings, actorDesktopSource: ["local", "remote"] } } };
    const fetch = backend(withRemote);
    vi.stubGlobal("fetch", fetch);
    const { container } = render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Remote desktop" }));
    expect((container.querySelector(".screen img") as HTMLImageElement).src).toContain("/api/live/session/remote/stream");
    fireEvent.click(screen.getByText("Take control ⤢"));
    expect(screen.getByText("Remote desktop", { selector: ".who" })).toBeTruthy();
    const pad = container.querySelector(".browserSheet.remote .pad") as HTMLDivElement;
    fireEvent.keyDown(pad, { key: "Enter" });
    const calls = fetch.mock.calls as unknown as [RequestInfo | URL, RequestInit][];
    expect(calls.filter(([url]) => String(url).endsWith("/api/live/session/remote/input")).map(([, init]) => JSON.parse(String(init.body)))).toEqual([{ kind: "key", key: "Enter" }]);
    fireEvent.keyDown(pad, { key: "Escape" });
    expect(container.querySelector(".browserSheet")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remote desktop" })); // back to the person's screen
    expect((container.querySelector(".screen img") as HTMLImageElement).src).toContain("/api/live/session/screenshots/");
  });

  it("in the Zen theme, a press on the page outside the tasks or Pilot's bubble closes them, a press on a control does not", async () => {
    const estimated = [{ ...proposals[0]!, proposals: [{ ...proposals[0]!.proposals[0]!, humanSeconds: 600 }, proposals[0]!.proposals[1]!] }];
    vi.stubGlobal("fetch", backend(live, estimated));
    const { container } = render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    expect(await screen.findByText("~10 min by hand")).toBeTruthy(); // the proposer's estimate, beside the task
    fireEvent.pointerDown(screen.getByRole("button", { name: "Pause" })); // a control elsewhere does its own thing
    expect(container.querySelector(".bubble")).toBeTruthy();
    fireEvent.pointerDown(container.querySelector(".surface")!); // the desk hushes Pilot, as a click on it would
    expect(container.querySelector(".bubble")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Your tasks" }));
    fireEvent.pointerDown(container.querySelector(".tasks")!); // inside the sheet: it stays
    expect(container.querySelector(".tasks")).toBeTruthy();
    fireEvent.pointerDown(container.querySelector(".screen img")!); // the monitor closes it
    expect(container.querySelector(".tasks")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    expect(container.querySelector(".top .menu")).toBeTruthy();
    fireEvent.pointerDown(container.querySelector(".surface")!);
    expect(container.querySelector(".top .menu")).toBeNull();
  });

  it("in the Zen theme, the menu switches suggestions on a pause and sets the pause, and a pause's suggestion says so", async () => {
    const away = { ...live, session: { ...live.session!, idleTrigger: { enabled: true, state: "away" as const, idleS: 10, thresholdS: 15, inputIdleS: 130, firedAt: "2026-10-07T14:12:00Z" } } };
    const fetch = backend(away, [{ ...proposals[0]!, trigger: "idle" }]);
    vi.stubGlobal("fetch", fetch);
    const { container } = render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    expect(await screen.findByText("· on a pause")).toBeTruthy(); // in Pilot's bubble
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    expect(screen.getByText(/you seem away/)).toBeTruthy();
    const posted = () => (fetch.mock.calls as unknown as [RequestInfo | URL, RequestInit][])
      .filter(([url]) => String(url).endsWith("/api/live/session/idle-trigger")).map(([, init]) => JSON.parse(String(init.body)));
    const box = screen.getByRole("checkbox", { name: /Suggest when I pause/ }) as HTMLInputElement;
    expect(box.checked).toBe(true);
    fireEvent.click(box);
    await waitFor(() => expect(posted()).toEqual([{ enabled: false, idleS: 10 }]));
    const seconds = screen.getByRole("spinbutton", { name: "Seconds of pause" }) as HTMLInputElement;
    fireEvent.change(seconds, { target: { value: "20" } });
    fireEvent.blur(seconds);
    await waitFor(() => expect(posted()).toEqual([{ enabled: false, idleS: 10 }, { enabled: true, idleS: 20 }]));
    fireEvent.change(seconds, { target: { value: "1" } }); // below the backend's bound: back to the saved value, nothing sent
    fireEvent.blur(seconds);
    expect(seconds.value).toBe("10");
    expect(posted()).toHaveLength(2);
    expect(container.querySelector(".top .menu")).toBeTruthy(); // its controls keep the menu open
  });

  it("in Settings, Quickstart is three cards of the simple view's rows: who does the work, each model and its provider, then a key for every provider those use; Finish saves", async () => {
    const saved: Record<string, unknown>[] = [];
    const checked = [{ label: "Summarizer", role: "summary", ok: true, latencyMs: 12 }, { label: "Proposer", role: "proposer", ok: false, error: "no key" }];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/api/live/settings") && init?.method === "POST") saved.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      if (String(input).endsWith("/api/live/settings/check")) return { ok: true, status: 200, json: async () => ({ ok: checked.every((check) => check.ok), checks: checked }) } as Response;
      return backend(idle)(input);
    }));
    const { container } = render(<Settings expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Quickstart" }));
    const card = within(screen.getByRole("dialog", { name: "Quickstart" }));
    const rows = () => [...container.querySelectorAll(".quick .row > label")].map((label) => label.firstChild?.textContent);
    expect(card.getByRole("heading").textContent).toBe("Welcome to Cue");
    expect(container.querySelector(".form")).toBeNull(); // the page's own form steps aside: one of each setting on the page
    expect(rows()).toEqual(["Main agent", "Session length"]); // in the quickstart's own words
    expect(container.querySelector<HTMLInputElement>("#sessionHours")!.value).toBe("2"); // in hours: 120 minutes
    fireEvent.change(container.querySelector("#sessionHours")!, { target: { value: "1.5" } });
    fireEvent.click(card.getByRole("button", { name: "Continue" }));
    expect(card.getByRole("heading").textContent).toBe("Model configuration");
    expect(rows()).toEqual(["Notes", "Tasks", "Suggestions", "Preferences", "Semantic dedup"]); // as the simple view has them
    expect(container.querySelector("#proposerDedup")).toBeNull(); // but for the repeat filter's switch
    fireEvent.change(container.querySelector("#summaryProvider")!, { target: { value: "openrouter" } }); // notes from one provider, the rest where they were
    fireEvent.change(container.querySelector("#summaryModel")!, { target: { value: "anthropic/claude-haiku-5.5" } });
    fireEvent.click(card.getByRole("button", { name: "Continue" }));
    expect(card.getByRole("heading").textContent).toBe("Add your keys");
    expect(rows()).toEqual(["Anthropic", "OpenRouter", "vLLM"]); // the providers the models use, and no other
    expect([...container.querySelectorAll(".quick-help")].map((link) => link.getAttribute("href"))).toEqual(["https://console.anthropic.com/", "https://openrouter.ai/keys", "https://docs.vllm.ai/"]); // a "?" beside each name
    expect([container.querySelector(".quick-via .chip.on")?.textContent, container.querySelector(".quick-note")?.textContent]).toEqual(["Enter here", "saved to .env in Cue's folder"]);
    fireEvent.click(card.getByRole("button", { name: ".env file" })); // keys kept off the page: the names for a .env filled in by hand
    expect([container.querySelector(".quick-via .chip.on")?.textContent, container.querySelector(".quick-note")]).toEqual([".env file", null]);
    expect([container.querySelector(".quick-code")?.textContent, rows()]).toEqual(["ANTHROPIC_API_KEY=\nOPENROUTER_API_KEY=\nVLLM_BASE_URL=\nVLLM_API_KEY=", []]);
    fireEvent.click(card.getByRole("button", { name: "Add to .env" }));
    await waitFor(() => expect(saved.at(-1)).toEqual({ anthropicApiKey: "", openrouterApiKey: "", vllmBaseUrl: "", vllmApiKey: "" })); // written blank
    await card.findByRole("button", { name: "Added ✓" });
    fireEvent.click(card.getByRole("button", { name: "Shell variables" })); // or the shell's variables, before launching
    expect(container.querySelector(".quick-code")?.textContent).toBe("export ANTHROPIC_API_KEY=\nexport OPENROUTER_API_KEY=\nexport VLLM_BASE_URL=\nexport VLLM_API_KEY=\npnpm cue web");
    fireEvent.click(card.getByRole("button", { name: "Enter here" }));
    fireEvent.change(container.querySelector("#openrouterApiKey")!, { target: { value: "sk-or-1" } });
    fireEvent.click(card.getByRole("button", { name: "Back" }));
    fireEvent.change(container.querySelector("#beliefProvider")!, { target: { value: "openrouter" } }); // the tasks model moves off vLLM: its key is no longer asked for
    fireEvent.click(card.getByRole("button", { name: "Continue" }));
    expect(rows()).toEqual(["Anthropic", "OpenRouter"]);
    const results = () => [...container.querySelectorAll(".quick .checks li")].map((item) => item.textContent);
    fireEvent.click(card.getByRole("button", { name: "Test configuration" }));
    await waitFor(() => expect(results()).toEqual(["✗ Proposer · no key"])); // a failed test shows the failed checks alone
    checked[1] = { label: "Proposer", role: "proposer", ok: true, latencyMs: 30 };
    fireEvent.click(card.getByRole("button", { name: "Test configuration" }));
    await waitFor(() => expect(results()).toEqual(["✓ All good"])); // and one line when every check passed
    fireEvent.click(card.getByRole("button", { name: "Finish" }));
    await waitFor(() => expect(saved.at(-1)).toMatchObject({ summaryProvider: "openrouter", summaryModel: "anthropic/claude-haiku-5.5", beliefProvider: "openrouter", openrouterApiKey: "sk-or-1", maxSessionMinutes: 90 }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(container.querySelector(".form")).toBeTruthy(); // opened from the page: it stays on it
    expect([...container.querySelectorAll(".form .row > label")].slice(0, 1).map((label) => label.firstChild?.textContent)).toEqual(["Who does the work"]); // the simple view keeps its own words
    expect([window.localStorage.getItem("v1.quickstart"), nav.router.push.mock.calls.length]).toEqual(["1", 0]);
  });

  it("the quickstart's models are three rows while the agents share one model: the Agents row sets tasks, suggestions and preferences", async () => {
    const saved: Record<string, unknown>[] = [];
    const shared: LiveSettings = { ...settings, proposerProvider: "vllm", proposerModel: "qwen", preferenceProvider: "vllm", preferenceModel: "qwen" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/api/live/settings") && init?.method === "POST") saved.push(JSON.parse(String(init.body)) as Record<string, unknown>);
      return backend({ ...idle, defaults: shared })(input);
    }));
    const { container } = render(<Settings expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Quickstart" }));
    const card = within(screen.getByRole("dialog", { name: "Quickstart" }));
    const rows = () => [...container.querySelectorAll(".quick .row > label")].map((label) => label.firstChild?.textContent);
    fireEvent.click(card.getByRole("button", { name: "Continue" }));
    expect(rows()).toEqual(["Screen understanding", "Agents", "Embeddings"]);
    expect([...container.querySelectorAll(".quick .tip")].map((tip) => tip.textContent)).toEqual(["recommended: Claude Haiku 5.5 or equivalent", "recommended: Claude Opus 5.5 or equivalent", "recommended: text-embedding-3-small"]);
    fireEvent.change(container.querySelector("#beliefProvider")!, { target: { value: "openrouter" } });
    fireEvent.change(container.querySelector("#beliefModel")!, { target: { value: "anthropic/claude-sonnet-5.5" } });
    expect(rows()).toEqual(["Screen understanding", "Agents", "Embeddings"]); // still three: the rows are chosen by what is saved
    fireEvent.click(card.getByRole("button", { name: "Continue" }));
    expect(rows()).toEqual(["OpenRouter", "vLLM"]);
    fireEvent.click(card.getByRole("button", { name: "Finish" }));
    const model = "anthropic/claude-sonnet-5.5";
    await waitFor(() => expect(saved.at(-1)).toMatchObject({ beliefProvider: "openrouter", proposerProvider: "openrouter", preferenceProvider: "openrouter", beliefModel: model, proposerModel: model, preferenceModel: model }));
  });

  it("a first-time user arrives in Settings with the quickstart up; closing it goes home and is remembered", async () => {
    nav.query = "quickstart=1";
    vi.stubGlobal("fetch", backend(idle));
    render(<Settings expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Quickstart" })).getByRole("button", { name: "Close" }));
    expect(nav.router.push).toHaveBeenCalledWith("/v1");
    expect(window.localStorage.getItem("v1.quickstart")).toBe("1");
  });

  it("in Settings, a permission rule is a row of a pattern and a level, written by its own ✓ and removed by its ×", async () => {
    const saved: Record<string, unknown>[] = [];
    let rules: NonNullable<LiveSettings["permissionsApps"]> = { Mail: "block" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/api/live/settings") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        saved.push(body);
        rules = (body.permissionsApps as typeof rules | undefined) ?? rules;
      }
      return backend({ ...idle, defaults: { ...settings, permissionsApps: rules } })(input);
    }));
    render(<Settings expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Advanced" }));
    fireEvent.click(screen.getByRole("button", { name: "Permissions" }));
    await screen.findByDisplayValue("Mail"); // the saved rule, as the file holds it: nothing to save on it
    expect(screen.queryByRole("button", { name: "save Apps rule 1" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "add Apps rule" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Apps rule 2" }), { target: { value: "Slack" } }); // typing keeps the row: no round trip through text
    expect((screen.getByRole("combobox", { name: "Apps rule 2 level" }) as HTMLSelectElement).value).toBe("block");
    fireEvent.change(screen.getByRole("combobox", { name: "Apps rule 2 level" }), { target: { value: "look" } });
    expect(saved.length).toBe(1); // the page's first write is {} to read the files; the row is not written until its ✓
    fireEvent.click(screen.getByRole("button", { name: "save Apps rule 2" }));
    await waitFor(() => expect(saved[1]).toEqual({ permissionsApps: { Mail: "block", Slack: "look" } }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "save Apps rule 2" })).toBeNull()); // written: the ✓ goes
    fireEvent.click(screen.getByRole("button", { name: "remove Apps rule 1" }));
    await waitFor(() => expect(saved[2]).toEqual({ permissionsApps: { Slack: "look" } }));
    fireEvent.click(screen.getByRole("button", { name: "add Sites rule" })); // unnamed: no ✓, nothing written
    expect(screen.queryByRole("button", { name: "save Sites rule 1" })).toBeNull();
    expect(saved.length).toBe(3);
  });

  it("in the Zen theme, a blocked look shows the backend's blurred glimpse under a veil that says it is not watching", async () => {
    const blocked = { ...live, session: { ...live.session!, latestSnapshot: { ts: "2026-10-07T14:14:00Z", app: "Slack", blocked: "app" as const } } };
    vi.stubGlobal("fetch", backend(blocked));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    expect(await screen.findByText("Not watching Slack")).toBeTruthy();
    expect(screen.getByText("a Permissions rule blocks it")).toBeTruthy();
    expect((screen.getByAltText("Slack, blurred") as HTMLImageElement).src).toContain("/api/live/session/screen/blocked?t="); // taken now, blurred before it is sent
    expect(screen.queryByAltText("VS Code — prompts.py")).toBeNull(); // not the last frame of another app
  });

  it("in the Zen theme, the remote-desktop button follows the saved settings while Pilot is asleep", async () => {
    vi.stubGlobal("fetch", backend({ ...idle, defaults: { ...settings, actorDesktopSource: ["local", "remote"] } }));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    expect(await screen.findByRole("button", { name: "Remote desktop" })).toBeTruthy();
  });

  it("in the Zen theme, an app looked at under ask counts on Pilot's glasses and shows a card whose answer is posted", async () => {
    const asked = { ...live, session: { ...live.session!, permissionAsks: [{ app: "Slack", bundle: "com.tinyspeck.slackmacgap", ts: "2026-10-07T14:14:00Z" }] } };
    const posted: unknown[] = [];
    const base = backend(asked);
    const answered = backend({ ...live, session: { ...live.session!, latestSnapshot: { ts: "2026-10-07T14:14:05Z", app: "Slack", window: "general" } } }); // the look after the answer
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/api/live/session/permissions")) { posted.push(JSON.parse(String(init?.body))); return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response; }
      return posted.length ? answered(input) : base(input);
    }));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    expect(await screen.findByText("Pilot wants to look at Slack")).toBeTruthy(); // the card, apart from Pilot's bubble
    expect(screen.getByRole("button", { name: "Permission questions" }).textContent).toContain("1");
    fireEvent.click(screen.getByRole("button", { name: "Look" }));
    await waitFor(() => expect(posted).toEqual([{ app: "Slack", level: "look" }]));
    await waitFor(() => expect(screen.queryByText("Pilot wants to look at Slack")).toBeNull()); // the next look is fetched at once, not at the next poll
  });

  it("in the Zen theme, the agent's ask for an app says what for, and Allow for this task posts the answer with its scope", async () => {
    const asked = { ...live, session: { ...live.session!, permissionAsks: [{ app: "Slack", bundle: "com.tinyspeck.slackmacgap", ts: "2026-10-07T14:14:00Z", source: "agent" as const, level: "use" as const, why: "send the reply you drafted" }] } };
    const posted: unknown[] = [];
    const base = backend(asked);
    const answered = backend(live); // the ask left the list
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/api/live/session/permissions")) { posted.push(JSON.parse(String(init?.body))); return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response; }
      return posted.length ? answered(input) : base(input);
    }));
    render(<Zen expectedBackend="python" runnerBaseUrl="http://runner" />);
    expect(await screen.findByText("Pilot wants to use Slack")).toBeTruthy();
    expect(screen.getByText("send the reply you drafted")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Permission questions" }).textContent).toContain("1");
    fireEvent.click(screen.getByRole("button", { name: "Allow ▾" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Allow for this task" }));
    await waitFor(() => expect(posted).toEqual([{ app: "Slack", level: "use", scope: "task" }]));
    await waitFor(() => expect(screen.queryByText("Pilot wants to use Slack")).toBeNull());
  });

  it("on the Usage page, Cost shows the span's total, its parts and a column per day, and Disk what fills the data folder with its sessions largest first", async () => {
    vi.stubGlobal("fetch", backend(idle));
    const { container } = render(<Usage expectedBackend="python" runnerBaseUrl="http://runner" />);
    expect(container.querySelectorAll(".page.usage .panel.loading .skel").length).toBeGreaterThan(0); // the panels in outline until the numbers are in
    await waitFor(() => expect(container.querySelector(".usage .head")?.textContent).toBe("$0.50last 7 daysthis session $0.50 · all time $0.00"));
    expect(container.querySelector(".skel")).toBeNull();
    expect([...container.querySelectorAll(".summary .legend span")].map((item) => item.textContent)).toEqual(["Suggestions $0.50"]); // only what cost something
    expect(container.querySelectorAll(".days > div")).toHaveLength(8); // every day of the span to today, the days that cost nothing included
    expect(container.querySelector(".days > div:last-child b")?.textContent).toBe("$0.50");
    expect(screen.getByText("sonnet")).toBeTruthy();
    expect(screen.queryByText(/Prices from the table/)).toBeNull();
    expect([...container.querySelectorAll("header.top .status + .ctl button")].map((button) => button.textContent)).toEqual(["Start"]); // Timeline v2's bar, on every page
    expect([...container.querySelectorAll("header.top .pages a")].map((link) => [link.textContent, link.getAttribute("aria-current")])).toEqual([["Notes", null], ["History", null], ["Usage", "page"]]);
    expect(container.querySelector("header.top .themeSwitch select")).toBeTruthy(); // the theme switch too: nothing in the bar moves between pages
    expect([container.querySelector("header.top a.b")?.textContent, container.querySelector("header.top a.b")?.getAttribute("href")]).toEqual(["Home", "/v1"]); // the way back, on every page but the landing one
    fireEvent.click(screen.getByRole("button", { name: "Disk" }));
    await waitFor(() => expect(container.querySelector(".usage .head")?.textContent).toBe("29.8 GBused468.0 GB free"));
    expect(container.querySelector(".usage .head b")?.getAttribute("title")).toBe("/data"); // where the folder is
    expect(screen.queryByRole("button", { name: "7 days" })).toBeNull(); // the spans are the cost view's
    expect([...container.querySelectorAll(".summary .legend span")].map((item) => item.textContent)).toEqual(["Screenshots 27.5 GB", "Model calls 1.0 GB", "Screen reads 200 MB", "Memory 600 MB", "Agent browser 400 MB", "Other 100 MB"]);
    const rows = () => [...container.querySelectorAll(".usage tbody tr")];
    expect(rows()).toHaveLength(8);
    expect(rows()[0]!.querySelector("a")?.getAttribute("href")).toBe("/v1/history?session=20261010-100000"); // the largest, a link to where it can be deleted
    expect(rows()[0]!.textContent).toContain("Deck week");
    expect([...rows()[0]!.querySelectorAll("td.num")].map((cell) => cell.textContent)).toEqual(["5.0 GB", "100 MB", "0 KB", "20 MB", "5.1 GB"]);
    fireEvent.click(screen.getByRole("button", { name: "+2 more" }));
    expect(rows()).toHaveLength(10);
    expect(screen.queryByRole("button", { name: "+2 more" })).toBeNull();
  });

  it("on the Usage page's Disk view, sessions are ticked one by one, by age or all at once, compressed together behind a progress bar, marked, and restored the same way", async () => {
    const ago = (days: number) => { // the id of a session that began so many days ago
      const day = new Date(Date.now() - days * 86_400_000);
      return `${day.getFullYear()}${String(day.getMonth() + 1).padStart(2, "0")}${String(day.getDate()).padStart(2, "0")}-100000`;
    };
    const [recent, week, month] = [ago(2), ago(10), ago(30)];
    const row = (id: string, screenshots: number) => ({ id, name: null, compressed: false, screenshots, modelCalls: 0, runs: 0, screenReads: 0, other: 0 });
    let report: LiveDisk = { ...disk, sessions: [row(recent, 3e9), row(week, 2e9), row(month, 1e9)] };
    let through: LiveDisk | null = null; // the report once the job that was started is through
    const posted: string[] = [];
    const fallback = backend(idle);
    const answer = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const action = url.match(/\/api\/live\/disk\/(compress|restore)$/)?.[1];
      if (action) { // started: the answer says how far along; the next look finds the first of them through and the second not
        const { ids } = JSON.parse(String(init?.body)) as { ids: string[] };
        posted.push(`${action} ${ids.join(" ")}`);
        const job = { total: 3e9, done: 1.2e9, sessions: ids.length, finished: 0, failed: {}, running: true };
        const sessions = report.sessions.map((item) => (item.id === ids[0] ? { ...item, compressed: action === "compress" } : item));
        through = { ...report, sessions, packing: { ...job, done: 3e9, finished: ids.length, failed: ids[1] ? { [ids[1]]: "no room" } : {}, running: false } };
        report = { ...report, packing: job };
      } else if (url.endsWith("/api/live/disk")) {
        if (through) report = through;
        through = null;
      } else return fallback(input);
      return { ok: true, json: async () => report } as Response;
    };
    vi.stubGlobal("fetch", vi.fn(answer));
    window.localStorage.setItem("v1.usage", "disk"); // the view last shown
    const { container } = render(<Usage expectedBackend="python" runnerBaseUrl="http://runner" />);
    const ticked = () => [...container.querySelectorAll<HTMLInputElement>(".usage tbody input")].filter((box) => box.checked).map((box) => box.getAttribute("aria-label"));
    const button = (name: RegExp) => screen.getByRole<HTMLButtonElement>("button", { name });
    const marked = () => [...container.querySelectorAll(".usage tbody tr")].filter((line) => line.querySelector(".state svg")).map((line) => line.querySelector("input")?.getAttribute("aria-label"));
    await waitFor(() => expect(container.querySelectorAll(".usage tbody input")).toHaveLength(3)); // a box on every row
    expect([button(/^Compress/).disabled, button(/^Restore/).disabled, marked()]).toEqual([true, true, []]);
    fireEvent.click(button(/Older than 1 week/));
    expect(ticked()).toEqual([`Select ${week}`, `Select ${month}`]);
    fireEvent.click(button(/Older than 3 weeks/));
    expect(ticked()).toEqual([`Select ${month}`]);
    fireEvent.click(screen.getByRole("checkbox", { name: "Every session" }));
    expect(ticked()).toHaveLength(3);
    fireEvent.click(screen.getByRole("checkbox", { name: `Select ${recent}` }));
    expect([button(/^Compress/).textContent, button(/^Restore/).disabled]).toEqual(["Compress 2", true]);
    fireEvent.click(button(/^Compress/));
    await waitFor(() => expect(container.querySelector<HTMLElement>(".packbar i")?.style.width).toBe("40%"));
    expect(posted).toEqual([`compress ${week} ${month}`]);
    expect(container.querySelector(".packtools")?.textContent).toBe("0 of 2");
    expect([...container.querySelectorAll<HTMLInputElement>(".usage input")].every((box) => box.disabled)).toBe(true);
    await waitFor(() => expect(container.querySelector(".packbar")).toBeNull(), { timeout: 4000 }); // read again every two seconds
    expect(container.querySelector(".packfail")?.textContent).toContain("no room");
    expect([marked(), ticked()]).toEqual([[`Select ${week}`], []]); // the one that went through is marked; nothing stays ticked
    fireEvent.click(screen.getByRole("checkbox", { name: "Every session" })); // the compressed one among them is what Restore takes
    expect([button(/^Compress/).textContent, button(/^Restore/).textContent]).toEqual(["Compress 2", "Restore 1"]);
    fireEvent.click(button(/^Restore/));
    await waitFor(() => expect(posted[1]).toBe(`restore ${week}`));
    await waitFor(() => expect(container.querySelector(".packbar")).toBeNull(), { timeout: 4000 });
    expect(marked()).toEqual([]);
  });
});
