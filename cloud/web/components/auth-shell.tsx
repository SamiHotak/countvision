"use client";

import * as React from "react";

import { Wordmark } from "@/components/brand";

/** The counting line: a dot walks through a door line and the IN number goes up. */
function CountingLine() {
  const [count, setCount] = React.useState(0);
  React.useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let timer: ReturnType<typeof setInterval> | undefined;
    // The walker crosses the line halfway through its 4.8 s loop.
    const start = setTimeout(() => {
      setCount((c) => c + 1);
      timer = setInterval(() => setCount((c) => c + 1), 4800);
    }, 2400);
    return () => {
      clearTimeout(start);
      if (timer) clearInterval(timer);
    };
  }, []);

  return (
    <div className="flex w-full max-w-[380px] flex-col gap-8">
      <svg viewBox="0 0 320 180" className="w-full" aria-hidden>
        <rect x="0.5" y="0.5" width="319" height="179" rx="10" className="fill-ink stroke-rule" />
        {/* door frame */}
        <path d="M150 28 V152" className="stroke-faint" strokeWidth="1" strokeDasharray="3 5" />
        <line x1="160" y1="20" x2="160" y2="160" className="stroke-accent" strokeWidth="3" strokeLinecap="round" />
        <circle cx="160" cy="20" r="5" className="fill-accent" />
        <circle cx="160" cy="160" r="5" className="fill-accent" />
        <g className="walker">
          {/* a person seen from above-front, and the tracking point at the feet */}
          <circle cx="17" cy="70" r="9" fill="none" className="stroke-text" strokeWidth="1.5" />
          <path d="M3 124 V104 a14 14 0 0 1 28 0 V124" fill="none" className="stroke-text" strokeWidth="1.5" />
          <circle cx="17" cy="132" r="3" className="fill-accent" />
        </g>
      </svg>
      <div>
        <p className="text-[length:var(--text-num)] font-semibold leading-none tracking-tight" aria-live="off">
          {count}
          <span className="ml-3 align-middle text-xl font-medium text-accent">in</span>
        </p>
        <p className="mt-4 max-w-[34ch] text-muted">
          Counted on site. Only the number travels to the dashboard, never the picture.
        </p>
      </div>
    </div>
  );
}

export function AuthShell({ title, intro, children }: { title: string; intro?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <main className="flex flex-col px-5 py-6 sm:px-10">
        <Wordmark href="/login" />
        <div className="flex flex-1 items-center py-10">
          <div className="w-full max-w-[380px]">
            <h1 className="text-2xl font-semibold leading-tight">{title}</h1>
            {intro ? <div className="mt-2 text-muted">{intro}</div> : null}
            <div className="mt-7">{children}</div>
          </div>
        </div>
      </main>
      <aside className="hidden items-center justify-center border-l border-rule bg-panel px-10 lg:flex">
        <CountingLine />
      </aside>
    </div>
  );
}
