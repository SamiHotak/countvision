"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { AuthShell } from "@/components/auth-shell";
import { useEdition } from "@/components/edition";
import { GoogleButton } from "@/components/google-button";
import { useMe } from "@/components/me";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/input";
import { Notice } from "@/components/ui/panel";
import { api, errorText, type Me } from "@/lib/api";
import { safeNext } from "@/lib/utils";

function LoginForm() {
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const router = useRouter();
  const { setMe } = useMe();
  const [error, setError] = React.useState<string | null>(params.get("error"));
  const [busy, setBusy] = React.useState(false);
  const { edition, loaded } = useEdition();
  const local = edition.edition === "local";
  React.useEffect(() => {
    if (loaded && edition.needs_setup) router.replace("/setup"); // CountVision Local, first start
  }, [loaded, edition.needs_setup, router]);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    try {
      const me = await api.post<Me>("/api/auth/login", { email: form.get("email"), password: form.get("password") });
      setMe(me);
      router.replace(next);
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <AuthShell title="Log in" intro={local
      ? <>CountVision Local on this computer. New team members get an invitation link from the owner.</>
      : <>New here? <Link href={`/signup?next=${encodeURIComponent(next)}`} className="text-accent">Create an account</Link></>}>
      <GoogleButton next={next} />
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error ? <Notice tone="error">{error}</Notice> : null}
        <Field label="Email" name="email" type="email" autoComplete="email" required autoFocus />
        <Field label="Password" name="password" type="password" autoComplete="current-password" required />
        <Button type="submit" size="lg" className="w-full" busy={busy}>Log in</Button>
        {edition.email ? (
          <p className="text-sm">
            <Link href="/forgot-password" className="text-muted hover:text-text">Forgot your password?</Link>
          </p>
        ) : (
          <p className="text-sm text-faint" data-testid="no-email-reset">
            Forgot your password? The owner can invite you again. The owner&apos;s own password is reset with
            the CountVision tools on this computer.
          </p>
        )}
      </form>
    </AuthShell>
  );
}

export default function LoginPage() {
  return (
    <React.Suspense>
      <LoginForm />
    </React.Suspense>
  );
}
