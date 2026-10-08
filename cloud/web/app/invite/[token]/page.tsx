"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import * as React from "react";

import { AuthShell } from "@/components/auth-shell";
import { GoogleButton } from "@/components/google-button";
import { Loading, useMe } from "@/components/me";
import { SignupForm } from "@/components/signup-form";
import { Button } from "@/components/ui/button";
import { Notice, RoleBadge } from "@/components/ui/panel";
import { api, ApiError, errorText, fmtDate, ROLE_HELP, type AcceptOut, type InvitePreview } from "@/lib/api";

export default function InvitePage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const { me, loading, refresh } = useMe();
  const [invite, setInvite] = React.useState<InvitePreview | null>(null);
  const [loadError, setLoadError] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const here = `/invite/${token}`;

  React.useEffect(() => {
    api.get<InvitePreview>(`/api/invites/${encodeURIComponent(token)}`)
      .then(setInvite)
      .catch((e) => setLoadError(e instanceof ApiError && e.status === 404
        ? "This invitation does not exist. Check that you opened the full link from the email."
        : errorText(e)));
  }, [token]);

  async function accept() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<AcceptOut>(`/api/invites/${encodeURIComponent(token)}/accept`);
      await refresh();
      router.replace(`/app/orgs/${res.org_id}`);
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  async function logout() {
    await api.post("/api/auth/logout").catch(() => undefined);
    await refresh();
  }

  if (loadError) {
    return (
      <AuthShell title="Invitation not found">
        <Notice tone="error">{loadError}</Notice>
      </AuthShell>
    );
  }
  if (!invite || loading) return <AuthShell title="Invitation"><Loading /></AuthShell>;

  const intro = (
    <>
      {invite.invited_by ?? "Someone"} invited <b className="text-text">{invite.email}</b> to join{" "}
      <b className="text-text">{invite.organization}</b>.
    </>
  );

  if (invite.status !== "pending") {
    const text = {
      accepted: "This invitation was already used.",
      revoked: "This invitation was cancelled. Ask the person who invited you for a new one.",
      expired: "This invitation has expired. Ask the person who invited you for a new one.",
    }[invite.status];
    return (
      <AuthShell title={`Join ${invite.organization}`} intro={intro}>
        <Notice tone="warn">{text}</Notice>
        <p className="mt-5"><Link href="/app" className="text-accent">Go to CountVision</Link></p>
      </AuthShell>
    );
  }

  const roleLine = (
    <div className="mb-6 flex items-start gap-3 rounded-[var(--radius-s)] border border-rule p-3.5">
      <RoleBadge role={invite.role} />
      <p className="text-sm text-muted">{ROLE_HELP[invite.role]}. Valid until {fmtDate(invite.expires_at)}.</p>
    </div>
  );

  if (me) {
    const matches = me.email === invite.email;
    return (
      <AuthShell title={`Join ${invite.organization}`} intro={intro}>
        {roleLine}
        {error ? <Notice tone="error" className="mb-4">{error}</Notice> : null}
        {matches ? (
          <Button size="lg" className="w-full" onClick={accept} busy={busy}>Join {invite.organization}</Button>
        ) : (
          <div className="space-y-4">
            <Notice tone="warn">
              You are logged in as {me.email}. This invitation is for {invite.email}.
            </Notice>
            <Button variant="secondary" size="lg" className="w-full" onClick={logout}>
              Log out and continue as {invite.email}
            </Button>
          </div>
        )}
      </AuthShell>
    );
  }

  return (
    <AuthShell title={`Join ${invite.organization}`} intro={intro}>
      {roleLine}
      {invite.account_exists ? (
        <div className="space-y-4">
          <p className="text-muted">You already have an account with this email.</p>
          <Link
            href={`/login?next=${encodeURIComponent(here)}`}
            className="flex h-11 items-center justify-center rounded-[var(--radius-s)] bg-accent font-medium text-accent-ink no-underline hover:bg-accent/85"
          >
            Log in to accept
          </Link>
        </div>
      ) : (
        <>
          <GoogleButton next={here} />
          <SignupForm next="/app" inviteToken={token} fixedEmail={invite.email} />
        </>
      )}
    </AuthShell>
  );
}
