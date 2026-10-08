"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { useMe } from "@/components/me";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/input";
import { Notice } from "@/components/ui/panel";
import { api, errorText, fieldErrors, type Me } from "@/lib/api";

/** Sign-up form. Also used on the invite page (with the invited email fixed). */
export function SignupForm({ next, inviteToken, fixedEmail }: { next: string; inviteToken?: string; fixedEmail?: string }) {
  const router = useRouter();
  const { setMe } = useMe();
  const [error, setError] = React.useState<string | null>(null);
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    setFields({});
    try {
      const me = await api.post<Me>("/api/auth/signup", {
        name: form.get("name"),
        email: fixedEmail ?? form.get("email"),
        password: form.get("password"),
        ...(inviteToken ? { invite_token: inviteToken } : {}),
      });
      setMe(me);
      router.replace(next);
    } catch (err) {
      setError(errorText(err));
      setFields(fieldErrors(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      {error ? <Notice tone="error">{error}</Notice> : null}
      <Field label="Your name" name="name" autoComplete="name" required error={fields.name} autoFocus />
      {fixedEmail ? (
        <Field label="Email" name="email" type="email" value={fixedEmail} readOnly hint="The invitation was sent to this address." />
      ) : (
        <Field label="Email" name="email" type="email" autoComplete="email" required error={fields.email} />
      )}
      <Field
        label="Password"
        name="password"
        type="password"
        autoComplete="new-password"
        required
        minLength={10}
        error={fields.password}
        hint="At least 10 characters. A short sentence works well."
      />
      <Button type="submit" size="lg" className="w-full" busy={busy}>Create account</Button>
      <p className="text-sm text-faint">
        We only use your email to log you in and to send account messages.
      </p>
    </form>
  );
}
