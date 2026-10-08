"use client";

import { useParams, useRouter } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { Loading, useMe } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { api, errorText, fieldErrors, fmtDateTime, roleRank, type AuditEntry, type Org } from "@/lib/api";

/** Plain-language text for an audit entry. */
function describe(e: AuditEntry): string {
  const m = e.meta as Record<string, string | boolean | undefined>;
  switch (e.action) {
    case "org.created": return `created the organization "${m.name}"`;
    case "org.renamed": return `renamed the organization from "${m.old}" to "${m.new}"`;
    case "member.invited": return `${m.renewed ? "sent a new invitation to" : "invited"} ${m.email} as ${m.role}`;
    case "invite.revoked": return `cancelled the invitation for ${m.email}`;
    case "invite.accepted": return `joined as ${m.role}`;
    case "member.role_changed": return `changed the role of ${m.email} from ${m.old} to ${m.new}`;
    case "member.removed": return `removed ${m.email}`;
    case "member.left": return "left the organization";
    default: return e.action;
  }
}

function RenameForm({ org, onSaved }: { org: Org; onSaved: () => Promise<void> }) {
  const { refresh } = useMe();
  const [msg, setMsg] = React.useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    setFields({});
    try {
      await api.patch(`/api/orgs/${org.id}`, { name: new FormData(e.currentTarget).get("name") });
      await Promise.all([onSaved(), refresh()]);
      setMsg({ tone: "success", text: "Name saved." });
    } catch (err) {
      setMsg({ tone: "error", text: errorText(err) });
      setFields(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4 p-5" noValidate>
      {msg && !fields.name ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
      <div className="max-w-[420px]">
        <Field label="Organization name" name="name" defaultValue={org.name} required minLength={2} maxLength={120} error={fields.name} />
      </div>
      <Button type="submit" variant="secondary" busy={busy}>Save name</Button>
    </form>
  );
}

function AuditLog({ org }: { org: Org }) {
  const [rows, setRows] = React.useState<AuditEntry[] | null>(null);
  const [more, setMore] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const PAGE = 25;

  const load = React.useCallback(async (before?: number) => {
    try {
      const data = await api.get<AuditEntry[]>(`/api/orgs/${org.id}/audit?limit=${PAGE}${before ? `&before=${before}` : ""}`);
      setRows((old) => (before && old ? [...old, ...data] : data));
      setMore(data.length === PAGE);
    } catch (e) {
      setError(errorText(e));
    }
  }, [org.id]);

  React.useEffect(() => {
    void load();
  }, [load]);

  if (error) return <Notice tone="error" className="m-5">{error}</Notice>;
  if (!rows) return <Loading />;
  return (
    <>
      <ol className="divide-y divide-rule" aria-label="Activity">
        {rows.map((e) => (
          <li key={e.id} className="flex flex-wrap gap-x-4 gap-y-0.5 px-5 py-3" data-testid="audit-row">
            <span className="min-w-0 flex-1 basis-72">
              <b className="font-medium">{e.actor ?? "Deleted user"}</b> <span className="text-muted">{describe(e)}</span>
            </span>
            <time className="text-sm text-faint" dateTime={e.created_at}>{fmtDateTime(e.created_at)}</time>
          </li>
        ))}
      </ol>
      {more ? (
        <div className="border-t border-rule px-5 py-3">
          <Button size="sm" variant="ghost" onClick={() => void load(rows[rows.length - 1]?.id)}>Show older</Button>
        </div>
      ) : null}
    </>
  );
}

function DeleteOrg({ org }: { org: Org }) {
  const router = useRouter();
  const { refresh } = useMe();
  const [open, setOpen] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [typed, setTyped] = React.useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/orgs/${org.id}/delete`, { confirm_name: typed });
      await refresh();
      router.replace("/app");
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-4 p-5">
      <p className="max-w-[56ch] flex-1 text-muted">
        Deletes the organization with all members, invitations and activity. This cannot be undone.
      </p>
      <Button variant="danger" onClick={() => { setTyped(""); setError(null); setOpen(true); }}>Delete organization</Button>
      <Dialog open={open} onClose={() => setOpen(false)} title={`Delete ${org.name}?`}>
        <form onSubmit={submit} className="space-y-4">
          {error ? <Notice tone="error">{error}</Notice> : null}
          <Field label={`Type "${org.name}" to confirm`} name="confirm_name" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" autoFocus />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Keep it</Button>
            <Button type="submit" variant="danger" busy={busy} disabled={typed.trim() !== org.name}>Delete forever</Button>
          </div>
        </form>
      </Dialog>
    </div>
  );
}

export default function SettingsPage() {
  const { orgId } = useParams<{ orgId: string }>();
  return (
    <OrgGate orgId={orgId}>
      {(org, reload) => {
        const isAdmin = roleRank(org.my_role) >= roleRank("admin");
        return (
          <>
            <PageTitle title="Settings" />
            {!isAdmin ? (
              <Notice>Only admins and owners can change settings. Your role is {org.my_role}.</Notice>
            ) : (
              <div className="space-y-6">
                <Panel>
                  <PanelHead title="Name" />
                  <RenameForm org={org} onSaved={reload} />
                </Panel>
                <Panel>
                  <PanelHead title="Activity" />
                  <AuditLog org={org} />
                </Panel>
                {org.my_role === "owner" ? (
                  <Panel className="border-danger/40">
                    <PanelHead title="Delete organization" />
                    <DeleteOrg org={org} />
                  </Panel>
                ) : null}
              </div>
            )}
          </>
        );
      }}
    </OrgGate>
  );
}
