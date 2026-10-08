"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/** Native <dialog> (focus trap, Esc, backdrop) with our styling. */
export function Dialog({
  open,
  onClose,
  title,
  children,
  className,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  const ref = React.useRef<HTMLDialogElement>(null);
  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      aria-labelledby="dialog-title"
      className={cn(
        "m-auto w-[min(440px,calc(100vw-32px))] rounded-[var(--radius-m)] border border-rule bg-panel p-0 text-text",
        "backdrop:bg-black/60",
        className,
      )}
    >
      {open ? (
        <div className="p-5">
          <h2 id="dialog-title" className="mb-4 text-xl font-semibold">{title}</h2>
          {children}
        </div>
      ) : null}
    </dialog>
  );
}
