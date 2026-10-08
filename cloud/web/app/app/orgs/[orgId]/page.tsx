"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { PageTitle } from "@/components/app-shell";
import { OrgGate } from "@/components/org-gate";
import { Panel, RoleBadge } from "@/components/ui/panel";
import { roleRank } from "@/lib/api";

function Stat({ value, label, href }: { value: number | string; label: string; href?: string }) {
  const body = (
    <>
      <span className="block text-[40px] font-semibold leading-none tracking-tight sm:text-[length:var(--text-num)]">{value}</span>
      <span className="mt-2 block text-sm text-muted sm:mt-3 sm:text-base">{label}</span>
    </>
  );
  return href ? (
    <Link href={href} className="block p-4 no-underline hover:bg-raise/50 sm:p-6">{body}</Link>
  ) : (
    <div className="p-4 sm:p-6">{body}</div>
  );
}

export default function OrgOverview() {
  const { orgId } = useParams<{ orgId: string }>();
  return (
    <OrgGate orgId={orgId}>
      {(org) => (
        <>
          <PageTitle title={org.name}>
            <RoleBadge role={org.my_role} />
          </PageTitle>

          <Panel className="grid grid-cols-3 divide-x divide-rule overflow-hidden">
            <Stat value={org.member_count} label={org.member_count === 1 ? "member" : "members"} href={`/app/orgs/${org.id}/members`} />
            <Stat value={0} label="sites" />
            <Stat value={0} label="cameras online" />
          </Panel>

          <Panel className="mt-6 p-5 sm:p-6">
            <h2 className="text-lg font-semibold">No cameras connected yet</h2>
            <p className="mt-2 max-w-[62ch] text-muted">
              Cameras are added through a CountVision device on site: a mini PC that counts on the spot and sends
              only numbers here. Pairing a device with this organization is the next step in the setup.
            </p>
            {roleRank(org.my_role) >= roleRank("admin") ? (
              <p className="mt-4">
                <Link href={`/app/orgs/${org.id}/members`} className="text-accent">Invite your team</Link>
                <span className="text-muted"> while the device is being set up.</span>
              </p>
            ) : null}
          </Panel>
        </>
      )}
    </OrgGate>
  );
}
