import { cn } from "@/lib/utils";

export type Health = "ok" | "warn" | "down" | "idle";

/** A small status light. Always paired with a text label (never colour alone). */
export function StatusDot({ health, className }: { health: Health; className?: string }) {
  const tone = { ok: "bg-accent", warn: "bg-warn", down: "bg-danger", idle: "bg-faint" }[health];
  return <span aria-hidden className={cn("inline-block size-2.5 shrink-0 rounded-full", tone, className)} />;
}
