import { notFound } from "next/navigation";

import { release } from "../../../components/release";
import { Temporary } from "../../../components/v1/Temporary";

export const dynamic = "force-dynamic";

export default function TemporaryPage() {
  if (release()) notFound(); // a debug page, reached from the Full theme
  const runnerBaseUrl = process.env.RUNNER_BASE_URL ?? "http://127.0.0.1:4041";
  return <Temporary expectedBackend="python" runnerBaseUrl={runnerBaseUrl} />;
}
