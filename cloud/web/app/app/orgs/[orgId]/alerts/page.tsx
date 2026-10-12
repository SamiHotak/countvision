"use client";

import { useParams } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, Input, Label, Select } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { StatusDot } from "@/components/ui/status-dot";
import { api, errorText, fieldErrors, roleRank, timeAgo, type Member, type Org, type Site } from "@/lib/api";
import type { Dashboard } from "@/lib/dashboard";
import {
  EVENT_INFO, KIND_INFO, type AlertEvent, type AlertKind, type AlertRule, type Channel, type ChannelKind,
} from "@/lib/integrations";
import { useLive } from "@/lib/live";
import { cn } from "@/lib/utils";

const KIND_ORDER: AlertKind[] = ["occupancy", "queue", "camera_offline", "traffic_drop"];
const CHANNEL_LABEL: Record<ChannelKind, string> = { email: "Email", telegram: "Telegram", webhook: "Webhook" };

function channelDetail(c: Channel): string {
  if (c.kind === "email") return (c.recipients ?? []).map((r) => r.name + (r.email_verified ? "" : " (email not confirmed)")).join(", ") || "nobody";
  if (c.kind === "telegram") return `Chat: ${c.chat_title ?? c.chat_id}`;
  return c.url ?? "";
}

// --- channel dialog ----------------------------------------------------------------------------

function ChannelDialog({ org, members, open, onClose, onSaved }: {
  org: Org; members: Member[]; open: boolean; onClose: () => void; onSaved: (c: Channel) => void;
}) {
  const [kind, setKind] = React.useState<ChannelKind>("telegram");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [chosen, setChosen] = React.useState<Set<string>>(new Set());
  const [token, setToken] = React.useState("");
  const [chats, setChats] = React.useState<{ id: number; title: string; type: string; bot: string | null }[] | null>(null);
  const [chat, setChat] = React.useState<string>("");
  const [events, setEvents] = React.useState<Set<string>>(new Set());

  React.useEffect(() => {
    if (!open) return;
    setError(null); setFields({}); setToken(""); setChats(null); setChat(""); setEvents(new Set());
    const me = members.find((m) => m.is_me);
    setChosen(new Set(me ? [me.user_id] : []));
  }, [open, members]);

  async function findChats() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.post<{ chats: { id: number; title: string; type: string; bot: string | null }[] }>(
        `/api/orgs/${org.id}/channels/telegram/chats`, { bot_token: token.trim() });
      setChats(res.chats);
      if (res.chats.length) setChat(String(res.chats[0].id));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const body: Record<string, unknown> = { kind, name: form.get("name") };
    if (kind === "email") body.recipients = [...chosen];
    if (kind === "telegram") {
      const c = chats?.find((x) => String(x.id) === chat);
      Object.assign(body, { bot_token: token.trim(), chat_id: c?.id ?? chat, chat_title: c?.title ?? chat });
    }
    if (kind === "webhook") Object.assign(body, { url: form.get("url"), events: [...events] });
    setBusy(true);
    setError(null);
    try {
      onSaved(await api.post<Channel>(`/api/orgs/${org.id}/channels`, body));
    } catch (err) {
      setError(errorText(err));
      setFields(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  const tgReady = kind !== "telegram" || (token.trim().length > 20 && !!chat);
  return (
    <Dialog open={open} onClose={onClose} title="Add a channel" className="w-[min(560px,calc(100vw-32px))]">
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error ? <Notice tone="error">{error}</Notice> : null}
        <div className="inline-flex rounded-[var(--radius-s)] border border-rule bg-ink p-0.5" role="radiogroup" aria-label="Kind">
          {(["telegram", "email", "webhook"] as const).map((k) => (
            <button key={k} type="button" role="radio" aria-checked={kind === k} onClick={() => setKind(k)}
              className={cn("h-8 cursor-pointer rounded-[5px] px-3 text-sm", kind === k ? "bg-raise text-text" : "text-muted hover:text-text")}>
              {CHANNEL_LABEL[k]}
            </button>
          ))}
        </div>
        <Field label="Name" name="name" required maxLength={120} key={kind}
          defaultValue={kind === "telegram" ? "My phone" : kind === "email" ? "Team email" : "Our system"} error={fields.name} />

        {kind === "telegram" ? (
          <div className="space-y-3">
            <ol className="list-decimal space-y-1 pl-5 text-sm text-muted">
              <li>In Telegram, open <b className="text-text">@BotFather</b>, send <code>/newbot</code> and follow the steps.</li>
              <li>Copy the <b className="text-text">token</b> it gives you and paste it below.</li>
              <li>Open your new bot and send it any message (for a group: add the bot to the group, write in it).</li>
              <li>Click <b className="text-text">Find my chat</b> and choose the chat.</li>
            </ol>
            <div className="space-y-1.5">
              <Label htmlFor="tg-token">Bot token</Label>
              <div className="flex gap-2">
                <Input id="tg-token" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off"
                  placeholder="123456789:AA..." aria-invalid={fields.bot_token ? true : undefined} />
                <Button type="button" variant="secondary" onClick={() => void findChats()} busy={busy && !chats}
                  disabled={token.trim().length < 20}>Find my chat</Button>
              </div>
              <p className="text-sm text-faint">The token is stored encrypted. Anyone with it can write as your bot.</p>
            </div>
            {chats ? (
              chats.length ? (
                <div className="space-y-1.5">
                  <Label htmlFor="tg-chat">Send alerts to</Label>
                  <Select id="tg-chat" value={chat} onChange={(e) => setChat(e.target.value)}>
                    {chats.map((c) => <option key={c.id} value={c.id}>{c.title} ({c.type === "private" ? "private chat" : c.type})</option>)}
                  </Select>
                </div>
              ) : (
                <Notice tone="warn">No chat found. Send a message to {chats[0]?.bot ? `@${chats[0].bot}` : "your bot"} first, then click Find my chat again.</Notice>
              )
            ) : null}
          </div>
        ) : null}

        {kind === "email" ? (
          <fieldset>
            <legend className="mb-1.5 text-sm font-medium text-muted">Send to</legend>
            <ul className="max-h-[200px] space-y-1 overflow-auto rounded-[var(--radius-s)] border border-rule bg-ink p-2">
              {members.map((m) => (
                <li key={m.user_id}>
                  <label className="flex cursor-pointer items-center gap-3 rounded px-2 py-1.5 hover:bg-raise/60">
                    <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={chosen.has(m.user_id)}
                      onChange={(e) => setChosen((p) => { const n = new Set(p); if (e.target.checked) n.add(m.user_id); else n.delete(m.user_id); return n; })} />
                    <span className="min-w-0 truncate">{m.name}{m.is_me ? " (you)" : ""}</span>
                    <span className="ml-auto truncate text-sm text-faint">{m.email}</span>
                  </label>
                </li>
              ))}
            </ul>
            <p className="mt-1.5 text-sm text-faint">Only members with a confirmed email address get alerts.</p>
          </fieldset>
        ) : null}

        {kind === "webhook" ? (
          <div className="space-y-3">
            <Field label="Address (URL)" name="url" required maxLength={500} placeholder="https://example.com/hooks/countvision"
              error={fields.url} hint="CountVision sends a signed POST with JSON. Alerts go here when you choose this channel in an alert." />
            <fieldset>
              <legend className="mb-1.5 text-sm font-medium text-muted">Also send</legend>
              {Object.entries(EVENT_INFO).map(([ev, text]) => (
                <label key={ev} className="flex cursor-pointer items-start gap-3 py-1">
                  <input type="checkbox" className="mt-1 size-4 accent-[var(--color-accent)]" checked={events.has(ev)}
                    onChange={(e) => setEvents((p) => { const n = new Set(p); if (e.target.checked) n.add(ev); else n.delete(ev); return n; })} />
                  <span><code className="text-sm">{ev}</code> <span className="text-sm text-muted">{text}</span></span>
                </label>
              ))}
            </fieldset>
          </div>
        ) : null}

        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy && (kind !== "telegram" || !!chats)} disabled={!tgReady || (kind === "email" && chosen.size === 0)}>
            Add channel
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

// --- rule dialog ---------------------------------------------------------------------------------

function RuleDialog({ org, sites, channels, rule, open, onClose, onSaved }: {
  org: Org; sites: Site[]; channels: Channel[]; rule: AlertRule | null; open: boolean; onClose: () => void;
  onSaved: () => void;
}) {
  const [kind, setKind] = React.useState<AlertKind>("occupancy");
  const [siteId, setSiteId] = React.useState("");
  const [threshold, setThreshold] = React.useState("20");
  const [duration, setDuration] = React.useState("3");
  const [zone, setZone] = React.useState("");
  const [camera, setCamera] = React.useState("");
  const [picked, setPicked] = React.useState<Set<string>>(new Set());
  const [shape, setShape] = React.useState<Dashboard | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [fields, setFields] = React.useState<Record<string, string>>({});

  React.useEffect(() => {
    if (!open) return;
    const k = rule?.kind ?? "occupancy";
    setKind(k);
    setSiteId(rule?.site_id ?? sites[0]?.id ?? "");
    setThreshold(String(rule?.threshold ?? KIND_INFO[k].defaultThreshold));
    setDuration(String(rule?.duration_min ?? KIND_INFO[k].defaultDuration));
    setZone(rule?.zone ?? "");
    setCamera(rule?.camera_id ?? "");
    setPicked(new Set(rule?.channel_ids ?? channels.map((c) => c.id).slice(0, 1)));
    setError(null);
    setFields({});
  }, [open, rule, sites, channels]);

  React.useEffect(() => {
    if (!open || !siteId) return;
    setShape(null);
    api.get<Dashboard>(`/api/orgs/${org.id}/sites/${siteId}/dashboard`).then(setShape).catch(() => setShape(null));
  }, [open, org.id, siteId]);

  function pickKind(k: AlertKind) {
    setKind(k);
    if (!rule) {
      setThreshold(String(KIND_INFO[k].defaultThreshold));
      setDuration(String(KIND_INFO[k].defaultDuration));
    }
    setZone("");
  }

  const zoneKind = kind === "queue" ? "queue" : "area";
  const zones = (shape?.zones ?? []).filter((z) => z.kind === zoneKind);
  const cameras = shape?.coverage.cameras ?? [];

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const body = {
      site_id: siteId, kind, threshold: Number(threshold || 0), duration_min: Number(duration || 1),
      zone: zone || null, camera_id: camera || null, channel_ids: [...picked],
      name: (e.currentTarget as HTMLFormElement).querySelector<HTMLInputElement>("#rule-name")?.value || undefined,
    };
    setBusy(true);
    setError(null);
    try {
      if (rule) await api.patch(`/api/orgs/${org.id}/alert-rules/${rule.id}`, body);
      else await api.post(`/api/orgs/${org.id}/alert-rules`, body);
      onSaved();
    } catch (err) {
      setError(errorText(err));
      setFields(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  const info = KIND_INFO[kind];
  return (
    <Dialog open={open} onClose={onClose} title={rule ? "Edit alert" : "New alert"} className="w-[min(580px,calc(100vw-32px))]">
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error ? <Notice tone="error">{error}</Notice> : null}
        <fieldset>
          <legend className="mb-1.5 text-sm font-medium text-muted">When</legend>
          <div className="grid gap-2 sm:grid-cols-2">
            {KIND_ORDER.map((k) => (
              <label key={k} className={cn("cursor-pointer rounded-[var(--radius-s)] border px-3 py-2.5",
                kind === k ? "border-accent bg-raise" : "border-rule hover:border-faint")}>
                <input type="radio" name="kind" className="sr-only" checked={kind === k} onChange={() => pickKind(k)} />
                <span className="block font-medium">{KIND_INFO[k].label}</span>
                <span className="block text-sm text-muted">{KIND_INFO[k].help}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="rule-site">Site</Label>
            <Select id="rule-site" value={siteId} onChange={(e) => { setSiteId(e.target.value); setZone(""); setCamera(""); }}>
              {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
          </div>
          {kind === "occupancy" || kind === "queue" ? (
            <div className="space-y-1.5">
              <Label htmlFor="rule-zone">{kind === "queue" ? "Queue zone" : "Zone"}</Label>
              <Select id="rule-zone" value={zone} onChange={(e) => setZone(e.target.value)}>
                <option value="">{zones.length ? `All ${zoneKind} zones together` : `No ${zoneKind} zone yet`}</option>
                {zones.map((z) => <option key={`${z.camera_id}${z.zone}`} value={z.zone}>{z.zone} ({z.camera_name})</option>)}
              </Select>
            </div>
          ) : (
            <div className="space-y-1.5">
              <Label htmlFor="rule-cam">Camera</Label>
              <Select id="rule-cam" value={camera} onChange={(e) => setCamera(e.target.value)}>
                <option value="">All cameras of the site</option>
                {cameras.map((c) => <option key={c.camera_id} value={c.camera_id}>{c.camera_name}</option>)}
              </Select>
            </div>
          )}
          {kind !== "camera_offline" ? (
            <Field label={info.thresholdLabel} name="threshold" type="number" min={kind === "traffic_drop" ? 1 : 0.5}
              max={kind === "traffic_drop" ? 99 : 100000} step={kind === "traffic_drop" ? 5 : 1} value={threshold}
              onChange={(e) => setThreshold(e.target.value)} error={fields.threshold} />
          ) : null}
          <Field label={info.durationLabel} name="duration" type="number" min={kind === "traffic_drop" ? 30 : 1} max={1440}
            value={duration} onChange={(e) => setDuration(e.target.value)} error={fields.duration_min} />
        </div>
        {(kind === "occupancy" || kind === "queue") && shape && !zones.length ? (
          <Notice tone="warn">This site has no {zoneKind} zone. Draw one in the camera editor (Devices, then the camera, then Edit lines and zones).</Notice>
        ) : null}
        <fieldset>
          <legend className="mb-1.5 text-sm font-medium text-muted">Send to</legend>
          {channels.length ? (
            <div className="flex flex-wrap gap-2">
              {channels.map((c) => (
                <label key={c.id} className={cn("flex cursor-pointer items-center gap-2 rounded-[var(--radius-s)] border px-3 py-1.5 text-sm",
                  picked.has(c.id) ? "border-accent" : "border-rule")}>
                  <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={picked.has(c.id)}
                    onChange={(e) => setPicked((p) => { const n = new Set(p); if (e.target.checked) n.add(c.id); else n.delete(c.id); return n; })} />
                  {c.name} <span className="text-faint">{CHANNEL_LABEL[c.kind]}</span>
                </label>
              ))}
            </div>
          ) : <p className="text-sm text-muted">Add a channel first (below the alerts).</p>}
          {fields.channel_ids ? <p className="mt-1 text-sm text-danger">{fields.channel_ids}</p> : null}
        </fieldset>
        <Field label="Name (optional)" id="rule-name" name="name" maxLength={120} defaultValue={rule?.name ?? ""}
          placeholder={info.label} key={`${rule?.id}-${open}`} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy} disabled={!picked.size || !siteId}>{rule ? "Save alert" : "Create alert"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

// --- page -----------------------------------------------------------------------------------

function ruleCondition(r: AlertRule): string {
  const where = r.zone ? ` in ${r.zone}` : "";
  if (r.kind === "occupancy") return `${r.threshold} or more people inside${where} for ${r.duration_min} min`;
  if (r.kind === "queue") return `${r.threshold} or more people waiting${where} for ${r.duration_min} min`;
  if (r.kind === "camera_offline") return `A camera does not count for ${r.duration_min} min`;
  return `Visitors in ${r.duration_min} min below ${r.threshold} % of a normal week`;
}

function AlertsView({ org }: { org: Org }) {
  const [rules, setRules] = React.useState<AlertRule[] | null>(null);
  const [channels, setChannels] = React.useState<Channel[]>([]);
  const [events, setEvents] = React.useState<AlertEvent[]>([]);
  const [sites, setSites] = React.useState<Site[]>([]);
  const [members, setMembers] = React.useState<Member[]>([]);
  const [notice, setNotice] = React.useState<{ tone: "success" | "error" | "warn"; text: string } | null>(null);
  const [ruleOpen, setRuleOpen] = React.useState(false);
  const [editing, setEditing] = React.useState<AlertRule | null>(null);
  const [chOpen, setChOpen] = React.useState(false);
  const [secret, setSecret] = React.useState<{ name: string; value: string } | null>(null);
  const [testing, setTesting] = React.useState<string | null>(null);
  const canEdit = roleRank(org.my_role) >= roleRank("member");
  const base = `/api/orgs/${org.id}`;

  const load = React.useCallback(async () => {
    try {
      const [r, c, e, s, m] = await Promise.all([
        api.get<AlertRule[]>(`${base}/alert-rules`), api.get<Channel[]>(`${base}/channels`),
        api.get<AlertEvent[]>(`${base}/alert-events?limit=50`), api.get<Site[]>(`${base}/sites`),
        api.get<Member[]>(`${base}/members`),
      ]);
      setRules(r); setChannels(c); setEvents(e); setSites(s); setMembers(m);
    } catch (err) {
      setNotice({ tone: "error", text: errorText(err) });
    }
  }, [base]);

  React.useEffect(() => { void load(); }, [load]);
  useLive(org.id, React.useCallback((msg) => { if (msg.type === "alert") void load(); }, [load]));
  React.useEffect(() => {
    const id = setInterval(() => void load(), 60_000);
    return () => clearInterval(id);
  }, [load]);

  async function test(c: Channel) {
    setTesting(c.id);
    setNotice(null);
    try {
      const res = await api.post<{ message: string }>(`${base}/channels/${c.id}/test`);
      setNotice({ tone: "success", text: res.message });
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    } finally {
      setTesting(null);
      void load();
    }
  }

  async function act(fn: () => Promise<unknown>, ok?: string) {
    try {
      await fn();
      if (ok) setNotice({ tone: "success", text: ok });
      await load();
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }

  const byId = new Map(channels.map((c) => [c.id, c]));
  return (
    <>
      <PageTitle title="Alerts">
        {canEdit && sites.length ? (
          <Button onClick={() => { setEditing(null); setRuleOpen(true); }} disabled={!channels.length}
            title={channels.length ? undefined : "Add a channel first"}>New alert</Button>
        ) : null}
      </PageTitle>
      {notice ? <Notice tone={notice.tone} className="mb-5">{notice.text}</Notice> : null}
      {secret ? (
        <Notice tone="warn" className="mb-5">
          <p className="font-medium text-text">Signing secret of {secret.name}. Copy it now: it is shown only once.</p>
          <code className="mt-1 block break-all text-text" data-testid="signing-secret">{secret.value}</code>
          <p className="mt-1">Your system uses it to check that a request really comes from CountVision (see the API page).</p>
          <Button size="sm" variant="secondary" className="mt-2" onClick={() => setSecret(null)}>I copied it</Button>
        </Notice>
      ) : null}

      <Panel>
        <PanelHead title="Your alerts" />
        {!rules ? <Loading /> : rules.length === 0 ? (
          <div className="px-5 py-6 text-muted">
            <p className="max-w-[62ch]">
              Get a message when a shop is full, a queue is long, a camera stops counting or visitors drop. First add a
              channel below (Telegram is the fastest), then create an alert.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-rule" aria-label="Alert rules">
            {rules.map((r) => (
              <li key={r.id} className="flex flex-wrap items-start gap-x-5 gap-y-2 px-5 py-4" data-testid="rule-row">
                <StatusDot className="mt-1.5" health={!r.enabled ? "idle" : r.state === "firing" ? "down" : r.pending_since ? "warn" : "ok"} />
                <div className="min-w-0 flex-1 basis-64">
                  <p className="font-medium">
                    {r.name}<span className="text-muted">, {r.site_name}</span>
                    {r.state === "firing" ? <span className="ml-2 rounded-full border border-danger/60 px-2 py-0.5 text-xs text-danger">Active since {timeAgo(r.fired_at)}</span> : null}
                    {!r.enabled ? <span className="ml-2 rounded-full border border-rule px-2 py-0.5 text-xs text-muted">Paused</span> : null}
                  </p>
                  <p className="text-sm text-muted">{ruleCondition(r)}</p>
                  <p className="text-sm text-faint">
                    To {r.channel_ids.map((id) => byId.get(id)?.name).filter(Boolean).join(", ") || "nobody (choose a channel)"}
                    {r.last_message ? `. Now: ${r.last_message}` : ""}
                  </p>
                </div>
                {canEdit ? (
                  <div className="flex flex-wrap gap-1">
                    <Button size="sm" variant="ghost" onClick={() => { setEditing(r); setRuleOpen(true); }}>Edit</Button>
                    <Button size="sm" variant="ghost" onClick={() => void act(() => api.patch(`${base}/alert-rules/${r.id}`, { enabled: !r.enabled }))}>
                      {r.enabled ? "Pause" : "Resume"}
                    </Button>
                    <Button size="sm" variant="ghost" aria-label={`Delete ${r.name}`}
                      onClick={() => window.confirm(`Delete the alert "${r.name}"?`) && void act(() => api.del(`${base}/alert-rules/${r.id}`), "Alert deleted.")}>
                      Delete
                    </Button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel className="mt-6">
        <PanelHead title="Where alerts go">
          {canEdit ? <Button size="sm" variant="secondary" onClick={() => setChOpen(true)}>Add channel</Button> : null}
        </PanelHead>
        {channels.length === 0 ? (
          <p className="px-5 py-5 text-muted">No channel yet. Add Telegram, email or a webhook.</p>
        ) : (
          <ul className="divide-y divide-rule" aria-label="Channels">
            {channels.map((c) => (
              <li key={c.id} className="flex flex-wrap items-start gap-x-5 gap-y-2 px-5 py-3.5" data-testid="channel-row">
                <StatusDot className="mt-1.5" health={!c.enabled || !c.secret_ok ? "idle" : c.last_error && (!c.last_ok_at || c.last_error_at! > c.last_ok_at) ? "warn" : "ok"} />
                <div className="min-w-0 flex-1 basis-64">
                  <p className="font-medium">{c.name} <span className="text-sm font-normal text-faint">{CHANNEL_LABEL[c.kind]}</span></p>
                  <p className="truncate text-sm text-muted">{channelDetail(c)}</p>
                  {c.kind === "webhook" && c.events?.length ? <p className="text-sm text-faint">Also: {c.events.join(", ")}</p> : null}
                  {!c.secret_ok ? <p className="text-sm text-warn">The secret cannot be read any more. Delete and add this channel again.</p>
                    : c.last_error && (!c.last_ok_at || c.last_error_at! > c.last_ok_at)
                      ? <p className="text-sm text-warn">Last problem ({timeAgo(c.last_error_at)}): {c.last_error}</p>
                      : c.last_ok_at ? <p className="text-sm text-faint">Last sent {timeAgo(c.last_ok_at)}</p> : null}
                </div>
                {canEdit ? (
                  <div className="flex flex-wrap gap-1">
                    <Button size="sm" variant="secondary" busy={testing === c.id} onClick={() => void test(c)}>Send test</Button>
                    <Button size="sm" variant="ghost" aria-label={`Delete ${c.name}`}
                      onClick={() => window.confirm(`Delete the channel "${c.name}"? Alerts stop going there.`) && void act(() => api.del(`${base}/channels/${c.id}`), "Channel deleted.")}>
                      Delete
                    </Button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel className="mt-6">
        <PanelHead title="History" />
        {events.length === 0 ? <p className="px-5 py-5 text-muted">No alert has fired yet.</p> : (
          <ul className="divide-y divide-rule text-sm" aria-label="Alert history">
            {events.map((e) => (
              <li key={e.id} className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-baseline gap-x-4 px-5 py-2.5">
                <span className={cn("w-16 font-medium", e.status === "fired" ? "text-danger" : "text-accent")}>
                  {e.status === "fired" ? "Started" : "Ended"}
                </span>
                <span className="min-w-0"><span className="font-medium">{e.rule_name}</span> <span className="text-muted">{e.message}</span></span>
                <span className="text-faint">{new Date(e.created_at).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <RuleDialog org={org} sites={sites} channels={channels} rule={editing} open={ruleOpen} onClose={() => setRuleOpen(false)}
        onSaved={() => { setRuleOpen(false); setNotice({ tone: "success", text: editing ? "Alert saved." : "Alert created. It is checked every minute." }); void load(); }} />
      <ChannelDialog org={org} members={members} open={chOpen} onClose={() => setChOpen(false)}
        onSaved={(c) => {
          setChOpen(false);
          if (c.signing_secret) setSecret({ name: c.name, value: c.signing_secret });
          setNotice({ tone: "success", text: `${c.name} added. Click Send test to check it.` });
          void load();
        }} />
    </>
  );
}

export default function AlertsPage() {
  const { orgId } = useParams<{ orgId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <AlertsView org={org} />}</OrgGate>;
}
