import { redirect } from "next/navigation";

// The public landing page comes in Phase 5. Until then: straight into the app.
export default function Home() {
  redirect("/app");
}
