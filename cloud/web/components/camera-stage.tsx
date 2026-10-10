"use client";

// The drawing surface of the camera editor: the snapshot (or an empty grid) with counting
// lines and zones on top. SVG in screen pixels, so lines and handles stay sharp and the same
// size at every window width. All editing logic is here; the page owns the document.

import * as React from "react";

import type { CameraConfig, LineCfg, Pt } from "@/lib/api";
import {
  corners,
  hitTest,
  inArrow,
  moveInside,
  snapPoint,
  toPx,
  uniqueName,
  type Hit,
  type Size,
} from "@/lib/geometry";

export type Tool = "select" | "line" | "zone";
export type Selection = { type: "line" | "zone"; i: number } | null;

type Drag =
  | { kind: "point" | "move"; hit: Hit; start: Pt; orig: CameraConfig; moved: boolean }
  | { kind: "newline"; p1: Pt; p2: Pt };

export interface StageProps {
  doc: CameraConfig;
  /** Change the document. ``record`` = false while dragging (one undo step per drag). */
  onChange: (doc: CameraConfig, record?: boolean) => void;
  /** Called once when a drag starts, before the first change (saves the undo step). */
  onBeginChange: () => void;
  tool: Tool;
  setTool: (tool: Tool) => void;
  selection: Selection;
  setSelection: (sel: Selection) => void;
  snap: boolean;
  readOnly: boolean;
  aspect: number; // width / height of the camera picture
  image: string | null;
  onImageSize?: (w: number, h: number) => void;
  today: Record<string, { in: number; out: number }>;
  pulses: Record<string, number>; // line name -> counter (changes on every crossing)
  onHint: (text: string) => void;
  pending: Pt[] | null;
  setPending: (pts: Pt[] | null) => void;
}

const HALO = "rgba(4, 10, 14, 0.6)";

function Label({ x, y, text, color, w, h, strong = false }: {
  x: number; y: number; text: string; color: string; w: number; h: number; strong?: boolean;
}) {
  const size = w < 520 ? 11 : 13;
  const boxW = text.length * size * 0.5 + 12; // Barlow is narrow: about half the font size per character
  const boxH = size + 8;
  const lx = Math.min(Math.max(2, x), w - boxW - 2);
  const ly = Math.min(Math.max(2, y), h - boxH - 2);
  return (
    <g pointerEvents="none">
      <rect x={lx} y={ly} width={boxW} height={boxH} rx={4} fill={strong ? "rgba(13, 52, 50, 0.92)" : "rgba(8, 16, 22, 0.84)"} />
      <text x={lx + 6} y={ly + size + 2} fill={color} fontSize={size} fontWeight={600}
        style={{ fontVariantNumeric: "tabular-nums" }}>{text}</text>
    </g>
  );
}

function LineShape({ line, s, selected, editable, counts, pulse, touch }: {
  line: LineCfg; s: Size; selected: boolean; editable: boolean; counts?: { in: number; out: number };
  pulse?: number; touch: boolean;
}) {
  const [ax, ay] = toPx(line.p1, s);
  const [bx, by] = toPx(line.p2, s);
  const { mx, my, nx, ny } = inArrow(line, s);
  const tipX = mx + nx * 34;
  const tipY = my + ny * 34;
  const ang = Math.atan2(ny, nx);
  const head = [
    [tipX + Math.cos(ang) * 4, tipY + Math.sin(ang) * 4],
    [tipX - Math.cos(ang - 0.55) * 12, tipY - Math.sin(ang - 0.55) * 12],
    [tipX - Math.cos(ang + 0.55) * 12, tipY - Math.sin(ang + 0.55) * 12],
  ].map((p) => p.join(",")).join(" ");
  const r = touch ? 11 : 6;
  const text = counts ? `${line.name}   in ${counts.in}   out ${counts.out}` : line.name;
  return (
    <g data-shape="line" data-name={line.name}>
      <line x1={ax} y1={ay} x2={bx} y2={by} stroke={HALO} strokeWidth={selected ? 10 : 8} strokeLinecap="round" />
      <line x1={ax} y1={ay} x2={bx} y2={by} stroke="var(--color-accent)" strokeWidth={selected ? 5 : 3} strokeLinecap="round" />
      {pulse ? (
        <line key={pulse} className="cv-pulse" x1={ax} y1={ay} x2={bx} y2={by} stroke="var(--color-accent)"
          strokeLinecap="round" />
      ) : null}
      <line x1={mx} y1={my} x2={tipX} y2={tipY} stroke="var(--color-accent)" strokeWidth={3} />
      <polygon points={head} fill="var(--color-accent)" />
      <Label x={tipX + nx * 10 - 12} y={tipY + ny * 10 - 10} text="IN" color="var(--color-accent)" w={s.w} h={s.h} />
      <Label x={Math.min(ax, bx)} y={Math.min(ay, by) - 30} text={text} color="#dce6ea" w={s.w} h={s.h} strong={selected} />
      {editable ? (
        <>
          <circle cx={ax} cy={ay} r={r} fill={selected ? "var(--color-accent)" : "#dce6ea"} stroke="#081016" strokeWidth={2} />
          <circle cx={bx} cy={by} r={r} fill={selected ? "var(--color-accent)" : "#dce6ea"} stroke="#081016" strokeWidth={2} />
        </>
      ) : null}
    </g>
  );
}

export function CameraStage(props: StageProps) {
  const { doc, onChange, onBeginChange, tool, setTool, selection, setSelection, snap, readOnly, aspect, image,
    today, pulses, onHint, pending, setPending } = props;
  const wrap = React.useRef<HTMLDivElement>(null);
  const [s, setS] = React.useState<Size>({ w: 800, h: 450 });
  const [drag, setDrag] = React.useState<Drag | null>(null);
  const [guide, setGuide] = React.useState<Pt | null>(null);
  const [pointer, setPointer] = React.useState<Pt | null>(null);
  const [touch, setTouch] = React.useState(false);
  const [cursor, setCursor] = React.useState("default");

  React.useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const update = () => setS({ w: el.clientWidth || 800, h: el.clientHeight || 450 });
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const norm = (ev: React.PointerEvent | React.MouseEvent): Pt => {
    const rect = wrap.current!.getBoundingClientRect();
    return [(ev.clientX - rect.left) / rect.width, (ev.clientY - rect.top) / rect.height];
  };
  const grab = touch ? 18 : 10;

  function finishZone(points: Pt[]) {
    if (points.length < 3) {
      onHint("A zone needs at least 3 corners.");
      return;
    }
    const next = { ...doc, zones: [...doc.zones, { name: uniqueName(doc, "zone"), kind: "area" as const, polygon: points }] };
    onChange(next);
    setPending(null);
    setSelection({ type: "zone", i: next.zones.length - 1 });
    setTool("select");
  }

  function onPointerDown(ev: React.PointerEvent<SVGSVGElement>) {
    if (readOnly || ev.button > 0) return;
    const isTouch = ev.pointerType === "touch";
    setTouch(isTouch);
    const n = norm(ev);
    const px = toPx(n, s);
    ev.currentTarget.setPointerCapture(ev.pointerId);
    ev.preventDefault();
    if (tool === "select") {
      const hit = hitTest(doc, px, s, isTouch ? 18 : 10);
      if (!hit) {
        setSelection(null);
        return;
      }
      setSelection({ type: hit.type, i: hit.i });
      setDrag({ kind: hit.key === null ? "move" : "point", hit, start: n, orig: doc, moved: false });
    } else if (tool === "line") {
      const { p } = snapPoint(n, corners(doc, null), s, null, snap);
      setDrag({ kind: "newline", p1: p, p2: p });
    } else {
      const { p } = snapPoint(n, corners(doc, null, pending ?? []), s, null, snap);
      if (pending && pending.length >= 3) {
        const [fx, fy] = toPx(pending[0], s);
        if (Math.hypot(fx - px[0], fy - px[1]) <= (isTouch ? 20 : 12)) {
          finishZone(pending);
          return;
        }
      }
      const next = [...(pending ?? []), p];
      setPending(next);
      onHint(next.length < 3 ? `Corner ${next.length} set. Click the next corner.`
        : "Click the first corner (or double-click, or press Enter) to finish the zone.");
    }
  }

  function onPointerMove(ev: React.PointerEvent<SVGSVGElement>) {
    if (readOnly) return;
    const n = norm(ev);
    setPointer(n);
    if (!drag) {
      if (tool === "zone" && pending) setGuide(snapPoint(n, corners(doc, null, pending), s, null, snap).guide);
      else if (tool === "select") {
        const hit = hitTest(doc, toPx(n, s), s, grab);
        setCursor(hit ? (hit.key === null ? "move" : "grab") : "default");
      }
      return;
    }
    if (drag.kind === "newline") {
      const r = snapPoint(n, corners(doc, null), s, drag.p1, snap);
      setGuide(r.guide);
      setDrag({ ...drag, p2: r.p });
      return;
    }
    if (!drag.moved) {
      if (Math.hypot((n[0] - drag.start[0]) * s.w, (n[1] - drag.start[1]) * s.h) < 2) return;
      onBeginChange();
      setDrag({ ...drag, moved: true });
    }
    const { hit, orig } = drag;
    const next: CameraConfig = { ...orig, lines: [...orig.lines], zones: [...orig.zones] };
    if (drag.kind === "point") {
      if (hit.type === "line") {
        const key = hit.key as "p1" | "p2";
        const other = orig.lines[hit.i][key === "p1" ? "p2" : "p1"];
        const r = snapPoint(n, corners(orig, hit), s, other, snap);
        setGuide(r.guide);
        next.lines[hit.i] = { ...orig.lines[hit.i], [key]: r.p };
      } else {
        const r = snapPoint(n, corners(orig, hit), s, null, snap);
        setGuide(r.guide);
        const poly = [...orig.zones[hit.i].polygon];
        poly[hit.key as number] = r.p;
        next.zones[hit.i] = { ...orig.zones[hit.i], polygon: poly };
      }
    } else {
      const dx = n[0] - drag.start[0];
      const dy = n[1] - drag.start[1];
      if (hit.type === "line") {
        const [p1, p2] = moveInside([orig.lines[hit.i].p1, orig.lines[hit.i].p2], dx, dy);
        next.lines[hit.i] = { ...orig.lines[hit.i], p1, p2 };
      } else {
        next.zones[hit.i] = { ...orig.zones[hit.i], polygon: moveInside(orig.zones[hit.i].polygon, dx, dy) };
      }
    }
    onChange(next, false);
  }

  function onPointerUp() {
    const d = drag;
    setDrag(null);
    setGuide(null);
    if (!d || d.kind !== "newline") return;
    const [ax, ay] = toPx(d.p1, s);
    const [bx, by] = toPx(d.p2, s);
    if (Math.hypot(bx - ax, by - ay) < 15) {
      onHint("Drag across the door or path to draw a line.");
      return;
    }
    const next = { ...doc, lines: [...doc.lines, { name: uniqueName(doc, "line"), p1: d.p1, p2: d.p2, in_direction: "to_right" as const }] };
    onChange(next);
    setSelection({ type: "line", i: next.lines.length - 1 });
    setTool("select");
    onHint("The arrow points to the IN side. Flip it if people come in the other way.");
  }

  function onDoubleClick(ev: React.MouseEvent) {
    if (tool === "zone" && pending) {
      ev.preventDefault();
      // the double click also set two single corners: drop the duplicate
      finishZone(pending.length > 3 ? pending.slice(0, -1) : pending);
    }
  }

  const sel = selection;
  const r = touch ? 11 : 6;
  const preview: LineCfg | null =
    drag?.kind === "newline" ? { name: "", p1: drag.p1, p2: drag.p2, in_direction: "to_right" } : null;

  return (
    <div ref={wrap} className="relative w-full select-none overflow-hidden rounded-[var(--radius-s)] border border-rule bg-[#0a1218]"
      style={{ aspectRatio: String(aspect) }} data-testid="stage">
      {image ? (
        // eslint-disable-next-line @next/next/no-img-element -- a private, short-lived snapshot (no optimisation)
        <img src={image} alt="Camera snapshot, people pixelated" className="absolute inset-0 size-full object-fill"
          draggable={false} onLoad={(e) => props.onImageSize?.(e.currentTarget.naturalWidth, e.currentTarget.naturalHeight)} />
      ) : (
        <div aria-hidden className="absolute inset-0 cv-grid" />
      )}
      <svg
        className="absolute inset-0 size-full touch-none"
        viewBox={`0 0 ${s.w} ${s.h}`}
        style={{ cursor: readOnly ? "default" : tool === "select" ? cursor : "crosshair" }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onPointerLeave={() => setPointer(null)}
        onDoubleClick={onDoubleClick}
        role="img"
        aria-label={`Counting lines: ${doc.lines.map((l) => l.name).join(", ") || "none"}. Zones: ${doc.zones.map((z) => z.name).join(", ") || "none"}.`}
      >
        {doc.zones.map((z, i) => {
          const selected = sel?.type === "zone" && sel.i === i;
          const pts = z.polygon.map((p) => toPx(p, s));
          const top = pts.reduce((a, b) => (b[1] < a[1] ? b : a));
          return (
            <g key={`z${i}`} data-shape="zone" data-name={z.name}>
              <polygon points={pts.map((p) => p.join(",")).join(" ")} fill="var(--color-accent)"
                fillOpacity={selected ? 0.22 : 0.12} stroke="var(--color-accent)" strokeWidth={selected ? 3 : 2}
                strokeDasharray={z.kind === "queue" ? "8 5" : undefined} strokeLinejoin="round" />
              <Label x={top[0] - 4} y={top[1] - 28} text={z.kind === "queue" ? `${z.name} (queue)` : z.name}
                color="var(--color-accent)" w={s.w} h={s.h} strong={selected} />
              {!readOnly
                ? pts.map(([x, y], k) => (
                    <circle key={k} cx={x} cy={y} r={r} fill={selected ? "var(--color-accent)" : "#dce6ea"}
                      stroke="#081016" strokeWidth={2} />
                  ))
                : null}
            </g>
          );
        })}
        {doc.lines.map((l, i) => (
          <LineShape key={`l${i}`} line={l} s={s} selected={sel?.type === "line" && sel.i === i} editable={!readOnly}
            counts={today[l.name] ?? { in: 0, out: 0 }} pulse={pulses[l.name]} touch={touch} />
        ))}
        {preview ? <LineShape line={preview} s={s} selected editable touch={touch} /> : null}
        {pending ? (
          <g>
            <polyline
              points={[...pending, ...(pointer ? [pointer] : [])].map((p) => toPx(p, s).join(",")).join(" ")}
              fill="none" stroke="var(--color-accent)" strokeWidth={2} strokeDasharray="6 4" />
            {pending.map((p, k) => {
              const [x, y] = toPx(p, s);
              return <circle key={k} cx={x} cy={y} r={k === 0 && pending.length >= 3 ? r + 3 : r}
                fill={k === 0 ? "var(--color-accent)" : "#dce6ea"} stroke="#081016" strokeWidth={2} />;
            })}
          </g>
        ) : null}
        {guide ? (
          <circle cx={toPx(guide, s)[0]} cy={toPx(guide, s)[1]} r={r + 6} fill="none" stroke="#fff" strokeWidth={1.5} />
        ) : null}
      </svg>
    </div>
  );
}
