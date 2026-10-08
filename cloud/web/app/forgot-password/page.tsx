"use client";

import Link from "next/link";
import * as React from "react";

import { AuthShell } from "@/components/auth-shell";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/input";
import { Notice } from "@/components/ui/panel";
import { api, errorText, type Ok } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [done, setDone] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<Ok>("/api/auth/forgot-password", { email: new FormData(e.currentTarget).get("email") });
      setDone(res.message);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell title="Reset your password" intro="We send you a link to choose a new password.">
      {done ? (
        <div className="space-y-5">
          <Notice tone="success">{done}</Notice>
          <p className="text-muted">The link works for one hour. Check your spam folder if nothing arrives.</p>
          <Link href="/login" className="text-accent">Back to log in</Link>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-4" noValidate>
          {error ? <Notice tone="error">{error}</Notice> : null}
          <Field label="Email" name="email" type="email" autoComplete="email" required autoFocus />
          <Button type="submit" size="lg" className="w-full" busy={busy}>Send reset link</Button>
          <p className="text-sm"><Link href="/login" className="text-muted hover:text-text">Back to log in</Link></p>
        </form>
      )}
    </AuthShell>
  );
}
