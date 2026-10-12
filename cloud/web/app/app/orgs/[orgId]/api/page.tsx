"use client";

import { useParams } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { api, errorText, roleRank, timeAgo, type Org, type Site } from "@/lib/api";
import type { ApiKeyRow, Channel, Delivery } from "@/lib/integrations";
import { cn } from "@/lib/utils";

function Code({ children, label }: { children: string; label: string }) {
  const [copied, setCopied] = React.useState(false);
  return (
    <figure className="overflow-hidden rounded-[var(--radius-s)] border border-rule bg-ink">
      <figcaption className="flex items-center gap-3 border-b border-rule px-3 py-1.5 text-sm text-muted">
        <span className="mr-auto">{label}</span>
        <button type="button" className="h-7 cursor-pointer rounded px-2 text-xs text-muted hover:bg-raise hover:text-text"
          onClick={() => { void navigator.clipboard?.writeText(children).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }); }}>
          {copied ? "Copied" : "Copy"}
        </button>
      </figcaption>
      <pre className="overflow-x-auto p-3 text-[13px] leading-relaxed" aria-label={label}><code>{children}</code></pre>
    </figure>
  );
}

const VERIFY_PY = `import hashlib, hmac, time

def is_from_countvision(secret: str, body: bytes, header: str) -> bool:
    """header = request.headers["X-CountVision-Signature"], body = the raw request body"""
    parts = dict(p.split("=", 1) for p in header.split(","))
    if abs(time.time() - int(parts["t"])) > 300:      # older than 5 minutes: refuse
        return False
    mac = hmac.new(secret.encode(), parts["t"].encode() + b"." + body, hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), parts["v1"])`;

const VERIFY_JS = `import crypto from "node:crypto";

export function isFromCountVision(secret, rawBody, header) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=")));
  if (Math.abs(Date.now() / 1000 - Number(parts.t)) > 300) return false;
  const mac = crypto.createHmac("sha256", secret).update(parts.t + "." + rawBody).digest("hex");
  return crypto.timingSafeEqual(Buffer.from(mac), Buffer.from(parts.v1));
}`;

function ApiView({ org }: { org: Org }) {
  const isAdmin = roleRank(org.my_role) >= roleRank("admin");
  const canEdit = roleRank(org.my_role) >= roleRank("member");
  const base = `/api/orgs/${org.id}`;
  const [keys, setKeys] = React.useState<ApiKeyRow[] | null>(null);
  const [sites, setSites] = React.useState<Site[]>([]);
  const [hooks, setHooks] = React.useState<Channel[]>([]);
  const [log, setLog] = React.useState<Delivery[]>([]);
  const [open, setOpen] = React.useState(false);
  const [newKey, setNewKey] = React.useState<ApiKeyRow | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState<{ tone: "success" | "error"; text: string } | null>(null);
  const origin = typeof window === "undefined" ? "" : window.location.origin;

  const load = React.useCallback(async () => {
    try {
      const [s, c] = await Promise.all([api.get<Site[]>(`${base}/sites`), api.get<Channel[]>(`${base}/channels`)]);
      setSites(s);
      setHooks(c.filter((x) => x.kind === "webhook"));
      if (isAdmin) setKeys(await api.get<ApiKeyRow[]>(`${base}/api-keys`));
      if (canEdit) setLog(await api.get<Delivery[]>(`${base}/webhook-deliveries?limit=30`));
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }, [base, isAdmin, canEdit]);
  React.useEffect(() => { void load(); }, [load]);

  async function create(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    try {
      const row = await api.post<ApiKeyRow>(`${base}/api-keys`, { name: new FormData(e.currentTarget).get("name") });
      setNewKey(row);
      setOpen(false);
      await load();
    } catch (err) {
      setNotice({ tone: "error", text: errorText(err) });
    } finally {
      setBusy(false);
    }
  }

  async function revoke(k: ApiKeyRow) {
    if (!window.confirm(`Revoke "${k.name}"? Programs using it stop working at once.`)) return;
    try {
      const res = await api.del<{ message: string }>(`${base}/api-keys/${k.id}`);
      setNotice({ tone: "success", text: res.message });
      await load();
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }

  async function retry(d: Delivery) {
    try {
      const res = await api.post<{ ok: boolean; message: string }>(`${base}/webhook-deliveries/${d.id}/retry`);
      setNotice({ tone: res.ok ? "success" : "error", text: res.message });
      await load();
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }

  const site = sites[0];
  const key = newKey?.key ?? "cvk_YOUR_KEY";
  const hookName = new Map(hooks.map((h) => [h.id, h.name]));
  return (
    <>
      <PageTitle title="API and webhooks">
        <a href="/api/v1/docs" target="_blank" rel="noreferrer"
          className="inline-flex h-10 items-center rounded-[var(--radius-s)] border border-rule bg-raise px-4 no-underline hover:border-faint">
          API reference
        </a>
      </PageTitle>
      <p className="mb-5 max-w-[70ch] text-muted">
        Connect CountVision to your own systems: read numbers with the API (pull), or get them pushed by webhooks.
        Only numbers are available, never pictures.
      </p>
      {notice ? <Notice tone={notice.tone} className="mb-5">{notice.text}</Notice> : null}
      {newKey?.key ? (
        <Notice tone="warn" className="mb-5">
          <p className="font-medium text-text">Your new key &quot;{newKey.name}&quot;. Copy it now: it is shown only once.</p>
          <code className="mt-1 block break-all text-text" data-testid="new-api-key">{newKey.key}</code>
          <Button size="sm" variant="secondary" className="mt-2" onClick={() => setNewKey({ ...newKey, key: undefined })}>I copied it</Button>
        </Notice>
      ) : null}

      <Panel>
        <PanelHead title="API keys">
          {isAdmin ? <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>New key</Button> : null}
        </PanelHead>
        {!isAdmin ? <p className="px-5 py-5 text-muted">Only admins see and create API keys.</p>
          : !keys ? <Loading /> : keys.length === 0 ? <p className="px-5 py-5 text-muted">No key yet.</p> : (
            <ul className="divide-y divide-rule" aria-label="API keys">
              {keys.map((k) => (
                <li key={k.id} className="flex flex-wrap items-center gap-x-5 gap-y-1 px-5 py-3" data-testid="key-row">
                  <span className={cn("min-w-0 flex-1 font-medium", k.revoked && "text-faint line-through")}>{k.name}</span>
                  <code className="text-sm text-muted">{k.prefix}…</code>
                  <span className="text-sm text-faint">{k.revoked ? "Revoked" : k.last_used_at ? `Used ${timeAgo(k.last_used_at)}` : "Never used"}</span>
                  {!k.revoked ? <Button size="sm" variant="ghost" onClick={() => void revoke(k)} aria-label={`Revoke ${k.name}`}>Revoke</Button> : null}
                </li>
              ))}
            </ul>
          )}
      </Panel>

      <Panel className="mt-6">
        <PanelHead title="Try it" />
        <div className="space-y-4 px-5 py-4">
          <p className="text-sm text-muted">Send the key in the Authorization header. Limit: 120 requests per minute per key. Times are in the site&apos;s time zone.</p>
          <Code label="List sites">{`curl -H "Authorization: Bearer ${key}" ${origin}/api/v1/sites`}</Code>
          <Code label="Live numbers">{`curl -H "Authorization: Bearer ${key}" ${origin}/api/v1/sites/${site?.id ?? "SITE_ID"}/live`}</Code>
          <Code label="Visitors per hour">{`curl -H "Authorization: Bearer ${key}" "${origin}/api/v1/sites/${site?.id ?? "SITE_ID"}/counts?from=2026-10-01&to=2026-10-07&bucket=hour"`}</Code>
        </div>
      </Panel>

      <Panel className="mt-6">
        <PanelHead title="Webhooks" />
        <div className="space-y-4 px-5 py-4">
          <p className="max-w-[70ch] text-sm text-muted">
            Add a webhook on the Alerts page (Where alerts go, then Add channel, then Webhook). CountVision sends a POST with
            JSON: <code>{`{"id", "type", "created_at", "org_id", "data"}`}</code>. Types: <code>alert.fired</code>,{" "}
            <code>alert.resolved</code>, <code>crossing</code>, <code>counts.hourly</code>, <code>test</code>. Failed
            requests are tried again after 1, 5, 30, 120 and 360 minutes; the same <code>id</code> means the same event.
            Check the <code>X-CountVision-Signature</code> header with your signing secret:
          </p>
          <Code label="Check the signature in Python">{VERIFY_PY}</Code>
          <Code label="Check the signature in Node.js">{VERIFY_JS}</Code>
        </div>
        {canEdit ? (
          <>
            <h3 className="border-t border-rule px-5 pt-4 font-semibold">Recent deliveries</h3>
            {log.length === 0 ? <p className="px-5 py-4 text-sm text-muted">Nothing sent yet.</p> : (
              <ul className="divide-y divide-rule text-sm" aria-label="Webhook deliveries">
                {log.map((d) => (
                  <li key={d.id} className="grid grid-cols-[72px_minmax(0,1fr)_auto] items-baseline gap-x-4 px-5 py-2">
                    <span className={cn("font-medium", d.status === "ok" ? "text-accent" : d.status === "failed" ? "text-danger" : "text-warn")}>
                      {d.status === "ok" ? `OK ${d.response_code ?? ""}` : d.status === "failed" ? "Failed" : "Retrying"}
                    </span>
                    <span className="min-w-0 truncate">
                      <code>{d.event_type}</code> to {hookName.get(d.channel_id) ?? "deleted webhook"}
                      {d.error ? <span className="text-faint">: {d.error}</span> : null}
                      {d.status === "pending" && d.next_try_at ? <span className="text-faint">, next try {timeAgo(d.next_try_at)}</span> : null}
                    </span>
                    <span className="flex items-center gap-2 text-faint">
                      {timeAgo(d.created_at)}
                      {d.status === "failed" && d.event_type !== "test" ? <Button size="sm" variant="ghost" onClick={() => void retry(d)}>Retry</Button> : null}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : null}
      </Panel>

      <Dialog open={open} onClose={() => setOpen(false)} title="New API key">
        <form onSubmit={create} className="space-y-4">
          <Field label="Name (what uses it)" name="name" required maxLength={120} placeholder="Power BI dashboard" autoFocus />
          <p className="text-sm text-faint">The key can read all numbers of this organization. Keep it secret like a password.</p>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" busy={busy}>Create key</Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}

export default function ApiPage() {
  const { orgId } = useParams<{ orgId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <ApiView org={org} />}</OrgGate>;
}
