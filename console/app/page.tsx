import { redirect } from "next/navigation";

// The console versions keep their URLs (/v0 the debug console, /v1 the pilot); / opens the newest.
export default function HomePage() {
  redirect("/v1");
}
