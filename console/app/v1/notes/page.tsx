import { Notes } from "../../../components/v1/Notes";

export const dynamic = "force-dynamic";

export default function NotesPage() {
  const runnerBaseUrl = process.env.RUNNER_BASE_URL ?? "http://127.0.0.1:4041";
  return <Notes expectedBackend="python" runnerBaseUrl={runnerBaseUrl} />;
}
