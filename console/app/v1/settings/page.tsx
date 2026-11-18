import { Settings } from "../../../components/v1/Settings";

export const dynamic = "force-dynamic";

export default function SettingsPage() {
  const runnerBaseUrl = process.env.RUNNER_BASE_URL ?? "http://127.0.0.1:4041";
  return <Settings expectedBackend="python" runnerBaseUrl={runnerBaseUrl} />;
}
