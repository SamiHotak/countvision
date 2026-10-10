// Pure geometry for the camera editor (no React). Points are normalized (0..1); "size" is the
// stage size in screen pixels, so snapping and hit areas feel the same at every zoom.
// Same rules as the edge app's editor (edge/countvision_edge/app/static/app.js).

import type { CameraConfig, LineCfg, Pt } from "@/lib/api";

export type Size = { w: number; h: number };

/** What the pointer touches: a corner (key = "p1"/"p2" or polygon index), or a whole shape. */
export type Hit = { type: "line" | "zone"; i: number; key: "p1" | "p2" | number | null };

export const clamp01 = (v: number) => Math.min(1, Math.max(0, v));

export const toPx = (p: Pt, s: Size): [number, number] => [p[0] * s.w, p[1] * s.h];

export function round(p: Pt): Pt {
  return [Math.round(clamp01(p[0]) * 100000) / 100000, Math.round(clamp01(p[1]) * 100000) / 100000];
}

export function distToSegment(p: [number, number], a: [number, number], b: [number, number]): number {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)));
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

export function insidePolygon([x, y]: [number, number], poly: [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Corners first (they sit on top), then lines, then zones. ``r`` = grab radius in px. */
export function hitTest(doc: CameraConfig, px: [number, number], s: Size, r: number): Hit | null {
  for (let i = doc.lines.length - 1; i >= 0; i--) {
    for (const key of ["p1", "p2"] as const) {
      const [x, y] = toPx(doc.lines[i][key], s);
      if (Math.hypot(x - px[0], y - px[1]) <= r) return { type: "line", i, key };
    }
  }
  for (let i = doc.zones.length - 1; i >= 0; i--) {
    const poly = doc.zones[i].polygon;
    for (let k = 0; k < poly.length; k++) {
      const [x, y] = toPx(poly[k], s);
      if (Math.hypot(x - px[0], y - px[1]) <= r) return { type: "zone", i, key: k };
    }
  }
  for (let i = doc.lines.length - 1; i >= 0; i--) {
    const l = doc.lines[i];
    if (distToSegment(px, toPx(l.p1, s), toPx(l.p2, s)) <= r) return { type: "line", i, key: null };
  }
  for (let i = doc.zones.length - 1; i >= 0; i--) {
    if (insidePolygon(px, doc.zones[i].polygon.map((p) => toPx(p, s)))) return { type: "zone", i, key: null };
  }
  return null;
}

/** Every corner except the one being dragged (snap targets). */
export function corners(doc: CameraConfig, exclude: Hit | null, extra: Pt[] = []): Pt[] {
  const out: Pt[] = [];
  doc.lines.forEach((l, i) =>
    (["p1", "p2"] as const).forEach((k) => {
      if (!(exclude && exclude.type === "line" && exclude.i === i && exclude.key === k)) out.push(l[k]);
    }),
  );
  doc.zones.forEach((z, i) =>
    z.polygon.forEach((p, k) => {
      if (!(exclude && exclude.type === "zone" && exclude.i === i && exclude.key === k)) out.push(p);
    }),
  );
  return out.concat(extra);
}

/**
 * Snap a point: to an existing corner within 12 px; else, for a line end, to 0/45/90 degrees
 * from the other end when within 6 degrees. Returns the point and the corner it snapped to.
 */
export function snapPoint(p: Pt, targets: Pt[], s: Size, anchor: Pt | null, enabled: boolean): { p: Pt; guide: Pt | null } {
  const free: Pt = [clamp01(p[0]), clamp01(p[1])];
  if (!enabled) return { p: free, guide: null };
  const [px, py] = toPx(p, s);
  let best: Pt | null = null;
  let bestD = 12;
  for (const q of targets) {
    const [qx, qy] = toPx(q, s);
    const d = Math.hypot(qx - px, qy - py);
    if (d < bestD) {
      best = q;
      bestD = d;
    }
  }
  if (best) return { p: [best[0], best[1]], guide: best };
  if (anchor) {
    const [ax, ay] = toPx(anchor, s);
    const dx = px - ax;
    const dy = py - ay;
    const len = Math.hypot(dx, dy);
    const ang = Math.atan2(dy, dx);
    const target = Math.round(ang / (Math.PI / 4)) * (Math.PI / 4);
    if (len > 0 && Math.abs(ang - target) < (6 * Math.PI) / 180) {
      return { p: [clamp01((ax + Math.cos(target) * len) / s.w), clamp01((ay + Math.sin(target) * len) / s.h)], guide: null };
    }
  }
  return { p: free, guide: null };
}

/**
 * The IN arrow of a line: from the middle, to the side that counts as "in".
 * ``to_right`` = moving from the left side to the right side, looking from p1 to p2
 * (screen coordinates, y down) — the same rule as the edge's LineCounter.
 */
export function inArrow(line: LineCfg, s: Size): { mx: number; my: number; nx: number; ny: number } {
  const [ax, ay] = toPx(line.p1, s);
  const [bx, by] = toPx(line.p2, s);
  const dx = bx - ax;
  const dy = by - ay;
  const len = Math.hypot(dx, dy) || 1;
  let nx = -dy / len;
  let ny = dx / len;
  if (line.in_direction === "to_left") {
    nx = -nx;
    ny = -ny;
  }
  return { mx: (ax + bx) / 2, my: (ay + by) / 2, nx, ny };
}

/** Move a whole shape by (dx, dy) but keep it inside the picture. */
export function moveInside(points: Pt[], dx: number, dy: number): Pt[] {
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const mx = Math.min(Math.max(dx, -Math.min(...xs)), 1 - Math.max(...xs));
  const my = Math.min(Math.max(dy, -Math.min(...ys)), 1 - Math.max(...ys));
  return points.map((p) => [p[0] + mx, p[1] + my] as Pt);
}

export function uniqueName(doc: CameraConfig, base: string): string {
  const used = new Set([...doc.lines, ...doc.zones].map((x) => x.name));
  for (let n = 1; ; n++) if (!used.has(`${base} ${n}`)) return `${base} ${n}`;
}

export function cloneDoc(doc: CameraConfig): CameraConfig {
  return JSON.parse(JSON.stringify(doc)) as CameraConfig;
}

export const EMPTY_CONFIG: CameraConfig = { lines: [], zones: [], classes: ["person"], anchor: "bottom_center", schedule: null };

/** Same content? (for "unsaved changes") */
export function sameDoc(a: CameraConfig, b: CameraConfig): boolean {
  return JSON.stringify(normalize(a)) === JSON.stringify(normalize(b));
}

/** The document as the API stores it (rounded points, sorted days). */
export function normalize(doc: CameraConfig): CameraConfig {
  return {
    lines: doc.lines.map((l) => ({ name: l.name.trim(), p1: round(l.p1), p2: round(l.p2), in_direction: l.in_direction })),
    zones: doc.zones.map((z) => ({ name: z.name.trim(), polygon: z.polygon.map(round), kind: z.kind })),
    classes: [...doc.classes],
    anchor: doc.anchor,
    schedule: doc.schedule ? { days: [...doc.schedule.days].sort((a, b) => a - b), start: doc.schedule.start, end: doc.schedule.end } : null,
  };
}

/** Problems that would make the save fail, in plain words (checked before sending). */
export function problems(doc: CameraConfig): string[] {
  const out: string[] = [];
  const names = (xs: { name: string }[]) => xs.map((x) => x.name.trim());
  const dup = (xs: string[]) => xs.filter((x, i) => xs.indexOf(x) !== i);
  if (names(doc.lines).some((n) => !n) || names(doc.zones).some((n) => !n)) out.push("Every line and zone needs a name.");
  for (const n of new Set(dup(names(doc.lines)))) out.push(`Two lines are called "${n}". Give each line its own name.`);
  for (const n of new Set(dup(names(doc.zones)))) out.push(`Two zones are called "${n}". Give each zone its own name.`);
  if (doc.classes.length === 0) out.push("Choose at least one thing to count.");
  if (doc.schedule && doc.schedule.days.length === 0) out.push("Choose at least one day, or count all the time.");
  if (doc.lines.length > 16 || doc.zones.length > 16) out.push("At most 16 lines and 16 zones per camera.");
  return out;
}
