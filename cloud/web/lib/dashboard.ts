// Types and small helpers for the dashboards and reports (Phase 4 A).

export interface SeriesPoint { t: string; in: number | null; out: number | null; compare_in: number }

export interface DashLine {
  camera_id: string; camera_name: string; line: string; visitor: boolean;
  in: number; out: number; compare_in: number; compare_out: number; change_pct: number | null;
}

export interface DashZone {
  camera_id: string; camera_name: string; zone: string; kind: "area" | "queue";
  occ_avg: number | null; occ_max: number; queue_avg: number | null; queue_max: number;
  visits: number; dwell_avg_s: number | null;
  series: { t: string; avg: number | null; max: number | null }[];
}

export interface HeatCell { weekday: number; hour: number; days: number; avg_in: number | null }

export interface Dashboard {
  site: { id: string; name: string; timezone: string };
  period: { from: string; to: string; days: number; bucket: "hour" | "day"; start: string; end: string; live: boolean };
  compare: { from: string; to: string; label: string; shift_days: number };
  visitor_lines: { camera_id: string; line: string }[] | null;
  totals: { in: number; out: number; compare_in: number; compare_out: number; change_pct: number | null;
    comparable: boolean; inside_estimate: number | null };
  series: SeriesPoint[];
  peak: { t: string; in: number } | null;
  lines: DashLine[];
  classes: { class_name: string; in: number; out: number }[];
  zones: DashZone[];
  occupancy_now: { camera_id: string; camera_name: string; zone: string; kind: "area" | "queue"; value: number;
    as_of: string }[];
  dwell: { count: number; avg_s: number | null; median_s: number | null; buckets: { label: string; count: number }[] };
  heatmap: { from: string; to: string; max: number; cells: HeatCell[] };
  coverage: { pct: number | null; cameras: { camera_id: string; camera_name: string; seconds: number;
    expected_s: number; pct: number | null }[] };
  camera_count: number;
  has_data: boolean;
  generated_at: string;
}

export interface SiteToday {
  site_id: string; name: string; timezone: string; camera_count: number;
  today_in: number; today_out: number; compare_in: number; change_pct: number | null; compare_label: string;
  inside_estimate: number; occupancy: number | null; queue: number | null; hours: (number | null)[];
}

export interface ReportRecipient { user_id: string; name: string; email: string; email_verified: boolean }

export interface ReportSchedule {
  id: string; site_id: string; site_name: string; timezone: string;
  frequency: "daily" | "weekly" | "monthly"; weekday: number; hour: number;
  recipients: ReportRecipient[]; attach_csv: boolean; enabled: boolean; created_at: string;
  last_sent_at: string | null; last_period_end: string | null; last_error: string | null;
  next_send_at: string | null; next_period: { from: string; to: string } | null;
}

export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
export const WD_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// --- numbers -----------------------------------------------------------------------------------

const intFmt = new Intl.NumberFormat("en");
const oneFmt = new Intl.NumberFormat("en", { maximumFractionDigits: 1 });

export function fmtNum(v: number | null | undefined): string {
  if (v === null || v === undefined) return "–";
  return Number.isInteger(v) ? intFmt.format(v) : oneFmt.format(v);
}

/** 12,900 -> "12.9K" for axis ticks. */
export function fmtCompact(v: number): string {
  return v >= 1000 ? `${oneFmt.format(v / 1000)}K` : intFmt.format(v);
}

export function fmtChange(v: number | null): string {
  if (v === null) return "–";
  if (Math.abs(v) < 0.5) return "±0 %";
  return `${v > 0 ? "+" : "−"}${Math.round(Math.abs(v))} %`;
}

export function changeTone(v: number | null): "up" | "down" | "flat" {
  if (v === null || Math.abs(v) < 0.5) return "flat";
  return v > 0 ? "up" : "down";
}

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined) return "–";
  const t = Math.round(s);
  if (t < 60) return `${t} s`;
  if (t < 3600) return `${Math.floor(t / 60)} min ${String(t % 60).padStart(2, "0")} s`;
  return `${Math.floor(t / 3600)} h ${String(Math.floor((t % 3600) / 60)).padStart(2, "0")} min`;
}

// --- dates (all dates are local dates of the site, "YYYY-MM-DD") -------------------------------

/** Today's date in a time zone. */
export function todayIn(tz: string, now: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" })
    .format(now);
}

export function addDays(iso: string, n: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + n));
  return dt.toISOString().slice(0, 10);
}

function parts(iso: string): Date {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d, 12));
}

const dayFmt = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" });
const dayYearFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

export function fmtDay(iso: string): string {
  return dayFmt.format(parts(iso));
}

export function fmtPeriod(from: string, to: string): string {
  if (from === to) return dayFmt.format(parts(from));
  return `${dayFmt.format(parts(from))} to ${dayYearFmt.format(parts(to))}`;
}

/** Label for a series bucket "2026-10-09T14:00". */
export function bucketLabel(t: string, bucket: "hour" | "day", days: number): string {
  if (bucket === "hour") return days === 1 ? t.slice(11, 13) : `${WD_SHORT[(parts(t).getUTCDay() + 6) % 7]} ${t.slice(11, 13)}`;
  return days > 7 ? `${t.slice(8, 10)}.${t.slice(5, 7)}` : `${WD_SHORT[(parts(t).getUTCDay() + 6) % 7]} ${Number(t.slice(8, 10))}`;
}

export function bucketTitle(t: string, bucket: "hour" | "day"): string {
  if (bucket === "day") return dayFmt.format(parts(t));
  const h = Number(t.slice(11, 13));
  return `${dayFmt.format(parts(t))}, ${String(h).padStart(2, "0")}:00–${String((h + 1) % 24).padStart(2, "0")}:00`;
}

export function peakText(peak: { t: string } | null, days: number): string {
  if (!peak) return "–";
  const h = Number(peak.t.slice(11, 13));
  const hours = `${String(h).padStart(2, "0")}:00–${String((h + 1) % 24).padStart(2, "0")}:00`;
  return days === 1 ? hours : `${fmtDay(peak.t)}, ${hours}`;
}

export type Preset = "today" | "yesterday" | "7d" | "30d" | "custom";

export function presetRange(p: Exclude<Preset, "custom">, today: string): { from: string; to: string } {
  switch (p) {
    case "today": return { from: today, to: today };
    case "yesterday": return { from: addDays(today, -1), to: addDays(today, -1) };
    case "7d": return { from: addDays(today, -6), to: today };
    case "30d": return { from: addDays(today, -29), to: today };
  }
}

export function whichPreset(from: string, to: string, today: string): Preset {
  for (const p of ["today", "yesterday", "7d", "30d"] as const) {
    const r = presetRange(p, today);
    if (r.from === from && r.to === to) return p;
  }
  return "custom";
}

export function scheduleText(s: Pick<ReportSchedule, "frequency" | "weekday" | "hour">): string {
  const at = `${String(s.hour).padStart(2, "0")}:00`;
  if (s.frequency === "daily") return `Every day at ${at}, about the day before`;
  if (s.frequency === "weekly") return `Every ${WEEKDAYS[s.weekday]} at ${at}, about the 7 days before`;
  return `On the 1st of each month at ${at}, about the month before`;
}
