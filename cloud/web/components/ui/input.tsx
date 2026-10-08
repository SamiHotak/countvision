import * as React from "react";

import { cn } from "@/lib/utils";

export const inputClass =
  "h-10 w-full rounded-[var(--radius-s)] border border-rule bg-ink px-3 text-base text-text " +
  "placeholder:text-faint focus-visible:border-accent focus-visible:outline-none " +
  "aria-[invalid=true]:border-danger disabled:opacity-60";

export function Input({ className, ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(inputClass, className)} {...props} />;
}

export function Select({ className, children, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={cn(inputClass, "appearance-auto pr-8", className)} {...props}>
      {children}
    </select>
  );
}

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  return <label className={cn("block text-sm font-medium text-muted", className)} {...props} />;
}

interface FieldProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label: string;
  error?: string;
  hint?: React.ReactNode;
}

/** Label + input + error/hint, wired for screen readers. */
export function Field({ label, error, hint, id, name, ...props }: FieldProps) {
  const fieldId = id ?? name ?? label.toLowerCase().replace(/\W+/g, "-");
  const describedBy = error ? `${fieldId}-error` : hint ? `${fieldId}-hint` : undefined;
  return (
    <div className="space-y-1.5">
      <Label htmlFor={fieldId}>{label}</Label>
      <Input id={fieldId} name={name} aria-invalid={error ? true : undefined} aria-describedby={describedBy} {...props} />
      {error ? (
        <p id={`${fieldId}-error`} className="text-sm text-danger">{error}</p>
      ) : hint ? (
        <p id={`${fieldId}-hint`} className="text-sm text-faint">{hint}</p>
      ) : null}
    </div>
  );
}
