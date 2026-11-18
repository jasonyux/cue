import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { LiveChat, LiveProposalSet, LiveSessionStatus, LiveSettings } from "@cue/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { scenarioFixture } from "./fixtures";

import { THEMES, pickTheme } from "../components/v1/theme";
import { Landing } from "../components/v1/Landing";
import { Settings } from "../components/v1/Settings";
import { Timeline2 } from "../components/v1/Timeline2";
import { SessionProvider } from "../components/v1/useSession";

const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router, useSearchParams: () => new URLSearchParams() }));

const at = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60_000).toISOString(); // the session runs now

const settings: LiveSettings = {
  summaryModel: "qwen", summaryProvider: "vllm", summaryStream: true, proposerModel: "sonnet", proposerProvider: "anthropic", proposerStream: true,
  beliefModel: "qwen", beliefProvider: "vllm", beliefStream: true, embedModel: "", snapshotIntervalS: 10, summaryIntervalS: 60, proposerIntervalS: 180,
  summaryThinking: false, proposerThinking: true, summaryScreenshots: false, proposerScreenshots: false, proposerInputs: ["tasks", "worklog"],
  beliefIntervalS: 300, beliefThinking: true, beliefMaxTokens: 0, beliefStaleDays: 3, beliefDoneStepDays: 7, beliefTrashDays: 7, streamLookbackDays: 30,
  actorYieldS: 20, actorContextSummaries: 12, actorDesktopSource: ["local"], actorAgents: ["claude"], actorCmuxPermissionMode: "auto", actorClaudePermissionMode: "auto", actorCodexSandbox: "workspace-write",
  maxProposals: 3, summaryMaxTokens: 0, proposerMaxTokens: 0, proposerFeedbackRejected: 6, proposerFeedbackAccepted: 3, proposerFeedbackRecent: 2, proposerSearchRounds: 5, proposerDedup: false,
  proposerDedupThreshold: 0.8, proposerDedupNotNowHours: 24, proposerDedupSessionHours: 24, screenshotMode: "switch", effectiveScreenshotMode: "switch",
  screenshotMaxSide: 1024, maxSessionMinutes: 120, maxElements: 400, notify: false, proposerContextSummaries: 20,
};
const tasks = [
  { id: "t1", title: "ICLR 2027 paper", status: "active" as const, steps: [{ text: "Revise the introduction", callId: "c1" }, { text: "Send the draft", doneAt: "2026-10-07T10:00:00Z" }], lastSeen: "2026-10-07T10:00:00Z", sessionId: "s1", evidence: [] },
  { id: "t2", title: "Sidequests", status: "active" as const, steps: [{ text: "Attend the colloquium" }], lastSeen: "2026-10-07T09:00:00Z", sessionId: "s1", evidence: [] },
  { id: "t3", title: "ICLR review", status: "done" as const, steps: [{ text: "Submit the review", doneAt: "2026-10-06T10:00:00Z" }], lastSeen: "2026-10-06T10:00:00Z", sessionId: "s1", evidence: [] },
  { id: "t4", title: "Thesis proposal", status: "active" as const, steps: [{ text: "Port the slides" }], lastSeen: "2026-10-07T08:00:00Z", sessionId: "s1", evidence: [] },
];
const base = { defaults: settings, rejectReasons: ["not now", "already done"], limits: { maxProposals: 3 }, tasks, trash: [], taskFlags: [] };
const idle: LiveSessionStatus = { ...base, active: false, session: null };
const live: LiveSessionStatus = { ...base, active: true, session: {
  id: "20261007-135300", folder: "/data/sessions/20261007-135300", startedAt: at(20), expiresAt: at(-100), settings, runs: [], chatRuns: [],
  observerAlive: true, paused: false, snapshotCount: 4, tickCount: 8, screenshotCount: 1, summaryTicks: 1, beliefTicks: 1, proposerTicks: 2, latestProposals: null,
  latestSnapshot: { ts: at(1), app: "VS Code", window: "prompts.py", visible: [] },
  latestScreenshot: { ts: at(8), name: "shot-1.png", app: "VS Code", window: "prompts.py" },
  agentBrowser: null,
  nextSummaryAt: at(-1), nextBeliefAt: at(-4), nextProposalAt: at(-2),
} };
const notes = [{ id: "n1", ts: at(7), windowStart: at(8), windowEnd: at(7), summary: "Edited prompts.py in VS Code." }];
const shots = [{ ts: at(8), name: "shot-1.png", app: "VS Code", window: "prompts.py" }];
const changes = [{ ts: at(6), windowStart: at(10), windowEnd: at(6), ops: [{ op: "update" as const, taskId: "t1", steps: [{ text: "Revise the introduction" }] }], callId: "c1" }];
const proposals = [
  { ts: at(15), trigger: "idle" as const, proposals: [] }, // a pause that found nothing
  { ts: at(11), proposals: [{ kind: "act" as const, task: "Reply to Wei" }], feedback: { decision: "reject" as const, ts: at(10.5), rejectReason: "not now" } },
  { id: "p3", ts: at(8), windowStart: at(11), windowEnd: at(8), proposals: [{ kind: "act" as const, task: "Register for NeurIPS 2026 before early-bird ends", reason: "Registration opened today.", evidence: ["n1", "h9", "t1", "zz"] }, { kind: "lookup" as const, task: "Reply to Wei" }],
    cited: [{ id: "h9", ts: "2026-08-10T09:00:00Z", kind: "summary_written", sessionId: "old", text: "Booked the Sydney hotel.", line: "[h9] [2026-08-10 09:00] worklog: Booked the Sydney hotel." }] },
];
const chat = { id: "c1", current: "c1", agent: null, turn: 0, runningRunId: null, messages: [], pending: [], conversations: [] };
const usage = { all: [], recent: [], byDay: [], days: 1, session: [] };
const flag = { id: "f1", ts: at(0), sessionId: "", taskId: "t1", title: "ICLR 2027 paper", step: null, text: "", verdict: "down" as const, reason: "" };

function backend(status: LiveSessionStatus, posted: { url: string; body: unknown }[] = [], sets: LiveProposalSet[] = proposals, conversation: LiveChat = chat) {
  const flagged: (typeof flag)[] = []; // a thumbs given: the status carries it from then on
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST" || init?.method === "PATCH") { posted.push({ url: url.split("/api/")[1] ?? url, body: JSON.parse(String(init.body ?? "{}")) }); }
    if (url.endsWith("/api/live/tasks/feedback")) flagged.push(flag);
    const body = url.includes("/api/live/chats") ? { conversations: conversation.conversations, current: conversation.current, sessions: { "20261104-120000": "Thesis morning", "20261101-090000": null } } : url.includes("/api/live/tasks/feedback") ? flag : url.includes("/api/live/tasks") ? { tasks, trash: [] } : url.includes("/api/live/session/proposals") ? sets : url.includes("/api/live/session/chat") ? (url.includes("?id=") ? { ...conversation, id: decodeURIComponent(url.split("?id=")[1]!) } : conversation) // an earlier conversation asked for by id
      : url.includes("/api/live/session/changes") ? changes : url.includes("/api/live/session/summaries") ? notes : url.includes("/api/live/session/screenshots") ? shots
      : url.includes("/api/live/session/permissions") ? { ok: true } : url.includes("/api/live/sessions") ? [] : url.includes("/api/live/usage") ? usage : url.includes("/api/live/session") ? { ...status, taskFlags: flagged } : null;
    return { ok: body !== null, status: body === null ? 404 : 200, json: async () => body ?? { error: `no stub for ${url}` } } as Response;
  });
}

describe("the Timeline v2 theme", () => {
  beforeEach(() => { vi.stubGlobal("EventSource", vi.fn(() => ({ close: vi.fn() }))); vi.stubGlobal("ResizeObserver", vi.fn(() => ({ observe: vi.fn(), disconnect: vi.fn() }))); window.localStorage.clear(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.localStorage.clear(); });
  const onTheTimeline = () => window.localStorage.setItem("v1.side", "timeline"); // the sidebar's other panel, as chosen on an earlier visit

  it("is a theme of its own", () => {
    expect(THEMES.map(([theme]) => theme)).toContain("timelinev2");
    expect(pickTheme("timelinev2", null)).toBe("timelinev2");
  });

  it("asleep: the curtain offers to start, as does the header beside the session's state, and the task bar names the first tasks", async () => {
    const posted: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", backend(idle, posted));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Cue is asleep");
    expect(screen.getByRole("button", { name: "▶ Start session" })).toBeTruthy();
    expect([...container.querySelectorAll("header.top .status + .ctl button")].map((button) => button.textContent)).toEqual(["Start"]);
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(posted.map((call) => call.url)).toEqual(["live/session"]));
    expect(container.querySelector(".trow .lbl")?.textContent).toContain("Tasks");
    expect(container.querySelector(".trow .count")?.textContent).toBe("3"); // the three open tasks; the done one is behind its fold
    expect([...container.querySelectorAll(".trow .t")].map((item) => item.textContent)).toEqual(["ICLR 2027 paper", "Thesis proposal", "Sidequests"]); // in the tray's order, the Sidequests last
    expect(container.querySelector("button.trow.fold .chev")).toBeTruthy(); // folded, the whole bar is the button
    fireEvent.click(container.querySelector(".trow .lbl")!);
    expect(container.querySelectorAll(".tlist .task")).toHaveLength(3); // the tray unfolds into a section per task
    expect((screen.getByRole("button", { name: "Ask Cue" }) as HTMLButtonElement).disabled).toBe(true); // nothing to send to without a session
    expect(container.querySelector(".trow.fold")).toBeNull();
    fireEvent.click(container.querySelector(".trow .chev")!);
    expect(container.querySelector(".trow.fold")).toBeTruthy(); // the chevron at the right edge folds it back
  });

  it("Ask Cue picks entries in the tray, Attach holds them in the box as chips, and the message quotes them under their tasks and lists them beside the text", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const fallback = backend(live, posted);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/live/session/chat/messages")) {
        const body = JSON.parse(String(init?.body)); posted.push({ url: "live/session/chat/messages", body });
        const detail = { eventStreamUrl: "/api/runs/r2/events", events: [], replayUrl: "/api/runs/r2/replay", workspacePath: "/tmp/r2", scenario: scenarioFixture, run: { id: "r2", browserMode: "headful", maxResponseTurns: 24, labId: scenarioFixture.labId, model: "test-model", prompt: body.text, scenarioId: scenarioFixture.id, startedAt: at(0), status: "running" } };
        return { ok: true, json: async () => ({ runId: "r2", status: "running", eventStreamUrl: detail.eventStreamUrl, replayUrl: detail.replayUrl, detail }) } as Response; // whole, so the send counts as sent
      }
      return fallback(input, init);
    }));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByRole("button", { name: "Chat" });
    fireEvent.click(container.querySelector(".trow .lbl")!);
    fireEvent.click(screen.getByRole("button", { name: "Ask Cue" }));
    const tray = within(container.querySelector<HTMLElement>(".tray.picking")!);
    expect(container.querySelector(".tlist .more")).toBeNull(); // no ⋯, trash or add while picking
    expect(container.querySelector(".tlist .add")).toBeNull();
    fireEvent.click(tray.getByText("Revise the introduction"));
    fireEvent.click(tray.getByText("Port the slides"));
    fireEvent.click(tray.getByText("Attend the colloquium"));
    expect(container.querySelectorAll(".entry.picked")).toHaveLength(3);
    expect(container.querySelector(".trow .count")?.textContent).toBe("3");
    fireEvent.click(screen.getByRole("button", { name: "Attach" }));
    expect(container.querySelector(".trow.fold")).toBeTruthy(); // the tray folded, the picks are in the box
    await waitFor(() => expect([...container.querySelectorAll(".composer .pickchip span")].map((chip) => chip.textContent)).toEqual(["Revise the introduction", "Port the slides", "Attend the colloquium"])); // once the box is aimed
    fireEvent.click(screen.getAllByRole("button", { name: "Drop" })[2]!);
    fireEvent.change(screen.getByRole("textbox", { name: "Message Cue" }), { target: { value: "Do the first one first." } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const sent = {
      text: "> ICLR 2027 paper:\n>   • Revise the introduction\n> Thesis proposal:\n>   • Port the slides\n\nDo the first one first.",
      selectedTasks: [{ taskId: "t1", title: "ICLR 2027 paper", text: "Revise the introduction" }, { taskId: "t4", title: "Thesis proposal", text: "Port the slides" }], // the record tells the tray's lines from the typed ones
    };
    await waitFor(() => expect(posted.find((item) => item.url === "live/session/chat/messages")?.body).toMatchObject(sent));
    await waitFor(() => expect(container.querySelectorAll(".composer .pickchip")).toHaveLength(0)); // sent: the box is clear
    expect((screen.getByRole("textbox", { name: "Message Cue" }) as HTMLTextAreaElement).value).toBe("");
  });

  it("a message that starts with quoted lines shows them as a quotation above the user's own words", async () => {
    const said = { ts: at(3), chatId: "c1", turn: 1, role: "user", text: "> ICLR 2027 paper:\n>   • Revise the introduction\n\nDo it today.", runId: "r1" };
    const conversation = { ...chat, agent: "claude", turn: 1, messages: [said] };
    const fallback = backend(live);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => (String(input).endsWith("/api/live/session/chat") ? { ok: true, json: async () => conversation } as Response : fallback(input, init))));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Chat" }));
    await waitFor(() => expect(container.querySelector(".me blockquote")?.textContent).toBe("ICLR 2027 paper:\n  • Revise the introduction"));
    expect(container.querySelector(".me")?.textContent).toContain("Do it today.");
  });

  it("a thumbs down on a card is recorded, then asked why, with no session running", async () => {
    const posted: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", backend(idle, posted));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Cue is asleep");
    fireEvent.click(container.querySelector(".trow .lbl")!);
    const card = container.querySelector<HTMLElement>(".tlist .task")!;
    fireEvent.click(within(card).getAllByTitle("This is wrong")[0]!); // the task's own thumbs come before its entries'
    await waitFor(() => expect(posted).toContainEqual({ url: "live/tasks/feedback", body: { taskId: "t1", step: null, verdict: "down" } }));
    fireEvent.click(await within(card).findByRole("button", { name: "made up" }));
    await waitFor(() => expect(posted).toContainEqual({ url: "live/tasks/feedback/reason", body: { flagId: "f1", reason: "made up" } }));
    expect(within(card).getAllByTitle("This is wrong")[0]!.className).toBe("active");
    expect(card.querySelector(".thd .flagMark")?.textContent).toBe("👎"); // the flag given, in the slot after the title
  });

  it("a task is a section: an entry unfolds on a click, the ⋯ menu renames, folds, finishes or trashes it, the footer lists the done entries, a dragged header reorders", async () => {
    const posted: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", backend(idle, posted));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Cue is asleep");
    fireEvent.click(container.querySelector(".trow .lbl")!);
    const task = container.querySelector<HTMLElement>(".tlist .task")!;
    expect(task.querySelector(".thd .ttl")?.textContent).toBe("ICLR 2027 paper");
    expect(task.querySelector(".thd .cnt")?.textContent).toBe("1 open");
    expect([...task.querySelectorAll(".entry .txt")].map((item) => item.textContent)).toEqual(["Revise the introduction"]); // the done one waits behind the footer
    fireEvent.click(within(task).getByTitle("Revise the introduction"));
    expect(task.querySelector(".entry.open .txt")?.textContent).toBe("Revise the introduction");
    fireEvent.click(within(task).getByRole("button", { name: "More" }));
    expect(within(task).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Rename", "Fold all", "Mark done", "Trash"]);
    fireEvent.click(within(task).getByRole("menuitem", { name: "Fold all" }));
    expect(task.querySelector(".entry.open")).toBeNull();
    fireEvent.click(within(task).getByRole("button", { name: "1 done" }));
    expect([...task.querySelectorAll(".entry.done .txt")].map((item) => item.textContent)).toEqual(["Send the draft"]);
    const transfer = { effectAllowed: "", setDragImage: () => undefined };
    fireEvent.dragStart(container.querySelectorAll(".tlist .thd")[1]!, { dataTransfer: transfer });
    fireEvent.dragOver(task, { dataTransfer: transfer });
    fireEvent.drop(task, { dataTransfer: transfer, clientX: 0 }); // onto the left half of the first: before it
    expect([...container.querySelectorAll(".tlist .thd .ttl")].map((item) => item.textContent)).toEqual(["Thesis proposal", "ICLR 2027 paper", "Sidequests"]); // the sidequests stay last
    expect(JSON.parse(window.localStorage.getItem("v1.tasks")!).order).toEqual(["t4", "t1"]);
    const quests = container.querySelector<HTMLElement>(".tlist .task.standing")!;
    expect(quests.querySelector(".thd")?.getAttribute("draggable")).toBe("false");
    expect(quests.querySelector(".thd .cnt")?.textContent).toBe("one-offs");
    fireEvent.click(within(quests).getByRole("button", { name: "More" }));
    expect(within(quests).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Fold all"]); // renamed, finished or trashed, the model would start another
    fireEvent.click(within(quests).getByRole("menuitem", { name: "Fold all" }));
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("ICLR 2027 camera-ready");
    fireEvent.click(within(task).getByRole("button", { name: "More" }));
    fireEvent.click(within(task).getByRole("menuitem", { name: "Rename" }));
    await waitFor(() => expect(posted).toContainEqual({ url: "live/tasks", body: { ops: [{ op: "rename", taskId: "t1", title: "ICLR 2027 camera-ready" }] } }));
    fireEvent.click(within(task).getByRole("button", { name: "More" }));
    fireEvent.click(within(task).getByRole("menuitem", { name: "Mark done" }));
    await waitFor(() => expect(posted).toContainEqual({ url: "live/tasks", body: { ops: [{ op: "status", taskId: "t1", done: true }] } }));
    fireEvent.click(screen.getByRole("button", { name: "+ New task" }));
    await waitFor(() => expect(posted).toContainEqual({ url: "live/tasks", body: { ops: [{ op: "add", taskId: null, title: "ICLR 2027 camera-ready", steps: [] }] } }));
    prompt.mockRestore();
  });

  it("only offers the completed-task toggle in an open task panel", async () => {
    vi.stubGlobal("fetch", backend(live));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    expect(screen.queryByRole("button", { name: /Show done|Hide done/ })).toBeNull();
    fireEvent.click(container.querySelector(".trow .lbl")!);
    const show = screen.getByRole("button", { name: /Show done/ });
    expect(show.getAttribute("aria-pressed")).toBe("false");
    expect(container.querySelector(".trow .n")?.textContent).toBe("3 open · 1 done");
    fireEvent.click(show);
    expect(container.querySelector(".task.done .ttl")?.textContent).toBe("ICLR review");
    expect(screen.getByRole("button", { name: /Hide done/ }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(container.querySelector(".trow .lbl")!);
    expect(screen.queryByRole("button", { name: /Show done|Hide done/ })).toBeNull();
    fireEvent.click(container.querySelector(".trow .lbl")!);
    fireEvent.click(screen.getByRole("button", { name: /Hide done/ }));
    expect(container.querySelector(".task.done")).toBeNull();
    expect(screen.getByRole("button", { name: /Show done/ })).toBeTruthy();
  });

  it("live: the rail carries the records, the suggestion is the front card, Why? opens the page on what it rests on", async () => {
    onTheTimeline();
    vi.stubGlobal("fetch", backend(live));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    await waitFor(() => expect(container.querySelector(".rail .tick.note")).toBeTruthy()); // the stored records are in
    expect(container.querySelector(".rail .tick.look img.mini")?.getAttribute("src")).toContain("shot-1.png"); // a look: its screenshot as the thumbnail
    expect(container.querySelector(".rail .tick.note .glyph")?.textContent).toBe("≋");
    expect(container.querySelector(".rail .tick.suggest.pending")).toBeTruthy();
    expect(container.querySelector(".rail .tick.answer.no .glyph")?.textContent).toBe("✘"); // the declined one
    expect(container.querySelector(".card h1")?.textContent).toContain("Register for NeurIPS 2026");
    expect(container.querySelector(".card .description")).toBeNull();
    expect(container.querySelector(".card .evidence-chips")).toBeNull();
    expect(container.querySelector(".card .meta time")).toBeNull();
    expect(screen.getByRole("button", { name: "→ Do it" })).toBeTruthy();
    const expand = screen.getByRole("button", { name: "+ Details" });
    expect(expand.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(expand);
    expect(container.querySelector(".card .description")?.textContent).toBe("Registration opened today.");
    expect(screen.getByRole("button", { name: "− Less" }).getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelectorAll(".filters button")).toHaveLength(4);
    expect(container.querySelector(".paging span")?.textContent).toBe("1/1");
    expect(container.querySelector(".rearwrap")).toBeNull(); // nothing behind it
    const strip = [...container.querySelectorAll(".card .evidence-chips .ctx")];
    expect(strip.map((item) => item.className)).toEqual(["ctx note", "ctx far", "ctx task"]); // what it rests on: this session's note, an earlier session's line, a task; the unknown id left out
    expect(strip[0]!.getAttribute("title")).toBe("Edited prompts.py in VS Code.");
    expect(screen.getByRole("button", { name: "→ Do it" }).hasAttribute("disabled")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Why? ›" }));
    expect(container.querySelector(".why-panel>header h3")?.textContent).toBe("Why this suggestion?");
    expect(container.querySelector(".card h1")).toBeTruthy();
    expect(container.querySelector(".stage-content.with-why")).toBeTruthy();
    expect(container.querySelector(".reasonbox p")?.textContent).toBe("Registration opened today.");
    const sources = [...container.querySelectorAll(".whymain .source")];
    expect(sources.map((item) => item.querySelector("h3")?.textContent)).toEqual(["Edited prompts.py in VS Code.", "Booked the Sydney hotel.", "ICLR 2027 paper", "zz"]);
    expect(container.querySelector(".rail .tick.note.cited")).toBeTruthy(); // the cited note is ringed on the rail
    fireEvent.click(sources[0]!);
    expect(container.querySelector(".whyside h3")?.textContent).toBe("Note");
    expect(container.querySelector(".whyside p")?.textContent).toBe("Edited prompts.py in VS Code.");
    expect(container.querySelector(".whyside .shots img")).toBeTruthy(); // and the look it was made from
    fireEvent.click(sources[1]!);
    expect(container.querySelector(".whyside a")?.getAttribute("href")).toBe("/v1/notes?session=old&id=h9"); // an earlier session's line opens that session's page
    fireEvent.click(screen.getByRole("button", { name: "Close Why panel" }));
    expect(container.querySelector(".why-panel")).toBeNull();
    expect(container.querySelector(".card h1")).toBeTruthy();
    fireEvent.click(container.querySelector(".card .evidence-chips .ctx.note")!); // a chip on the card opens the Why page on that record
    expect(container.querySelector(".whyside h3")?.textContent).toBe("Note");
  });

  it("collapses proposal details again while keeping its title and actions available", async () => {
    vi.stubGlobal("fetch", backend(live));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    fireEvent.click(screen.getByRole("button", { name: "+ Details" }));
    expect(container.querySelector(".card .description")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "− Less" }));
    expect(container.querySelector(".card .description")).toBeNull();
    expect(container.querySelector(".card .evidence-chips")).toBeNull();
    expect(container.querySelector(".card h1")?.textContent).toBe("Register for NeurIPS 2026 before early-bird ends");
    expect(screen.getByRole("button", { name: "→ Do it" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Not now ▾" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Why? ›" }));
    expect(container.querySelector(".why-panel .reasonbox p")?.textContent).toBe("Registration opened today.");
  });

  it("a tick opens its record in the panel on the right", async () => {
    onTheTimeline();
    vi.stubGlobal("fetch", backend(live));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    await waitFor(() => expect(container.querySelector(".rail .tick.note")).toBeTruthy());
    fireEvent.click(container.querySelector(".rail .tick.note")!);
    expect(container.querySelector(".detail header h3")?.textContent).toBe("Note");
    expect(container.querySelector(".detail .body p")?.textContent).toBe("Edited prompts.py in VS Code.");
    fireEvent.click(screen.getByRole("button", { name: "Close detail" }));
    expect(container.querySelector(".detail")).toBeNull();
    fireEvent.click(container.querySelector(".rail .tick.look")!);
    expect(container.querySelector(".detail header h3")?.textContent).toBe("Screenshot");
    expect(container.querySelector(".detail img.big")?.getAttribute("src")).toContain("shot-1.png");
  });

  it("keeps a visible conversation restore button and preserves the draft when going Home", async () => {
    vi.stubGlobal("fetch", backend(live));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    const reopen = await screen.findByRole("button", { name: "Chat" });
    expect(reopen.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(reopen);
    const composer = screen.getByRole("textbox", { name: "Message Cue" });
    fireEvent.change(composer, { target: { value: "Keep this draft while I check the cards." } });
    expect(screen.getAllByRole("button", { name: "Home" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Home" }));
    expect(container.querySelector(".execution")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect((screen.getByRole("textbox", { name: "Message Cue" }) as HTMLTextAreaElement).value).toBe("Keep this draft while I check the cards.");
    expect(screen.getByRole("button", { name: "Home" }).getAttribute("aria-expanded")).toBe("true");
  });

  it("shows the agent browser in the chat's screen pane with no run in flight, and lets the view be picked", async () => {
    const browsing = { ...live, session: { ...live.session!, agentBrowser: { endpoint: "http://127.0.0.1:9222", viewport: { width: 1280, height: 800 }, tabs: [{ id: "t1", title: "Maps", url: "https://maps.example", agent: true }] } } };
    vi.stubGlobal("fetch", backend(browsing));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Chat" }));
    expect(screen.getByText("Nothing to show yet")).toBeTruthy(); // nothing runs: following shows nothing until a view is picked
    const views = within(screen.getByRole("group", { name: "Screen view" })).getAllByRole("button");
    expect(views.map((button) => [button.textContent, button.getAttribute("aria-pressed")])).toEqual([["Follow agent", "true"], ["Cue's browser", "false"]]); // no remote desktop in the settings
    fireEvent.click(screen.getByRole("button", { name: "Cue's browser" }));
    expect(screen.getByRole("img", { name: "The agent's browser" }).getAttribute("src")).toBe("http://runner/api/live/session/browser/stream?width=900");
    expect(screen.getByText("● Live · Cue's browser · Maps")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Take control" })).toBeTruthy();
    const toggle = screen.getByRole("button", { name: "Monitor" }); // the pane itself: off, the chat alone; on, there even with nothing in flight
    fireEvent.click(toggle);
    expect(container.querySelector(".screenpane")).toBeNull();
    fireEvent.click(toggle);
    expect(container.querySelector(".screenpane")).toBeTruthy();
  });

  it("stands in for the browser's blank first tab with a placeholder instead of a white stream", async () => {
    const blank = { ...live, session: { ...live.session!, agentBrowser: { endpoint: "http://127.0.0.1:9222", viewport: { width: 1280, height: 800 }, tabs: [{ id: "t1", title: "", url: "about:blank", agent: false }] } } };
    vi.stubGlobal("fetch", backend(blank));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Cue's browser" }));
    expect(screen.getByText("No page open yet")).toBeTruthy();
    expect(screen.queryByRole("img", { name: "The agent's browser" })).toBeNull();
    expect(screen.getByText("● Live · Cue's browser")).toBeTruthy();
  });

  it("offers Cue's browser before any run opened it, with the placeholder and nothing to take control of", async () => {
    vi.stubGlobal("fetch", backend({ ...live, session: { ...live.session!, agentBrowser: null } }));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Cue's browser" }));
    expect(screen.getByText("No page open yet")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Take control" })).toBeNull();
  });

  it("the card offers the agent while the conversation has none, and Do it sends to the one picked", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const twoAgents: LiveSessionStatus = { ...live, session: { ...live.session!, settings: { ...settings, actorAgents: ["claude", "codex"] } } };
    const fallback = backend(twoAgents, posted);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/live/session/proposals/feedback")) return { ok: true, json: async () => ({ ts: at(0), proposalsTs: proposals[2]!.ts, decision: "accept", choice: 0 }) } as Response;
      if (url.endsWith("/api/live/session/chat/messages")) {
        const body = JSON.parse(String(init?.body)); posted.push({ url: "live/session/chat/messages", body });
        const detail = { eventStreamUrl: "/api/runs/r2/events", events: [], replayUrl: "/api/runs/r2/replay", workspacePath: "/tmp/r2", scenario: scenarioFixture, run: { id: "r2", browserMode: "headful", maxResponseTurns: 24, labId: scenarioFixture.labId, model: "test-model", prompt: body.text, scenarioId: scenarioFixture.id, startedAt: at(0), status: "running" } };
        return { ok: true, json: async () => ({ runId: "r2", eventStreamUrl: detail.eventStreamUrl, detail }) } as Response;
      }
      return fallback(input, init);
    }));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    fireEvent.change(screen.getByRole("combobox", { name: "Agent" }), { target: { value: "codex" } }); // the card's own picker: no conversation yet, so the agent alone
    fireEvent.click(screen.getByRole("button", { name: "→ Do it" }));
    await waitFor(() => expect(posted.find((item) => item.url === "live/session/chat/messages")?.body).toMatchObject({ agent: "codex", text: "Register for NeurIPS 2026 before early-bird ends" }));
  });

  it("the card's Send to lists the session's conversations and a new one; nothing happens before Accept, which switches or resets first", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const twoAgents: LiveSessionStatus = { ...live, session: { ...live.session!, settings: { ...settings, actorAgents: ["claude", "codex"] } } };
    const talks: LiveChat = { ...chat, id: "c2", current: "c2", agent: "claude", turn: 1, conversations: [ // latest first, as the backend lists them
      { id: "c2", agent: "claude", turns: 1, startedAt: at(10), firstMessage: "Fix the lint" },
      { id: "c1", agent: "codex", turns: 2, startedAt: at(30), firstMessage: "Port the slides" },
    ] };
    vi.stubGlobal("fetch", backend(twoAgents, posted, proposals, talks));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    const pick = screen.getByRole("combobox", { name: "Send to" }) as HTMLSelectElement;
    expect(pick.value).toBe("conv:c2"); // the current conversation
    expect([...pick.options].map((option) => option.textContent)).toEqual(["Claude Code · Fix the lint", "Codex · Port the slides", "New conversation"]);
    expect(screen.queryByRole("combobox", { name: "Agent" })).toBeNull(); // the conversation's agent applies
    fireEvent.change(pick, { target: { value: "conv:c1" } });
    fireEvent.change(pick, { target: { value: "new" } });
    expect(screen.getByRole("combobox", { name: "Agent" })).toBeTruthy(); // a new conversation: the agent is to pick
    fireEvent.change(pick, { target: { value: "conv:c1" } });
    expect(posted).toEqual([]); // picking back and forth sent nothing
    fireEvent.click(screen.getByRole("button", { name: "→ Do it" }));
    await waitFor(() => expect(posted.map((item) => item.url)).toEqual(["live/session/chat/switch", "live/session/proposals/feedback", "live/session/chat/messages"]));
    expect(posted[0]!.body).toEqual({ id: "c1" });
    expect(posted[2]!.body).toMatchObject({ agent: "codex" }); // that conversation's agent
    posted.length = 0;
    fireEvent.click(screen.getByRole("button", { name: "Home" })); // Do it opened the chat; back to the card
    fireEvent.change(screen.getByRole("combobox", { name: "Send to" }), { target: { value: "new" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Agent" }), { target: { value: "codex" } });
    fireEvent.click(screen.getByRole("button", { name: "→ Do it" }));
    await waitFor(() => expect(posted.map((item) => item.url)).toEqual(["live/session/chat/reset", "live/session/proposals/feedback", "live/session/chat/messages"]));
    expect(posted[2]!.body).toMatchObject({ agent: "codex" });
  });

  it("a new conversation picked on the card shows the agent picker beside it", async () => {
    const twoAgents: LiveSessionStatus = { ...live, session: { ...live.session!, settings: { ...settings, actorAgents: ["claude", "codex"] } } };
    const fresh: LiveChat = { ...chat, current: null, conversations: [{ id: "c1", agent: "codex", turns: 2, startedAt: at(30), firstMessage: "Port the slides" }] }; // the earlier one parked, none current
    vi.stubGlobal("fetch", backend(twoAgents, [], proposals, fresh));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    expect((screen.getByRole("combobox", { name: "Send to" }) as HTMLSelectElement).value).toBe("new");
    expect((screen.getByRole("combobox", { name: "Agent" }) as HTMLSelectElement).value).toBe("claude");
  });

  it("keeps the earlier search reply when hiding chat and sending the next turn, without resetting", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const first = { ts: at(3), chatId: "c1", turn: 1, role: "user", text: "Search for the official booking site.", runId: "r1" };
    const reply = { ...first, ts: at(2), role: "agent", text: "Here is the official booking site and my feedback.", status: "completed" };
    let conversation = { ...chat, agent: "claude", turn: 1, messages: [first, reply], conversations: [{ id: "c1", agent: "claude", turns: 1, startedAt: at(3), firstMessage: first.text }] };
    const fallback = backend(live, posted);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/live/session/chat")) return { ok: true, json: async () => conversation } as Response;
      if (url.endsWith("/api/live/session/chat/messages")) {
        const body = JSON.parse(String(init?.body)); posted.push({ url: "live/session/chat/messages", body });
        conversation = { ...conversation, turn: 2, messages: [...conversation.messages, { ...first, runId: "r2", turn: 2, ts: at(0), text: body.text }] };
        const detail = { eventStreamUrl: "/api/runs/r2/events", events: [], replayUrl: "/api/runs/r2/replay", workspacePath: "/tmp/r2", scenario: scenarioFixture, run: { id: "r2", browserMode: "headful", maxResponseTurns: 24, labId: scenarioFixture.labId, model: "test-model", prompt: body.text, scenarioId: scenarioFixture.id, startedAt: at(0), status: "running" } };
        return { ok: true, json: async () => ({ runId: "r2", eventStreamUrl: detail.eventStreamUrl, detail }) } as Response;
      }
      return fallback(input, init);
    }));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Chat" }));
    await screen.findByText(reply.text);
    fireEvent.click(screen.getByRole("button", { name: "Home" }));
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByText(reply.text)).toBeTruthy();
    fireEvent.change(screen.getByRole("textbox", { name: "Message Cue" }), { target: { value: "What should I do next?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(posted.some((item) => item.url === "live/session/chat/messages")).toBe(true));
    await screen.findByText("What should I do next?");
    expect(screen.getByText(reply.text)).toBeTruthy();
    expect(posted.some((item) => item.url.includes("chat/reset"))).toBe(false);
  });

  it("queues a message sent while a turn runs and sends it as the next turn once the run ends", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const run = { eventStreamUrl: "/api/runs/r1/events", events: [], replayUrl: "/api/runs/r1/replay", workspacePath: "/tmp/r1", scenario: scenarioFixture,
      run: { id: "r1", browserMode: "headful", maxResponseTurns: 24, labId: scenarioFixture.labId, model: "test-model", prompt: "first", scenarioId: scenarioFixture.id, startedAt: at(1), status: "running" } };
    const busy: LiveSessionStatus = { ...live, session: { ...live.session!, runs: ["r1"] } };
    const fallback = backend(busy, posted);
    const sources: { close: () => void; onmessage: ((message: { data: string }) => void) | null }[] = [];
    vi.stubGlobal("EventSource", vi.fn(() => { const source = { close: vi.fn(), onmessage: null }; sources.push(source); return source; }));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/runs/r1")) return { ok: true, status: 200, json: async () => run } as Response;
      if (url.endsWith("/api/live/session/proposals/feedback")) return { ok: true, status: 200, json: async () => ({ ts: at(0), proposalsTs: proposals[2]!.ts, decision: "accept", choice: 0 }) } as Response;
      if (url.endsWith("/api/live/session/chat/messages")) {
        const body = JSON.parse(String(init?.body)); posted.push({ url: "live/session/chat/messages", body });
        const detail = { ...run, eventStreamUrl: "/api/runs/r2/events", run: { ...run.run, id: "r2", prompt: body.text, startedAt: at(0) } };
        return { ok: true, status: 200, json: async () => ({ runId: "r2", eventStreamUrl: detail.eventStreamUrl, detail }) } as Response;
      }
      return fallback(input, init);
    }));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    await waitFor(() => expect(sources.length).toBeGreaterThan(0)); // the running turn's stream is attached: the page knows a turn runs
    fireEvent.click(screen.getByTitle("Send it to the chat as it is")); // Do it on the card during the run: the chat opens, the suggestion waits there
    const box = await screen.findByRole("textbox", { name: "Message Cue" });
    await waitFor(() => expect(document.querySelectorAll(".me.queued")).toHaveLength(1));
    expect((box as HTMLTextAreaElement).disabled).toBe(false);
    for (const text of ["second", "third"]) {
      fireEvent.change(box, { target: { value: text } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      await screen.findByText(text);
    }
    expect(posted.filter((item) => item.url === "live/session/chat/messages")).toHaveLength(0); // nothing sent while the turn runs
    expect(document.querySelectorAll(".me.queued")).toHaveLength(3);
    fireEvent.click(within(screen.getByText("third").closest(".me")!).getByRole("button", { name: "remove" }));
    expect(screen.queryByText("third")).toBeNull();
    run.run.status = "completed"; // the turn ends: its stream says so, the run is reloaded, and the queued messages go out in order, one turn each (the stub's turns end at once)
    sources[0]!.onmessage!({ data: JSON.stringify({ id: "e9", runId: "r1", sequence: 9, type: "run_completed", level: "ok", message: "Run completed.", createdAt: at(0) }) });
    await waitFor(() => expect(posted.filter((item) => item.url === "live/session/chat/messages").map((item) => (item.body as { text: string }).text)).toEqual(["Register for NeurIPS 2026 before early-bird ends", "second"]));
    expect((posted.find((item) => item.url === "live/session/chat/messages")!.body as { proposal?: unknown }).proposal).toEqual({ proposalsTs: proposals[2]!.ts, choice: 0 }); // the acceptance travels with the first
    expect(document.querySelectorAll(".me.queued")).toHaveLength(0);
  });

  it("the sidebar's toggles pick the conversations or the timeline, a tree by session with the pinned on top; picking one continues it, a view-only one opens read-only, the ⋯ menus pin and rename", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const first = { ts: at(3), chatId: "c1", turn: 1, role: "user" as const, text: "Current discussion", runId: "current-run" };
    const talks: LiveChat = {
      ...chat,
      id: "c1",
      current: "c1",
      agent: "claude",
      turn: 1,
      messages: [first],
      conversations: [
        { id: "c1", agent: "claude", turns: 1, startedAt: at(10), lastAt: at(3), firstMessage: "Current discussion", sessions: ["20261104-120000"] },
        { id: "c0", agent: "codex", turns: 3, startedAt: at(90), lastAt: at(80), firstMessage: "Port the slides", sessions: ["20261104-120000"] },
        { id: "old", agent: "claude", turns: 2, startedAt: at(60 * 24 * 3), lastAt: at(60 * 24 * 3), firstMessage: "Reply to the reviewers", sessions: ["20261101-090000"], legacy: true, viewOnly: true },
        { id: "p1", agent: "claude", turns: 4, startedAt: at(60 * 24 * 5), lastAt: at(60 * 24 * 5), firstMessage: "Deck for the talk", sessions: ["20261101-090000"], pinned: true },
      ],
    };
    vi.stubGlobal("fetch", backend(live, posted, proposals, talks));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    expect([...container.querySelectorAll(".side-tabs button")].map((button) => [button.getAttribute("aria-label"), button.getAttribute("aria-pressed")])).toEqual([["Conversations", "true"], ["Timeline", "false"]]); // the conversations first, and to begin with
    expect(container.querySelector(".rail-shell")).toBeNull();
    expect([...container.querySelectorAll(".chatlist .grp")].map((group) => group.textContent)).toEqual(["Pinned", "Named", "Earlier"]); // no Current: the running session has no conversation yet
    expect([...container.querySelectorAll(".chatlist .folder .what")].map((name) => name.textContent)).toEqual(["Thesis morning", "Nov 1 09:00"]); // a folder per session: its name, else when it started
    const rows = () => [...container.querySelectorAll(".chatlist .thread")] as HTMLElement[];
    const pick = (index: number) => fireEvent.click(rows()[index]!.querySelector(".pick")!);
    expect(rows().map((row) => [row.className, row.querySelector(".what")?.textContent])).toEqual([["thread", "Deck for the talk"], ["thread on", "Current discussion"], ["thread", "Port the slides"]]); // the pinned one on top, the latest folder open, the older shut
    pick(2); // continue the Codex one: a switch, and the chat opens
    await waitFor(() => expect(posted).toEqual([{ url: "live/session/chat/switch", body: { id: "c0" } }]));
    expect(container.querySelector("#timelinev2-conversation")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Nov 1 09:00/ })); // the older folder opens
    pick(3); // the view-only one: read in place, nothing posted
    await screen.findByText(/view only/);
    expect((screen.getByRole("textbox", { name: "Message Cue" }) as HTMLTextAreaElement).disabled).toBe(true);
    expect(posted).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect((screen.getByRole("textbox", { name: "Message Cue" }) as HTMLTextAreaElement).disabled).toBe(false));
    fireEvent.click(within(rows()[2]!).getByRole("button", { name: "More" })); // the row's ⋯: the name is typed in place
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const box = screen.getByRole("textbox", { name: "Rename the conversation" }) as HTMLInputElement;
    expect(box.value).toBe("Port the slides");
    fireEvent.change(box, { target: { value: "Slides port" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(posted).toContainEqual({ url: "live/chats/c0", body: { title: "Slides port" } }));
    expect(screen.queryByRole("textbox", { name: "Rename the conversation" })).toBeNull();
    fireEvent.click(within(rows()[2]!).getByRole("button", { name: "More" })); // and pins
    fireEvent.click(screen.getByRole("menuitem", { name: "Pin" }));
    await waitFor(() => expect(posted).toContainEqual({ url: "live/chats/c0", body: { pinned: true } }));
    expect(screen.queryByRole("menu")).toBeNull();
    const folders = () => [...container.querySelectorAll(".chatlist .folder")] as HTMLElement[];
    fireEvent.click(within(folders()[1]!).getByRole("button", { name: "More" })); // a folder's ⋯ renames the session
    fireEvent.click(screen.getByRole("menuitem", { name: "Rename" }));
    const name = screen.getByRole("textbox", { name: "Rename the session" }) as HTMLInputElement;
    fireEvent.change(name, { target: { value: "Review week" } });
    fireEvent.keyDown(name, { key: "Enter" });
    await waitFor(() => expect(posted).toContainEqual({ url: "live/sessions/20261101-090000", body: { name: "Review week" } }));
    fireEvent.click(screen.getByRole("button", { name: "Timeline" }));
    expect(container.querySelector(".side .rail-shell")).toBeTruthy();
    expect(window.localStorage.getItem("v1.side")).toBe("timeline");
  });

  it("asleep: the sidebar still lists the conversations and opens one to read, nothing to continue it in; Home unloads it", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const talks: LiveChat = {
      ...chat,
      id: null,
      current: null,
      messages: [{ ts: at(80), chatId: "c0", turn: 1, role: "user", text: "Port the slides", runId: "run-c0" }],
      conversations: [
        { id: "c0", agent: "codex", turns: 3, startedAt: at(90), lastAt: at(80), firstMessage: "Port the slides", sessions: ["20261104-120000"] },
        { id: "old", agent: "claude", turns: 2, startedAt: at(60 * 24 * 3), lastAt: at(60 * 24 * 3), firstMessage: "Reply to the reviewers", sessions: ["20261101-090000"], legacy: true, viewOnly: true },
      ],
    };
    vi.stubGlobal("fetch", backend(idle, posted, proposals, talks));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Cue is asleep");
    fireEvent.click(screen.getByRole("button", { name: "Conversations" }));
    const rows = await waitFor(() => { const found = [...container.querySelectorAll(".chatlist .thread")] as HTMLElement[]; expect(found).toHaveLength(1); return found; }); // the latest folder open
    expect(screen.queryByRole("button", { name: "New conversation" })).toBeNull();
    fireEvent.click(rows[0]!.querySelector(".pick")!); // a continuable one too: with no session, it opens to read
    await screen.findByText(/No session · view only/);
    expect(screen.queryByText("Cue is asleep")).toBeNull();
    expect(container.querySelector(".execution .screenpane")).toBeTruthy(); // the same chat layout: the monitor pane stays, with nothing live in it
    expect(screen.getByText("Nothing to show yet")).toBeTruthy();
    expect((screen.getByRole("textbox", { name: "Message Cue" }) as HTMLTextAreaElement).disabled).toBe(true);
    expect(posted).toEqual([]);
    fireEvent.click(screen.getByRole("button", { name: "Home" })); // the header's Home, as with a session: back to the Start card
    await screen.findByText("Cue is asleep");
  });

  it("the running session's folder is the Current group whatever its date, and opens by default", async () => {
    const now = new Date(), today = `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}`;
    const talks: LiveChat = {
      ...chat,
      conversations: [
        { id: "c9", agent: "codex", turns: 1, startedAt: at(5), lastAt: at(5), firstMessage: "Still going", sessions: ["20261007-135300"] }, // the fixture's running session, started weeks ago
        { id: "c8", agent: "claude", turns: 1, startedAt: at(60), lastAt: at(60), firstMessage: "Earlier today", sessions: [`${today}-080000`] },
      ],
    };
    vi.stubGlobal("fetch", backend(live, [], proposals, talks));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    fireEvent.click(screen.getByRole("button", { name: "Conversations" }));
    expect([...container.querySelectorAll(".chatlist .grp")].map((group) => group.textContent)).toEqual(["Current", "Earlier"]);
    const folders = [...container.querySelectorAll(".chatlist .folder")] as HTMLElement[];
    expect(folders.map((folder) => [folder.querySelector(".what")?.textContent, Boolean(folder.querySelector(".run")), folder.className])).toEqual([["Oct 7 13:53", true, "folder open"], ["Today 08:00", false, "folder"]]); // the running one first, labelled by its own start, dotted and open; today's stopped one under Earlier, shut
  });

  it("opens Why beside an existing conversation without losing the draft", async () => {
    onTheTimeline();
    vi.stubGlobal("fetch", backend(live));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Chat" }));
    const composer = screen.getByRole("textbox", { name: "Message Cue" });
    fireEvent.change(composer, { target: { value: "Keep my conversation draft." } });
    await waitFor(() => expect(container.querySelector(".rail .tick.answer")).toBeTruthy());
    fireEvent.click(container.querySelector(".rail .tick.answer")!);
    fireEvent.click(container.querySelector(".detail .ideas .link")!);
    expect(container.querySelector(".why-panel")).toBeTruthy();
    expect(container.querySelector(".execution")).toBeTruthy();
    expect((composer as HTMLTextAreaElement).value).toBe("Keep my conversation draft.");
    fireEvent.click(screen.getByRole("button", { name: "Close Why panel" }));
    expect(container.querySelector(".why-panel")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Message Cue" })).toBe(composer);
  });

  it("Accept comes apart into Do it and Edit first, and either way the message carries the suggestion it sends", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const set = proposals[2]!.ts; // the front card's set
    vi.stubGlobal("fetch", backend(live, posted));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    expect(container.querySelector(".accept .face")?.textContent).toBe("→ Accept"); // what shows until the pointer arrives
    fireEvent.click(screen.getByRole("button", { name: "→ Do it" }));
    await waitFor(() => expect(posted.map((item) => item.url)).toEqual(["live/session/proposals/feedback", "live/session/chat/messages"]));
    expect(posted[0]!.body).toEqual({ proposalsTs: set, decision: "accept", choice: 0 });
    expect(posted[1]!.body).toMatchObject({ text: "Register for NeurIPS 2026 before early-bird ends", proposal: { proposalsTs: set, choice: 0 } });
    expect(container.querySelector(".execution")).toBeTruthy(); // and the conversation opened on it
    fireEvent.click(screen.getByRole("button", { name: "Home" }));
    // Edit first: the conversation opens with the text in the chat box; what is sent from there carries the reference too, so the change is recorded.
    fireEvent.click(screen.getByRole("button", { name: "Edit first" }));
    const composer = await screen.findByRole("textbox", { name: "Message Cue" });
    await waitFor(() => expect((composer as HTMLTextAreaElement).value).toBe("Register for NeurIPS 2026 before early-bird ends"));
    fireEvent.change(composer, { target: { value: "Register for NeurIPS 2026 today, the student rate" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(posted.at(-1)).toMatchObject({ url: "live/session/chat/messages", body: { text: "Register for NeurIPS 2026 today, the student rate", proposal: { proposalsTs: set, choice: 0 } } }));
  });

  it("the Tune menu's sliders set how often to suggest and how strongly to filter repeats", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const tuned = { ...settings, proposerDedup: true };
    vi.stubGlobal("fetch", backend({ ...live, defaults: tuned, session: { ...live.session!, settings: tuned } }, posted));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Tune" }));
    const often = screen.getByRole("slider", { name: "Frequency regular" }) as HTMLInputElement;
    expect(often.value).toBe("2"); // 180 s: the third of 600, 300, 180, 90
    const tunes = () => posted.filter((item) => item.url === "live/session/tune").map((item) => item.body);
    fireEvent.pointerUp(often); // let go where it already is: nothing to send
    fireEvent.change(often, { target: { value: "1" } }); // a drag crosses notches: nothing is sent until the knob is let go
    expect(tunes()).toEqual([]);
    fireEvent.change(often, { target: { value: "0" } });
    fireEvent.pointerUp(often);
    await waitFor(() => expect(tunes()).toEqual([{ proposerIntervalS: 600, proposerIdleCooldownS: 120 }]));
    const filter = screen.getByRole("slider", { name: "Uniqueness medium" }) as HTMLInputElement;
    expect(filter.value).toBe("1"); // 0.8: the second of 0.9, 0.8, 0.65
    fireEvent.change(filter, { target: { value: "2" } });
    fireEvent.keyUp(filter, { key: "ArrowRight" });
    await waitFor(() => expect(tunes()).toEqual([{ proposerIntervalS: 600, proposerIdleCooldownS: 120 }, { proposerDedupThreshold: 0.65 }]));
  });

  it("a value set by hand reads custom at the nearest notch, and a move goes to a level", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const custom = { ...settings, proposerDedup: true, proposerIntervalS: 260, proposerDedupThreshold: 0.75 };
    vi.stubGlobal("fetch", backend({ ...live, defaults: custom, session: { ...live.session!, settings: custom } }, posted));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Tune" }));
    const often = screen.getByRole("slider", { name: "Frequency custom" }) as HTMLInputElement;
    expect(often.value).toBe("1"); // 260 s sits nearest 300
    expect((screen.getByRole("slider", { name: "Uniqueness custom" }) as HTMLInputElement).value).toBe("1"); // 0.75 nearest 0.8
    fireEvent.change(often, { target: { value: "0" } }); // the knob already sits on 300: only a move to another level is a change
    fireEvent.pointerUp(often);
    await waitFor(() => expect(posted.filter((item) => item.url === "live/session/tune").map((item) => item.body)).toEqual([{ proposerIntervalS: 600, proposerIdleCooldownS: 120 }]));
  });

  it("offers no uniqueness slider while the repeat filter is off", async () => {
    vi.stubGlobal("fetch", backend(live));
    render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Tune" }));
    expect(screen.getByRole("slider", { name: "Frequency regular" })).toBeTruthy();
    expect(screen.queryByRole("slider", { name: /Uniqueness/ })).toBeNull();
  });

  it("in the chat, suggestions sit in the log at their time: the newest waiting one as a card, earlier ones folded, answered ones as receipts, and Do it sends from there", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const earlier = { ts: at(13), proposals: [{ kind: "act" as const, task: "Book the Sydney flights" }] };
    const older = { ts: at(14), proposals: [{ kind: "act" as const, task: "Renew the passport" }] };
    const laterDone = { ts: at(6), proposals: [{ kind: "act" as const, task: "Email the venue" }], feedback: { decision: "reject" as const, ts: at(5), rejectReason: "already done" } }; // answered after the open card arrived
    vi.stubGlobal("fetch", backend(live, posted, [...proposals, earlier, older, laterDone]));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(container.querySelector(".proposal-preview")).toBeNull(); // no strip over the conversation any more
    const log = () => within(container.querySelector(".log")!);
    const items = () => [...container.querySelectorAll(".log .fold, .log .rc, .log .sg")].map((item) => item.className.split(" ")[0]);
    expect(items()).toEqual(["fold", "rc", "rc", "sg"]); // the two folded, then the receipts, then the open card last, though one receipt is newer than it
    expect(container.querySelector(".log .rc")?.textContent).toContain("✘ rejected (not now)");
    expect(container.querySelector(".log .sg h4")?.textContent).toBe("Register for NeurIPS 2026 before early-bird ends");
    expect(container.querySelector(".log .sg .agentMenu")).toBeNull(); // the conversation's agent and model apply; the picker is the Home card's
    fireEvent.click(log().getByRole("button", { name: /2 earlier suggestions/ })); // show: one row each
    fireEvent.click(log().getByRole("button", { name: /Book the Sydney flights/ })); // a row opens into its card
    expect(items()).toEqual(["fold", "rc", "sg", "rc", "rc", "sg"]);
    fireEvent.click(log().getByRole("button", { name: /1 earlier suggestion/ })); // the line counts what is still folded; hide: the original view, the opened card folded again
    expect(items()).toEqual(["fold", "rc", "rc", "sg"]);
    expect(container.querySelector(".log .fold")?.textContent).toContain("show");
    expect(container.querySelector(".log .sg .proposal-details")).toBeNull(); // details are off by default
    fireEvent.click(log().getByRole("button", { name: "+ Details" }));
    expect(container.querySelector(".log .sg .description")?.textContent).toBe("Registration opened today.");
    expect(container.querySelectorAll(".log .sg .evidence-chips .ctx").length).toBeGreaterThan(0);
    expect(container.querySelector(".log .sg .alts li b")?.textContent).toBe("Reply to Wei"); // the other idea, with its own buttons
    fireEvent.click(log().getByRole("button", { name: "− Less" }));
    expect(container.querySelector(".log .sg .proposal-details")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "→ Do it" })); // the newest card's
    await waitFor(() => expect(posted.map((item) => item.url)).toEqual(["live/session/proposals/feedback", "live/session/chat/messages"]));
    expect(posted[1]!.body).toMatchObject({ text: "Register for NeurIPS 2026 before early-bird ends" });
    expect(container.querySelector("#timelinev2-conversation")).toBeTruthy(); // and the chat stayed open
  });

  it("once a message follows them, the waiting suggestions before it all fold; only the stretch after the last message holds the open card", async () => {
    const earlier = { ts: at(13), proposals: [{ kind: "act" as const, task: "Book the Sydney flights" }] };
    const spoken = { ...chat, messages: [{ ts: at(5), chatId: "c1", turn: 1, role: "user" as const, text: "go on with the slides", runId: "r9" }] };
    vi.stubGlobal("fetch", backend(live, [], [...proposals, earlier], spoken));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    await screen.findByText("go on with the slides");
    const items = () => [...container.querySelectorAll(".log .fold, .log .rc, .log .sg, .log .me")].map((item) => item.className.split(" ")[0]);
    expect(items()).toEqual(["fold", "rc", "me"]); // the newest waiting one is before the message, so it folds with the other
    expect(container.querySelector(".log .fold")?.textContent).toContain("2 earlier suggestions");
  });

  it("a rail tick of a waiting suggestion keeps the chat open and shows its card there", async () => {
    onTheTimeline();
    const earlier = { ts: at(13), proposals: [{ kind: "act" as const, task: "Book the Sydney flights" }] };
    vi.stubGlobal("fetch", backend(live, [], [...proposals, earlier]));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    await waitFor(() => expect(container.querySelector('.rail .tick.suggest[title$="Book the Sydney flights"]')).toBeTruthy());
    fireEvent.click(container.querySelector('.rail .tick.suggest[title$="Book the Sydney flights"]')!);
    expect(container.querySelector("#timelinev2-conversation")).toBeTruthy();
    expect([...container.querySelectorAll(".log .sg h4")].map((item) => item.textContent)).toEqual(["Book the Sydney flights", "Register for NeurIPS 2026 before early-bird ends"]);
  });

  it("paused: the curtain holds the page and the suggestion cannot be accepted", async () => {
    const paused = { ...live, session: { ...live.session!, paused: true, pausedAt: at(3) } };
    vi.stubGlobal("fetch", backend(paused));
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Session paused");
    expect(container.querySelector(".curtain .b.primary")?.textContent).toBe("Resume session");
    expect([...container.querySelectorAll("header.top .status + .ctl button")].map((button) => [button.textContent, button.getAttribute("title")])).toEqual([["Resume", null], ["Stop", null]]); // named by their text, no tooltip
    expect(screen.getByRole("button", { name: "→ Do it" }).hasAttribute("disabled")).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Chat" })); // the conversation stays readable: only its box is held, and says why
    expect(container.querySelector(".curtain")).toBeNull();
    const box = screen.getByRole("textbox", { name: "Message Cue" }) as HTMLTextAreaElement;
    expect([box.disabled, box.placeholder]).toEqual([true, "Session paused. Resume it to message Cue."]);
    fireEvent.click(screen.getByRole("button", { name: "Home" }));
    expect(container.querySelector(".curtain")).toBeTruthy();
    expect(container.querySelector("header.top a.b")).toBeNull(); // on the home view itself the bar offers no Home
  });

  it("a first visit, with no session ever recorded, is handed to the quickstart; once that was closed here, the landing page stays", async () => {
    vi.stubGlobal("fetch", backend(idle)); // its session list is empty
    const first = render(<Landing expectedBackend="python" runnerBaseUrl="http://runner" />);
    await waitFor(() => expect(router.replace).toHaveBeenCalledWith("/v1/settings?quickstart=1"));
    first.unmount();
    router.replace.mockClear();
    window.localStorage.setItem("v1.quickstart", "1");
    render(<Landing expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByRole("button", { name: "Start" });
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("the release shows Timeline v2 whatever theme was chosen before, with no theme switch; the dev server keeps both", async () => {
    vi.stubGlobal("fetch", backend(live));
    window.localStorage.setItem("v1.theme", "timeline");
    const dev = render(<Landing expectedBackend="python" runnerBaseUrl="http://runner" />);
    await waitFor(() => expect(dev.container.querySelector(".v1.timeline")).toBeTruthy()); // the theme this browser remembers
    expect(dev.container.querySelector(".themeSwitch")).toBeTruthy();
    dev.unmount();
    vi.stubEnv("NODE_ENV", "production");
    try {
      const { container } = render(<Landing expectedBackend="python" runnerBaseUrl="http://runner" />);
      await waitFor(() => expect(container.querySelector("header.top .status")?.textContent).toMatch(/^Watching your screen/));
      expect(container.querySelector(".v1.timelinev2")).toBeTruthy();
      expect(container.querySelector(".themeSwitch")).toBeNull();
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("a message on its way shows the bar a running turn shows, counting from the send", async () => {
    const fallback = backend(live);
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input).endsWith("/api/live/session/chat/messages") ? new Promise<Response>(() => undefined) : fallback(input, init))); // the send is never answered
    const { container } = render(<Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" />);
    fireEvent.click(await screen.findByRole("button", { name: "Chat" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Message Cue" }), { target: { value: "Check the deadline." } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(container.querySelector(".log .ai:last-of-type .turn.running .turn-fold")?.textContent).toBe("Working · 0s"));
    expect([...container.querySelectorAll(".log .me")].at(-1)?.textContent).toBe("Check the deadline.");
    expect(container.querySelector(".log .busy")).toBeNull();
  });

  it("another page under the same layout starts from the session already known: its top bar does not reset", async () => {
    vi.stubGlobal("fetch", backend(live));
    const view = render(<SessionProvider runnerBaseUrl="http://runner"><Timeline2 expectedBackend="python" runnerBaseUrl="http://runner" /></SessionProvider>);
    await waitFor(() => expect(view.container.querySelector("header.top .status")?.textContent).toMatch(/^Watching your screen/));
    const bar = view.container.querySelector("header.top .status")!.textContent;
    view.rerender(<SessionProvider runnerBaseUrl="http://runner"><Settings expectedBackend="python" runnerBaseUrl="http://runner" /></SessionProvider>);
    expect(view.container.querySelector("header.top .status")?.textContent).toBe(bar); // at once, with nothing fetched again
    expect([...view.container.querySelectorAll("header.top .ctl button")].map((button) => button.textContent)).toEqual(["Pause", "Stop"]);
  });
});
