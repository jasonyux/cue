import { cleanup, render, screen } from "@testing-library/react";
import type { RunDetail, RunEvent } from "@cue/contracts";
import { afterEach, describe, expect, it } from "vitest";

import { scenarioFixture } from "./fixtures";

import { NowScreen } from "../components/v1/NowScreen";

const at = (seconds: number) => new Date(Date.UTC(2026, 10, 3, 10, 0, seconds)).toISOString();
let sequence = 0;
const event = (type: RunEvent["type"], message: string, detail: string | undefined, seconds: number): RunEvent =>
  ({ id: `e${++sequence}`, runId: "r1", sequence, type, level: "ok", message, createdAt: at(seconds), ...(detail === undefined ? {} : { detail }) });

const detailOf = (status: RunDetail["run"]["status"], extra: Record<string, unknown> = {}): RunDetail =>
  ({ eventStreamUrl: "/api/runs/r1/events", events: [], replayUrl: "/api/runs/r1/replay", workspacePath: "/tmp/r1", scenario: scenarioFixture, actorAgent: "codex",
    run: { id: "r1", browserMode: "headful", maxResponseTurns: 24, labId: scenarioFixture.labId, model: "m", prompt: "Lint it", scenarioId: scenarioFixture.id, startedAt: at(0), status, ...extra } }) as unknown as RunDetail;

const events = [
  event("run_progress", "Model progress.", "Checking the lint first.", 0),
  event("function_call_requested", "Function tool call received from the model.", 'shell {"command": "pnpm run lint"}', 1),
  event("tool_progress", "shell output.", Array.from({ length: 20 }, (_, index) => `line ${index + 1}`).join("\n"), 2),
  event("function_call_completed", "Function tool call completed.", "shell", 3),
  event("function_call_requested", "Function tool call received from the model.", 'shell {"command": "pnpm test"}', 4),
  event("tool_progress", "shell output.", "running 3 files", 5),
];

afterEach(cleanup);

describe("NowScreen", () => {
  it("shows what the agent said last, what it is doing now, the tail of that output, and the run so far", () => {
    const { container } = render(<NowScreen detail={detailOf("running")} events={events} runnerBaseUrl="http://runner" />);
    expect(container.querySelector(".nowHead")?.textContent).toBe("Codex2 steps");
    expect(container.querySelector(".nowSay")?.textContent).toBe("Checking the lint first.");
    expect(container.querySelector(".nowLine")?.textContent).toBe("Runningpnpm test");
    expect(container.querySelector(".nowLine .turn-now")).toBeTruthy();
    expect(container.querySelector(".nowOut")?.textContent).toBe("running 3 files");
    expect(container.querySelector(".nowSum")?.textContent).toBe("Ran 1 command so far");
  });

  it("keeps only the last twelve lines of a long output while it runs, and settles to the summary when the run is over", () => {
    const between = render(<NowScreen detail={detailOf("running")} events={events.slice(0, 4)} runnerBaseUrl="http://runner" />);
    expect(between.container.querySelector(".nowOut")?.textContent).toBe(Array.from({ length: 12 }, (_, index) => `line ${index + 9}`).join("\n"));
    expect(between.container.querySelector(".nowLine")?.textContent).toBe("Deciding what to do next…");
    cleanup();
    const reply = "All clean: **no lint errors**.";
    const done = [...events.slice(0, 4), event("run_progress", "Model progress.", reply, 8)];
    const { container } = render(<NowScreen detail={detailOf("completed", { completedAt: at(9), durationMs: 9000, summary: { notes: [`Model final response: ${reply}`] } })} events={done} runnerBaseUrl="http://runner" />);
    expect(container.querySelector(".nowHead")?.textContent).toBe("CodexDone · 9s");
    expect(container.querySelector(".nowLine")?.textContent).toBe("Ran 1 command"); // the receipt: the sentence, then one line per call
    expect([...container.querySelectorAll(".nowCalls li")].map((item) => item.textContent)).toEqual(["Ranpnpm run lint2s"]);
    expect(container.querySelector(".nowSay")).toBeNull(); // the reply is the chat's, and nothing is said after the run
    expect(container.querySelector(".nowOut")).toBeNull(); // a finished run's output is history
    expect(container.querySelector(".nowSum")).toBeNull();
  });

  it("renders narration as markdown while it runs", () => {
    const talking = [...events.slice(0, 4), event("run_progress", "Model progress.", "Now the **tests**.", 8)];
    const { container } = render(<NowScreen detail={detailOf("running")} events={talking} runnerBaseUrl="http://runner" />);
    expect(container.querySelector(".nowSay")?.innerHTML).toContain("<strong>tests</strong>");
  });

  it("puts the failure first when the run failed", () => {
    const failed = [...events.slice(0, 4), event("run_failed", "Run failed during execution.", "Codex ended with failed: You’ve hit your usage limit.", 6)];
    render(<NowScreen detail={detailOf("failed", { completedAt: at(6), durationMs: 6000 })} events={failed} runnerBaseUrl="http://runner" />);
    expect(screen.getByRole("alert").textContent).toBe("Codex: You’ve hit your usage limit.");
  });
});
