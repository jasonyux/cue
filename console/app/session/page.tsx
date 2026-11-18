import { notFound } from "next/navigation";

import { LiveConsole } from "../../components/live/LiveConsole";
import { release } from "../../components/release";

export const dynamic = "force-dynamic";

export default function SessionPage() {
  if (release()) notFound(); // a debug page
  const runnerBaseUrl = process.env.RUNNER_BASE_URL ?? "http://127.0.0.1:4041";
  return <LiveConsole expectedBackend="python" runnerBaseUrl={runnerBaseUrl} />;
}
