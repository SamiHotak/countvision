import * as React from "react";

import { cn } from "@/lib/utils";

/** A flat panel with a hairline border (no shadows: calm control-room look). */
export function Panel({ className, ...props }: React.HTMLAttributes<HTMLElement>) {
  return <section className={cn("rounded-[var(--radius-m)] border border-rule bg-panel", className)} {...props} />;
}

export function PanelHead({ title, children, className }: { title: string; children?: React.ReactNode; className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-3 border-b border-rule px-5 py-3.5", className)}>
      <h2 className="mr-auto text-lg font-semibold">{title}</h2>
      {children}
    </div>
  );
}

export function Notice({
  tone = "info",
  className,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { tone?: "info" | "error" | "success" | "warn" }) {
  const tones = {
    info: "border-rule text-muted",
    success: "border-accent/40 text-text",
    warn: "border-warn/50 text-text",
    error: "border-danger/50 text-text",
  };
  const bar = { info: "bg-faint", success: "bg-accent", warn: "bg-warn", error: "bg-danger" };
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn("relative overflow-hidden rounded-[var(--radius-s)] border bg-raise/60 py-2.5 pl-4 pr-3 text-sm", tones[tone], className)}
      {...props}
    >
      <span className={cn("absolute inset-y-0 left-0 w-[3px]", bar[tone])} aria-hidden />
      {children}
    </div>
  );
}

export function RoleBadge({ role }: { role: string }) {
  return (
    <span
      className={cn(
        "inline-flex h-6 items-center rounded-full border px-2.5 text-xs font-medium capitalize",
        role === "owner" ? "border-accent/50 text-accent" : "border-rule text-muted",
      )}
    >
      {role}
    </span>
  );
}
