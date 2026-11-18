import { Usage } from "../../../components/v1/Usage";

export const dynamic = "force-dynamic";

export default function UsagePage() {
  const runnerBaseUrl = process.env.RUNNER_BASE_URL ?? "http://127.0.0.1:4041";
  return <Usage expectedBackend="python" runnerBaseUrl={runnerBaseUrl} />;
}
