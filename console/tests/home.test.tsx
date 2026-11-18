import { describe, expect, it, vi } from "vitest";

import HomePage from "../app/page";
import DebugPage from "../app/v0/page";
import { pickTheme } from "../components/v1/theme";

vi.mock("next/navigation", () => ({ redirect: vi.fn((to: string) => { throw new Error(`redirect:${to}`); }) }));

describe("home page", () => {
  it("opens the landing page", () => {
    expect(() => HomePage()).toThrow("redirect:/v1");
  });

  it("sends the old debug URL to the landing page's Debug theme", () => {
    expect(() => DebugPage()).toThrow("redirect:/v1?theme=debug");
  });

  it("takes the theme from the URL, then from this browser, else Zen", () => {
    expect(pickTheme("debug", "full")).toBe("debug");
    expect(pickTheme("pilot", null)).toBe("full"); // the old name
    expect(pickTheme(null, "debug")).toBe("debug");
    expect(pickTheme("zen", null)).toBe("zen");
    expect(pickTheme("bogus", null)).toBe("timelinev2");
    expect(pickTheme(null, null)).toBe("timelinev2"); // the default page
  });
});
