"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { HBars, Heatmap, TrafficChart, ZoneChart } from "@/components/charts";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { api, CLASSES, errorText, roleRank, type Org } from "@/lib/api";
import {
  bucketTitle, changeTone, fmtChange, fmtDay, fmtDuration, fmtNum, fmtPeriod, peakText, presetRange,
  todayIn, whichPreset, type Dashboard, type DashLine, type Preset,
} from "@/lib/dashboard";
import { useLive } from "@/lib/live";
import { cn } from "@/lib/utils";

const SAMPLE_SITE = "Demo café (sample data)";
const PRESETS: { id: Exclude<Preset, "custom">; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "yesterday", label: "Yesterday" },
  { id: "7d", label: "7 days" },
  { id: "30d", label: "30 days" },
];

/** Excel in Germany wants ";" and decimal commas; this follows the browser's number format. */
function csvDelimiter(): string {
  return (1.5).toLocaleString().includes(",") ? ";" : ",";
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "warn" }) {
  return (
    <div className="min-w-0 px-4 py-4 sm:px-5">
      <p className="text-sm text-muted">{label}</p>
      <p className={cn("mt-1 truncate text-[26px] font-semibold leading-tight", tone === "warn" && "text-warn")}
        style={{ fontVariantNumeric: "proportional-nums" }}>{value}</p>
      {sub ? <p className="mt-0.5 truncate text-sm text-faint">{sub}</p> : null}
    </div>
  );
}

function PeriodBar({ tz, from, to, onChange }: { tz: string; from: string; to: string; onChange: (f: string, t: string) => void }) {
  const today = todayIn(tz);
  const preset = whichPreset(from, to, today);
  const [custom, setCustom] = React.useState(preset === "custom");
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Period">
      <div className="inline-flex rounded-[var(--radius-s)] border border-rule bg-ink p-0.5">
        {PRESETS.map((p) => (
          <button key={p.id} type="button" aria-pressed={!custom && preset === p.id}
            onClick={() => { setCustom(false); const r = presetRange(p.id, today); onChange(r.from, r.to); }}
            className={cn("h-8 cursor-pointer rounded-[5px] px-3 text-sm",
              !custom && preset === p.id ? "bg-raise text-text" : "text-muted hover:text-text")}>
            {p.label}
          </button>
        ))}
        <button type="button" aria-pressed={custom || preset === "custom"} onClick={() => setCustom(true)}
          className={cn("h-8 cursor-pointer rounded-[5px] px-3 text-sm",
            custom || preset === "custom" ? "bg-raise text-text" : "text-muted hover:text-text")}>
          Dates
        </button>
      </div>
      {custom || preset === "custom" ? (
        <div className="flex items-center gap-2">
          <label className="sr-only" htmlFor="from">From</label>
          <Input id="from" type="date" value={from} max={today} className="h-9 w-[150px]"
            onChange={(e) => e.target.value && onChange(e.target.value, e.target.value > to ? e.target.value : to)} />
          <span className="text-muted">to</span>
          <label className="sr-only" htmlFor="to">To</label>
          <Input id="to" type="date" value={to} min={from} className="h-9 w-[150px]"
            onChange={(e) => e.target.value && onChange(from, e.target.value)} />
        </div>
      ) : null}
    </div>
  );
}

function Downloads({ base, from, to }: { base: string; from: string; to: string }) {
  const q = `from=${from}&to=${to}`;
  const d = encodeURIComponent(csvDelimiter());
  const days = (Date.parse(to) - Date.parse(from)) / 86400000 + 1;
  const bucket = days > 31 ? "day" : "hour";
  const link = "inline-flex h-9 items-center rounded-[var(--radius-s)] border border-rule bg-raise px-3 text-sm no-underline hover:border-faint";
  return (
    <div className="flex flex-wrap gap-2">
      <a className={link} href={`${base}/report.pdf?${q}`} download data-testid="download-pdf">PDF report</a>
      <a className={link} href={`${base}/export.csv?${q}&bucket=${bucket}&delimiter=${d}`} download data-testid="download-csv">CSV lines</a>
      <a className={link} href={`${base}/export.csv?${q}&kind=zones&bucket=${bucket}&delimiter=${d}`} download>CSV zones</a>
    </div>
  );
}

function TrafficTable({ dash }: { dash: Dashboard }) {
  return (
    <div className="max-h-[320px] overflow-auto">
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-panel text-left text-muted">
          <tr><th scope="col" className="py-2 font-medium">Time</th><th scope="col" className="text-right font-medium">In</th>
            <th scope="col" className="text-right font-medium">Out</th><th scope="col" className="text-right font-medium">{dash.compare.label}</th></tr>
        </thead>
        <tbody className="divide-y divide-rule">
          {dash.series.map((s) => (
            <tr key={s.t}><td className="py-1.5">{bucketTitle(s.t, dash.period.bucket)}</td>
              <td className="text-right">{fmtNum(s.in)}</td><td className="text-right">{fmtNum(s.out)}</td>
              <td className="text-right text-muted">{fmtNum(s.compare_in)}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LinesTable({ dash, canEdit, onToggle, busy }: {
  dash: Dashboard; canEdit: boolean; busy: boolean; onToggle: (line: DashLine) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[560px] text-sm">
        <thead className="text-left text-muted">
          <tr className="border-b border-rule">
            <th scope="col" className="px-5 py-2.5 font-medium">Line</th>
            <th scope="col" className="py-2.5 font-medium">Counts as visitors</th>
            <th scope="col" className="py-2.5 text-right font-medium">In</th>
            <th scope="col" className="py-2.5 text-right font-medium">Out</th>
            <th scope="col" className="px-5 py-2.5 text-right font-medium">vs {dash.compare.label}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-rule">
          {dash.lines.map((l) => (
            <tr key={`${l.camera_id}-${l.line}`} data-testid="line-row">
              <td className="px-5 py-2.5"><span className="font-medium">{l.line}</span>
                <span className="text-muted">, {l.camera_name}</span></td>
              <td className="py-2.5">
                {canEdit ? (
                  <label className="inline-flex cursor-pointer items-center gap-2">
                    <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={l.visitor}
                      disabled={busy} onChange={() => onToggle(l)} aria-label={`${l.line} counts as visitors`} />
                    <span className="text-muted">{l.visitor ? "Yes" : "No"}</span>
                  </label>
                ) : <span className="text-muted">{l.visitor ? "Yes" : "No"}</span>}
              </td>
              <td className="py-2.5 text-right">{fmtNum(l.in)}</td>
              <td className="py-2.5 text-right">{fmtNum(l.out)}</td>
              <td className="px-5 py-2.5 text-right text-muted">{fmtChange(l.change_pct)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DashboardView({ org, siteId }: { org: Org; siteId: string }) {
  const router = useRouter();
  const params = useSearchParams();
  const [dash, setDash] = React.useState<Dashboard | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [table, setTable] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const base = `/api/orgs/${org.id}/sites/${siteId}`;
  const canEdit = roleRank(org.my_role) >= roleRank("member");

  // No dates in the address = today (the server knows the site's time zone).
  const from = params.get("from");
  const to = params.get("to") ?? from;

  const load = React.useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const q = from ? `?from=${from}&to=${to}` : "";
      const d = await api.get<Dashboard>(`${base}/dashboard${q}`);
      setDash(d);
      setError(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }, [base, from, to]);

  React.useEffect(() => {
    void load();
  }, [load]);

  // Live: a crossing on one of this site's cameras reloads the numbers (at most every 1.5 s).
  const cameras = React.useMemo(() => new Set([...(dash?.lines ?? []).map((l) => l.camera_id),
    ...(dash?.coverage.cameras ?? []).map((c) => c.camera_id)]), [dash]);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const live = dash?.period.live ?? false;
  const onMessage = React.useCallback((msg: { type: string; camera_id?: string }) => {
    if (!live || timer.current) return;
    if (msg.type === "refresh" || (msg.type === "count" && msg.camera_id && (cameras.has(msg.camera_id) || cameras.size === 0))) {
      timer.current = setTimeout(() => { timer.current = null; void load(true); }, 1500);
    }
  }, [live, cameras, load]);
  const liveState = useLive(org.id, onMessage);
  React.useEffect(() => {
    if (!live) return;
    const id = setInterval(() => void load(true), 60_000); // zones update once a minute
    return () => clearInterval(id);
  }, [live, load]);
  React.useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  function setPeriod(f: string, t: string) {
    router.replace(`?from=${f}&to=${t}`, { scroll: false });
  }

  async function toggleLine(line: DashLine) {
    if (!dash) return;
    const next = dash.lines.map((l) => ({ camera_id: l.camera_id, line: l.line,
      on: l.camera_id === line.camera_id && l.line === line.line ? !l.visitor : l.visitor }));
    const chosen = next.filter((l) => l.on).map(({ camera_id, line: name }) => ({ camera_id, line: name }));
    // show the change at once; the totals follow from the server a moment later
    setDash({ ...dash, lines: dash.lines.map((l) => (l.camera_id === line.camera_id && l.line === line.line ? { ...l, visitor: !l.visitor } : l)) });
    setBusy(true);
    try {
      await api.put(`${base}/visitor-lines`, { lines: chosen.length === next.length ? null : chosen });
      await load(true);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function removeSample() {
    if (!window.confirm("Remove the sample site and all its invented numbers?")) return;
    try {
      await api.del(`/api/orgs/${org.id}/sample-data`);
      router.push(`/app/orgs/${org.id}`);
    } catch (e) {
      setError(errorText(e));
    }
  }

  if (!dash) return error ? <Notice tone="error">{error}</Notice> : <Loading />;

  const t = dash.totals;
  const tone = changeTone(t.change_pct);
  const areas = dash.occupancy_now.filter((o) => o.kind === "area");
  const queues = dash.occupancy_now.filter((o) => o.kind === "queue");
  const cov = dash.coverage.pct;
  const p = dash.period;
  const heroLabel = p.days === 1 && p.live ? "Visitors today" : p.days === 1 ? `Visitors on ${fmtPeriod(p.from, p.to)}` : "Visitors in this period";
  const stats: { label: string; value: string; sub?: string; tone?: "warn" }[] = [];
  if (p.live) {
    stats.push(areas.length
      ? { label: "Inside now", value: fmtNum(areas.reduce((n, o) => n + o.value, 0)),
          sub: areas.length === 1 ? areas[0].zone : `${areas.length} zones` }
      : { label: "Inside now (about)", value: fmtNum(t.inside_estimate), sub: "in minus out today" });
    if (queues.length) {
      stats.push({ label: "In the queue now", value: fmtNum(queues.reduce((n, o) => n + o.value, 0)),
        sub: queues.length === 1 ? queues[0].zone : `${queues.length} queues` });
    }
  }
  stats.push({ label: "Went out", value: fmtNum(t.out), sub: `${fmtNum(t.compare_out)} ${dash.compare.label}` });
  stats.push({ label: "Busiest hour", value: peakText(dash.peak, 1),
    sub: dash.peak ? (p.days > 1 ? `${fmtDay(dash.peak.t)}, ${fmtNum(dash.peak.in)} in` : `${fmtNum(dash.peak.in)} in`) : "No visitors yet" });
  stats.push({ label: "Average visit", value: fmtDuration(dash.dwell.avg_s),
    sub: dash.dwell.count ? `${fmtNum(dash.dwell.count)} visits in zones` : "No zone visits" });
  stats.push({ label: "Data coverage", value: cov === null ? "–" : `${Math.round(cov)} %`, sub: "of the counting hours",
    tone: cov !== null && cov < 90 ? "warn" : undefined });
  const open = dash.series.filter((x) => (x.in ?? 0) > 0);
  if (stats.length < 5) {
    stats.push({ label: p.bucket === "day" ? "Per open day" : "Per open hour",
      value: open.length ? fmtNum(Math.round((t.in / open.length) * 10) / 10) : "–",
      sub: `on ${open.length} ${p.bucket === "day" ? "days" : "hours"} with visitors` });
  }
  stats.push({ label: "Cameras", value: fmtNum(dash.camera_count), sub: `${dash.lines.length} ${dash.lines.length === 1 ? "line" : "lines"}, ${dash.zones.length} ${dash.zones.length === 1 ? "zone" : "zones"}` });
  const classColor = (c: string) => CLASSES.find((x) => x.id === c)?.color;
  const classLabel = (c: string) => CLASSES.find((x) => x.id === c)?.label ?? c;

  return (
    <>
      <nav className="mb-2 text-sm text-muted" aria-label="Breadcrumb">
        <Link href={`/app/orgs/${org.id}/sites`} className="no-underline hover:text-text">Sites</Link>
      </nav>
      <div className="mb-5 flex flex-wrap items-end gap-x-4 gap-y-2">
        <h1 className="mr-auto text-2xl font-semibold leading-tight">{dash.site.name}</h1>
        {p.live ? (
          <span className="inline-flex items-center gap-2 text-sm text-muted" data-testid="live-state">
            <span className={cn("size-2 rounded-full", liveState === "live" ? "bg-accent" : "bg-faint")} />
            {liveState === "live" ? "Live" : "Updates every minute"}
          </span>
        ) : null}
      </div>

      <div className="mb-6 flex flex-wrap items-center gap-3 border-b border-rule pb-4">
        <PeriodBar tz={dash.site.timezone} from={p.from} to={p.to} onChange={setPeriod} />
        <span className="text-sm text-faint">{fmtPeriod(p.from, p.to)}, {dash.site.timezone.replace("_", " ")}</span>
        <div className="ml-auto"><Downloads base={base} from={p.from} to={p.to} /></div>
      </div>

      {error ? <Notice tone="error" className="mb-5">{error}</Notice> : null}
      {dash.site.name === SAMPLE_SITE ? (
        <Notice tone="info" className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-2">
          <span className="mr-auto">These numbers are invented sample data, to show what the dashboard and reports look like.</span>
          {roleRank(org.my_role) >= roleRank("admin") ? (
            <Button size="sm" variant="ghost" onClick={() => void removeSample()}>Remove sample data</Button>
          ) : null}
        </Notice>
      ) : null}
      {dash.camera_count === 0 ? (
        <Notice tone="info" className="mb-5">
          No cameras on this site yet. <Link href={`/app/orgs/${org.id}/devices`} className="text-accent">Add a device</Link>{" "}
          to start counting.
        </Notice>
      ) : !dash.has_data ? (
        <Notice tone="info" className="mb-5">No counting data in this period.</Notice>
      ) : cov !== null && cov < 90 ? (
        <Notice tone="warn" className="mb-5">
          The cameras ran only {Math.round(cov)} % of the expected time in this period. The real numbers are
          higher than shown.
        </Notice>
      ) : null}

      <div className={cn("space-y-6 transition-opacity", loading && "opacity-60")} aria-busy={loading}>
        <Panel className="grid overflow-hidden lg:grid-cols-[minmax(0,1.3fr)_minmax(0,2fr)]">
          <div className="border-b border-rule p-5 sm:p-6 lg:border-r lg:border-b-0">
            <p className="text-base text-muted">{heroLabel}</p>
            <p className="mt-1 text-[64px] font-semibold leading-none tracking-tight" style={{ fontVariantNumeric: "proportional-nums" }}
              data-testid="visitors-total">{fmtNum(t.in)}</p>
            {t.comparable ? (
              <p className="mt-3 text-base">
                <span className={cn("font-medium", tone === "up" && "text-accent", tone === "down" && "text-danger", tone === "flat" && "text-muted")}
                  data-testid="visitors-change">
                  {tone === "up" ? "▲ " : tone === "down" ? "▼ " : ""}{fmtChange(t.change_pct)}
                </span>
                <span className="text-muted"> vs {dash.compare.label} ({fmtNum(t.compare_in)})</span>
              </p>
            ) : (
              <p className="mt-3 text-base text-muted" data-testid="visitors-change">
                No comparison: the cameras ran too little in the {dash.compare.label.replace("same time ", "")} ({fmtNum(t.compare_in)} counted).
              </p>
            )}
          </div>
          <div className="grid grid-cols-2 gap-px bg-rule sm:grid-cols-3 [&>*]:bg-panel">
            {stats.slice(0, 6).map((st) => <Stat key={st.label} {...st} />)}
          </div>
        </Panel>

        <Panel>
          <PanelHead title={`Visitors per ${p.bucket}`}>
            <Button size="sm" variant="ghost" onClick={() => setTable((v) => !v)} aria-pressed={table}>
              {table ? "Show chart" : "Show table"}
            </Button>
          </PanelHead>
          <div className="px-4 py-4 sm:px-5">
            {table ? <TrafficTable dash={dash} /> : (
              <TrafficChart series={dash.series} bucket={p.bucket} days={p.days} compareLabel={dash.compare.label} />
            )}
          </div>
        </Panel>

        {dash.lines.length ? (
          <Panel>
            <PanelHead title="Lines">
              {canEdit ? <span className="text-sm text-faint">Tick the lines that count people coming in (doors), not aisles.</span> : null}
            </PanelHead>
            <LinesTable dash={dash} canEdit={canEdit} busy={busy} onToggle={(l) => void toggleLine(l)} />
          </Panel>
        ) : null}

        <div className="grid gap-6 md:grid-cols-2">
          <Panel>
            <PanelHead title="What was counted" />
            <div className="px-5 py-4">
              {dash.classes.length ? (
                <HBars rows={dash.classes.map((c) => ({ label: classLabel(c.class_name), value: c.in, color: classColor(c.class_name),
                  text: `${fmtNum(c.in)} (${Math.round((c.in / Math.max(1, t.in)) * 100)} %)` }))} />
              ) : <p className="text-sm text-muted">Nothing counted yet.</p>}
            </div>
          </Panel>
          <Panel>
            <PanelHead title="Length of visits">
              {dash.dwell.count ? <span className="text-sm text-muted">median {fmtDuration(dash.dwell.median_s)}</span> : null}
            </PanelHead>
            <div className="px-5 py-4">
              {dash.dwell.count ? (
                <HBars rows={dash.dwell.buckets.map((b) => ({ label: b.label, value: b.count, text: fmtNum(b.count) }))} />
              ) : <p className="text-sm text-muted">Draw a zone (for example at the counter) to measure how long people stay.</p>}
            </div>
          </Panel>
        </div>

        <Panel>
          <PanelHead title="When visitors come">
            <span className="text-sm text-faint">Average per hour, {fmtPeriod(dash.heatmap.from, dash.heatmap.to)}</span>
          </PanelHead>
          <div className="px-5 py-4">
            <Heatmap cells={dash.heatmap.cells} max={dash.heatmap.max} />
          </div>
        </Panel>

        {dash.zones.map((z) => {
          const queue = z.kind === "queue";
          return (
            <Panel key={`${z.camera_id}-${z.zone}`}>
              <PanelHead title={z.zone}>
                <span className="text-sm text-muted">{queue ? "Queue" : "Area"}, {z.camera_name}</span>
              </PanelHead>
              <div className="grid gap-4 px-5 py-4 md:grid-cols-[220px_minmax(0,1fr)]">
                <dl className="grid grid-cols-2 gap-x-4 gap-y-3 self-start text-sm md:grid-cols-1">
                  <div><dt className="text-muted">{queue ? "Waiting on average" : "People on average"}</dt>
                    <dd className="text-xl font-semibold">{fmtNum(queue ? z.queue_avg : z.occ_avg)}</dd></div>
                  <div><dt className="text-muted">Most at once</dt><dd className="text-xl font-semibold">{fmtNum(queue ? z.queue_max : z.occ_max)}</dd></div>
                  <div><dt className="text-muted">Visits</dt><dd className="text-xl font-semibold">{fmtNum(z.visits)}</dd></div>
                  <div><dt className="text-muted">Average visit</dt><dd className="text-xl font-semibold">{fmtDuration(z.dwell_avg_s)}</dd></div>
                </dl>
                <ZoneChart points={z.series} bucket={p.bucket} days={p.days} unit={queue ? "waiting" : "inside"} />
              </div>
            </Panel>
          );
        })}

        {dash.coverage.cameras.length ? (
          <Panel>
            <PanelHead title="How complete the data is" />
            <ul className="divide-y divide-rule text-sm">
              {dash.coverage.cameras.map((c) => (
                <li key={c.camera_id} className="flex flex-wrap items-center gap-x-4 px-5 py-2.5">
                  <span className="mr-auto font-medium">{c.camera_name}</span>
                  <span className="text-muted">{fmtDuration(c.seconds)} of {fmtDuration(c.expected_s)}</span>
                  <span className={cn("w-14 text-right", c.pct !== null && c.pct < 90 ? "text-warn" : "")}>
                    {c.pct === null ? "–" : `${Math.round(c.pct)} %`}
                  </span>
                </li>
              ))}
            </ul>
          </Panel>
        ) : null}
      </div>
    </>
  );
}

export default function SiteDashboardPage() {
  const { orgId, siteId } = useParams<{ orgId: string; siteId: string }>();
  return (
    <OrgGate orgId={orgId}>
      {(org) => (
        <React.Suspense fallback={<Loading />}>
          <DashboardView org={org} siteId={siteId} />
        </React.Suspense>
      )}
    </OrgGate>
  );
}
