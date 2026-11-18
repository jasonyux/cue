import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { LiveSessionStatus, LiveSettings } from "@cue/contracts";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pickTheme } from "../components/v1/theme";
import { Timeline } from "../components/v1/Timeline";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));

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
const WARNING = 'Warning, not applied: "Revise the introduction" is close to entry 2 of t1 "Send the draft" (open since 2026-10-01).';
const changes = [{ ts: at(6), windowStart: at(10), windowEnd: at(6), ops: [{ op: "update" as const, taskId: "t1", steps: [{ text: "Revise the introduction" }] }, { op: "update" as const, taskId: "t1", reword: [{ step: 2, text: "Send the camera-ready draft" }] }],
  callId: "c1", rounds: 2, warned: [WARNING], calls: [ // the tick took two replies: the add held by the lookalike check, then the same add and a reword applied
    { round: 1, tool: "update_task_checklist", op: { op: "update" as const, taskId: "t1", steps: [{ text: "Revise the introduction" }] }, applied: false, result: WARNING },
    { round: 2, tool: "update_task_checklist", op: { op: "update" as const, taskId: "t1", steps: [{ text: "Revise the introduction" }] }, applied: true, result: "Accepted." },
    { round: 2, tool: "update_task_checklist", op: { op: "update" as const, taskId: "t1", reword: [{ step: 2, text: "Send the camera-ready draft" }] }, applied: true, result: "Accepted." },
  ] }];
const proposals = [
  { ts: at(15), trigger: "idle" as const, proposals: [] }, // a pause that found nothing
  { ts: at(11), proposals: [{ kind: "act" as const, task: "Reply to Wei" }], feedback: { decision: "reject" as const, ts: at(10.5), rejectReason: "not now" } },
  { id: "p3", ts: at(8), windowStart: at(11), windowEnd: at(8), proposals: [{ kind: "act" as const, task: "Register for NeurIPS 2026 before early-bird ends", reason: "Registration opened today.", evidence: ["n1", "h9", "t1", "zz"] }, { kind: "lookup" as const, task: "Reply to Wei" }],
    cited: [{ id: "h9", ts: "2026-08-10T09:00:00Z", kind: "summary_written", sessionId: "old", text: "Booked the Sydney hotel.", line: "[h9] [2026-08-10 09:00] worklog: Booked the Sydney hotel." }] },
];
const chat = { id: "c1", current: "c1", agent: null, turn: 0, runningRunId: null, messages: [], pending: [], conversations: [] };
const usage = { all: [], recent: [], byDay: [], days: 1, session: [] };

function backend(status: LiveSessionStatus, posted: { url: string; body: unknown }[] = []) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST") { posted.push({ url: url.split("/api/")[1] ?? url, body: JSON.parse(String(init.body ?? "{}")) }); }
    const body = url.includes("/api/live/tasks") ? { tasks, trash: [] } : url.includes("/api/live/session/proposals") ? proposals : url.includes("/api/live/session/chat") ? chat
      : url.includes("/api/live/session/changes") ? changes : url.includes("/api/live/session/summaries") ? notes : url.includes("/api/live/session/screenshots") ? shots
      : url.includes("/api/live/session/permissions") ? { ok: true } : url.includes("/api/live/sessions") ? [] : url.includes("/api/live/usage") ? usage : url.includes("/api/live/session") ? status : null;
    return { ok: body !== null, status: body === null ? 404 : 200, json: async () => body ?? { error: `no stub for ${url}` } } as Response;
  });
}

describe("the Timeline theme", () => {
  beforeEach(() => { vi.stubGlobal("EventSource", vi.fn(() => ({ close: vi.fn() }))); window.localStorage.clear(); });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("is a theme of the landing page", () => {
    expect(pickTheme("timeline", null)).toBe("timeline");
  });

  it("sleeps and offers to start when there is no session, with the tasks still in their tray", async () => {
    vi.stubGlobal("fetch", backend(idle));
    render(<Timeline expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Cue is asleep");
    expect(screen.getByRole("button", { name: "▶ Start session" }).hasAttribute("disabled")).toBe(false);
    expect(screen.getAllByText("ICLR 2027 paper").length).toBeGreaterThan(0);
    expect(document.querySelector(".v1.timeline")?.getAttribute("data-state")).toBe("off");
  });

  it("during a session, the spine carries every record and the suggestion waits in front; a mark opens its record and what it was made from", async () => {
    vi.stubGlobal("fetch", backend(live));
    const { container } = render(<Timeline expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    expect(container.querySelector(".v1.timeline")?.getAttribute("data-state")).toBe("noticed");
    await waitFor(() => expect(container.querySelector(".spine .notebar")).toBeTruthy()); // the stored records, half a minute behind the polled ones
    expect(container.querySelector(".spine .flag.pending")?.textContent).toBe("2"); // the waiting suggestion, with its count of ideas
    expect(container.querySelector(".spine .flag.answered")).toBeTruthy(); // the declined one
    expect(container.querySelector(".spine .dc")?.textContent).toBe("✘");
    expect(container.querySelector(".spine .pc")).toBeTruthy(); // the pause that found nothing
    expect(container.querySelector(".spine .dia")).toBeTruthy(); // the task change
    expect((container.querySelector(".spine .shot") as HTMLImageElement).src).toBe("http://runner/api/live/session/screenshots/shot-1.png");
    expect(container.querySelectorAll(".spine .gap").length).toBeGreaterThan(0); // the minutes with nothing recorded, folded
    expect(container.querySelector(".spine .nowline")?.textContent).toMatch(/^now /);
    expect(container.querySelector(".nav span:not(.seg)")?.textContent).toBe("1/1"); // one thing waits: nothing to flip through
    expect(screen.getByRole("button", { name: "permissions" })).toHaveProperty("disabled", true); // none wait
    expect(container.querySelector(".deck.n0")).toBeTruthy(); // no edge beneath the card
    expect(screen.getByRole("button", { name: "Do it" }).hasAttribute("disabled")).toBe(false);
    expect(screen.getByRole("button", { name: "1 more idea" })).toBeTruthy();
    expect(screen.queryByText("Nothing needs you")).toBeNull(); // a card is up
    expect(container.querySelector(".pop .kind")?.textContent).toBe("on schedule"); // the periodic proposer made it
    expect(screen.getByRole("button", { name: "answered" })).toHaveProperty("disabled", false); // the declined one is there
    fireEvent.click(container.querySelector(".spine .notebar")!);
    expect(screen.getByText("Edited prompts.py in VS Code.")).toBeTruthy(); // the note, in the drawer
    expect(container.querySelector(".drawer.open .shots img")).toBeTruthy(); // the look it was made from
    fireEvent.click(container.querySelector(".spine .dia")!);
    expect(screen.getByText("added Revise the introduction to ICLR 2027 paper")).toBeTruthy(); // the task change, and the notes in its window
    expect(screen.getByText("reworded entry 2 of ICLR 2027 paper to Send the camera-ready draft")).toBeTruthy(); // a reword, in words
    expect(container.querySelector(".drawer.open time")?.textContent).toContain("2 rounds");
    expect(screen.getByText("✗ added Revise the introduction to ICLR 2027 paper").querySelector("p")?.textContent).toBe(WARNING); // round 1: held, with what the model read
    expect(screen.getByText("Round 2").nextElementSibling?.textContent).toBe("✓ added Revise the introduction to ICLR 2027 paper✓ reworded entry 2 of ICLR 2027 paper to Send the camera-ready draft");
    expect(container.querySelector(".drawer.open .src button")?.textContent).toContain("Edited prompts.py");
    fireEvent.click(screen.getByRole("button", { name: "1 more idea" })); // every proposal has its own why?
    fireEvent.click(screen.getAllByRole("button", { name: "why?" })[1]!);
    expect(container.querySelector(".drawer.open .big")?.textContent).toContain("Reply to Wei"); // the second proposal, alone
    expect(container.querySelector(".drawer.open")?.textContent).not.toContain("Register for NeurIPS");
    expect(container.querySelector(".drawer.open .chips")).toBeNull(); // it cites nothing
    expect(container.querySelectorAll(".spine .links line")).toHaveLength(0);
    fireEvent.click(screen.getAllByRole("button", { name: "why?" })[0]!);
    expect(container.querySelector(".drawer.open .big")?.textContent).toContain("Register for NeurIPS"); // the first, with what it rests on as chips
    expect(container.querySelector(".drawer.open")?.textContent).not.toContain("Reply to Wei");
    const chips = [...container.querySelectorAll(".drawer.open .chip")];
    expect(chips).toHaveLength(4);
    expect(chips[0]!.tagName).toBe("BUTTON"); // this session's note: its time on the chip, the text on hover
    expect(chips[0]!.getAttribute("title")).toBe("Edited prompts.py in VS Code.");
    expect(chips[1]!.getAttribute("href")).toBe("/v1/notes?session=old&id=h9"); // a search result from another session: a link to it on the Notes page
    expect(chips[1]!.getAttribute("title")).toBe("Booked the Sydney hotel.");
    expect(chips[1]!.textContent).toContain("Aug 10");
    expect(chips[2]!.textContent).toContain("ICLR 2027 paper"); // a task, by its title
    expect(chips[3]!.getAttribute("title")).toBe("zz"); // an id nothing here knows
    expect(container.querySelector(".spine .notebar.cited")).toBeTruthy(); // the cited note is ringed while the suggestion is selected,
    expect(container.querySelectorAll(".spine .links line")).toHaveLength(1); // and a dashed line runs from the suggestion to it
    fireEvent.click(chips[0]!);
    expect(container.querySelector(".drawer.open h2")?.textContent).toBe("Note"); // the chip opens the note's record
    expect(container.querySelector(".spine .notebar.cited")).toBeNull();
    expect(container.querySelectorAll(".spine .links line")).toHaveLength(0);
  });

  it("while paused, the spine says so and stops growing", async () => {
    vi.stubGlobal("fetch", backend({ ...live, session: { ...live.session!, paused: true, pausedAt: at(3) } }));
    const { container } = render(<Timeline expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("⏸ Paused");
    expect(container.querySelector(".spine .nowline")?.textContent).toMatch(/^paused /);
    expect([...container.querySelectorAll(".spine .gap span")].some((span) => span.textContent?.startsWith("paused"))).toBe(true);
    expect(container.querySelector(".stage.paused .pausecard")).toBeTruthy(); // the paused card, over the suggestion that still waits
    expect(screen.getByRole("button", { name: "Do it" }).hasAttribute("disabled")).toBe(true);
    expect(container.querySelector(".pausecard button")?.hasAttribute("disabled")).toBe(false); // its own Resume, beside the header's
  });

  it("a permission question comes in front of a suggestion; ‹ › flip through them and latest comes back; an answer is posted", async () => {
    const asked = { ...live, session: { ...live.session!, permissionAsks: [{ app: "Slack", bundle: "com.tinyspeck.slackmacgap", ts: at(2) }] } };
    const posted: { url: string; body: unknown }[] = [];
    const before = backend(asked, posted), after = backend({ ...live, session: { ...live.session!, latestSnapshot: { ts: at(0), app: "Slack", window: "general" } } }, posted);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => (posted.some((call) => call.url.endsWith("permissions")) ? after : before)(input, init)));
    const { container } = render(<Timeline expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Cue wants to look at Slack");
    expect(container.querySelector(".v1.timeline")?.getAttribute("data-state")).toBe("needs");
    expect(container.querySelector(".spine .askm.pending")).toBeTruthy();
    expect(container.querySelector(".pop time")?.textContent).toBe("2m ago");
    expect(container.querySelector(".nav span:not(.seg)")?.textContent).toBe("2/2"); // the suggestion waits behind the question
    expect(container.querySelector(".deck.n1")).toBeTruthy(); // its edge shows beneath the card
    expect(screen.getByRole("button", { name: "Newer" })).toHaveProperty("disabled", true);
    expect(screen.getByRole("button", { name: "latest" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "Older" }));
    expect(screen.getByRole("button", { name: "Do it" })).toBeTruthy(); // the suggestion, in front
    expect(container.querySelector(".nav span:not(.seg)")?.textContent).toBe("1/2");
    expect(screen.getByRole("button", { name: "Older" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "latest" }));
    expect(screen.queryByRole("button", { name: "Do it" })).toBeNull();
    expect(container.querySelector(".pop.ask")).toBeTruthy(); // a permission question's card
    fireEvent.click(screen.getByRole("button", { name: "suggestions" })); // the filter: only the suggestions
    expect(screen.getByRole("button", { name: "Do it" })).toBeTruthy();
    expect(container.querySelector(".nav span:not(.seg)")?.textContent).toBe("1/1");
    expect(container.querySelector(".deck.n0")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "permissions" }));
    expect(container.querySelector(".pop.ask")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "all" }));
    expect(container.querySelector(".nav span:not(.seg)")?.textContent).toBe("2/2");
    fireEvent.click(screen.getByRole("button", { name: "Look" }));
    await waitFor(() => expect(posted.find((call) => call.url.endsWith("permissions"))?.body).toEqual({ app: "Slack", level: "look" }));
    await waitFor(() => expect(screen.queryByText("Cue wants to look at Slack")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "answered" })); // the answered pile: the question, with its answer
    expect(container.querySelector(".pop.ask h4")?.textContent).toBe("Cue wants to look at Slack");
    expect(container.querySelector(".pop.ask .ans")?.textContent).toBe("Look");
    fireEvent.click(screen.getByRole("button", { name: "all" }));
    expect(screen.getByRole("button", { name: "Do it" })).toBeTruthy();
  });

  it("the task cards tick an entry through the backend, and an entry's time tag opens the update that added it", async () => {
    const posted: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", backend(live, posted));
    const { container } = render(<Timeline expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Register for NeurIPS 2026 before early-bird ends");
    fireEvent.click(screen.getByRole("checkbox", { name: "Done: Revise the introduction" }));
    await waitFor(() => expect(posted.find((call) => call.url === "live/tasks")?.body).toEqual({ ops: [{ op: "status", taskId: "t1", step: 1, done: true }] }));
    expect(screen.getByText("1 done")).toBeTruthy(); // the finished entry, folded
    await waitFor(() => expect(container.querySelector(".kc .prov")).toBeTruthy()); // the tag names the update that added the entry, once the changes are in
    fireEvent.click(container.querySelector(".kc .prov")!);
    expect(screen.getByText("added Revise the introduction to ICLR 2027 paper")).toBeTruthy();
    expect(container.querySelector(".spine .dia.sel")).toBeTruthy(); // and lights it on the spine
    expect(container.querySelector(".tc.done")).toBeNull(); // the done task waits behind the row's count
    fireEvent.click(screen.getByRole("button", { name: /Show done/ }));
    expect(container.querySelector(".tc.done .ttl")?.textContent).toBe("ICLR review");
    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(posted.filter((call) => call.url === "live/tasks").at(-1)?.body).toEqual({ ops: [{ op: "status", taskId: "t3", done: false }] }));
  });
});
