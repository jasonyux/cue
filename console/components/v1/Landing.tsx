"use client";

import Link from "next/link";

import { LiveConsole } from "../live/LiveConsole";
import { Pilot } from "./Pilot";
import { useFirstRun } from "./Quickstart";
import { Timeline } from "./Timeline";
import { Timeline2 } from "./Timeline2";
import { Zen } from "./Zen";
import { ThemeSwitch, useTheme } from "./theme";

type Props = { expectedBackend: "python"; runnerBaseUrl: string };

// The landing page in the chosen theme: Zen (your screen on a desk with Pilot beside it, everything else tucked away;
// the default), Timeline (the session's stream on the left, the right side empty until something needs you), Timeline v2
// (the same, in the stacked-cards look: a slim timeline, one card in front with the rest behind it, a Why page), Full
// (the screen band, the conversation and the tasks) or Debug (every panel and number on one page).
export function Landing({ expectedBackend, runnerBaseUrl }: Props) {
  const [theme, setTheme] = useTheme();
  useFirstRun(runnerBaseUrl, expectedBackend);
  const switcher = <ThemeSwitch theme={theme} onChange={setTheme} />;
  if (theme === "debug") {
    return <LiveConsole expectedBackend={expectedBackend} runnerBaseUrl={runnerBaseUrl} nav={<>{switcher}<Link href="/v1/history">Pages →</Link></>} />;
  }
  if (theme === "full") return <Pilot expectedBackend={expectedBackend} runnerBaseUrl={runnerBaseUrl} switcher={switcher} />;
  if (theme === "timeline") return <Timeline expectedBackend={expectedBackend} runnerBaseUrl={runnerBaseUrl} switcher={switcher} />;
  if (theme === "timelinev2") return <Timeline2 expectedBackend={expectedBackend} runnerBaseUrl={runnerBaseUrl} />; // its top bar carries the switch
  return <Zen expectedBackend={expectedBackend} runnerBaseUrl={runnerBaseUrl} switcher={switcher} />;
}
