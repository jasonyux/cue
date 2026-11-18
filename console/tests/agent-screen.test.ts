import type { RunEvent } from "@cue/contracts";
import { describe, expect, it } from "vitest";

import { agentLines } from "../components/v1/AgentScreen";

let sequence = 0;
const event = (type: RunEvent["type"], message: string, detail?: string): RunEvent =>
  ({ id: `e${++sequence}`, runId: "r1", sequence, type, level: "ok", message, detail, createdAt: "2026-10-07T22:01:31Z" }) as RunEvent;

describe("the agent's terminal", () => {
  it("shows narration, each call by its telling argument, its output, and the call still running", () => {
    const lines = agentLines([
      event("run_progress", "Claude turn 1 completed.", "msg · 30176 in · 7 out"),
      event("run_progress", "Model progress.", "I found the RWML paper sources."),
      event("function_call_requested", "Function tool call received from the model.", 'Bash {"command": "ls -la sections\\nwc -l main.tex"}'),
      event("tool_progress", "Bash output.", "a\nb\nc\nd\ne"),
      event("function_call_completed", "Function tool call completed.", "Bash"),
      event("function_call_requested", "Function tool call received from the model.", 'mcp__cmux-cua__click {"element_index": 4, "session": "run-1"}'),
    ]);
    expect(lines).toEqual([
      { kind: "say", text: "I found the RWML paper sources." },
      { kind: "tool", text: "Bash(ls -la sections …)" },
      { kind: "out", text: "a\nb\nc\n… +2 lines" },
      { kind: "tool", text: "cmux-cua · click(run-1)", state: "running" },
    ]);
  });

  it("marks the call waiting on a permission, and shows thinking only as the newest thing", () => {
    expect(agentLines([event("run_progress", "Model reasoning."), event("run_progress", "Model progress.", "Done.")])).toEqual([{ kind: "say", text: "Done." }]);
    expect(agentLines([event("run_progress", "Model reasoning.")])).toEqual([{ kind: "think", text: "thinking…" }]);
    const call = event("function_call_requested", "Function tool call received from the model.", 'Bash {"command": "rm -rf build"}');
    const asked = event("run_progress", "Claude is waiting for your approval", 'Bash {"command": "rm -rf build", "description": "Clean"}\nmore');
    expect(agentLines([call, asked])).toEqual([{ kind: "tool", text: "Bash(rm -rf build)", state: "waiting" }]); // Pilot's bubble asks; no line repeats it
    expect(agentLines([call, asked, event("run_progress", "Approval answered: allow.")])).toEqual([{ kind: "tool", text: "Bash(rm -rf build)", state: "running" }]);
  });
});
