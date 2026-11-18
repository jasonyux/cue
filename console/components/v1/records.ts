"use client";

import {
  liveChatSchema,
  liveProposalSetsResponseSchema,
  liveScreenshotsSchema,
  liveSessionDetailSchema,
  liveSummariesResponseSchema,
  liveTaskChangesSchema,
  type LiveChat,
  type LiveProposalSet,
  type LiveScreenshot,
  type LiveSessionDetail,
  type LiveSummary,
  type LiveTaskChange,
} from "@cue/contracts";
import { useCallback, useEffect, useState } from "react";

import { callRunner } from "../live/shared";

export type Records = {
  summaries: LiveSummary[];
  proposalSets: LiveProposalSet[];
  changes: LiveTaskChange[];
  screenshots: LiveScreenshot[];
  detail: LiveSessionDetail | null; // a stored session's record, tasks and settings; null for the running one
  chat: LiveChat | null;
};

// Everything recorded in a session, for the Notes and History pages: the running session's (``sessionId`` null,
// re-read every half minute) or a stored one's; ``undefined`` loads nothing yet. The screenshot route prefix goes with it.
export function useRecords(runnerBaseUrl: string, backend: string, sessionId: string | null | undefined) {
  const [records, setRecords] = useState<Records | null>(null);
  const [issue, setIssue] = useState<string | null>(null);
  const base = sessionId ? `/api/live/sessions/${encodeURIComponent(sessionId)}` : "/api/live/session";
  const load = useCallback(async () => {
    try {
      const [changes, screenshots, chat] = await Promise.all([
        callRunner(runnerBaseUrl, backend, `${base}/changes?limit=500`, liveTaskChangesSchema),
        callRunner(runnerBaseUrl, backend, `${base}/screenshots?limit=2000`, liveScreenshotsSchema),
        callRunner(runnerBaseUrl, backend, `${base}/chat`, liveChatSchema),
      ]);
      if (sessionId) {
        const detail = await callRunner(runnerBaseUrl, backend, base, liveSessionDetailSchema);
        setRecords({ summaries: detail.summaries, proposalSets: detail.proposals, changes, screenshots, detail, chat });
      } else {
        const [summaries, proposalSets] = await Promise.all([
          callRunner(runnerBaseUrl, backend, `${base}/summaries?limit=500`, liveSummariesResponseSchema),
          callRunner(runnerBaseUrl, backend, `${base}/proposals?limit=500`, liveProposalSetsResponseSchema),
        ]);
        setRecords({ summaries, proposalSets, changes, screenshots, detail: null, chat });
      }
      setIssue(null);
    } catch (error) {
      setRecords(null);
      setIssue(error instanceof Error ? error.message : "Could not load the session.");
    }
  }, [runnerBaseUrl, backend, base, sessionId]);
  useEffect(() => {
    if (sessionId === undefined) return;
    void load();
    if (sessionId) return;
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load, sessionId]);
  return { records, issue, reload: load, screenshotUrl: (name: string) => `${runnerBaseUrl}${base}/screenshots/${encodeURIComponent(name)}` };
}
