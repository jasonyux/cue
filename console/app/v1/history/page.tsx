import { History } from "../../../components/v1/History";

export const dynamic = "force-dynamic";

export default function HistoryPage() {
  const runnerBaseUrl = process.env.RUNNER_BASE_URL ?? "http://127.0.0.1:4041";
  return <History expectedBackend="python" runnerBaseUrl={runnerBaseUrl} />;
}
