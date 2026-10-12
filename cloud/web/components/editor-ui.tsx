"use client";

// Small controls shared by the camera editor and the video analysis editor.

import * as React from "react";

import { cn } from "@/lib/utils";

export function Section({ title, children, className }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("border-t border-rule px-4 py-4", className)}>
      <h2 className="mb-3 font-semibold">{title}</h2>
      {children}
    </section>
  );
}

export function Seg<T extends string>({ value, options, onChange, disabled, label }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; disabled?: boolean; label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-[var(--radius-s)] border border-rule bg-ink p-0.5">
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={value === o.value} disabled={disabled}
          onClick={() => onChange(o.value)}
          className={cn("h-8 cursor-pointer rounded-[4px] px-3 text-sm disabled:cursor-default",
            value === o.value ? "bg-raise text-text" : "text-muted hover:text-text")}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function ToolButton({ active, onClick, children, keyHint, disabled, label, toggle = false }: {
  active?: boolean; onClick: () => void; children: React.ReactNode; keyHint?: string; disabled?: boolean; label?: string;
  toggle?: boolean; // an on/off setting (outlined), not a drawing tool (filled)
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} aria-pressed={active} aria-label={label}
      title={keyHint ? `${label ?? ""} (${keyHint})`.trim() : label}
      className={cn("inline-flex h-9 cursor-pointer items-center gap-2 rounded-[var(--radius-s)] px-3 text-sm",
        "disabled:cursor-default disabled:opacity-40",
        active && !toggle ? "bg-accent text-accent-ink"
          : active ? "text-accent ring-1 ring-inset ring-accent/50 hover:bg-raise" : "text-muted hover:bg-raise hover:text-text")}>
      {children}
      {keyHint ? <kbd className={cn("hidden text-xs sm:inline", active && !toggle ? "text-accent-ink/70" : "text-faint")}>{keyHint}</kbd> : null}
    </button>
  );
}

