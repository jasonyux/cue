import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { LiveSettings } from "@cue/contracts";
import { afterEach, describe, expect, it, vi } from "vitest";

import { History } from "../components/v1/History";

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
const stored = Array.from({ length: 45 }, (_, index) => { // newest first, as the backend lists them; every fifth one named
  const day = String(45 - index).padStart(2, "0");
  return { id: `202608${day}-090000`, folder: `/data/sessions/202608${day}-090000`, startedAt: `2026-08-${day}T09:00:00Z`, stoppedAt: `2026-08-${day}T10:00:00Z`,
           name: index % 5 ? null : `paper ${index}`, settings, runs: [] as string[], chatRuns: [] as string[], summaryCount: 0, proposalCount: 0 };
});
const chat = { id: "c1", current: "c1", agent: null, turn: 0, runningRunId: null, messages: [] as object[], pending: [], conversations: [] as object[] };

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }), useSearchParams: () => new URLSearchParams() }));
vi.mock("../components/v1/useSession", () => ({ useSession: () => ({ status: { active: false }, session: null, busy: false }) })); // nothing running
vi.mock("../components/v1/records", () => ({ useRecords: () => ({ records: { summaries: [], proposalSets: [], changes: [], screenshots: [], chat }, issue: null, reload: vi.fn(), screenshotUrl: (name: string) => name }) }));

function listing() { // the backend's page: limit from offset, the named filter applied first; what each request asked is kept
  const asked: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const { pathname, searchParams: query } = new URL(String(input));
    if (pathname !== "/api/live/sessions") return { ok: true, status: 200, json: async () => [] } as unknown as Response; // the selected one's usage
    asked.push(`${query.get("offset")}${query.get("named") === null ? "" : ` named=${query.get("named")}`}`);
    const rows = stored.filter((item) => query.get("named") === null || Boolean(item.name) === (query.get("named") === "true"));
    return { ok: true, status: 200, json: async () => rows.slice(Number(query.get("offset")), Number(query.get("offset")) + Number(query.get("limit"))) } as unknown as Response;
  }));
  return asked;
}
const rows = (container: HTMLElement) => container.querySelectorAll(".sess").length;
const groups = (container: HTMLElement) => Array.from(container.querySelectorAll(".grp")).map((group) => group.textContent);

describe("the History page", () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.localStorage.clear(); });

  it("turns pages of 20 with ‹ ›, the named sessions first", async () => {
    const asked = listing();
    const { container } = render(<History expectedBackend="python" runnerBaseUrl="http://runner" />);
    expect([container.querySelectorAll(".sessions .sess.loading").length, container.querySelectorAll(".detail .loading .skel").length > 0]).toEqual([8, true]); // the list and the session in outline until they are in
    await waitFor(() => expect(rows(container)).toBe(20));
    expect(container.querySelector(".sessions .skel")).toBeNull();
    expect(groups(container)).toEqual(["Named 4", "Recent 16"]);
    const newer = screen.getByRole("button", { name: "Newer" }), older = screen.getByRole("button", { name: "Older" });
    expect(newer.hasAttribute("disabled")).toBe(true); // the first page
    fireEvent.click(older);
    await waitFor(() => expect(asked).toEqual(["0", "20"]));
    await waitFor(() => expect(rows(container)).toBe(20));
    expect(newer.hasAttribute("disabled")).toBe(false);
    fireEvent.click(older);
    await waitFor(() => expect(rows(container)).toBe(5)); // the last page
    expect(older.hasAttribute("disabled")).toBe(true);
    fireEvent.click(newer);
    await waitFor(() => expect(asked).toEqual(["0", "20", "40", "20"]));
  });

  it("filters to the named or the recent sessions alone, each paged on its own", async () => {
    const asked = listing();
    const { container } = render(<History expectedBackend="python" runnerBaseUrl="http://runner" />);
    await waitFor(() => expect(rows(container)).toBe(20));
    fireEvent.click(screen.getByRole("button", { name: "Named" }));
    await waitFor(() => expect(rows(container)).toBe(9));
    expect(groups(container)).toEqual([]); // one kind: no group headings
    expect(screen.getByRole("button", { name: "Older" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Older" })); // nothing: disabled
    fireEvent.click(screen.getByRole("button", { name: "Recent" }));
    await waitFor(() => expect(asked).toEqual(["0", "0 named=true", "0 named=false"]));
    await waitFor(() => expect(rows(container)).toBe(20));
    fireEvent.click(screen.getByRole("button", { name: "Older" }));
    await waitFor(() => expect(rows(container)).toBe(16)); // 36 unnamed: 20 and 16
    expect(asked.at(-1)).toBe("20 named=false");
  });

  it("lets the list's edge be dragged within its bounds and keeps the width", async () => {
    listing();
    const { container } = render(<History expectedBackend="python" runnerBaseUrl="http://runner" />);
    await waitFor(() => expect(rows(container)).toBe(20));
    const split = container.querySelector<HTMLElement>(".page.split2")!;
    expect(split.style.gridTemplateColumns).toBe("380px minmax(0,1fr)");
    const pointer = (type: string, clientX: number, target: Element | Window = window) => fireEvent(target, new MouseEvent(type, { bubbles: true, clientX })); // jsdom has no PointerEvent
    pointer("pointerdown", 380, container.querySelector(".grip")!);
    pointer("pointermove", 500);
    expect(split.style.gridTemplateColumns).toBe("500px minmax(0,1fr)");
    pointer("pointermove", 900);
    expect(split.style.gridTemplateColumns).toBe("600px minmax(0,1fr)"); // no wider
    pointer("pointerup", 900);
    expect(window.localStorage.getItem("v1.historyNav")).toBe("600");
    pointer("pointermove", 450); // released: the edge no longer follows
    expect(split.style.gridTemplateColumns).toBe("600px minmax(0,1fr)");
  });

  it("lists under a session only the turns of a conversation that ran in it: one that went on in a later session leaves nothing waiting", async () => {
    const first = stored[0]!; // the session shown
    try {
      first.runs = ["r1"];
      first.chatRuns = ["r1"];
      chat.conversations = [{ id: "c1", startedAt: "2026-08-01T09:01:00Z", turns: 2 }];
      chat.messages = [{ chatId: "c1", role: "user", runId: "r1", text: "Draft the abstract." }, { chatId: "c1", role: "user", runId: "r9", text: "Now the intro." }]; // r9 ran in another session
      listing();
      const { container } = render(<History expectedBackend="python" runnerBaseUrl="http://runner" />);
      await waitFor(() => expect(container.querySelector(".conv h3 small")?.textContent).toBe("1 of 2 turns"));
      expect(container.querySelectorAll(".conv .skel")).toHaveLength(1); // r1 alone, on its way
    } finally {
      first.runs = [];
      first.chatRuns = [];
      chat.conversations = [];
      chat.messages = [];
    }
  });
});
