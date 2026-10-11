"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Label, Select } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { api, errorText, fieldErrors, roleRank, type Member, type Org, type Site } from "@/lib/api";
import { fmtPeriod, scheduleText, WEEKDAYS, type ReportSchedule } from "@/lib/dashboard";
import { cn } from "@/lib/utils";

const COVERS = {
  daily: "Each report covers the day before.",
  weekly: "Each report covers the 7 days before the send day (Monday = last week Monday to Sunday).",
  monthly: "Sent on the 1st, about the month before.",
};
/** A time as the site sees it (the send hour is in the site's time zone, not the browser's). */
function siteTime(iso: string, tz: string): string {
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, weekday: "short", day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit" }).format(new Date(iso));
}

const HOURS = Array.from({ length: 24 }, (_, h) => h);

function ScheduleDialog({ org, sites, members, schedule, open, onClose, onSaved }: {
  org: Org; sites: Site[]; members: Member[]; schedule: ReportSchedule | null; open: boolean;
  onClose: () => void; onSaved: (s: ReportSchedule) => void;
}) {
  const [freq, setFreq] = React.useState<ReportSchedule["frequency"]>("weekly");
  const [chosen, setChosen] = React.useState<Set<string>>(new Set());
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (!open) return;
    setError(null);
    setFreq(schedule?.frequency ?? "weekly");
    const me = members.find((m) => m.is_me);
    setChosen(new Set(schedule ? schedule.recipients.map((r) => r.user_id) : me ? [me.user_id] : []));
  }, [open, schedule, members]);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const body = {
      site_id: form.get("site_id"), frequency: freq, weekday: Number(form.get("weekday") ?? 0),
      hour: Number(form.get("hour")), recipients: [...chosen], attach_csv: form.get("attach_csv") === "on",
    };
    setBusy(true);
    setError(null);
    try {
      const saved = schedule
        ? await api.patch<ReportSchedule>(`/api/orgs/${org.id}/report-schedules/${schedule.id}`, body)
        : await api.post<ReportSchedule>(`/api/orgs/${org.id}/report-schedules`, body);
      onSaved(saved);
    } catch (err) {
      const f = fieldErrors(err);
      setError(f.recipients ?? errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title={schedule ? "Edit report email" : "New report email"} className="w-[min(520px,calc(100vw-32px))]">
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error ? <Notice tone="error">{error}</Notice> : null}
        <div className="space-y-1.5">
          <Label htmlFor="r-site">Site</Label>
          <Select id="r-site" name="site_id" defaultValue={schedule?.site_id ?? sites[0]?.id}>
            {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </Select>
        </div>
        <fieldset className="space-y-1.5">
          <legend className="mb-1.5 text-sm font-medium text-muted">How often</legend>
          <div className="inline-flex rounded-[var(--radius-s)] border border-rule bg-ink p-0.5">
            {(["daily", "weekly", "monthly"] as const).map((f) => (
              <button key={f} type="button" aria-pressed={freq === f} onClick={() => setFreq(f)}
                className={cn("h-8 cursor-pointer rounded-[5px] px-3 text-sm capitalize", freq === f ? "bg-raise text-text" : "text-muted hover:text-text")}>
                {f}
              </button>
            ))}
          </div>
        </fieldset>
        <div className="grid grid-cols-2 gap-3">
          {freq === "weekly" ? (
            <div className="space-y-1.5">
              <Label htmlFor="r-day">Send on</Label>
              <Select id="r-day" name="weekday" defaultValue={String(schedule?.weekday ?? 0)}>
                {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
              </Select>
            </div>
          ) : null}
          <div className="space-y-1.5">
            <Label htmlFor="r-hour">At</Label>
            <Select id="r-hour" name="hour" defaultValue={String(schedule?.hour ?? 7)}>
              {HOURS.map((h) => <option key={h} value={h}>{String(h).padStart(2, "0")}:00</option>)}
            </Select>
          </div>
        </div>
        <p className="text-sm text-faint">{COVERS[freq]} Times are in the site&apos;s time zone.</p>
        <fieldset>
          <legend className="mb-1.5 text-sm font-medium text-muted">Send to</legend>
          <ul className="max-h-[200px] space-y-1 overflow-auto rounded-[var(--radius-s)] border border-rule bg-ink p-2">
            {members.map((m) => (
              <li key={m.user_id}>
                <label className="flex cursor-pointer items-center gap-3 rounded px-2 py-1.5 hover:bg-raise/60">
                  <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={chosen.has(m.user_id)}
                    onChange={(e) => setChosen((prev) => {
                      const next = new Set(prev);
                      if (e.target.checked) next.add(m.user_id); else next.delete(m.user_id);
                      return next;
                    })} />
                  <span className="min-w-0 truncate">{m.name}{m.is_me ? " (you)" : ""}</span>
                  <span className="ml-auto truncate text-sm text-faint">{m.email}</span>
                </label>
              </li>
            ))}
          </ul>
          <p className="mt-1.5 text-sm text-faint">Only members of this organization. Invite someone first to add them.</p>
        </fieldset>
        <label className="flex cursor-pointer items-center gap-3">
          <input type="checkbox" name="attach_csv" defaultChecked={schedule?.attach_csv ?? false} className="size-4 accent-[var(--color-accent)]" />
          <span>Also attach the numbers as a CSV file (for Excel)</span>
        </label>
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy} disabled={chosen.size === 0}>{schedule ? "Save report" : "Create report"}</Button>
        </div>
      </form>
    </Dialog>
  );
}

function ReportsView({ org }: { org: Org }) {
  const [list, setList] = React.useState<ReportSchedule[] | null>(null);
  const [sites, setSites] = React.useState<Site[]>([]);
  const [members, setMembers] = React.useState<Member[]>([]);
  const [notice, setNotice] = React.useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [editing, setEditing] = React.useState<ReportSchedule | null>(null);
  const [open, setOpen] = React.useState(false);
  const [sending, setSending] = React.useState<string | null>(null);
  const canEdit = roleRank(org.my_role) >= roleRank("member");
  const base = `/api/orgs/${org.id}/report-schedules`;

  const load = React.useCallback(async () => {
    try {
      const [l, s, m] = await Promise.all([
        api.get<ReportSchedule[]>(base), api.get<Site[]>(`/api/orgs/${org.id}/sites`),
        api.get<Member[]>(`/api/orgs/${org.id}/members`),
      ]);
      setList(l);
      setSites(s);
      setMembers(m);
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }, [base, org.id]);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function sendNow(s: ReportSchedule) {
    setSending(s.id);
    setNotice(null);
    try {
      const res = await api.post<{ emails: number; period_from: string; period_to: string }>(`${base}/${s.id}/send`);
      setNotice({ tone: "success", text: `Sent the report for ${fmtPeriod(res.period_from, res.period_to)} to ${res.emails} ${res.emails === 1 ? "person" : "people"}.` });
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    } finally {
      setSending(null);
    }
  }

  async function toggle(s: ReportSchedule) {
    try {
      await api.patch(`${base}/${s.id}`, { enabled: !s.enabled });
      await load();
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }

  async function remove(s: ReportSchedule) {
    if (!window.confirm(`Stop and delete the ${s.frequency} report for ${s.site_name}?`)) return;
    try {
      await api.del(`${base}/${s.id}`);
      setNotice({ tone: "success", text: "Report deleted." });
      await load();
    } catch (e) {
      setNotice({ tone: "error", text: errorText(e) });
    }
  }

  return (
    <>
      <PageTitle title="Reports">
        {canEdit && sites.length ? <Button onClick={() => { setEditing(null); setOpen(true); }}>New report email</Button> : null}
      </PageTitle>
      {notice ? <Notice tone={notice.tone} className="mb-5">{notice.text}</Notice> : null}
      <p className="mb-5 max-w-[68ch] text-muted">
        CountVision emails a PDF report to the people you choose: every day, week or month. To download a report
        once, open a site&apos;s dashboard and click PDF report.
      </p>

      <Panel>
        <PanelHead title="Report emails" />
        {!list ? <Loading /> : list.length === 0 ? (
          <div className="px-5 py-6 text-muted">
            {sites.length === 0 ? (
              <>Add a <Link href={`/app/orgs/${org.id}/sites`} className="text-accent">site</Link> first.</>
            ) : canEdit ? (
              <>
                <p>No report emails yet. A weekly report on Monday morning is a good start.</p>
                <Button className="mt-4" onClick={() => { setEditing(null); setOpen(true); }}>New report email</Button>
              </>
            ) : "No report emails yet."}
          </div>
        ) : (
          <ul className="divide-y divide-rule" aria-label="Report emails">
            {list.map((s) => (
              <li key={s.id} className="flex flex-wrap items-start gap-x-6 gap-y-3 px-5 py-4" data-testid="schedule-row">
                <div className="min-w-0 flex-1 basis-72">
                  <p className="font-medium">
                    {s.frequency[0].toUpperCase() + s.frequency.slice(1)} report, {s.site_name}
                    {!s.enabled ? <span className="ml-2 rounded-full border border-rule px-2 py-0.5 text-xs text-muted">Paused</span> : null}
                  </p>
                  <p className="text-sm text-muted">{scheduleText(s)}</p>
                  <p className="mt-1 text-sm text-muted">
                    To {s.recipients.map((r) => r.name + (r.email_verified ? "" : " (email not confirmed)")).join(", ") || "nobody"}
                    {s.attach_csv ? ", with CSV" : ""}
                  </p>
                  <p className="mt-1 text-sm text-faint">
                    {s.enabled && s.next_send_at && s.next_period
                      ? `Next: ${siteTime(s.next_send_at, s.timezone)}, about ${fmtPeriod(s.next_period.from, s.next_period.to)}.`
                      : "Paused: no emails are sent."}
                    {s.last_sent_at ? ` Last sent ${siteTime(s.last_sent_at, s.timezone)}.` : ""}
                  </p>
                  {s.last_error ? <p className="mt-1 text-sm text-warn">Last problem: {s.last_error}</p> : null}
                </div>
                {canEdit ? (
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" variant="secondary" busy={sending === s.id} onClick={() => void sendNow(s)}>Send now</Button>
                    <Button size="sm" variant="ghost" onClick={() => { setEditing(s); setOpen(true); }}>Edit</Button>
                    <Button size="sm" variant="ghost" onClick={() => void toggle(s)}>{s.enabled ? "Pause" : "Resume"}</Button>
                    <Button size="sm" variant="ghost" onClick={() => void remove(s)} aria-label={`Delete the ${s.frequency} report for ${s.site_name}`}>Delete</Button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <ScheduleDialog org={org} sites={sites} members={members} schedule={editing} open={open}
        onClose={() => setOpen(false)}
        onSaved={() => {
          setOpen(false);
          setNotice({ tone: "success", text: editing ? "Report saved." : "Report created. It goes out at the next send time." });
          void load();
        }} />
    </>
  );
}

export default function ReportsPage() {
  const { orgId } = useParams<{ orgId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <ReportsView org={org} />}</OrgGate>;
}
