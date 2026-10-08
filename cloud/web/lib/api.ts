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
