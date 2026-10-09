// Small typed client for the CountVision API (same origin, cookie login).

export type Role = "owner" | "admin" | "member" | "viewer";

export const ROLES: Role[] = ["owner", "admin", "member", "viewer"];

export const ROLE_HELP: Record<Role, string> = {
  owner: "Everything, including managing owners and deleting the organization",
  admin: "Manage members, invitations and settings",
  member: "Set up sites, cameras and counting lines",
  viewer: "See dashboards and reports only",
};

export function roleRank(role: Role): number {
  return { owner: 4, admin: 3, member: 2, viewer: 1 }[role];
}

export interface OrgBrief { id: string; name: string; slug: string; role: Role }

export interface Me {
  id: string;
  email: string;
  name: string;
  email_verified: boolean;
  has_password: boolean;
  google_linked: boolean;
  created_at: string;
  organizations: OrgBrief[];
}

export interface Org {
  id: string;
  name: string;
  slug: string;
  created_at: string;
  my_role: Role;
  member_count: number;
}

export interface Member {
  id: string;
  user_id: string;
  email: string;
  name: string;
  role: Role;
  joined_at: string;
  is_me: boolean;
}

export interface Invite {
  id: string;
  email: string;
  role: Role;
  status: string;
  invited_by: string | null;
  created_at: string;
  expires_at: string;
}

export interface InvitePreview {
  organization: string;
  email: string;
  role: Role;
  invited_by: string | null;
  status: "pending" | "accepted" | "revoked" | "expired";
  expires_at: string;
  account_exists: boolean;
}

export interface AuditEntry {
  id: number;
  action: string;
  actor: string | null;
  target_type: string | null;
  target_id: string | null;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface Ok { ok: boolean; message: string | null }

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public fields: Record<string, string> = {},
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        // Required by the API for every change (protection against cross-site requests).
        "X-CountVision": "1",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the server. Check your connection and try again.");
  }
  if (resp.status === 204) return undefined as T;
  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const err = data?.error;
    throw new ApiError(
      resp.status,
      err?.code ?? "http_" + resp.status,
      err?.message ?? `The server answered ${resp.status}. Please try again.`,
      err?.fields ?? {},
    );
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body: unknown = {}) => request<T>("POST", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export function errorText(e: unknown): string {
  return e instanceof ApiError ? e.message : "Something went wrong. Please try again.";
}

export function fieldErrors(e: unknown): Record<string, string> {
  return e instanceof ApiError ? e.fields : {};
}

const dateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const timeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export const fmtDate = (iso: string) => dateFmt.format(new Date(iso));
export const fmtDateTime = (iso: string) => timeFmt.format(new Date(iso));

export interface AcceptOut { org_id: string; org_name: string }

// --- sites, devices, cameras (Phase 3 B) ------------------------------------------------------

export interface Site {
  id: string;
  name: string;
  timezone: string;
  address: string | null;
  created_at: string;
  device_count: number;
  camera_count: number;
}

export interface PairingCode {
  id: string;
  code: string | null;
  site_id: string;
  device_name: string;
  expires_at: string;
  status: "pending" | "used" | "expired";
  device_id: string | null;
}

export interface Device {
  id: string;
  name: string;
  site_id: string;
  site_name: string;
  edge_device_id: string;
  agent_version: string | null;
  online: boolean;
  last_seen_at: string | null;
  last_data_at: string | null;
  paired_at: string;
  revoked: boolean;
  camera_count: number;
  cameras_online: number;
  detector: { name?: string; runtime?: string; device?: string; license?: string } | null;
  upload: { pending?: number; last_success?: number | null; state?: string } | null;
}

export interface Camera {
  id: string;
  edge_camera_id: string;
  name: string;
  state: string | null;
  connected: boolean | null;
  fps: number | null;
  reconnects: number | null;
  alert: string | null;
  lines: string[];
  zones: string[];
  last_seen_at: string | null;
  today: Record<string, { in: number; out: number }>;
}

export interface DeviceDetail extends Device {
  cameras: Camera[];
  site_timezone: string;
  batches_24h: number;
  rows_24h: number;
}

const rtf = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/** "12 seconds ago", "3 minutes ago", "yesterday" ... */
export function timeAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "never";
  const s = Math.round((new Date(iso).getTime() - now) / 1000);
  const abs = Math.abs(s);
  if (abs < 10) return "just now";
  if (abs < 60) return rtf.format(s, "second");
  if (abs < 3600) return rtf.format(Math.round(s / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(s / 3600), "hour");
  return rtf.format(Math.round(s / 86400), "day");
}

export const COMMON_TIMEZONES = [
  "Europe/Berlin", "Europe/Vienna", "Europe/Zurich", "Europe/Amsterdam", "Europe/Brussels", "Europe/Paris",
  "Europe/London", "Europe/Madrid", "Europe/Rome", "Europe/Warsaw", "Europe/Istanbul", "Asia/Kabul",
  "Asia/Dubai", "America/New_York", "America/Los_Angeles", "UTC",
];
