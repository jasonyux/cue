import { redirect } from "next/navigation";

// The debug console is now the landing page's Debug theme.
export default function DebugPage() {
  redirect("/v1?theme=debug");
}
