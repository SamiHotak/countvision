"use client";

// On-screen alerts (CountVision Local: often no email, no Telegram). A bell with the number of alerts
// that are on now; the list shows the newest alert messages. Seen = remembered in this browser.
// Polls every 30 s (alerts are checked once a minute), so it needs no extra live connection.

import Link from "next/link";
import * as React from "react";

import { api, fmtDateTime, timeAgo } from "@/lib/api";
import { cn } from "@/lib/utils";

interface AlertEvent {
  id: number; rule_id: string; rule_name: string; kind: string; status: "fired" | "resolved"; message: string;
  created_at: string;
}
interface Rule { id: string; name: string; state: "ok" | "firing"; enabled: boolean }

const SEEN_KEY = "cv:alerts-seen";

function readSeen(): number {
  try {
    return Number(localStorage.getItem(SEEN_KEY) ?? 0) || 0;
  } catch {
    return 0;
  }
}

export function AlertBell({ orgId }: { orgId: string }) {
  const [events, setEvents] = React.useState<AlertEvent[]>([]);
  const [firing, setFiring] = React.useState<Rule[]>([]);
  const [open, setOpen] = React.useState(false);
  const [seen, setSeen] = React.useState(0);
  const box = React.useRef<HTMLDivElement>(null);

  const load = React.useCallback(async () => {
    try {
      const [ev, rules] = await Promise.all([
        api.get<AlertEvent[]>(`/api/orgs/${orgId}/alert-events?limit=12`),
        api.get<Rule[]>(`/api/orgs/${orgId}/alert-rules`),
      ]);
      setEvents(ev);
      setFiring(rules.filter((r) => r.enabled && r.state === "firing"));
    } catch {
      /* no plan or offline: the bell just stays quiet */
    }
  }, [orgId]);

  React.useEffect(() => {
    setSeen(readSeen());
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load]);

  React.useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !box.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const unseen = events.filter((e) => e.status === "fired" && new Date(e.created_at).getTime() > seen).length;
  const badge = firing.length;

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next) {
      const newest = events[0] ? new Date(events[0].created_at).getTime() : Date.now();
      try {
        localStorage.setItem(SEEN_KEY, String(newest));
      } catch {
        /* ignore */
      }
      setSeen(newest);
    }
  }

  return (
    <div ref={box} className="relative">
      <button type="button" onClick={toggle} aria-expanded={open} data-testid="alert-bell"
        aria-label={badge ? `${badge} alert${badge === 1 ? "" : "s"} on now` : "Alerts"}
        className={cn("relative grid size-9 cursor-pointer place-items-center rounded-[var(--radius-s)] border",
          badge ? "border-danger/60 text-text" : "border-rule text-muted hover:text-text")}>
        <svg viewBox="0 0 24 24" className="size-[18px]" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15z" strokeLinejoin="round" />
          <path d="M10 20.5a2 2 0 0 0 4 0" strokeLinecap="round" />
        </svg>
        {badge ? (
          <span className="absolute -right-1.5 -top-1.5 grid h-[18px] min-w-[18px] place-items-center rounded-full bg-danger px-1 text-xs font-semibold text-ink"
            data-testid="alert-badge">{badge}</span>
        ) : unseen ? <span className="absolute -right-0.5 -top-0.5 size-2 rounded-full bg-warn" aria-hidden /> : null}
      </button>
      {open ? (
        <div role="dialog" aria-label="Alerts"
          className="absolute left-0 top-11 z-30 w-[min(340px,calc(100vw-32px))] rounded-[var(--radius-m)] border border-rule bg-panel shadow-[0_12px_40px_rgba(0,0,0,0.45)] lg:left-auto lg:right-0">
          <div className="border-b border-rule px-4 py-3">
            <p className="font-semibold">{badge ? `${badge} alert${badge === 1 ? "" : "s"} on now` : "No alert on now"}</p>
            {firing.length ? <p className="mt-0.5 text-sm text-muted">{firing.map((r) => r.name).join(", ")}</p> : null}
          </div>
          {events.length === 0 ? (
            <p className="px-4 py-4 text-sm text-muted">Nothing yet. Set up alerts for a full shop, a long queue or a camera that stops counting.</p>
          ) : (
            <ul className="max-h-[320px] divide-y divide-rule overflow-y-auto" aria-label="Recent alerts">
              {events.map((e) => (
                <li key={e.id} className="px-4 py-2.5" data-testid="alert-item">
                  <p className="flex items-center gap-2 text-sm">
                    <span className={cn("size-2 shrink-0 rounded-full", e.status === "fired" ? "bg-danger" : "bg-accent")} aria-hidden />
                    <span className="font-medium">{e.rule_name}</span>
                    <span className="ml-auto shrink-0 text-faint" title={fmtDateTime(e.created_at)}>{timeAgo(e.created_at)}</span>
                  </p>
                  <p className="mt-0.5 pl-4 text-sm text-muted">{e.status === "fired" ? e.message : "Over."}</p>
                </li>
              ))}
            </ul>
          )}
          <div className="border-t border-rule px-4 py-2.5">
            <Link href={`/app/orgs/${orgId}/alerts`} className="text-sm text-accent" onClick={() => setOpen(false)}>
              Alert settings and history
            </Link>
          </div>
        </div>
      ) : null}
    </div>
  );
}
