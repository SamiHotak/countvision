"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import * as React from "react";

import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, Label, Select } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { StatusDot } from "@/components/ui/status-dot";
import {
  api,
  ApiError,
  errorText,
  fmtDateTime,
  roleRank,
  timeAgo,
  type Camera,
  type DeviceDetail,
  type Org,
  type Site,
} from "@/lib/api";
import { cameraHealth, deviceHealth } from "@/lib/device";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="mt-0.5 truncate">{children}</dd>
    </div>
  );
}

function CameraRow({ cam, org, online, onRenamed }: {
  cam: Camera; org: Org; online: boolean; onRenamed: () => void;
}) {
  const h = cameraHealth(cam, online);
  const canEdit = roleRank(org.my_role) >= roleRank("member");
  const lines = Object.keys(cam.today).length ? Object.keys(cam.today) : cam.lines;

  async function rename() {
    const name = window.prompt("Camera name", cam.name)?.trim();
    if (!name || name === cam.name) return;
    try {
      await api.patch(`/api/orgs/${org.id}/cameras/${cam.id}`, { name });
      onRenamed();
    } catch (e) {
      window.alert(errorText(e));
    }
  }

  return (
    <li className="grid gap-x-6 gap-y-3 px-5 py-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]" data-testid="camera-row">
      <div className="min-w-0">
        <div className="flex items-center gap-2.5">
          <StatusDot health={h.health} />
          <p className="truncate font-medium">{cam.name}</p>
          {canEdit ? (
            <button onClick={() => void rename()} className="cursor-pointer text-sm text-faint hover:text-text"
              aria-label={`Rename ${cam.name}`}>Rename</button>
          ) : null}
        </div>
        <p className="mt-1 text-sm text-muted">
          {h.label}
          {online && cam.fps != null && cam.state === "running" ? `, ${cam.fps.toFixed(1)} FPS` : ""}
          {cam.reconnects ? `, ${cam.reconnects} reconnects` : ""}
        </p>
        <p className="mt-0.5 text-sm text-faint">
          id {cam.edge_camera_id}
          {cam.zones.length ? `, zones: ${cam.zones.join(", ")}` : ""}
        </p>
      </div>
      <div>
        {lines.length === 0 ? (
          <p className="text-sm text-muted">No counting line yet. Draw one in the edge app.</p>
        ) : (
          <table className="w-full table-fixed text-left">
            <caption className="sr-only">Counts today for {cam.name}</caption>
            <thead>
              <tr className="text-sm text-muted">
                <th className="pb-1 font-normal">Line, today</th>
                <th className="w-20 pb-1 text-right font-normal">In</th>
                <th className="w-20 pb-1 text-right font-normal">Out</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <tr key={line} className="border-t border-rule">
                  <td className="truncate py-1.5 pr-3">{line}</td>
                  <td className="py-1.5 text-right text-xl font-semibold">{cam.today[line]?.in ?? 0}</td>
                  <td className="py-1.5 pl-4 text-right text-xl font-semibold text-muted">{cam.today[line]?.out ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </li>
  );
}

function EditDialog({ org, device, sites, open, onClose, onSaved }: {
  org: Org; device: DeviceDetail; sites: Site[]; open: boolean; onClose: () => void; onSaved: () => void;
}) {
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await api.patch(`/api/orgs/${org.id}/devices/${device.id}`, { name: form.get("name"), site_id: form.get("site_id") });
      onSaved();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} title={`Edit ${device.name}`}>
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error ? <Notice tone="error">{error}</Notice> : null}
        <Field label="Device name" name="name" defaultValue={device.name} required maxLength={120} />
        <div className="space-y-1.5">
          <Label htmlFor="edit-site">Site</Label>
          <Select id="edit-site" name="site_id" defaultValue={device.site_id}>
            {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy}>Save device</Button>
        </div>
      </form>
    </Dialog>
  );
}

function DeviceView({ org, deviceId }: { org: Org; deviceId: string }) {
  const router = useRouter();
  const [d, setD] = React.useState<DeviceDetail | null>(null);
  const [sites, setSites] = React.useState<Site[]>([]);
  const [error, setError] = React.useState<{ text: string; missing: boolean } | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const [editing, setEditing] = React.useState(false);
  const isAdmin = roleRank(org.my_role) >= roleRank("admin");
  const canEdit = roleRank(org.my_role) >= roleRank("member");

  const load = React.useCallback(async () => {
    try {
      setD(await api.get<DeviceDetail>(`/api/orgs/${org.id}/devices/${deviceId}`));
      setError(null);
    } catch (e) {
      setError({ text: errorText(e), missing: e instanceof ApiError && (e.status === 404 || e.status === 422) });
    }
  }, [org.id, deviceId]);

  React.useEffect(() => {
    void load();
    api.get<Site[]>(`/api/orgs/${org.id}/sites`).then(setSites).catch(() => undefined);
    const timer = setInterval(() => void load(), 10_000);
    return () => clearInterval(timer);
  }, [load, org.id]);

  async function revoke() {
    if (!d || !window.confirm(`Remove ${d.name}? It stops sending data at once. Its numbers stay. ` +
      "To use it again you need a new pairing code.")) return;
    try {
      await api.post(`/api/orgs/${org.id}/devices/${d.id}/revoke`);
      setNotice(`${d.name} was removed. Counting on site goes on locally until you stop it there.`);
      await load();
    } catch (e) {
      setNotice(errorText(e));
    }
  }

  async function remove() {
    if (!d || !window.confirm(`Delete ${d.name} and ALL its counting data? This cannot be undone.`)) return;
    try {
      await api.del(`/api/orgs/${org.id}/devices/${d.id}`);
      router.replace(`/app/orgs/${org.id}/devices`);
    } catch (e) {
      setNotice(errorText(e));
    }
  }

  if (error && !d) {
    return (
      <div className="max-w-[520px] space-y-4">
        <Notice tone="error">{error.missing ? "This device does not exist (any more)." : error.text}</Notice>
        <Link href={`/app/orgs/${org.id}/devices`} className="text-accent">Back to devices</Link>
      </div>
    );
  }
  if (!d) return <Loading />;
  const h = deviceHealth(d);
  const waiting = d.upload?.pending ?? 0;

  return (
    <>
      <p className="mb-2 text-sm">
        <Link href={`/app/orgs/${org.id}/devices`} className="text-muted no-underline hover:text-text">Devices</Link>
      </p>
      <div className="mb-7 flex flex-wrap items-center gap-x-4 gap-y-3">
        <h1 className="text-2xl font-semibold leading-tight">{d.name}</h1>
        <span className="flex items-center gap-2 text-muted" data-testid="device-state">
          <StatusDot health={h.health} /> {h.label}
        </span>
        <span className="ml-auto flex gap-2">
          {canEdit && !d.revoked ? <Button variant="secondary" onClick={() => setEditing(true)}>Edit</Button> : null}
          {isAdmin && !d.revoked ? <Button variant="danger" onClick={() => void revoke()}>Remove device</Button> : null}
          {isAdmin && d.revoked ? <Button variant="danger" onClick={() => void remove()}>Delete device and data</Button> : null}
        </span>
      </div>
      {notice ? <Notice className="mb-5">{notice}</Notice> : null}
      {d.revoked ? (
        <Notice tone="warn" className="mb-5">
          This device was removed. It can no longer send data. Its numbers are kept.
        </Notice>
      ) : !d.online && d.last_seen_at ? (
        <Notice tone="warn" className="mb-5">
          No contact for {timeAgo(d.last_seen_at).replace(" ago", "")}. Check that the computer is on and has
          internet. It keeps counting offline and sends everything when it is back.
        </Notice>
      ) : null}

      <Panel className="mb-6">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 p-5 sm:grid-cols-3 lg:grid-cols-4">
          <Fact label="Site">{d.site_name}</Fact>
          <Fact label="Last contact">{timeAgo(d.last_seen_at)}</Fact>
          <Fact label="Newest numbers">{d.last_data_at ? fmtDateTime(d.last_data_at) : "none yet"}</Fact>
          <Fact label="Waiting on the device">{waiting} {waiting === 1 ? "row" : "rows"}</Fact>
          <Fact label="Detector">
            {d.detector?.name ?? "unknown"}
            {d.detector?.license ? <span className="text-muted">, {d.detector.license}</span> : null}
          </Fact>
          <Fact label="Agent version">{d.agent_version ?? "unknown"}</Fact>
          <Fact label="Uploads, last 24 h">{d.batches_24h} ({d.rows_24h} rows)</Fact>
          <Fact label="Connected">{fmtDateTime(d.paired_at)}</Fact>
        </dl>
      </Panel>

      <Panel>
        <PanelHead title={`Cameras (${d.camera_count})`}>
          <span className="text-sm text-faint">Today since midnight, {d.site_timezone.replace("_", " ")}</span>
        </PanelHead>
        {d.cameras.length === 0 ? (
          <p className="px-5 py-6 text-muted">
            No camera reported yet. Cameras appear here after the device&apos;s first upload (within 15 seconds
            after the counting service starts).
          </p>
        ) : (
          <ul className="divide-y divide-rule" aria-label="Cameras">
            {d.cameras.map((c) => (
              <CameraRow key={c.id} cam={c} org={org} online={d.online} onRenamed={() => void load()} />
            ))}
          </ul>
        )}
      </Panel>

      <EditDialog org={org} device={d} sites={sites} open={editing} onClose={() => setEditing(false)}
        onSaved={() => { setEditing(false); void load(); }} />
    </>
  );
}

export default function DevicePage() {
  const { orgId, deviceId } = useParams<{ orgId: string; deviceId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <DeviceView org={org} deviceId={deviceId} />}</OrgGate>;
}
