"use client";

import { useParams, useRouter } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { Loading, useMe } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, Label, Select } from "@/components/ui/input";
import { Notice, Panel, PanelHead, RoleBadge } from "@/components/ui/panel";
import {
  api,
  errorText,
  fieldErrors,
  fmtDate,
  ROLE_HELP,
  ROLES,
  roleRank,
  type Invite,
  type Member,
  type Org,
  type Role,
} from "@/lib/api";

/** Roles this person may give to others (owners: all, admins: up to admin). */
function assignable(myRole: Role): Role[] {
  return ROLES.filter((r) => (myRole === "owner" ? true : roleRank(r) <= roleRank(myRole) && r !== "owner"));
}

function InviteDialog({ org, open, onClose, onSent }: { org: Org; open: boolean; onClose: () => void; onSent: (email: string) => void }) {
  const [error, setError] = React.useState<string | null>(null);
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const [role, setRole] = React.useState<Role>("member");

  React.useEffect(() => {
    if (open) {
      setError(null);
      setFields({});
      setRole("member");
    }
  }, [open]);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const email = String(new FormData(e.currentTarget).get("email") ?? "");
    setBusy(true);
    setError(null);
    setFields({});
    try {
      await api.post<Invite>(`/api/orgs/${org.id}/invites`, { email, role });
      onSent(email.trim().toLowerCase());
    } catch (err) {
      setError(errorText(err));
      setFields(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title={`Invite to ${org.name}`}>
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error && !fields.email ? <Notice tone="error">{error}</Notice> : null}
        <Field label="Email address" name="email" type="email" required autoFocus placeholder="name@example.com"
          error={fields.email ? error ?? fields.email : undefined} />
        <div className="space-y-1.5">
          <Label htmlFor="invite-role">Role</Label>
          <Select id="invite-role" value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {assignable(org.my_role).map((r) => (
              <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>
            ))}
          </Select>
          <p className="text-sm text-faint">{ROLE_HELP[role]}.</p>
        </div>
        <p className="text-sm text-muted">We email a link that works for 7 days. Only this address can use it.</p>
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy}>Send invitation</Button>
        </div>
      </form>
    </Dialog>
  );
}

function MemberRow({ m, org, onChange, onError }: {
  m: Member; org: Org; onChange: (msg: string) => void; onError: (msg: string) => void;
}) {
  const [busy, setBusy] = React.useState(false);
  const canManage = roleRank(org.my_role) >= roleRank("admin");
  const mayTouch = !m.is_me && canManage && (org.my_role === "owner" || m.role !== "owner");
  const roleOptions = assignable(org.my_role);

  async function changeRole(role: Role) {
    setBusy(true);
    try {
      await api.patch(`/api/orgs/${org.id}/members/${m.id}`, { role });
      onChange(`${m.name} is now ${role}.`);
    } catch (e) {
      onError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm(`Remove ${m.name} (${m.email}) from ${org.name}?`)) return;
    setBusy(true);
    try {
      await api.del(`/api/orgs/${org.id}/members/${m.id}`);
      onChange(`${m.name} was removed.`);
    } catch (e) {
      onError(errorText(e));
      setBusy(false);
    }
  }

  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3.5" data-testid="member-row">
      <div className="min-w-0 flex-1 basis-56">
        <p className="truncate font-medium">
          {m.name}
          {m.is_me ? <span className="ml-2 text-sm font-normal text-faint">you</span> : null}
        </p>
        <p className="truncate text-sm text-muted">{m.email}</p>
      </div>
      <p className="hidden text-sm text-faint md:block">since {fmtDate(m.joined_at)}</p>
      {mayTouch ? (
        <>
          <label className="sr-only" htmlFor={`role-${m.id}`}>Role of {m.name}</label>
          <Select
            id={`role-${m.id}`}
            className="h-8 w-auto text-sm"
            value={m.role}
            disabled={busy}
            onChange={(e) => void changeRole(e.target.value as Role)}
          >
            {(roleOptions.includes(m.role) ? roleOptions : [m.role, ...roleOptions]).map((r) => (
              <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>
            ))}
          </Select>
          <Button size="sm" variant="ghost" onClick={remove} disabled={busy} aria-label={`Remove ${m.name}`}>Remove</Button>
        </>
      ) : (
        <RoleBadge role={m.role} />
      )}
    </li>
  );
}

function MembersView({ org, reloadOrg }: { org: Org; reloadOrg: () => Promise<void> }) {
  const canManage = roleRank(org.my_role) >= roleRank("admin");
  const { refresh } = useMe();
  const router = useRouter();
  const [members, setMembers] = React.useState<Member[] | null>(null);
  const [invites, setInvites] = React.useState<Invite[]>([]);
  const [notice, setNotice] = React.useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [inviteOpen, setInviteOpen] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      const [m, i] = await Promise.all([
        api.get<Member[]>(`/api/orgs/${org.id}/members`),
        canManage ? api.get<Invite[]>(`/api/orgs/${org.id}/invites`) : Promise.resolve([]),
      ]);
      setMembers(m);
      setInvites(i);
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }, [org.id, canManage]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const changed = async (text: string) => {
    setNotice({ tone: "success", text });
    await Promise.all([load(), reloadOrg()]);
  };
  const failed = (text: string) => setNotice({ tone: "error", text });

  async function revoke(inv: Invite) {
    try {
      await api.del(`/api/orgs/${org.id}/invites/${inv.id}`);
      await changed(`Invitation for ${inv.email} cancelled.`);
    } catch (e) {
      failed(errorText(e));
    }
  }

  async function resend(inv: Invite) {
    try {
      await api.post(`/api/orgs/${org.id}/invites`, { email: inv.email, role: inv.role });
      await changed(`A new link was sent to ${inv.email}. The old link no longer works.`);
    } catch (e) {
      failed(errorText(e));
    }
  }

  async function leave() {
    if (!window.confirm(`Leave ${org.name}? You need a new invitation to come back.`)) return;
    try {
      await api.post(`/api/orgs/${org.id}/leave`);
      await refresh();
      router.replace("/app");
    } catch (e) {
      failed(errorText(e));
    }
  }

  return (
    <>
      <PageTitle title="Members">
        {canManage ? <Button onClick={() => setInviteOpen(true)}>Invite someone</Button> : null}
      </PageTitle>

      {notice ? <Notice tone={notice.tone} className="mb-5">{notice.text}</Notice> : null}

      <Panel>
        <PanelHead title={`${members?.length ?? ""} ${members?.length === 1 ? "person" : "people"} in ${org.name}`} />
        {members ? (
          <ul className="divide-y divide-rule" aria-label="Members">
            {members.map((m) => (
              <MemberRow key={m.id} m={m} org={org} onChange={changed} onError={failed} />
            ))}
          </ul>
        ) : (
          <Loading />
        )}
      </Panel>

      {canManage ? (
        <Panel className="mt-6">
          <PanelHead title="Open invitations" />
          {invites.length === 0 ? (
            <p className="px-5 py-5 text-muted">
              No open invitations. Invite the people who plan staff or read the numbers.
            </p>
          ) : (
            <ul className="divide-y divide-rule" aria-label="Open invitations">
              {invites.map((inv) => (
                <li key={inv.id} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3.5" data-testid="invite-row">
                  <div className="min-w-0 flex-1 basis-56">
                    <p className="truncate font-medium">{inv.email}</p>
                    <p className="text-sm text-muted">
                      {inv.status === "expired" ? (
                        <span className="text-warn">Expired {fmtDate(inv.expires_at)}</span>
                      ) : (
                        <>Valid until {fmtDate(inv.expires_at)}</>
                      )}
                      {inv.invited_by ? <>, invited by {inv.invited_by}</> : null}
                    </p>
                  </div>
                  <RoleBadge role={inv.role} />
                  <Button size="sm" variant="secondary" onClick={() => void resend(inv)}>Send again</Button>
                  <Button size="sm" variant="ghost" onClick={() => void revoke(inv)} aria-label={`Cancel invitation for ${inv.email}`}>Cancel</Button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      ) : null}

      <div className="mt-8 border-t border-rule pt-6">
        <Button variant="ghost" onClick={leave} className="-ml-3 text-danger hover:text-danger">Leave {org.name}</Button>
      </div>

      <InviteDialog
        org={org}
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        onSent={(email) => {
          setInviteOpen(false);
          void changed(`Invitation sent to ${email}.`);
        }}
      />
    </>
  );
}

export default function MembersPage() {
  const { orgId } = useParams<{ orgId: string }>();
  return <OrgGate orgId={orgId}>{(org, reload) => <MembersView org={org} reloadOrg={reload} />}</OrgGate>;
}
