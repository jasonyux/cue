import { type ReactNode, Suspense } from "react";

import { Tips } from "../../components/v1/Tips";
import { SessionProvider } from "../../components/v1/useSession";
import "./v1.css";

// What every /v1 page shares: one session, kept across a change of page, and the loading boundary. The boundary is
// here and not in each page so that a page still loading leaves the one before it on screen, not a blank.
export default function PilotLayout({ children }: { children: ReactNode }) {
  const runnerBaseUrl = process.env.RUNNER_BASE_URL ?? "http://127.0.0.1:4041";
  return <SessionProvider runnerBaseUrl={runnerBaseUrl}><Suspense>{children}</Suspense><Tips /></SessionProvider>;
}
