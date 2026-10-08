"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import * as React from "react";

import { AuthShell } from "@/components/auth-shell";
import { useMe } from "@/components/me";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/input";
import { Notice } from "@/components/ui/panel";
import { api, errorText, fieldErrors, type Ok } from "@/lib/api";

function ResetForm() {
  const token = useSearchParams().get("token") ?? "";
  const { setMe } = useMe();
  const [done, setDone] = React.useState(false);
  const [error, setError] = React.useState<string | null>(token ? null : "This link is incomplete. Open the link from the email again.");
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    if (form.get("password") !== form.get("repeat")) {
      setFields({ repeat: "The two passwords are different." });
      return;
    }
    setBusy(true);
    setError(null);
    setFields({});
    try {
      await api.post<Ok>("/api/auth/reset-password", { token, password: form.get("password") });
      setMe(null); // all sessions were logged out
      setDone(true);
    } catch (err) {
      setError(errorText(err));
      setFields(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <AuthShell title="Password changed">
        <div className="space-y-5">
          <Notice tone="success">Your new password is saved. All devices were logged out.</Notice>
          <Link href="/login" className="text-accent">Log in with the new password</Link>
        </div>
      </AuthShell>
    );
  }
  return (
    <AuthShell title="Choose a new password">
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error ? <Notice tone="error">{error}</Notice> : null}
        <Field label="New password" name="password" type="password" autoComplete="new-password" required minLength={10}
          error={fields.password} hint="At least 10 characters." autoFocus />
        <Field label="Repeat the new password" name="repeat" type="password" autoComplete="new-password" required error={fields.repeat} />
        <Button type="submit" size="lg" className="w-full" busy={busy} disabled={!token}>Save new password</Button>
        <p className="text-sm"><Link href="/forgot-password" className="text-muted hover:text-text">Ask for a new link</Link></p>
      </form>
    </AuthShell>
  );
}

export default function Page() {
  return (
    <React.Suspense>
      <ResetForm />
    </React.Suspense>
  );
}
