"use client";

import * as React from "react";

import { api } from "@/lib/api";

let cached: Promise<{ google: boolean }> | null = null;

/** "Continue with Google", shown only when the server has Google login set up. */
export function GoogleButton({ next }: { next: string }) {
  const [enabled, setEnabled] = React.useState(false);
  React.useEffect(() => {
    cached ??= api.get<{ google: boolean }>("/api/auth/providers").catch(() => ({ google: false }));
    void cached.then((p) => setEnabled(p.google));
  }, []);
  if (!enabled) return null;
  return (
    <>
      <a
        href={`/api/auth/google/start?next=${encodeURIComponent(next)}`}
        className="flex h-10 w-full items-center justify-center gap-2.5 rounded-[var(--radius-s)] border border-rule bg-raise font-medium no-underline hover:border-faint"
      >
        <svg viewBox="0 0 24 24" className="size-[18px]" aria-hidden>
          <path fill="#EA4335" d="M12 10.2v3.9h5.5c-.2 1.3-1.6 3.9-5.5 3.9-3.3 0-6-2.7-6-6.1s2.7-6.1 6-6.1c1.9 0 3.2.8 3.9 1.5l2.7-2.6C16.9 3.1 14.7 2 12 2 6.5 2 2 6.5 2 12s4.5 10 10 10c5.8 0 9.6-4.1 9.6-9.8 0-.7-.1-1.2-.2-1.7H12z" />
        </svg>
        Continue with Google
      </a>
      <div className="my-5 flex items-center gap-3 text-sm text-faint">
        <span className="h-px flex-1 bg-rule" /> or with email <span className="h-px flex-1 bg-rule" />
      </div>
    </>
  );
}
