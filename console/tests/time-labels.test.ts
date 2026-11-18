import type { LiveProposal, RunRecord } from "@cue/contracts";
import { describe, expect, it } from "vitest";

import { byHand, runTime } from "../components/live/shared";

const proposal = (humanSeconds?: number): LiveProposal => ({ kind: "act", task: "Draft the reply", ...(humanSeconds === undefined ? {} : { humanSeconds }) });
const run = (fields: Partial<RunRecord>): RunRecord => ({
  id: "r1", scenarioId: "s", labId: "desktop", browserMode: "headful", model: "m", maxResponseTurns: 24, prompt: "p", status: "completed",
  startedAt: "2026-10-13T10:00:00.000Z", ...fields,
}) as RunRecord;

describe("the time labels", () => {
  it("rounds the proposer's by-hand estimate the way a person would say it", () => {
    expect(byHand(proposal(30))).toBe("~30 s");
    expect(byHand(proposal(2))).toBe("~5 s");
    expect(byHand(proposal(600))).toBe("~10 min");
    expect(byHand(proposal(5400))).toBe("~1.5 h");
    expect(byHand(proposal(72_000))).toBe("~20 h");
    expect(byHand(proposal())).toBeNull(); // an older proposal carries none
    expect(byHand(proposal(0))).toBeNull();
  });

  it("gives a finished run's own time, from its duration or its end", () => {
    expect(runTime(run({ durationMs: 38_400 }))).toBe("38s");
    expect(runTime(run({ durationMs: 252_000 }))).toBe("4m 12s");
    expect(runTime(run({ durationMs: 3_900_000 }))).toBe("1h 05m");
    expect(runTime(run({ completedAt: "2026-10-13T10:02:05.000Z" }))).toBe("2m 05s");
    expect(runTime(run({ status: "running" }))).toBeNull(); // still at work
  });
});
