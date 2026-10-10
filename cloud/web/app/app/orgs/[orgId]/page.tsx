"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { OrgGate } from "@/components/org-gate";
import { Panel, RoleBadge } from "@/components/ui/panel";
import { StatusDot } from "@/components/ui/status-dot";
import { api, roleRank, timeAgo, type Device, type Org, type Site } from "@/lib/api";
import { deviceHealth } from "@/lib/device";
import { useLiveReload } from "@/lib/live";
import { cn } from "@/lib/utils";

function Stat({ value, label, href }: { value: React.ReactNode; label: string; href?: string }) {
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

function Step({ n, done, children }: { n: number; done: boolean; children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className={cn("mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full border text-sm",
        done ? "border-accent bg-accent text-accent-ink" : "border-rule text-muted")}>
        {done ? "✓" : n}
      </span>
      <span className={done ? "text-muted line-through decoration-faint" : ""}>{children}</span>
    </li>
  );
}

function Overview({ org }: { org: Org }) {
  const [sites, setSites] = React.useState<Site[] | null>(null);
  const [devices, setDevices] = React.useState<Device[] | null>(null);

  const load = React.useCallback(() => {
    api.get<Site[]>(`/api/orgs/${org.id}/sites`).then(setSites).catch(() => setSites([]));
    api.get<Device[]>(`/api/orgs/${org.id}/devices`).then(setDevices).catch(() => setDevices([]));
  }, [org.id]);
  React.useEffect(load, [load]);
  useLiveReload(org.id, load); // device status changes show within seconds

  const active = (devices ?? []).filter((d) => !d.revoked);
  const cams = active.reduce((n, d) => n + d.camera_count, 0);
  const camsOn = active.reduce((n, d) => n + d.cameras_online, 0);
  const ready = sites !== null && devices !== null;
  const base = `/app/orgs/${org.id}`;

  return (
    <>
      <PageTitle title={org.name}>
        <RoleBadge role={org.my_role} />
      </PageTitle>

      <Panel className="grid grid-cols-3 divide-x divide-rule overflow-hidden">
        <Stat value={ready ? `${camsOn}/${cams}` : "–"} label="cameras counting" href={`${base}/devices`} />
        <Stat value={ready ? `${active.filter((d) => d.online).length}/${active.length}` : "–"} label="devices online"
          href={`${base}/devices`} />
        <Stat value={sites?.length ?? "–"} label={sites?.length === 1 ? "site" : "sites"} href={`${base}/sites`} />
      </Panel>

      {ready && active.length === 0 ? (
        <Panel className="mt-6 p-5 sm:p-6">
          <h2 className="text-lg font-semibold">Get your first camera counting</h2>
          <ol className="mt-4 space-y-3">
            <Step n={1} done={(sites ?? []).length > 0}>
              <Link href={`${base}/sites`} className="text-accent">Add a site</Link>: the shop, café or car park.
            </Step>
            <Step n={2} done={false}>
              Install the CountVision agent on a computer there, then{" "}
              <Link href={`${base}/devices`} className="text-accent">add it as a device</Link> with a one-time code.
            </Step>
            <Step n={3} done={org.member_count > 1}>
              {roleRank(org.my_role) >= roleRank("admin") ? (
                <Link href={`${base}/members`} className="text-accent">Invite your team</Link>
              ) : "Invite your team"}{" "}
              so they can see the numbers.
            </Step>
          </ol>
        </Panel>
      ) : null}

      {active.length > 0 ? (
        <Panel className="mt-6">
          <ul className="divide-y divide-rule" aria-label="Devices">
            {active.map((d) => {
              const h = deviceHealth(d);
              return (
                <li key={d.id}>
                  <Link href={`${base}/devices/${d.id}`}
                    className="flex flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3 no-underline hover:bg-raise/50">
                    <StatusDot health={h.health} />
                    <span className="min-w-0 flex-1 truncate font-medium">{d.name}</span>
                    <span className="text-sm text-muted">{d.site_name}</span>
                    <span className="text-sm text-faint">{h.label}, seen {timeAgo(d.last_seen_at)}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </Panel>
      ) : null}
    </>
  );
}

export default function OrgOverview() {
  const { orgId } = useParams<{ orgId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <Overview org={org} />}</OrgGate>;
}
