"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import * as React from "react";

import { AuthShell } from "@/components/auth-shell";
import { Loading, useMe } from "@/components/me";
import { Notice } from "@/components/ui/panel";
import { api, errorText, type Ok } from "@/lib/api";

function Verify() {
  const token = useSearchParams().get("token") ?? "";
  const { refresh } = useMe();
  const [state, setState] = React.useState<{ ok: boolean; text: string } | null>(null);
  const started = React.useRef(false);

  React.useEffect(() => {
    if (started.current) return; // run once (React strict mode runs effects twice in development)
    started.current = true;
    if (!token) {
      setState({ ok: false, text: "This link is incomplete. Open the link from the email again." });
      return;
    }
    api.post<Ok>("/api/auth/verify-email", { token })
      .then(() => {
        setState({ ok: true, text: "Your email address is confirmed." });
        void refresh();
      })
      .catch((e) => setState({ ok: false, text: errorText(e) }));
  }, [token, refresh]);

  return (
    <AuthShell title="Confirm your email">
      {state ? (
        <div className="space-y-5">
          <Notice tone={state.ok ? "success" : "error"}>{state.text}</Notice>
          <Link href="/app" className="text-accent">{state.ok ? "Continue to CountVision" : "Go to CountVision"}</Link>
          {!state.ok ? <p className="text-sm text-muted">Logged in? You can ask for a new link under Account.</p> : null}
        </div>
      ) : (
        <Loading label="Checking the link" />
      )}
    </AuthShell>
  );
}

export default function Page() {
  return (
    <React.Suspense>
      <Verify />
    </React.Suspense>
  );
}
