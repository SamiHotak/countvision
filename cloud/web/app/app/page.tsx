"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { Loading, useMe } from "@/components/me";

/** /app: open the last used organization, the first one, or the "new organization" step. */
export default function AppHome() {
  const { me } = useMe();
  const router = useRouter();
  React.useEffect(() => {
    if (!me) return;
    let last: string | null = null;
    try {
      last = localStorage.getItem("cv:last-org");
    } catch {
      /* storage blocked: ignore */
    }
    const org = me.organizations.find((o) => o.id === last) ?? me.organizations[0];
    router.replace(org ? `/app/orgs/${org.id}` : "/app/new-org");
  }, [me, router]);
  return <Loading />;
}
