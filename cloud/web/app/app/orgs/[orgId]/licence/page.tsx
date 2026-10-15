"use client";

// CountVision Local: the licence key (instead of Stripe plans). Checked offline on this computer.

import { useParams } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { billingChanged, CameraSlots } from "@/components/billing";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { api, errorText, fmtDate, roleRank, type Org } from "@/lib/api";
import type { LicenceInfo } from "@/lib/billing";
import { cn } from "@/lib/utils";

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="mt-0.5 truncate">{children}</dd>
    </div>
  );
}

function headline(info: LicenceInfo): { text: string; tone: string } {
  switch (info.state) {
    case "trial":
      return {
        text: info.trial_days_left === 0 ? "Free trial, last day" : `Free trial, ${info.trial_days_left} day${info.trial_days_left === 1 ? "" : "s"} left`,
        tone: "text-warn",
      };
    case "active":
      return { text: `Licensed for ${info.plan.limits.cameras} camera${info.plan.limits.cameras === 1 ? "" : "s"}`, tone: "text-accent" };
    case "licence_expired":
      return { text: "Your licence has ended", tone: "text-danger" };
    default:
      return { text: "Your free trial has ended", tone: "text-danger" };
  }
}

function LicenceView({ org }: { org: Org }) {
  const [info, setInfo] = React.useState<LicenceInfo | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [message, setMessage] = React.useState<string | null>(null);
  const [key, setKey] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const isAdmin = roleRank(org.my_role) >= roleRank("admin");
  const isOwner = org.my_role === "owner";

  const load = React.useCallback(async () => {
    try {
      setInfo(await api.get<LicenceInfo>(`/api/orgs/${org.id}/licence`));
    } catch (e) {
      setError(errorText(e));
    }
  }, [org.id]);
  React.useEffect(() => void load(), [load]);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const out = await api.put<LicenceInfo>(`/api/orgs/${org.id}/licence`, { key });
      setInfo(out);
      setKey("");
      setMessage(out.message ?? "Licence added.");
      billingChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm("Remove the licence key from this computer? CountVision goes back to the free trial " +
      "(or stops showing dashboards when the trial is over).")) return;
    try {
      setInfo(await api.del<LicenceInfo>(`/api/orgs/${org.id}/licence`));
      setMessage("Licence removed.");
      billingChanged();
    } catch (e) {
      setError(errorText(e));
    }
  }

  if (!info) return error ? <Notice tone="error">{error}</Notice> : <Loading />;
  const head = headline(info);
  const used = info.usage.cameras ?? 0;
  const limit = info.plan.limits.cameras ?? 0;

  return (
    <>
      <PageTitle title="Licence" />
      {message ? <Notice tone="success" className="mb-5">{message}</Notice> : null}

      <Panel className="mb-6">
        <div className="space-y-5 p-5">
          <p className={cn("text-xl font-semibold", head.tone)} data-testid="licence-state">{head.text}</p>
          <div>
            <p className="mb-2 text-sm text-muted">
              Cameras: <span className="text-text">{used}</span> in use
              {limit >= 0 ? <>, room for <span className="text-text">{limit}</span></> : null}
            </p>
            <CameraSlots limit={limit} used={used} />
          </div>
          {info.state === "trial" ? (
            <p className="max-w-[64ch] text-muted">
              The free trial counts up to {info.trial_plan.limits.cameras} cameras until{" "}
              {info.trial_ends_at ? fmtDate(info.trial_ends_at) : "its end"}. With a licence key everything keeps
              running as it is: nothing is set up again and no data is lost.
            </p>
          ) : !info.active ? (
            <p className="max-w-[64ch] text-muted">
              The cameras still count and the numbers are kept. Add a licence key to see dashboards, reports and
              alerts again.
            </p>
          ) : null}
          {info.licence ? (
            <dl className="grid grid-cols-2 gap-x-6 gap-y-4 border-t border-rule pt-5 sm:grid-cols-3" data-testid="licence-facts">
              <Fact label="Licensed to">{info.licence.customer}</Fact>
              <Fact label="Cameras">{info.licence.cameras}</Fact>
              <Fact label="Valid until">{info.licence.valid_until ? fmtDate(info.licence.valid_until) : "Does not end"}</Fact>
              <Fact label="Updates and support until">
                {info.licence.updates_until ? fmtDate(info.licence.updates_until) : "Not included"}
              </Fact>
              <Fact label="Licence number">{info.licence.id}</Fact>
              <Fact label="Issued">{info.licence.issued ? fmtDate(info.licence.issued) : "-"}</Fact>
            </dl>
          ) : null}
          {info.licence_problem ? <Notice tone="warn">The saved key cannot be used: {info.licence_problem}</Notice> : null}
        </div>
      </Panel>

      <Panel>
        <PanelHead title={info.licence ? "Replace the licence key" : "Add a licence key"} />
        <div className="p-5">
          {!info.can_check ? (
            <Notice tone="warn" className="mb-4">
              This copy of CountVision Local cannot check licence keys. Please contact your supplier for an updated
              version.
            </Notice>
          ) : null}
          {isAdmin ? (
            <form onSubmit={add} className="space-y-3" noValidate>
              {error ? <Notice tone="error">{error}</Notice> : null}
              <Label htmlFor="licence-key">Licence key</Label>
              <textarea id="licence-key" value={key} onChange={(e) => setKey(e.target.value)} rows={4}
                spellCheck={false} autoComplete="off" placeholder="CVL1-…"
                className="w-full rounded-[var(--radius-s)] border border-rule bg-ink px-3 py-2 font-mono text-sm text-text placeholder:text-faint focus-visible:border-accent focus-visible:outline-none"
                data-testid="licence-key" />
              <p className="text-sm text-faint">
                Paste the whole key from your email (it starts with CVL1-). It is checked on this computer: no internet
                needed.
              </p>
              <div className="flex flex-wrap items-center gap-3 pt-1">
                <Button type="submit" busy={busy} disabled={key.trim().length < 10} data-testid="licence-save">
                  {info.licence ? "Replace key" : "Add key"}
                </Button>
                {info.licence && isOwner ? (
                  <Button type="button" variant="ghost" onClick={() => void remove()}>Remove licence</Button>
                ) : null}
              </div>
            </form>
          ) : (
            <p className="text-muted">Only owners and admins can add a licence key.</p>
          )}
        </div>
      </Panel>
    </>
  );
}

export default function LicencePage() {
  const { orgId } = useParams<{ orgId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <LicenceView org={org} />}</OrgGate>;
}
