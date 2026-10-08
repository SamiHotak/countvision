import Link from "next/link";

import { Wordmark } from "@/components/brand";

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-[520px] flex-col justify-center gap-5 px-5">
      <Wordmark />
      <h1 className="text-2xl font-semibold">This page does not exist</h1>
      <p className="text-muted">Check the address, or go back to your organizations.</p>
      <Link href="/app" className="text-accent">Go to CountVision</Link>
    </main>
  );
}
