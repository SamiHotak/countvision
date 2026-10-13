"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";

import { Notice } from "@/components/ui/panel";
import { api, type Org } from "@/lib/api";
import type { BillingBrief } from "@/lib/billing";
import { cn } from "@/lib/utils";

/**
 * Camera slots: one square per camera the plan allows. Filled = counting, hollow = free,
 * striped red = over the plan (paused, numbers not stored). This is what the customer pays for.
 */
export function CameraSlots({ limit, used, size = "md", className }: {
  limit: number; used: number; size?: "sm" | "md"; className?: string;
}) {
  const unlimited = limit < 0;
  const slots = unlimited ? Math.max(used, 1) : Math.max(limit, used);
  const shown = Math.min(slots, 16);
  const box = size === "sm" ? "h-3.5 w-[18px]" : "h-5 w-[26px]";
  return (
    <div className={cn("flex flex-wrap items-center gap-1", className)} role="img"
      aria-label={unlimited ? `${used} cameras in use, no limit`
        : `${Math.min(used, limit)} of ${limit} camera slots in use${used > limit ? `, ${used - limit} paused` : ""}`}>
      {Array.from({ length: shown }, (_, i) => {
        const over = !unlimited && i >= limit;
        const inUse = i < used;
        return (
          <span key={i} data-slot={over ? "paused" : inUse ? "counting" : "free"}
            className={cn("rounded-[3px] border", box,
              over ? "cv-slot-over border-danger/70" : inUse ? "border-accent bg-accent" : "border-faint/70 bg-transparent")} />
        );
      })}
      {slots > shown ? <span className="ml-1 text-xs text-muted">+{slots - shown}</span> : null}
    </div>
  );
}

export function MinutesBar({ used, limit }: { used: number; limit: number }) {
  if (limit < 0) return <p className="text-sm text-muted">{used} minutes used this month, no limit</p>;
  const pct = limit === 0 ? 100 : Math.min(100, (used / limit) * 100);
  return (
    <div>
      <div className="h-2 overflow-hidden rounded-full bg-raise" role="progressbar" aria-valuemin={0}
        aria-valuemax={limit} aria-valuenow={used} aria-label="Video minutes used this month">
        <div className={cn("h-full rounded-full", pct >= 100 ? "bg-danger" : pct >= 80 ? "bg-warn" : "bg-accent")}
          style={{ width: `${pct}%` }} />
      </div>
      <p className="mt-1.5 text-sm text-muted"><span className="text-text">{used}</span> of {limit} minutes used this month</p>
    </div>
  );
}

/** Pages that still work without a plan (everything else needs one). */
const FREE_PAGES = ["/billing", "/members", "/settings"];

/** Plan state of the open organization for the shell: reloads on page changes and after billing changes. */
export function useOrgBilling(orgId: string | null): BillingBrief | null {
  const path = usePathname();
  const [brief, setBrief] = React.useState<BillingBrief | null>(null);
  const load = React.useCallback(async () => {
    if (!orgId) return setBrief(null);
    try {
      const org = await api.get<Org>(`/api/orgs/${orgId}`);
      setBrief(org.billing ?? null);
    } catch {
      setBrief(null);
    }
  }, [orgId]);
  React.useEffect(() => {
    void load();
  }, [load, path]);
  React.useEffect(() => {
    const on = () => void load();
    window.addEventListener("cv:billing-changed", on);
    return () => window.removeEventListener("cv:billing-changed", on);
  }, [load]);
  return brief;
}

/** Tell the shell that the plan may have changed (after sync, grant ...). */
export const billingChanged = () => window.dispatchEvent(new Event("cv:billing-changed"));

export function BillingBanner({ orgId, brief }: { orgId: string; brief: BillingBrief | null }) {
  const path = usePathname();
  if (!brief?.enabled || path.endsWith("/billing")) return null;
  const link = <Link href={`/app/orgs/${orgId}/billing`} className="font-medium text-accent">Choose a plan</Link>;
  if (brief.state === "trial" && brief.trial_days_left !== null && brief.trial_days_left <= 5) {
    return (
      <Notice tone="warn" className="mb-6 flex flex-wrap gap-x-4 gap-y-1" data-testid="billing-banner">
        <span className="mr-auto">
          {brief.trial_days_left === 0 ? "Your free trial ends today." : `Your free trial ends in ${brief.trial_days_left} day${brief.trial_days_left === 1 ? "" : "s"}.`}
          {" "}Your counting and data stay as they are when you choose a plan.
        </span>
        {link}
      </Notice>
    );
  }
  if (brief.state === "past_due") {
    return (
      <Notice tone="error" className="mb-6 flex flex-wrap gap-x-4 gap-y-1" data-testid="billing-banner">
        <span className="mr-auto">The last payment failed. Stripe will try again; please update your card.</span>
        <Link href={`/app/orgs/${orgId}/billing`} className="font-medium text-accent">Update payment</Link>
      </Notice>
    );
  }
  return null;
}

/** Without a working plan, product pages show this instead of their content. */
export function PlanGate({ orgId, brief, children }: { orgId: string | null; brief: BillingBrief | null; children: React.ReactNode }) {
  const path = usePathname();
  const blocked = !!orgId && !!brief && !brief.active && !FREE_PAGES.some((p) => path.endsWith(p) || path.includes(`${p}/`));
  if (!blocked) return <>{children}</>;
  return (
    <div className="max-w-[560px] py-10" data-testid="plan-gate">
      <h1 className="text-2xl font-semibold">{brief!.state === "expired" ? "Your free trial has ended" : "Your subscription has ended"}</h1>
      <p className="mt-3 text-muted">
        Choose a plan to see your dashboards, alerts and reports again. Your devices keep sending numbers in the
        meantime, so nothing is lost.
      </p>
      <Link href={`/app/orgs/${orgId}/billing`}
        className="mt-6 inline-flex h-10 items-center rounded-[var(--radius-s)] bg-accent px-4 font-medium text-accent-ink no-underline hover:bg-accent/85">
        See plans
      </Link>
    </div>
  );
}
