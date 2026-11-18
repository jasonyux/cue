"use client";

import { type LiveSettings, liveSessionListSchema } from "@cue/contracts";
import { useRouter } from "next/navigation";
import { type ReactNode, useEffect } from "react";

import { callRunner } from "../live/shared";
import { Mark } from "./Brand";

const SEEN_KEY = "v1.quickstart"; // set in this browser once the quickstart was closed or finished, or a session exists
let seen = false; // the same for this visit, when storage is blocked

const icon = (shape: ReactNode) => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{shape}</svg>;

// The quickstart's steps: a picture, a title and one line each. The Settings page puts a few of its simple view's rows
// under it: who does the work, then each background model, then a key for every provider those models use.
export const STEPS: { title: string; lead: string; picture: ReactNode }[] = [
  { title: "Welcome to Cue", lead: "Cue watches your screen while you work and offers to take tasks off your hands. Its main agent does the work, on your own Claude Code or Codex login.", picture: <Mark /> },
  { title: "Model configuration", lead: "A fast model to follow and understand your screen, a strong one for the agents that keep your tasks and suggest, and embeddings to filter duplicate proposals and tasks.", picture: icon(<><path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h11M19 17h1" /><circle cx="15" cy="7" r="2" /><circle cx="9" cy="12" r="2" /><circle cx="17" cy="17" r="2" /></>) },
  { title: "Add your keys", lead: "An API key for each provider you chose in Model configuration.", picture: icon(<><circle cx="8" cy="15" r="4" /><path d="M11 12 20 3M16 7l3 3M13.5 9.5l2 2" /></>) },
];

// The first card's row: who carries out the tasks. The session's length sits under it, in hours.
export const AGENT: { label: string; keys: (keyof LiveSettings)[] } = { label: "Main agent", keys: ["actorAgents"] };

// The models card, in short: three rows, the agents (tasks, suggestions, preferences) sharing one model, each with the
// model we would pick. Settings that hold a different model for each agent get the simple view's own rows instead.
export const MODELS: { label: string; keys: (keyof LiveSettings)[]; tip: string }[] = [
  { label: "Screen understanding", keys: ["summaryProvider", "summaryModel"], tip: "recommended: Claude Haiku 5.5 or equivalent" },
  { label: "Agents", keys: ["beliefProvider", "beliefModel"], tip: "recommended: Claude Opus 5.5 or equivalent" },
  { label: "Embeddings", keys: ["embedProvider", "embedModel"], tip: "recommended: text-embedding-3-small" },
];

// What the Agents row sets besides the tasks' own provider and model.
export const SHARED: [keyof LiveSettings, (keyof LiveSettings)[]][] = [
  ["beliefProvider", ["proposerProvider", "preferenceProvider"]],
  ["beliefModel", ["proposerModel", "preferenceModel"]],
];

// Where the keys card's keys go: typed on the page, or kept off it, in a .env written by hand or the shell's variables.
export const VIA = [["page", "Enter here"], ["file", ".env file"], ["shell", "Shell variables"]] as const;

// Where a provider hands out its keys, or says how to run it: behind the "?" beside its name.
export const HELP_PAGES: Record<string, string> = {
  openrouter: "https://openrouter.ai/keys",
  anthropic: "https://console.anthropic.com/",
  openai: "https://platform.openai.com/api-keys",
  litellm: "https://docs.litellm.ai/",
  vllm: "https://docs.vllm.ai/",
};

export function markSeen(): void {
  seen = true;
  try { window.localStorage.setItem(SEEN_KEY, "1"); } catch { /* storage blocked: this visit only */ }
}

// The first time Cue is opened (no session was ever recorded, and the quickstart was not closed here before), the
// landing page hands over to the quickstart on the Settings page.
export function useFirstRun(runnerBaseUrl: string, backend: string): void {
  const router = useRouter();
  useEffect(() => {
    try { seen ||= window.localStorage.getItem(SEEN_KEY) !== null; } catch { /* storage blocked: asked again next visit */ }
    if (seen) return;
    let here = true;
    void callRunner(runnerBaseUrl, backend, "/api/live/sessions?limit=1", liveSessionListSchema).then((sessions) => {
      if (!here) return;
      if (sessions.length) markSeen(); else router.replace("/v1/settings?quickstart=1");
    }, () => undefined); // the backend is down: the page says so
    return () => { here = false; };
  }, [runnerBaseUrl, backend, router]);
}

type Props = {
  step: number;
  onStep: (step: number) => void;
  onClose: () => void;
  onFinish: () => void;
  busy: boolean; // saving
  error: string | null;
  extra?: ReactNode; // beside Finish, on the last step
  children: ReactNode; // the step's settings
};

// The quickstart's window: one step at a time over the page, as a card with a picture on top.
export function Quickstart({ step, onStep, onClose, onFinish, busy, error, extra, children }: Props) {
  const { title, lead, picture } = STEPS[step]!;
  const last = step === STEPS.length - 1;
  const next = last ? <button type="button" className="btn primary" disabled={busy} onClick={onFinish}>{busy ? "Saving…" : "Finish"}</button> : <button type="button" className="btn primary" onClick={() => onStep(step + 1)}>Continue</button>;
  return (
    <div className="quick" role="dialog" aria-modal="true" aria-label="Quickstart">
      <div className="quick-card">
        <button type="button" className="quick-close" aria-label="Close" onClick={onClose}>×</button>
        <div className="quick-hero"><span className="tile">{picture}</span></div>
        <div className="quick-body">
          <h2>{title}</h2>
          <p className="quick-lead">{lead}</p>
          {children}
          {error ? <p className="quick-fail" role="alert">{error}</p> : null}
          <div className="quick-foot">
            {next}
            {last ? extra : null}
            <span className="spacer" />
            {step ? <button type="button" className="btn ghost" onClick={() => onStep(step - 1)}>Back</button> : null}
          </div>
          <div className="quick-dots" aria-label={`Step ${step + 1} of ${STEPS.length}`}>{STEPS.map((item, index) => <i key={item.title} className={index === step ? "on" : ""} />)}</div>
        </div>
      </div>
    </div>
  );
}
