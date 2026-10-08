"use client";

import Link from "next/link";

import { Loading } from "@/components/me";
import { useOrg } from "@/components/use-org";
import { Notice } from "@/components/ui/panel";
import type { Org } from "@/lib/api";

/** Loads the organization; shows "not found" for wrong ids or organizations you left. */
export function OrgGate({ orgId, children }: { orgId: string; children: (org: Org, reload: () => Promise<void>) => React.ReactNode }) {
  const { org, error, reload } = useOrg(orgId);
  if (error) {
    return (
      <div className="max-w-[520px] space-y-4">
        <Notice tone="error">
          {error.notFound ? "This organization does not exist, or you are no longer a member." : error.text}
        </Notice>
        <Link href="/app" className="text-accent">Go to your organizations</Link>
      </div>
    );
  }
  if (!org) return <Loading />;
  return <>{children(org, reload)}</>;
}
