"use client";

import { useParams } from "next/navigation";

import { AppShell } from "@/components/app-shell";
import { RequireLogin } from "@/components/me";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const params = useParams<{ orgId?: string }>();
  return (
    <RequireLogin>
      {(me) => (
        <AppShell me={me} orgId={params.orgId ?? null}>
          {children}
        </AppShell>
      )}
    </RequireLogin>
  );
}
