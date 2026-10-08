import Link from "next/link";

/** The CountVision mark (same as the edge app): a counting line and one person. */
export function Mark({ className = "size-7" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <path d="M6 22 L26 10" className="stroke-accent" strokeWidth={3.5} strokeLinecap="round" fill="none" />
      <circle cx="12" cy="12" r="3.2" className="fill-text" />
    </svg>
  );
}

export function Wordmark({ href = "/app" }: { href?: string }) {
  return (
    <Link href={href} className="flex items-center gap-2.5 text-lg font-semibold tracking-[0.02em] no-underline">
      <Mark />
      CountVision
    </Link>
  );
}
