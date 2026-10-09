"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, Label, Select } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { api, COMMON_TIMEZONES, errorText, fieldErrors, roleRank, type Org, type Site } from "@/lib/api";

function SiteDialog({ org, site, open, onClose, onSaved }: {
  org: Org; site: Site | null; open: boolean; onClose: () => void; onSaved: (s: Site) => void;
}) {
  const [error, setError] = React.useState<string | null>(null);
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const browserTz = React.useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);
  const zones = React.useMemo(
    () => Array.from(new Set([site?.timezone ?? "Europe/Berlin", browserTz, ...COMMON_TIMEZONES])).filter(Boolean),
    [site, browserTz],
  );

  React.useEffect(() => {
    if (open) {
      setError(null);
      setFields({});
    }
  }, [open]);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const body = { name: form.get("name"), timezone: form.get("timezone"), address: form.get("address") || null };
    setBusy(true);
    setError(null);
    setFields({});
    try {
      const saved = site
        ? await api.patch<Site>(`/api/orgs/${org.id}/sites/${site.id}`, body)
        : await api.post<Site>(`/api/orgs/${org.id}/sites`, body);
      onSaved(saved);
    } catch (err) {
      setError(errorText(err));
      setFields(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title={site ? `Edit ${site.name}` : "Add a site"}>
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error && !fields.name ? <Notice tone="error">{error}</Notice> : null}
        <Field label="Name" name="name" defaultValue={site?.name ?? ""} required maxLength={120} autoFocus
          placeholder="Shop Mitte" error={fields.name} />
        <Field label="Address (optional)" name="address" defaultValue={site?.address ?? ""} maxLength={240}
          placeholder="Hindenburgstraße 1, Mönchengladbach" />
        <div className="space-y-1.5">
          <Label htmlFor="site-tz">Time zone</Label>
          <Select id="site-tz" name="timezone" defaultValue={site?.timezone ?? "Europe/Berlin"}>
            {zones.map((z) => <option key={z} value={z}>{z.replace("_", " ")}</option>)}
          </Select>
          <p className="text-sm text-faint">Decides when &quot;today&quot; starts in counts and reports.</p>
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy}>{site ? "Save site" : "Add site"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function SitesView({ org }: { org: Org }) {
  const [sites, setSites] = React.useState<Site[] | null>(null);
  const [notice, setNotice] = React.useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [editing, setEditing] = React.useState<Site | null>(null);
  const [open, setOpen] = React.useState(false);
  const canEdit = roleRank(org.my_role) >= roleRank("member");
  const canDelete = roleRank(org.my_role) >= roleRank("admin");

  const load = React.useCallback(async () => {
    try {
      setSites(await api.get<Site[]>(`/api/orgs/${org.id}/sites`));
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }, [org.id]);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function remove(site: Site) {
    if (!window.confirm(`Delete the site "${site.name}"?`)) return;
    try {
      await api.del(`/api/orgs/${org.id}/sites/${site.id}`);
      setNotice({ tone: "success", text: `${site.name} deleted.` });
      await load();
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }

  return (
    <>
      <PageTitle title="Sites">
        {canEdit ? <Button onClick={() => { setEditing(null); setOpen(true); }}>Add site</Button> : null}
      </PageTitle>
      {notice ? <Notice tone={notice.tone} className="mb-5">{notice.text}</Notice> : null}

      <Panel>
        <PanelHead title="Places with cameras" />
        {!sites ? (
          <Loading />
        ) : sites.length === 0 ? (
          <div className="px-5 py-6">
            <p className="max-w-[60ch] text-muted">
              A site is one place: a shop, a café, a car park. Add your first site, then connect the
              computer that counts there.
            </p>
            {canEdit ? (
              <Button className="mt-4" onClick={() => { setEditing(null); setOpen(true); }}>Add your first site</Button>
            ) : null}
          </div>
        ) : (
          <ul className="divide-y divide-rule" aria-label="Sites">
            {sites.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-x-5 gap-y-2 px-5 py-3.5" data-testid="site-row">
                <div className="min-w-0 flex-1 basis-56">
                  <p className="truncate font-medium">{s.name}</p>
                  <p className="truncate text-sm text-muted">{s.address || s.timezone.replace("_", " ")}</p>
                </div>
                <Link href={`/app/orgs/${org.id}/devices`} className="text-sm text-muted no-underline hover:text-text">
                  {s.device_count} {s.device_count === 1 ? "device" : "devices"}, {s.camera_count}{" "}
                  {s.camera_count === 1 ? "camera" : "cameras"}
                </Link>
                {canEdit ? (
                  <Button size="sm" variant="secondary" onClick={() => { setEditing(s); setOpen(true); }}>Edit</Button>
                ) : null}
                {canDelete && s.device_count === 0 ? (
                  <Button size="sm" variant="ghost" onClick={() => void remove(s)} aria-label={`Delete ${s.name}`}>Delete</Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <SiteDialog
        org={org}
        site={editing}
        open={open}
        onClose={() => setOpen(false)}
        onSaved={(s) => {
          setOpen(false);
          setNotice({ tone: "success", text: editing ? `${s.name} saved.` : `${s.name} added.` });
          void load();
        }}
      />
    </>
  );
}

export default function SitesPage() {
  const { orgId } = useParams<{ orgId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <SitesView org={org} />}</OrgGate>;
}
