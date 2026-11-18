import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { LiveChatMessage } from "@cue/contracts";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Rail } from "../components/v1/Rail";
import { activitySpans, clusterMarks, placeSpans } from "../components/v1/rail-layout";
import type { Mark } from "../components/v1/Spine";

const stamp = (seconds: number) => new Date(Date.UTC(2026, 9, 21, 10, 0, seconds)).toISOString();
const message = (seconds: number, role: "user" | "agent" = "user", chatId = "c1"): LiveChatMessage => ({ ts: stamp(seconds), chatId, turn: 1, role, text: `Message ${seconds}`, runId: `r${seconds}` });
const say = (seconds: number): Mark => ({ id: `m${seconds}`, kind: "say", ts: stamp(seconds), message: message(seconds) });
const run = (start: number, end: number | null, status = "completed"): Mark => ({ kind: "run", id: `r${start}`, ts: stamp(start), end: end === null ? null : stamp(end), detail: { run: { id: `r${start}`, status } } as Extract<Mark, { kind: "run" }>["detail"] });

afterEach(cleanup);

describe("clustered timeline rail", () => {
  it("groups nearby records without chaining an entire stream or mutating its input", () => {
    const marks = [say(50), say(20), say(0), say(29), say(30)];
    expect(clusterMarks(marks).map((group) => group.marks.map((mark) => mark.id))).toEqual([["m0", "m20", "m29"], ["m30", "m50"]]);
    expect(marks[0]!.id).toBe("m50");
  });

  it("keeps every dense-cluster event accessible and selectable on expansion", () => {
    const onPick = vi.fn(), marks = [0, 3, 6, 9, 12].map(say);
    const { container } = render(<Rail marks={marks} width={180} messages={[]} now={Date.parse(stamp(15))} selected={null} cited={new Set()} screenshotUrl={(name) => name} onPick={onPick} />);
    expect(container.querySelectorAll(".event-cluster")).toHaveLength(1);
    expect(container.querySelectorAll(".tick")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: /Expand 5 events/ }));
    expect(container.querySelectorAll(".tick")).toHaveLength(5);
    fireEvent.click(container.querySelectorAll(".tick")[4]!);
    expect(onPick).toHaveBeenCalledWith(marks[4]);
    fireEvent.click(screen.getByRole("button", { name: /Collapse 5 events/ }));
    expect(container.querySelectorAll(".tick")).toHaveLength(2);
  });

  it("holds as many events in a row as the sidebar's width fits", () => {
    const marks = [0, 3, 6, 9, 12, 15].map(say);
    const shown = (width: number) => {
      const { container } = render(<Rail marks={marks} width={width} messages={[]} now={Date.parse(stamp(20))} selected={null} cited={new Set()} screenshotUrl={(name) => name} onPick={vi.fn()} />);
      const row = [container.querySelectorAll(".tick").length, container.querySelector(".cluster-more")?.textContent ?? null, (container.querySelector(".rail") as HTMLElement).style.getPropertyValue("--events")];
      cleanup();
      return row;
    };
    expect(shown(160)).toEqual([1, "+5", "84px"]); // the narrowest sidebar
    expect(shown(232)).toEqual([3, "+3", "168px"]); // as it opens
    expect(shown(264)).toEqual([4, "+2", "210px"]);
    expect(shown(320)).toEqual([6, null, "252px"]); // all six fit: nothing to unfold
  });

  it("reveals a cited or selected event even when it would otherwise be hidden", () => {
    const marks = [0, 3, 6, 9].map(say);
    const { container } = render(<Rail marks={marks} width={180} messages={[]} now={Date.parse(stamp(15))} selected="m9" cited={new Set(["m6"])} screenshotUrl={(name) => name} onPick={vi.fn()} />);
    expect(container.querySelectorAll(".tick")).toHaveLength(4);
    expect(container.querySelector(".tick.sel")).toBeTruthy();
    expect(container.querySelector(".tick.cited")).toBeTruthy();
  });

  it("uses recorded run bounds, extends running ones to now, and never extends a finished run missing an end", () => {
    const spans = activitySpans([run(0, 120), run(40, null, "running"), run(10, null, "cancelled")], [], Date.parse(stamp(180)));
    expect(spans.map((span) => [span.start, span.end, span.active])).toEqual([[Date.parse(stamp(0)), Date.parse(stamp(120)), false], [Date.parse(stamp(40)), Date.parse(stamp(180)), true]]);
  });

  it("shows a conversation's observed message range, including replies, without extending idle chat to now", () => {
    const spans = activitySpans([say(0)], [message(0), message(125, "agent"), message(80), message(170, "agent", "other")], Date.parse(stamp(300)));
    expect(spans).toHaveLength(1);
    expect(spans[0]).toMatchObject({ label: "Conversation", start: Date.parse(stamp(0)), end: Date.parse(stamp(125)), active: false });
    expect(spans[0]!.segments).toHaveLength(0);
  });

  it("keeps overlapping agent runs on separate tracks and reuses a finished track", () => {
    const spans = placeSpans(activitySpans([run(0, 100), run(20, 80), run(110, 160)], [], Date.parse(stamp(200))));
    expect(spans[0]!.lane).not.toBe(spans[1]!.lane);
    expect(spans[2]!.lane).toBe(spans[0]!.lane);
    expect(spans.every((span) => span.lanes === 2)).toBe(true);
  });

  it("combines multiple agent turns into one conversation and keeps the pause between them", () => {
    const spans = activitySpans([say(0), say(120), run(0, 60), run(120, 180)], [message(0), message(120), message(240, "agent")], Date.parse(stamp(300)));
    expect(spans).toHaveLength(1);
    expect(spans[0]).toMatchObject({ label: "Conversation", start: Date.parse(stamp(0)), end: Date.parse(stamp(240)), active: false });
    expect(spans[0]!.segments.map((segment) => [segment.start, segment.end])).toEqual([[Date.parse(stamp(0)), Date.parse(stamp(60))], [Date.parse(stamp(120)), Date.parse(stamp(180))]]);
  });

  it("extends only the active turn to now within the same conversation", () => {
    const spans = activitySpans([say(0), say(120), run(0, 60), run(120, null, "running")], [message(0), message(120)], Date.parse(stamp(200)));
    expect(spans).toHaveLength(1);
    expect(spans[0]).toMatchObject({ active: true, end: Date.parse(stamp(200)) });
    expect(spans[0]!.segments.map((segment) => segment.active)).toEqual([false, true]);
  });

  it("connects saved turns using their native conversation ID when message rows are not loaded", () => {
    const first = run(0, 60), second = run(120, 180);
    if (first.kind !== "run" || second.kind !== "run") throw new Error("Expected run fixtures");
    first.detail.chat = { turn: 1, claudeSessionId: "native-chat" };
    second.detail.chat = { turn: 2, claudeSessionId: "native-chat" };
    const spans = activitySpans([first, second], [], Date.parse(stamp(300)));
    expect(spans).toHaveLength(1);
    expect(spans[0]!.segments).toHaveLength(2);
    expect(spans[0]!.label).toBe("Conversation");
  });

  it("renders one connector with solid work segments instead of duplicate run/chat bars", () => {
    const marks = [say(0), say(90), run(0, 110)], onPick = vi.fn();
    const { container } = render(<Rail marks={marks} width={180} messages={[message(0), message(120, "agent")]} now={Date.parse(stamp(200))} selected={null} cited={new Set()} screenshotUrl={(name) => name} onPick={onPick} />);
    expect(container.querySelectorAll(".conversation-span")).toHaveLength(1);
    expect(container.querySelectorAll(".conversation-connector")).toHaveLength(1);
    expect(container.querySelectorAll(".conversation-segment")).toHaveLength(1);
    expect(container.querySelector(".conversation-connector")!.getAttribute("title")).toContain("2m 0s");
    fireEvent.click(container.querySelector(".conversation-segment")!);
    expect(onPick).toHaveBeenCalledWith(marks[2]);
    expect(container.querySelectorAll(".tick")).toHaveLength(3);
  });
});
