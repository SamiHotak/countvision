import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

// shadcn/ui-style button, with the CountVision tokens.
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[var(--radius-s)] font-medium " +
    "transition-colors disabled:pointer-events-none disabled:opacity-50 cursor-pointer select-none",
  {
    variants: {
      variant: {
        primary: "bg-accent text-accent-ink hover:bg-accent/85",
        secondary: "bg-raise text-text border border-rule hover:border-faint",
        ghost: "text-muted hover:text-text hover:bg-raise",
        danger: "bg-transparent text-danger border border-danger/50 hover:bg-danger/10",
      },
      size: {
        sm: "h-8 px-3 text-sm",
        md: "h-10 px-4 text-base",
        lg: "h-11 px-5 text-base",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  busy?: boolean;
}

export function Button({ className, variant, size, busy, disabled, children, ...props }: ButtonProps) {
  return (
    <button
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      {...props}
    >
      {busy ? <span className="size-3.5 animate-spin rounded-full border-2 border-current border-r-transparent" /> : null}
      {children}
    </button>
  );
}

export { buttonVariants };
