import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { RunDetail, RunEvent } from "@cue/contracts";
import { afterEach, describe, expect, it } from "vitest";

import { scenarioFixture } from "./fixtures";

import { callLine, callTime, failureOf, stepsOf, workSentence } from "../components/live/steps";
import { Turn } from "../components/v1/Turn";

const at = (seconds: number) => new Date(Date.UTC(2026, 10, 3, 10, 0, seconds)).toISOString();
let sequence = 0;
const event = (type: RunEvent["type"], message: string, detail: string | undefined, seconds: number): RunEvent =>
  ({ id: `e${++sequence}`, runId: "r1", sequence, type, level: "ok", message, createdAt: at(seconds), ...(detail === undefined ? {} : { detail }) });

// A turn as Claude Code and Codex record it: narration, a shell command with output, a read, an edit, a Codex-style read
// through the shell, the model's turn marker, and a last narration that repeats the reply.
const events: RunEvent[] = [
  event("run_progress", "Model progress.", "Looking at the repo first.", 0),
  event("function_call_requested", "Function tool call received from the model.", 'Bash {"command": "pnpm run lint", "description": "Lint the console"}', 1),
  event("tool_progress", "Bash output.", "ok\n0 problems", 2),
  event("function_call_completed", "Function tool call completed.", "Bash", 4),
  event("function_call_requested", "Function tool call received from the model.", 'Read {"file_path": "/repo/app/prompts.py"}', 4),
  event("function_call_completed", "Function tool call completed.", "Read", 4),
  event("function_call_requested", "Function tool call received from the model.", 'Edit {"file_path": "/repo/app/prompts.py", "old_string": "a", "new_string": "b"}', 5),
  event("function_call_completed", "Function tool call completed.", "Edit", 5),
  event("function_call_requested", "Function tool call received from the model.", 'shell {"command": "cat README.md"}', 6),
  event("tool_progress", "shell output.", "# ComputerPilot", 6),
  event("function_call_completed", "Function tool call completed.", "shell", 7),
  event("run_progress", "Claude turn 1 completed.", "msg_1 · 100 in · 10 out · 0 reasoning", 7),
  event("run_progress", "Model progress.", "Done: lint is clean.", 8),
];

function detailOf(status: RunDetail["run"]["status"], extra: Record<string, unknown> = {}): RunDetail {
  return { eventStreamUrl: "/api/runs/r1/events", events: [], replayUrl: "/api/runs/r1/replay", workspacePath: "/tmp/r1", scenario: scenarioFixture, actorAgent: "claude",
    run: { id: "r1", browserMode: "headful", maxResponseTurns: 24, labId: scenarioFixture.labId, model: "m", prompt: "Lint it", scenarioId: scenarioFixture.id, startedAt: at(0), status, ...extra } } as unknown as RunDetail;
}

afterEach(cleanup);

describe("steps", () => {
  it("groups a run's events into narration and runs of classified calls", () => {
    const steps = stepsOf(events);
    expect(steps.map((step) => step.kind)).toEqual(["say", "work", "say"]);
    const calls = steps[1]!.kind === "work" ? steps[1]!.calls : [];
    expect(calls.map((call) => call.kind)).toEqual(["shell", "read", "edit", "read"]); // the shell's cat is a read
    expect(callLine(calls[0]!)).toEqual({ verb: "Ran", target: "pnpm run lint" });
    expect(callLine(calls[2]!)).toEqual({ verb: "Edited", target: "prompts.py" });
    expect(callLine(calls[3]!)).toEqual({ verb: "Read", target: "cat README.md" });
    expect(calls[0]!.output).toBe("ok\n0 problems");
    expect(calls[0]!.description).toBe("Lint the console");
    expect(callTime(calls[0]!)).toBe("3s");
    expect(callTime(calls[1]!)).toBe(""); // under a second: nothing
    expect(workSentence(calls)).toBe("Ran 1 command, read 2 files, and edited 1 file");
    expect(workSentence(calls.slice(0, 2))).toBe("Ran 1 command and read 1 file");
  });
});

describe("Turn", () => {
  it("folds a finished turn behind how long it took, opens into the steps, and swaps in the raw card on request", () => {
    const { container } = render(<Turn detail={detailOf("completed", { completedAt: at(8), durationMs: 8000 })} events={events} runnerBaseUrl="http://runner" finalText="Done: lint is clean." />);
    const bar = screen.getByRole("button", { name: "Worked 8s" });
    expect(bar.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Looking at the repo first.")).toBeNull();
    fireEvent.click(bar);
    expect(screen.getByText("Looking at the repo first.")).toBeTruthy();
    expect(screen.queryByText("Done: lint is clean.")).toBeNull(); // the reply is the message's: not repeated as narration
    expect(screen.getByText("Ran 1 command, read 2 files, and edited 1 file")).toBeTruthy();
    expect(container.querySelectorAll(".turn-call")).toHaveLength(4);
    expect(container.querySelector(".turn-out")?.textContent).toBe("ok\n0 problems");
    expect(screen.queryByText(/Model call 1/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "raw" }));
    expect(screen.getByText(/Model call 1/)).toBeTruthy(); // the run card, every event of it
    expect(container.querySelector(".turn-steps")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "steps" }));
    expect(container.querySelector(".turn-steps")).toBeTruthy();
  });

  it("pairs outputs and completions to overlapping calls by tool, and settles a call left open when the run is over", () => {
    const overlapping = [
      event("function_call_requested", "Function tool call received from the model.", 'Read {"file_path": "/repo/a.py"}', 0),
      event("function_call_requested", "Function tool call received from the model.", 'Read {"file_path": "/repo/b.py"}', 0),
      event("function_call_requested", "Function tool call received from the model.", 'Bash {"command": "pnpm test"}', 0),
      event("tool_progress", "Bash output.", "1 passed", 2),
      event("function_call_completed", "Function tool call completed.", "Read", 1),
      event("function_call_completed", "Function tool call completed.", "Read", 1),
      event("function_call_completed", "Function tool call completed.", "Bash", 3),
      event("function_call_requested", "Function tool call received from the model.", 'Bash {"command": "pnpm lint"}', 4), // never reported complete
    ];
    const live = stepsOf(overlapping);
    const calls = live[0]!.kind === "work" ? live[0]!.calls : [];
    expect(calls.map((call) => [callLine(call).target, call.endedAt !== null, call.output])).toEqual([["a.py", true, ""], ["b.py", true, ""], ["pnpm test", true, "1 passed"], ["pnpm lint", false, ""]]);
    const settled = stepsOf(overlapping, at(9));
    expect((settled[0]!.kind === "work" ? settled[0]!.calls : []).map((call) => callTime(call))).toEqual(["1s", "1s", "3s", "5s"]);
    render(<Turn detail={detailOf("completed", { completedAt: at(9), durationMs: 9000 })} events={overlapping} runnerBaseUrl="http://runner" />);
    fireEvent.click(screen.getByRole("button", { name: "Worked 9s" }));
    expect(screen.getByText("Read 2 files and ran 2 commands")).toBeTruthy(); // nothing "running" in a finished turn
    expect(screen.queryByText(/Running/)).toBeNull();
  });

  it("says why a turn failed without opening anything", () => {
    const limit = "Codex ended with failed: You’ve hit your usage limit. Upgrade to Pro, or try again at 8:38 PM.";
    const failed = [...events.slice(0, 4), event("run_failed", "Run failed during execution.", limit, 9)];
    expect(failureOf(failed)).toBe("Codex: You’ve hit your usage limit. Upgrade to Pro, or try again at 8:38 PM.");
    expect(failureOf([], [limit])).toBe("Codex: You’ve hit your usage limit. Upgrade to Pro, or try again at 8:38 PM."); // from the run's note when the events are gone
    render(<Turn detail={detailOf("failed", { completedAt: at(9), durationMs: 9000 })} events={failed} runnerBaseUrl="http://runner" />);
    expect(screen.getByRole("button", { name: "Failed after 9s" }).getAttribute("aria-expanded")).toBe("false");
    expect(screen.getByRole("alert").textContent).toBe("Codex: You’ve hit your usage limit. Upgrade to Pro, or try again at 8:38 PM.");
  });

  it("keeps a running turn open and says what the agent is doing now", () => {
    const live = [...events.slice(0, 4), event("function_call_requested", "Function tool call received from the model.", 'Bash {"command": "pnpm test"}', 9), event("tool_progress", "Bash output.", "running 3 files", 10)];
    const { container } = render(<Turn detail={detailOf("running")} events={live} runnerBaseUrl="http://runner" />);
    expect(screen.getByRole("button", { name: /^Working · / })).toBeTruthy();
    expect(container.querySelector(".turn-steps")).toBeTruthy();
    const summary = container.querySelector(".turn-work > summary")!;
    expect(summary.querySelector(".turn-now")).toBeTruthy();
    expect(summary.textContent).toContain("Running pnpm test");
    expect(summary.textContent).toContain("ran 1 command so far");
  });
});
