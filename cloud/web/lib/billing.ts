// Billing (Phase 4 C): plan, trial, usage, Stripe Checkout/portal, operator admin page.

export interface PlanInfo {
  id: string;
  name: string;
  price_eur: number | null;
  limits: Record<string, number>;
  blurb: string;
}

export type BillingState = "off" | "trial" | "active" | "past_due" | "comp" | "expired" | "canceled";

export interface BillingBrief {
  enabled: boolean;
  state: BillingState;
  active: boolean;
  plan: string;
  plan_name: string;
  trial_days_left: number | null;
}

export interface Billing {
  enabled: boolean;
  test_mode: boolean;
  state: BillingState;
  active: boolean;
  plan: PlanInfo;
  subscribed_plan: string | null;
  trial_ends_at: string | null;
  trial_days_left: number | null;
  period_end: string | null;
  cancel_at_period_end: boolean;
  paid_from: string | null;
  comp_until: string | null;
  comp_note: string | null;
  has_customer: boolean;
  usage: Record<string, number>;
  plans: PlanInfo[];
  trial_plan: PlanInfo;
  synced_at: string | null;
}

export const STATE_LABEL: Record<BillingState, string> = {
  off: "No limits",
  trial: "Free trial",
  active: "Active",
  past_due: "Payment failed",
  comp: "Granted",
  expired: "Trial ended",
  canceled: "Ended",
};

/** -1 = no limit. */
export const limitText = (n: number | undefined, unit: string) =>
  n === undefined || n < 0 ? `Unlimited ${unit}` : `${n} ${unit}`;

/** The smallest plan that fits the cameras in use (null = none fits: talk to us). */
export function fittingPlan(plans: PlanInfo[], cameras: number): PlanInfo | null {
  return plans.find((p) => (p.limits.cameras ?? -1) < 0 || p.limits.cameras >= cameras) ?? null;
}

export interface AdminOrg {
  id: string;
  name: string;
  slug: string;
  created_at: string;
  owner_email: string | null;
  members: number;
  state: BillingState;
  active: boolean;
  plan: string;
  plan_name: string;
  limits: Record<string, number>;
  usage: Record<string, number>;
  trial_ends_at: string | null;
  period_end: string | null;
  cancel_at_period_end: boolean;
  comp_plan: string | null;
  comp_until: string | null;
  comp_note: string | null;
  stripe_customer_id: string | null;
  sub_status: string | null;
  devices?: number;
  devices_online?: number;
  sites?: number;
  last_data_at?: string | null;
}

export interface AdminAudit {
  id: number;
  action: string;
  org_id: string | null;
  org_name: string | null;
  actor: string | null;
  target_type: string | null;
  target_id: string | null;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface AdminOrgDetail extends AdminOrg {
  audit: AdminAudit[];
  members_list: { email: string; name: string; role: string }[];
}

export interface AdminOverview {
  organizations: number;
  users: number;
  new_orgs_7d: number;
  states: Partial<Record<BillingState, number>>;
  paying: number;
  mrr_eur: number;
  trial_ending_3d: number;
}
