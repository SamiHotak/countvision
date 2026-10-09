"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import * as React from "react";

import { AddDeviceDialog } from "@/components/add-device";
import { PageTitle } from "@/components/app-shell";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { StatusDot } from "@/components/ui/status-dot";
import { deviceHealth } from "@/lib/device";
import { api, errorText, roleRank, timeAgo, type Device, type Org, type Site } from "@/lib/api";

function DevicesView({ org }: { org: Org }) {
  const [devices, setDevices] = React.useState<Device[] | null>(null);
  const [sites, setSites] = React.useState<Site[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [open, setOpen] = React.useState(false);
  const [, setNow] = React.useState(0);
  const canAdd = roleRank(org.my_role) >= roleRank("member");

  const load = React.useCallback(async () => {
    try {
      const [d, s] = await Promise.all([
        api.get<Device[]>(`/api/orgs/${org.id}/devices`),
        api.get<Site[]>(`/api/orgs/${org.id}/sites`),
      ]);
      setDevices(d);
      setSites(s);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [org.id]);

  React.useEffect(() => {
    void load();
    const timer = setInterval(() => {
      void load();
      setNow(Date.now());
    }, 10_000);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <>
      <PageTitle title="Devices">
        {canAdd && sites.length > 0 ? <Button onClick={() => setOpen(true)}>Add device</Button> : null}
      </PageTitle>
      {error ? <Notice tone="error" className="mb-5">{error}</Notice> : null}

      <Panel>
        <PanelHead title="Computers that count on site" />
        {!devices ? (
          <Loading />
        ) : devices.length === 0 ? (
          <div className="px-5 py-6">
            <p className="max-w-[62ch] text-muted">
              No device yet. A device is the computer at the site (laptop, mini PC) that runs the CountVision
              agent with your cameras. You connect it here with a one-time code.
            </p>
            {sites.length === 0 ? (
              <p className="mt-4">
                <Link href={`/app/orgs/${org.id}/sites`} className="text-accent">Add a site first</Link>
                <span className="text-muted">: every device belongs to a site.</span>
              </p>
            ) : canAdd ? (
              <Button className="mt-4" onClick={() => setOpen(true)}>Add your first device</Button>
            ) : null}
          </div>
        ) : (
          <ul className="divide-y divide-rule" aria-label="Devices">
            {devices.map((d) => {
              const h = deviceHealth(d);
              return (
                <li key={d.id} data-testid="device-row">
                  <Link href={`/app/orgs/${org.id}/devices/${d.id}`}
                    className="flex flex-wrap items-center gap-x-5 gap-y-1.5 px-5 py-3.5 no-underline hover:bg-raise/50">
                    <span className="flex min-w-0 flex-1 basis-56 items-center gap-3">
                      <StatusDot health={h.health} />
                      <span className="min-w-0">
                        <span className="block truncate font-medium">{d.name}</span>
                        <span className="block truncate text-sm text-muted">{d.site_name}</span>
                      </span>
                    </span>
                    <span className="text-sm text-muted">{h.label}</span>
                    <span className="w-28 text-sm text-muted">
                      {d.cameras_online}/{d.camera_count} cameras
                    </span>
                    <span className="w-36 text-sm text-faint">
                      {d.revoked ? "" : `seen ${timeAgo(d.last_seen_at)}`}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </Panel>

      <AddDeviceDialog org={org} sites={sites} open={open} onClose={() => setOpen(false)} onPaired={() => void load()} />
    </>
  );
}

export default function DevicesPage() {
  const { orgId } = useParams<{ orgId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <DevicesView org={org} />}</OrgGate>;
}
