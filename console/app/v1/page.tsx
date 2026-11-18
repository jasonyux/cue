import { Landing } from "../../components/v1/Landing";

export const dynamic = "force-dynamic";

// The landing page, in the theme chosen with the switch in its header (or ?theme=).
export default function LandingPage() {
  const runnerBaseUrl = process.env.RUNNER_BASE_URL ?? "http://127.0.0.1:4041";
  return <Landing expectedBackend="python" runnerBaseUrl={runnerBaseUrl} />;
}
