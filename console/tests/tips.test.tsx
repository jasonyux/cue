import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { Tips } from "../components/v1/Tips";

afterEach(() => { cleanup(); vi.useRealTimers(); });

it("keeps a title from the browser while the pointer is over it, and shows it itself", () => {
  vi.useFakeTimers();
  render(<><button type="button" title="Conversations"><i data-testid="icon" /></button><p>elsewhere</p><div className="zen"><b title="Pet">pet</b></div><Tips /></>);
  const button = screen.getByRole("button");
  const marks = () => [button.getAttribute("title"), button.getAttribute("data-tip")];

  fireEvent.mouseOver(screen.getByTestId("icon"));
  expect(marks()).toEqual([null, "Conversations"]); // nothing for the browser's own tooltip
  expect(screen.queryByRole("tooltip")).toBeNull();
  act(() => { vi.advanceTimersByTime(600); });
  expect(screen.getByRole("tooltip").textContent).toBe("Conversations");
  fireEvent.mouseOver(button); // still the same control: the tooltip stays
  expect(screen.getByRole("tooltip").textContent).toBe("Conversations");

  fireEvent.mouseOver(screen.getByText("elsewhere"));
  expect(marks()).toEqual(["Conversations", null]);
  expect(screen.queryByRole("tooltip")).toBeNull();

  fireEvent.mouseOver(screen.getByTestId("icon"));
  button.setAttribute("title", "Timeline"); // the page renamed it while the pointer was on it
  fireEvent.mouseOut(button, { relatedTarget: null }); // out of the window
  expect(marks()).toEqual(["Timeline", null]);

  fireEvent.mouseOver(screen.getByText("pet"));
  expect(screen.getByText("pet").getAttribute("title")).toBe("Pet"); // Zen keeps the browser's
});
