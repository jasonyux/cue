import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Notes } from "../components/v1/Notes";

const notes = [
  { id: "h8", ts: "2026-08-10T09:00:00Z", windowStart: "2026-08-10T08:59:00Z", windowEnd: "2026-08-10T09:00:00Z", summary: "Read the mail." },
  { id: "h9", ts: "2026-08-10T09:01:00Z", windowStart: "2026-08-10T09:00:00Z", windowEnd: "2026-08-10T09:01:00Z", summary: "Booked the Sydney hotel." },
];
const chat = { id: "c1", current: "c1", agent: null, turn: 0, runningRunId: null, messages: [], pending: [], conversations: [] };
const useRecords = vi.fn(() => ({ records: { summaries: notes, proposalSets: [], changes: [], screenshots: [], chat }, issue: null, reload: vi.fn(), screenshotUrl: (name: string) => name }));

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }), useSearchParams: () => new URLSearchParams("session=old&id=h9") })); // a citation's link
vi.mock("../components/v1/useSession", () => ({ useSession: () => ({ status: { active: false }, session: null, busy: false }) })); // nothing running
vi.mock("../components/v1/records", () => ({ useRecords: (...args: unknown[]) => useRecords(...(args as [])) }));

describe("the Notes page", () => {
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("lands on the note a citation names: that session's records, the note selected and scrolled to", async () => {
    const scrolled = vi.fn();
    Element.prototype.scrollIntoView = scrolled;
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, status: 200, json: async () => [] }) as unknown as Response)); // the session list
    const { container } = render(<Notes expectedBackend="python" runnerBaseUrl="http://runner" />);
    await screen.findByText("Booked the Sydney hotel.", { exact: false }); // the line carries its time range too
    expect(useRecords).toHaveBeenLastCalledWith("http://runner", "python", "old"); // the stored session the link named
    await waitFor(() => expect(container.querySelector("li#note-h9.sel")).toBeTruthy()); // the cited note, selected
    expect(container.querySelector("li#note-h8.sel")).toBeNull();
    expect(scrolled).toHaveBeenCalled();
  });
});
