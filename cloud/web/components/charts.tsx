"use client";

// Small hand-made SVG charts for the dashboards. No chart library: fast, same look everywhere.
// Rules (control-room style): thin marks, one baseline, hairline grid, one accent colour for
// visitors, the comparison as a thin muted line (same axis, same unit), text never in the data
// colour, a tooltip on hover AND keyboard focus, and every chart has a table twin on the page.

import * as React from "react";

import {
  bucketLabel, bucketTitle, fmtCompact, fmtNum, WD_SHORT,
  type HeatCell, type SeriesPoint,
} from "@/lib/dashboard";
import { cn } from "@/lib/utils";

const ACCENT = "var(--color-accent)";
const COMPARE = "#8aa2ae";

/** Width of an element, updated on resize (charts draw in real pixels, so text stays sharp). */
function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = React.useRef<T>(null);
  const [w, setW] = React.useState(0);
  React.useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function niceTicks(top: number): number[] {
  const steps = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 50000];
  const step = steps.find((s) => top / s <= 4) ?? 100000;
  const max = Math.max(step, Math.ceil(top / step) * step);
  return Array.from({ length: max / step + 1 }, (_, i) => i * step);
}

/** A bar whose top corners are rounded (4px) and whose foot is square on the baseline. */
function barPath(x: number, y: number, w: number, h: number, r = 4): string {
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + rr}Q${x},${y} ${x + rr},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + h}Z`;
}

function Tooltip({ x, y, width, children }: { x: number; y: number; width: number; children: React.ReactNode }) {
  const left = Math.min(Math.max(x, 90), width - 90);
  return (
    <div role="presentation"
      className="pointer-events-none absolute z-10 w-[180px] -translate-x-1/2 -translate-y-full rounded-[var(--radius-s)] border border-rule bg-raise px-3 py-2 text-sm shadow-lg shadow-black/30"
      style={{ left, top: y }}>
      {children}
    </div>
  );
}

// --- traffic: visitors per hour/day ---------------------------------------------------------------

export function TrafficChart({ series, bucket, days, compareLabel, height = 240 }: {
  series: SeriesPoint[]; bucket: "hour" | "day"; days: number; compareLabel: string; height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = React.useState<number | null>(null);
  const pad = { l: 40, r: 8, t: 12, b: 28 };
  const w = Math.max(0, width - pad.l - pad.r);
  const h = height - pad.t - pad.b;
  const top = Math.max(1, ...series.map((s) => Math.max(s.in ?? 0, s.compare_in)));
  const ticks = niceTicks(top);
  const max = ticks[ticks.length - 1];
  const n = series.length;
  const slot = n ? w / n : 0;
  const bar = Math.max(2, Math.min(24, slot * 0.62));
  const y = (v: number) => pad.t + h - (h * v) / max;
  const cx = (i: number) => pad.l + i * slot + slot / 2;
  const showCompare = series.some((s) => s.compare_in > 0);
  const every = Math.max(1, Math.ceil(n / Math.max(4, Math.floor(w / 46))));
  const compare = series.map((s, i) => `${i === 0 ? "M" : "L"}${cx(i).toFixed(1)},${y(s.compare_in).toFixed(1)}`).join("");
  const a = active !== null ? series[active] : null;

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowRight") setActive((v) => Math.min(n - 1, (v ?? -1) + 1));
    else if (e.key === "ArrowLeft") setActive((v) => Math.max(0, (v ?? n) - 1));
    else if (e.key === "Escape") setActive(null);
    else return;
    e.preventDefault();
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-sm text-muted">
        <span className="inline-flex items-center gap-2"><span className="h-2.5 w-2.5 rounded-[2px] bg-accent" />Visitors in</span>
        {showCompare ? (
          <span className="inline-flex items-center gap-2">
            <span className="h-[2px] w-4 rounded" style={{ background: COMPARE }} />{compareLabel[0].toUpperCase() + compareLabel.slice(1)}
          </span>
        ) : null}
      </div>
      <div ref={ref} className="relative outline-none" tabIndex={0} onKeyDown={onKey} onBlur={() => setActive(null)}
        aria-label="Visitors chart. Use the arrow keys to read the values." role="group" data-testid="traffic-chart">
        {width > 0 ? (
          <svg width={width} height={height} className="block" onMouseLeave={() => setActive(null)}>
            {ticks.map((t) => (
              <g key={t}>
                <line x1={pad.l} x2={pad.l + w} y1={y(t)} y2={y(t)} stroke="var(--color-rule)" strokeWidth={1} />
                <text x={pad.l - 8} y={y(t) + 4} textAnchor="end" fontSize={12} fill="var(--color-faint)">{fmtCompact(t)}</text>
              </g>
            ))}
            {series.map((s, i) => {
              const v = s.in ?? 0;
              const bh = (h * v) / max;
              return v > 0 ? (
                <path key={s.t} d={barPath(cx(i) - bar / 2, y(v), bar, bh)} fill={ACCENT}
                  opacity={active === null || active === i ? 1 : 0.45} />
              ) : null;
            })}
            {showCompare ? (
              <path d={compare} fill="none" stroke={COMPARE} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            ) : null}
            {active !== null ? (
              <circle cx={cx(active)} cy={y(series[active].compare_in)} r={4} fill={COMPARE}
                stroke="var(--color-panel)" strokeWidth={2} style={{ display: showCompare ? undefined : "none" }} />
            ) : null}
            {series.map((s, i) => (i % every === 0 ? (
              <text key={s.t} x={cx(i)} y={height - 8} textAnchor="middle" fontSize={12} fill="var(--color-muted)">
                {bucketLabel(s.t, bucket, days)}
              </text>
            ) : null))}
            {series.map((s, i) => (
              <rect key={s.t} x={pad.l + i * slot} y={pad.t} width={slot} height={h} fill="transparent"
                onMouseEnter={() => setActive(i)} onMouseMove={() => setActive(i)} />
            ))}
          </svg>
        ) : <div style={{ height }} />}
        {a && active !== null ? (
          <Tooltip x={cx(active)} y={Math.min(y(a.in ?? 0), y(a.compare_in)) - 8} width={width}>
            <div className="font-medium text-text">{bucketTitle(a.t, bucket)}</div>
            {a.in === null ? <div className="text-muted">Not yet</div> : (
              <>
                <div className="mt-1 flex justify-between"><span className="text-muted">In</span><span>{fmtNum(a.in)}</span></div>
                <div className="flex justify-between"><span className="text-muted">Out</span><span>{fmtNum(a.out)}</span></div>
              </>
            )}
            {showCompare ? (
              <div className="flex justify-between gap-2"><span className="truncate text-muted">{compareLabel}</span><span>{fmtNum(a.compare_in)}</span></div>
            ) : null}
          </Tooltip>
        ) : null}
      </div>
    </div>
  );
}

// --- weekday x hour --------------------------------------------------------------------------------

// One teal ramp on the dark surface: more visitors = brighter. "No data" is a hollow cell.
const RAMP = ["#162a31", "#183c41", "#1b5150", "#1e6a63", "#228777", "#29a78e", "#3fc8a8", "#86ecd6"];

export function heatColor(v: number, max: number): string {
  if (v <= 0 || max <= 0) return RAMP[0];
  return RAMP[1 + Math.min(RAMP.length - 2, Math.floor((v / max) * (RAMP.length - 1)))];
}

export function Heatmap({ cells, max }: { cells: HeatCell[]; max: number }) {
  const used = cells.filter((c) => c.days > 0).map((c) => c.hour);
  const h0 = used.length ? Math.min(...used) : 6;
  const h1 = used.length ? Math.max(...used) : 22;
  const hours = Array.from({ length: h1 - h0 + 1 }, (_, i) => h0 + i);
  const grid = new Map(cells.map((c) => [`${c.weekday}-${c.hour}`, c]));
  const [tip, setTip] = React.useState<HeatCell | null>(null);
  return (
    <div>
      <div className="overflow-x-auto pb-1">
        <div className="grid min-w-[520px] gap-[3px]" style={{ gridTemplateColumns: `40px repeat(${hours.length}, minmax(0, 1fr))` }}
          role="table" aria-label="Average visitors per weekday and hour" onMouseLeave={() => setTip(null)}>
          <div role="row" className="contents">
            <span role="columnheader" />
            {hours.map((h) => (
              <span key={h} role="columnheader" className="text-center text-xs text-faint">{h % 2 === 0 || hours.length <= 14 ? String(h).padStart(2, "0") : ""}</span>
            ))}
          </div>
          {WD_SHORT.map((wd, d) => (
            <div key={wd} role="row" className="contents">
              <span role="rowheader" className="self-center text-sm text-muted">{wd}</span>
              {hours.map((h) => {
                const c = grid.get(`${d}-${h}`);
                const empty = !c || c.avg_in === null;
                const label = empty ? `${wd} ${h}:00, no data` : `${wd} ${h}:00, ${fmtNum(c.avg_in)} visitors on average (${c.days} days)`;
                return (
                  <span key={h} role="cell" aria-label={label} title={label} tabIndex={-1}
                    onMouseEnter={() => setTip(c ?? null)}
                    className={cn("h-7 rounded-[3px]", empty ? "border border-rule/70" : "")}
                    style={empty ? undefined : { background: heatColor(c.avg_in ?? 0, max) }} />
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-sm text-muted">
        <span className="inline-flex items-center gap-1.5">
          Fewer
          {RAMP.slice(1).map((c) => <span key={c} className="h-3 w-4 rounded-[2px]" style={{ background: c }} />)}
          More
        </span>
        <span className="inline-flex items-center gap-1.5"><span className="h-3 w-4 rounded-[2px] border border-rule" />No data</span>
        <span className="ml-auto min-h-5 text-text" aria-live="polite">
          {tip && tip.avg_in !== null
            ? `${WD_SHORT[tip.weekday]} ${String(tip.hour).padStart(2, "0")}:00: ${fmtNum(tip.avg_in)} on average`
            : `Up to ${fmtNum(max)} per hour`}
        </span>
      </div>
    </div>
  );
}

// --- small horizontal bars ------------------------------------------------------------------------

export function HBars({ rows }: { rows: { label: string; value: number; text: string; color?: string }[] }) {
  const top = Math.max(1, ...rows.map((r) => r.value));
  return (
    <ul className="space-y-2.5">
      {rows.map((r) => (
        <li key={r.label} className="grid grid-cols-[88px_minmax(0,1fr)_auto] items-center gap-3 text-sm">
          <span className="truncate text-muted">{r.label}</span>
          <span className="h-2.5 overflow-hidden rounded-[3px] bg-raise">
            <span className="block h-full rounded-r-[3px]"
              style={{ width: `${(r.value / top) * 100}%`, background: r.color ?? ACCENT, minWidth: r.value > 0 ? 3 : 0 }} />
          </span>
          <span className="min-w-[3ch] text-right text-text">{r.text}</span>
        </li>
      ))}
    </ul>
  );
}

// --- tiny 24-hour bars for the overview ---------------------------------------------------------

export function HourSpark({ hours, label }: { hours: (number | null)[]; label: string }) {
  const top = Math.max(1, ...hours.map((v) => v ?? 0));
  const last = hours.reduce<number>((acc, v, i) => (v !== null ? i : acc), -1);
  return (
    <svg viewBox="0 0 96 28" className="h-7 w-full" preserveAspectRatio="none" role="img" aria-label={label}>
      <line x1={0} x2={96} y1={27.5} y2={27.5} stroke="var(--color-rule)" strokeWidth={1} />
      {hours.map((v, i) => (v ? (
        <rect key={i} x={i * 4 + 0.6} width={2.8} y={27 - (26 * v) / top} height={(26 * v) / top}
          fill={ACCENT} opacity={i === last ? 1 : 0.45} />
      ) : null))}
    </svg>
  );
}

// --- zone occupancy over time -------------------------------------------------------------------

export function ZoneChart({ points, bucket, days, unit, height = 120 }: {
  points: { t: string; avg: number | null; max: number | null }[]; bucket: "hour" | "day"; days: number;
  unit: string; height?: number;
}) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = React.useState<number | null>(null);
  const pad = { l: 28, r: 6, t: 8, b: 22 };
  const w = Math.max(0, width - pad.l - pad.r);
  const h = height - pad.t - pad.b;
  const top = Math.max(1, ...points.map((p) => p.avg ?? 0));
  const ticks = niceTicks(top);
  const max = ticks[ticks.length - 1];
  const n = points.length;
  const x = (i: number) => pad.l + (n <= 1 ? w / 2 : (w * i) / (n - 1));
  const y = (v: number) => pad.t + h - (h * v) / max;
  // break the line where there is no data
  let d = "";
  let area = "";
  let run: number[] = [];
  const flush = () => {
    if (run.length) {
      d += run.map((i, k) => `${k ? "L" : "M"}${x(i).toFixed(1)},${y(points[i].avg ?? 0).toFixed(1)}`).join("");
      area += `M${x(run[0]).toFixed(1)},${y(0)}` + run.map((i) => `L${x(i).toFixed(1)},${y(points[i].avg ?? 0).toFixed(1)}`).join("") +
        `L${x(run[run.length - 1]).toFixed(1)},${y(0)}Z`;
    }
    run = [];
  };
  points.forEach((p, i) => (p.avg === null ? flush() : run.push(i)));
  flush();
  const every = Math.max(1, Math.ceil(n / Math.max(3, Math.floor(w / 46))));
  const a = active !== null ? points[active] : null;
  return (
    <div ref={ref} className="relative">
      {width > 0 ? (
        <svg width={width} height={height} className="block" onMouseLeave={() => setActive(null)}>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={pad.l} x2={pad.l + w} y1={y(t)} y2={y(t)} stroke="var(--color-rule)" strokeWidth={1} />
              <text x={pad.l - 6} y={y(t) + 4} textAnchor="end" fontSize={11} fill="var(--color-faint)">{fmtCompact(t)}</text>
            </g>
          ))}
          <path d={area} fill={ACCENT} opacity={0.1} />
          <path d={d} fill="none" stroke={ACCENT} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
          {points.map((p, i) => (i % every === 0 ? (
            <text key={p.t} x={x(i)} y={height - 6} textAnchor="middle" fontSize={11} fill="var(--color-muted)">
              {bucketLabel(p.t, bucket, days)}
            </text>
          ) : null))}
          {a && active !== null && a.avg !== null ? (
            <circle cx={x(active)} cy={y(a.avg)} r={4} fill={ACCENT} stroke="var(--color-panel)" strokeWidth={2} />
          ) : null}
          {points.map((p, i) => (
            <rect key={p.t} x={x(i) - (n > 1 ? w / (n - 1) / 2 : w / 2)} y={pad.t} width={n > 1 ? w / (n - 1) : w}
              height={h} fill="transparent" onMouseEnter={() => setActive(i)} />
          ))}
        </svg>
      ) : <div style={{ height }} />}
      {a && active !== null ? (
        <Tooltip x={x(active)} y={a.avg !== null ? y(a.avg) - 8 : pad.t} width={width}>
          <div className="font-medium text-text">{bucketTitle(a.t, bucket)}</div>
          {a.avg === null ? <div className="text-muted">No data</div> : (
            <>
              <div className="mt-1 flex justify-between"><span className="text-muted">Average {unit}</span><span>{fmtNum(a.avg)}</span></div>
              <div className="flex justify-between"><span className="text-muted">Most at once</span><span>{fmtNum(a.max)}</span></div>
            </>
          )}
        </Tooltip>
      ) : null}
    </div>
  );
}
