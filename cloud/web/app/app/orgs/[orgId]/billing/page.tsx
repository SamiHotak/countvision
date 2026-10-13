"use client";

import { useParams, useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { billingChanged, CameraSlots, MinutesBar } from "@/components/billing";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { api, errorText, fmtDate, roleRank, type Org } from "@/lib/api";
import { fittingPlan, STATE_LABEL, type Billing, type PlanInfo } from "@/lib/billing";
import { cn } from "@/lib/utils";

function statusLine(b: Billing): string {
  const plan = b.plans.find((p) => p.id === b.plan.id);
  switch (b.state) {
    case "trial":
      return `Free until ${fmtDate(b.trial_ends_at!)} (${b.trial_days_left} day${b.trial_days_left === 1 ? "" : "s"} left). No card needed.`;
    case "active":
      if (b.cancel_at_period_end && b.period_end) return `Ends on ${fmtDate(b.period_end)}. To keep it, open Manage billing.`;
      if (b.paid_from) return `Free until ${fmtDate(b.paid_from)}, then ${plan?.price_eur} EUR per month.`;
      return b.period_end ? `Renews on ${fmtDate(b.period_end)} for ${plan?.price_eur} EUR.` : "Active.";
    case "past_due":
      return "The last payment failed. Stripe tries again over the next days. Please update your card in Manage billing.";
    case "comp":
      return `Granted for free until ${fmtDate(b.comp_until!)}${b.comp_note ? ` (${b.comp_note})` : ""}.`;
    case "expired":
      return "The free trial has ended. Choose a plan below. Your devices keep sending numbers, nothing is lost.";
    case "canceled":
      return "The subscription has ended. Choose a plan below to continue. Your data is kept.";
    default:
      return "Billing is off on this server: no limits.";
  }
}

function PlanRow({ plan, b, canPay, busy, onChoose, onManage }: {
  plan: PlanInfo; b: Billing; canPay: boolean; busy: string | null;
  onChoose: (p: PlanInfo) => void; onManage: () => void;
}) {
  const cams = b.usage.cameras ?? 0;
  const current = b.subscribed_plan === plan.id || (b.state === "comp" && b.plan.id === plan.id);
  const fits = fittingPlan(b.plans, cams)?.id === plan.id && cams > 0 && !b.subscribed_plan;
  const tooSmall = plan.limits.cameras >= 0 && cams > plan.limits.cameras;
  return (
    <li className={cn("grid gap-x-6 gap-y-3 px-5 py-5 sm:grid-cols-[minmax(0,1fr)_minmax(0,260px)_190px] sm:items-center",
      current && "bg-raise/50")} data-testid={`plan-${plan.id}`}>
      <div>
        <div className="flex flex-wrap items-baseline gap-x-3">
          <h3 className="text-lg font-semibold">{plan.name}</h3>
          <span className="text-muted"><span className="text-xl font-semibold text-text">{plan.price_eur} EUR</span> / month</span>
        </div>
        <p className="mt-1 text-sm text-muted">{plan.blurb}</p>
        {fits ? <p className="mt-1.5 text-sm text-accent">Fits your {cams} camera{cams === 1 ? "" : "s"}</p> : null}
        {tooSmall && !current ? <p className="mt-1.5 text-sm text-muted">Too small for your {cams} cameras: the newest would pause.</p> : null}
      </div>
      <div className="space-y-1.5">
        <CameraSlots limit={plan.limits.cameras} used={cams} size="sm" />
        <p className="text-sm text-muted">{plan.limits.cameras} camera{plan.limits.cameras === 1 ? "" : "s"}, {plan.limits.video_minutes} min video analysis</p>
      </div>
      <div className="sm:justify-self-end">
        {current ? (
          <span className="inline-flex h-10 items-center text-sm font-medium text-accent">Your plan</span>
        ) : !canPay ? null : b.subscribed_plan ? (
          <Button variant="secondary" onClick={onManage} busy={busy === "portal"}>Switch in Manage billing</Button>
        ) : (
          <Button variant={fits ? "primary" : "secondary"} onClick={() => onChoose(plan)} busy={busy === plan.id}
            disabled={busy !== null} data-testid={`choose-${plan.id}`}>
            Choose {plan.name}
          </Button>
        )}
      </div>
    </li>
  );
}

function BillingView({ org }: { org: Org }) {
  const params = useSearchParams();
  const router = useRouter();
  const [b, setB] = React.useState<Billing | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<{ tone: "success" | "info"; text: string } | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const canPay = roleRank(org.my_role) >= roleRank("admin");
  const back = params.get("checkout") ?? (params.get("portal") ? "portal" : null);

  const load = React.useCallback(async () => {
    try {
      setB(await api.get<Billing>(`/api/orgs/${org.id}/billing`));
    } catch (e) {
      setError(errorText(e));
    }
  }, [org.id]);

  React.useEffect(() => {
    let stop = false;
    (async () => {
      // Back from Stripe: read the new state straight from Stripe (the webhook may be a few seconds late).
      if ((back === "done" || back === "portal") && canPay) {
        try {
          const fresh = await api.post<Billing>(`/api/orgs/${org.id}/billing/sync`);
          if (stop) return;
          setB(fresh);
          billingChanged();
          if (back === "done") {
            setNotice({ tone: "success", text: fresh.subscribed_plan
              ? `Thank you. Your plan is now ${fresh.plan.name}.`
              : "Payment received. Stripe is still confirming it; this page updates in a moment." });
          }
        } catch (e) {
          setError(errorText(e));
          await load();
        }
        router.replace(`/app/orgs/${org.id}/billing`, { scroll: false });
        return;
      }
      if (back === "cancelled") {
        setNotice({ tone: "info", text: "No payment was made. You can choose a plan any time." });
        router.replace(`/app/orgs/${org.id}/billing`, { scroll: false });
      }
      await load();
    })();
    return () => {
      stop = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [org.id]);

  async function choose(plan: PlanInfo) {
    setBusy(plan.id);
    setError(null);
    try {
      const { url } = await api.post<{ url: string }>(`/api/orgs/${org.id}/billing/checkout`, { plan: plan.id });
      window.location.assign(url);
    } catch (e) {
      setError(errorText(e));
      setBusy(null);
    }
  }

  async function manage() {
    setBusy("portal");
    setError(null);
    try {
      const { url } = await api.post<{ url: string }>(`/api/orgs/${org.id}/billing/portal`);
      window.location.assign(url);
    } catch (e) {
      setError(errorText(e));
      setBusy(null);
    }
  }

  if (!b) return error ? <Notice tone="error">{error}</Notice> : <Loading />;
  if (!b.enabled) {
    return (
      <>
        <PageTitle title="Billing" />
        <Notice>Billing is switched off on this server, so there are no plan limits.</Notice>
      </>
    );
  }
  const cams = b.usage.cameras ?? 0;
  const limit = b.plan.limits.cameras ?? -1;
  const paused = limit >= 0 ? Math.max(0, cams - limit) : 0;

  return (
    <>
      <PageTitle title="Billing">
        {canPay && b.has_customer ? (
          <Button variant="secondary" onClick={manage} busy={busy === "portal"} data-testid="manage-billing">Manage billing</Button>
        ) : null}
      </PageTitle>
      <div className="space-y-6">
        {notice ? <Notice tone={notice.tone} data-testid="billing-notice">{notice.text}</Notice> : null}
        {error ? <Notice tone="error">{error}</Notice> : null}
        {b.test_mode ? (
          <Notice>Stripe test mode: pay with the card 4242 4242 4242 4242, any future date and any CVC. No real money moves.</Notice>
        ) : null}

        <Panel>
          <div className="grid gap-8 px-5 py-6 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <div>
              <p className="text-sm text-muted">Your plan</p>
              <div className="mt-1 flex flex-wrap items-center gap-3">
                <h2 className="text-[length:var(--text-2xl)] font-semibold leading-tight" data-testid="plan-name">{b.plan.name}</h2>
                <span className={cn("inline-flex h-6 items-center rounded-full border px-2.5 text-xs font-medium",
                  b.state === "past_due" || !b.active ? "border-danger/60 text-danger"
                    : b.state === "trial" ? "border-warn/60 text-warn" : "border-accent/50 text-accent")}
                  data-testid="plan-state">
                  {b.state === "trial" ? `${b.trial_days_left} day${b.trial_days_left === 1 ? "" : "s"} left` : STATE_LABEL[b.state]}
                </span>
              </div>
              <p className="mt-3 max-w-[46ch] text-muted">{statusLine(b)}</p>
              {!canPay ? <p className="mt-3 text-sm text-muted">Only owners and admins can change the plan.</p> : null}
            </div>
            <div className="space-y-6">
              <div>
                <h3 className="mb-2 font-medium">Cameras</h3>
                <CameraSlots limit={limit} used={cams} />
                <p className="mt-2 text-sm text-muted" data-testid="camera-usage">
                  {limit < 0 ? `${cams} in use` : `${Math.min(cams, limit)} of ${limit} counting`}
                  {paused ? <span className="text-danger">, {paused} paused: over the plan, numbers not stored</span> : null}
                </p>
              </div>
              <div>
                <h3 className="mb-2 font-medium">Video analysis</h3>
                <MinutesBar used={b.usage.video_minutes ?? 0} limit={b.plan.limits.video_minutes ?? -1} />
              </div>
            </div>
          </div>
        </Panel>

        <Panel>
          <PanelHead title="Plans" />
          <ul className="divide-y divide-rule">
            {b.plans.map((p) => (
              <PlanRow key={p.id} plan={p} b={b} canPay={canPay} busy={busy} onChoose={choose} onManage={manage} />
            ))}
          </ul>
          <p className="border-t border-rule px-5 py-3.5 text-sm text-muted">
            Prices per month, plus VAT. Cancel any time; you keep the plan until the end of the paid month.
            More than {b.plans[b.plans.length - 1]?.limits.cameras} cameras? Ask us for a custom plan.
          </p>
        </Panel>
      </div>
    </>
  );
}

export default function BillingPage() {
  const { orgId } = useParams<{ orgId: string }>();
  return (
    <OrgGate orgId={orgId}>
      {(org) => (
        <React.Suspense fallback={<Loading />}>
          <BillingView org={org} />
        </React.Suspense>
      )}
    </OrgGate>
  );
}
