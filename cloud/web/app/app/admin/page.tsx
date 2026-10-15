"use client";

import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { billingChanged, CameraSlots } from "@/components/billing";
import { Loading, useMe } from "@/components/me";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Label, Select } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { api, errorText, fmtDate, fmtDateTime, timeAgo } from "@/lib/api";
import { STATE_LABEL, type AdminAudit, type AdminOrg, type AdminOrgDetail, type AdminOverview, type BillingState } from "@/lib/billing";
import { cn } from "@/lib/utils";

const PLAN_IDS = [["starter", "Starter"], ["shop", "Shop"], ["business", "Business"]] as const;

const STATE_TONE: Record<BillingState, string> = {
  off: "text-muted", trial: "text-warn", active: "text-accent", comp: "text-accent", past_due: "text-danger",
  expired: "text-danger", canceled: "text-danger", licence_expired: "text-danger",
};

function when(o: AdminOrg): string {
  if (o.state === "trial" && o.trial_ends_at) return `trial until ${fmtDate(o.trial_ends_at)}`;
  if (o.state === "comp" && o.comp_until) return `granted until ${fmtDate(o.comp_until)}`;
  if (o.period_end && (o.state === "active" || o.state === "past_due"))
    return `${o.cancel_at_period_end ? "ends" : "renews"} ${fmtDate(o.period_end)}`;
  return "";
}

/** Plain words for an audit action (falls back to the action name). */
function auditText(a: AdminAudit): string {
  const m = a.meta as Record<string, string | number | boolean | null>;
  switch (a.action) {
    case "billing.subscription_changed":
      return `Plan ${m.old_plan ?? "none"} (${m.old_status ?? "-"}) became ${m.new_plan ?? "none"} (${m.new_status ?? "-"})`;
    case "billing.checkout_started":
      return `Opened Stripe Checkout for ${m.plan}`;
    case "billing.portal_opened":
      return "Opened the Stripe customer portal";
    case "admin.plan_granted":
      return `Granted ${m.plan} until ${m.until ? fmtDate(String(m.until)) : "?"}${m.note ? ` (${m.note})` : ""}`;
    case "admin.grant_removed":
      return `Removed the grant (${m.plan ?? "-"})`;
    case "admin.trial_extended":
      return `Trial extended by ${m.days} days`;
    case "camera.plan_paused":
      return `Camera ${m.name} paused: plan allows ${m.limit}`;
    case "camera.plan_resumed":
      return `Camera ${m.name} counts again`;
    default:
      return a.action;
  }
}

function OrgDialog({ orgId, onClose, onChanged }: { orgId: string | null; onClose: () => void; onChanged: () => void }) {
  const [d, setD] = React.useState<AdminOrgDetail | null>(null);
  const [msg, setMsg] = React.useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!orgId) return;
    try {
      setD(await api.get<AdminOrgDetail>(`/api/admin/orgs/${orgId}`));
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    }
  }, [orgId]);

  React.useEffect(() => {
    setD(null);
    setMsg(null);
    void load();
  }, [load]);

  async function act(key: string, fn: () => Promise<{ message: string | null }>) {
    setBusy(key);
    setMsg(null);
    try {
      const res = await fn();
      setMsg({ tone: "success", text: res.message ?? "Done." });
      await load();
      onChanged();
      billingChanged();
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(null);
    }
  }

  function grant(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const until = new Date(`${f.get("until")}T23:59:00`).toISOString();
    void act("grant", () => api.post(`/api/admin/orgs/${orgId}/grant`, { plan: f.get("plan"), until, note: f.get("note") || null }));
  }

  function extend(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const days = Number(new FormData(e.currentTarget).get("days"));
    void act("trial", () => api.post(`/api/admin/orgs/${orgId}/trial`, { days }));
  }

  const in30 = new Date(Date.now() + 30 * 86400_000).toISOString().slice(0, 10);
  return (
    <Dialog open={orgId !== null} onClose={onClose} title={d?.name ?? "Organization"} className="w-[min(640px,calc(100vw-32px))]">
      {!d ? <Loading /> : (
        <div className="space-y-5" data-testid="admin-org-dialog">
          {msg ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
          <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 text-sm">
            <dt className="text-muted">Plan</dt>
            <dd><span className={STATE_TONE[d.state]}>{STATE_LABEL[d.state]}</span>
              {d.state !== "trial" && d.active ? `, ${d.plan_name}` : ""}{when(d) ? `, ${when(d)}` : ""}</dd>
            <dt className="text-muted">Cameras</dt>
            <dd className="flex items-center gap-3"><CameraSlots limit={d.limits.cameras ?? -1} used={d.usage.cameras ?? 0} size="sm" />
              {d.usage.cameras ?? 0} / {(d.limits.cameras ?? -1) < 0 ? "any" : d.limits.cameras}</dd>
            <dt className="text-muted">Video</dt><dd>{d.usage.video_minutes ?? 0} min this month</dd>
            <dt className="text-muted">Devices</dt><dd>{d.devices_online ?? 0} online of {d.devices ?? 0}, {d.sites ?? 0} sites, last data {timeAgo(d.last_data_at)}</dd>
            <dt className="text-muted">Stripe</dt><dd className="break-all">{d.stripe_customer_id ?? "no customer yet"}{d.sub_status ? `, ${d.sub_status}` : ""}</dd>
            <dt className="text-muted">Members</dt>
            <dd>{d.members_list.map((m) => `${m.email} (${m.role})`).join(", ")}</dd>
          </dl>

          <form onSubmit={grant} className="space-y-3 border-t border-rule pt-4">
            <h3 className="font-medium">Give a plan for free (pilot)</h3>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="grant-plan">Plan</Label>
                <Select id="grant-plan" name="plan" defaultValue={d.comp_plan ?? "shop"}>
                  {PLAN_IDS.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
                </Select>
              </div>
              <Field label="Until" name="until" type="date" required defaultValue={d.comp_until?.slice(0, 10) ?? in30} />
            </div>
            <Field label="Note (shown to the customer)" name="note" maxLength={200} placeholder="Pilot, 1 month" defaultValue={d.comp_note ?? ""} />
            <div className="flex flex-wrap gap-2">
              <Button type="submit" busy={busy === "grant"} data-testid="admin-grant">Save grant</Button>
              {d.comp_plan ? (
                <Button type="button" variant="danger" busy={busy === "ungrant"}
                  onClick={() => void act("ungrant", () => api.del(`/api/admin/orgs/${orgId}/grant`))}>Remove grant</Button>
              ) : null}
            </div>
          </form>

          <form onSubmit={extend} className="flex flex-wrap items-end gap-3 border-t border-rule pt-4">
            <div className="w-28 space-y-1.5">
              <Label htmlFor="trial-days">Extend trial by</Label>
              <Input id="trial-days" name="days" type="number" min={1} max={365} defaultValue={14} />
            </div>
            <Button type="submit" variant="secondary" busy={busy === "trial"}>Extend trial</Button>
            {d.stripe_customer_id ? (
              <Button type="button" variant="ghost" busy={busy === "sync"}
                onClick={() => void act("sync", () => api.post(`/api/admin/orgs/${orgId}/sync`))}>Update from Stripe</Button>
            ) : null}
          </form>

          <div className="border-t border-rule pt-4">
            <h3 className="mb-2 font-medium">Recent activity</h3>
            <ul className="max-h-56 space-y-1.5 overflow-auto text-sm">
              {d.audit.map((a) => (
                <li key={a.id} className="flex gap-3"><span className="w-36 shrink-0 whitespace-nowrap text-muted">{fmtDateTime(a.created_at)}</span>
                  <span className="min-w-0">{auditText(a)}{a.actor ? <span className="text-muted"> by {a.actor}</span> : null}</span></li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </Dialog>
  );
}

export default function AdminPage() {
  const { me } = useMe();
  const [overview, setOverview] = React.useState<AdminOverview | null>(null);
  const [orgs, setOrgs] = React.useState<AdminOrg[] | null>(null);
  const [feed, setFeed] = React.useState<AdminAudit[]>([]);
  const [filter, setFilter] = React.useState("");
  const [q, setQ] = React.useState("");
  const [open, setOpen] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    try {
      const [ov, list, audit] = await Promise.all([
        api.get<AdminOverview>("/api/admin/overview"),
        api.get<AdminOrg[]>(`/api/admin/orgs${q ? `?q=${encodeURIComponent(q)}` : ""}`),
        api.get<AdminAudit[]>(`/api/admin/audit?limit=60${filter ? `&action=${encodeURIComponent(filter)}` : ""}`),
      ]);
      setOverview(ov);
      setOrgs(list);
      setFeed(audit);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    }
  }, [q, filter]);

  React.useEffect(() => {
    const t = setTimeout(() => void load(), 250);
    return () => clearTimeout(t);
  }, [load]);

  if (me && !me.is_platform_admin) return <Notice tone="error">This page is only for the operator of this server.</Notice>;
  if (error && !orgs) return <Notice tone="error">{error}</Notice>;
  if (!overview || !orgs) return <Loading />;

  const figures: [string, React.ReactNode][] = [
    ["Organizations", overview.organizations],
    ["Paying", overview.paying],
    ["Monthly revenue", `${overview.mrr_eur} EUR`],
    ["In trial", overview.states.trial ?? 0],
    ["Trial ends in 3 days", overview.trial_ending_3d],
    ["New this week", overview.new_orgs_7d],
  ];

  return (
    <>
      <PageTitle title="Admin" />
      <div className="space-y-6">
        <Panel className="grid grid-cols-2 divide-rule sm:grid-cols-3 lg:grid-cols-6 lg:divide-x">
          {figures.map(([label, value]) => (
            <div key={label} className="px-5 py-4">
              <div className="text-[length:var(--text-xl)] font-semibold tabular-nums">{value}</div>
              <div className="text-sm text-muted">{label}</div>
            </div>
          ))}
        </Panel>

        <Panel>
          <PanelHead title="Organizations">
            <Input className="h-9 w-[min(280px,100%)]" placeholder="Search name or email" value={q}
              onChange={(e) => setQ(e.target.value)} aria-label="Search organizations" />
          </PanelHead>
          {orgs.length === 0 ? <p className="px-5 py-6 text-muted">No organization matches.</p> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="whitespace-nowrap text-muted">
                  <tr className="border-b border-rule">
                    <th className="px-5 py-2.5 font-medium">Organization</th>
                    <th className="px-3 py-2.5 font-medium">Plan</th>
                    <th className="px-3 py-2.5 font-medium">Cameras</th>
                    <th className="px-3 py-2.5 font-medium">Devices</th>
                    <th className="px-3 py-2.5 font-medium">Last data</th>
                    <th className="px-5 py-2.5 font-medium" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-rule">
                  {orgs.map((o) => (
                    <tr key={o.id} data-testid={`admin-org-${o.slug}`}>
                      <td className="px-5 py-3">
                        <div className="font-medium">{o.name}</div>
                        <div className="text-muted">{o.owner_email ?? "no owner"}, {o.members} member{o.members === 1 ? "" : "s"}</div>
                      </td>
                      <td className="px-3 py-3">
                        <div><span className={STATE_TONE[o.state]}>{STATE_LABEL[o.state]}</span>{o.state !== "trial" && o.active ? `, ${o.plan_name}` : ""}</div>
                        <div className="text-muted">{when(o)}</div>
                      </td>
                      <td className="px-3 py-3"><CameraSlots limit={o.limits.cameras ?? -1} used={o.usage.cameras ?? 0} size="sm" /></td>
                      <td className="px-3 py-3 tabular-nums">{o.devices_online ?? 0} / {o.devices ?? 0}</td>
                      <td className="px-3 py-3 text-muted">{timeAgo(o.last_data_at)}</td>
                      <td className="px-5 py-3 text-right">
                        <Button size="sm" variant="secondary" onClick={() => setOpen(o.id)}>Open</Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel>
          <PanelHead title="Activity, all organizations">
            <Select className="h-9 w-auto" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter activity">
              <option value="">Everything</option>
              <option value="billing.">Billing</option>
              <option value="admin.">Admin actions</option>
              <option value="camera.plan">Camera limits</option>
              <option value="org.">Organizations</option>
              <option value="device.">Devices</option>
            </Select>
          </PanelHead>
          <ul className="divide-y divide-rule text-sm" data-testid="admin-feed">
            {feed.map((a) => (
              <li key={a.id} className={cn("grid gap-x-4 gap-y-0.5 px-5 py-2.5 sm:grid-cols-[150px_minmax(0,180px)_minmax(0,1fr)]")}>
                <span className="text-muted">{fmtDateTime(a.created_at)}</span>
                <span className="truncate">{a.org_name ?? "Server"}</span>
                <span className="min-w-0">{auditText(a)}{a.actor ? <span className="text-muted"> by {a.actor}</span> : null}</span>
              </li>
            ))}
            {feed.length === 0 ? <li className="px-5 py-4 text-muted">Nothing yet.</li> : null}
          </ul>
        </Panel>
      </div>
      <OrgDialog orgId={open} onClose={() => setOpen(null)} onChanged={() => void load()} />
    </>
  );
}
