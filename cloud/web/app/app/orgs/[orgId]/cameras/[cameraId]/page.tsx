"use client";

// Camera editor: draw counting lines and zones on a snapshot, choose what to count and when,
// save -> the device uses it within seconds. Counts on the lines update live.

import Link from "next/link";
import { useParams } from "next/navigation";
import * as React from "react";

import { CameraStage, type Selection, type Tool } from "@/components/camera-stage";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Notice, Panel } from "@/components/ui/panel";
import { StatusDot, type Health } from "@/components/ui/status-dot";
import {
  api,
  ApiError,
  CLASSES,
  errorText,
  roleRank,
  type CameraConfig,
  type CameraDetail,
  type LiveMessage,
  type Org,
  type Pt,
  type SnapshotStatus,
} from "@/lib/api";
import { cloneDoc, EMPTY_CONFIG, normalize, problems, sameDoc, uniqueName } from "@/lib/geometry";
import { liveLabel, useLive } from "@/lib/live";
import { cn } from "@/lib/utils";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

type Snap =
  | { status: "idle" }
  | { status: "pending"; req: string }
  | { status: "ready"; url: string; at: Date }
  | { status: "error"; message: string };

function cameraState(d: CameraDetail): { health: Health; label: string } {
  if (!d.device_online) return { health: "idle", label: "Device offline" };
  switch (d.state) {
    case "running":
      return { health: "ok", label: d.fps != null ? `Counting, ${d.fps.toFixed(1)} FPS` : "Counting" };
    case "paused":
      return { health: "idle", label: "Paused outside counting hours" };
    case "connecting":
    case "starting":
    case "offline":
    case "restarting":
      return { health: "warn", label: "Reconnecting to the camera" };
    case "failed":
      return { health: "down", label: "Camera failed" };
    default:
      return { health: "idle", label: d.state ?? "Unknown" };
  }
}

/** One line of text about where the saved config is. */
function syncText(d: CameraDetail, dirty: boolean): { tone: "info" | "warn" | "error" | "success"; text: string; busy?: boolean } {
  if (dirty) return { tone: "warn", text: "Unsaved changes. The device keeps counting with the saved version." };
  if (d.config_source === "none") return { tone: "info", text: "No lines yet. Draw the first one on the picture." };
  if (d.config_source === "device") {
    return { tone: "info", text: "These lines come from the device's own config file. When you save here, the cloud version is used from then on." };
  }
  if (d.desired_version > d.applied_version) {
    if (d.config_error) return { tone: "error", text: `The device could not use version ${d.desired_version}: ${d.config_error}` };
    if (!d.device_online) return { tone: "warn", text: `Version ${d.desired_version} saved. The device is offline and gets it when it is back.` };
    return { tone: "info", text: `Sending version ${d.desired_version} to the device`, busy: true };
  }
  if (!d.device_online) return { tone: "info", text: `The device has version ${d.applied_version}. It is offline right now.` };
  return { tone: "success", text: `Version ${d.applied_version} is running on the device.` };
}

function Section({ title, children, className }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <section className={cn("border-t border-rule px-4 py-4", className)}>
      <h2 className="mb-3 font-semibold">{title}</h2>
      {children}
    </section>
  );
}

function Seg<T extends string>({ value, options, onChange, disabled, label }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; disabled?: boolean; label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-[var(--radius-s)] border border-rule bg-ink p-0.5">
      {options.map((o) => (
        <button key={o.value} type="button" role="radio" aria-checked={value === o.value} disabled={disabled}
          onClick={() => onChange(o.value)}
          className={cn("h-8 cursor-pointer rounded-[4px] px-3 text-sm disabled:cursor-default",
            value === o.value ? "bg-raise text-text" : "text-muted hover:text-text")}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

function ToolButton({ active, onClick, children, keyHint, disabled, label, toggle = false }: {
  active?: boolean; onClick: () => void; children: React.ReactNode; keyHint?: string; disabled?: boolean; label?: string;
  toggle?: boolean; // an on/off setting (outlined), not a drawing tool (filled)
}) {
  return (
    <button type="button" onClick={onClick} disabled={disabled} aria-pressed={active} aria-label={label}
      title={keyHint ? `${label ?? ""} (${keyHint})`.trim() : label}
      className={cn("inline-flex h-9 cursor-pointer items-center gap-2 rounded-[var(--radius-s)] px-3 text-sm",
        "disabled:cursor-default disabled:opacity-40",
        active && !toggle ? "bg-accent text-accent-ink"
          : active ? "text-accent ring-1 ring-inset ring-accent/50 hover:bg-raise" : "text-muted hover:bg-raise hover:text-text")}>
      {children}
      {keyHint ? <kbd className={cn("hidden text-xs sm:inline", active && !toggle ? "text-accent-ink/70" : "text-faint")}>{keyHint}</kbd> : null}
    </button>
  );
}

function Editor({ org, cameraId }: { org: Org; cameraId: string }) {
  const canEdit = roleRank(org.my_role) >= roleRank("member");
  const base = `/api/orgs/${org.id}/cameras/${cameraId}`;
  const [d, setD] = React.useState<CameraDetail | null>(null);
  const [loadError, setLoadError] = React.useState<{ text: string; missing: boolean } | null>(null);
  const [saved, setSaved] = React.useState<CameraConfig>(EMPTY_CONFIG);
  const [doc, setDoc] = React.useState<CameraConfig>(EMPTY_CONFIG);
  const [past, setPast] = React.useState<CameraConfig[]>([]);
  const [future, setFuture] = React.useState<CameraConfig[]>([]);
  const [tool, setTool] = React.useState<Tool>("select");
  const [sel, setSel] = React.useState<Selection>(null);
  const [snap, setSnapOn] = React.useState(true);
  const [pending, setPending] = React.useState<Pt[] | null>(null);
  const [hint, setHint] = React.useState("");
  const [shot, setShot] = React.useState<Snap>({ status: "idle" });
  const [imgAspect, setImgAspect] = React.useState<number | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [message, setMessage] = React.useState<{ tone: "error" | "success"; text: string } | null>(null);
  const [today, setToday] = React.useState<Record<string, { in: number; out: number }>>({});
  const [pulses, setPulses] = React.useState<Record<string, number>>({});
  const dirty = !sameDoc(doc, saved);
  const dirtyRef = React.useRef(dirty);
  const pollNow = React.useRef<(() => void) | null>(null);
  dirtyRef.current = dirty;

  const load = React.useCallback(async (resetDoc: boolean) => {
    try {
      const detail = await api.get<CameraDetail>(base);
      setD(detail);
      setToday(detail.today);
      setLoadError(null);
      if (resetDoc || !dirtyRef.current) {
        // never overwrite the user's unsaved drawing with a background refresh
        const cfg = detail.config ? cloneDoc(detail.config) : cloneDoc(EMPTY_CONFIG);
        setSaved(cfg);
        setDoc(cloneDoc(cfg));
      }
    } catch (e) {
      setLoadError({ text: errorText(e), missing: e instanceof ApiError && (e.status === 404 || e.status === 422) });
    }
  }, [base]);

  React.useEffect(() => {
    void load(true);
  }, [load]);

  // --- live updates ---------------------------------------------------------------------------
  const onLive = React.useCallback((msg: LiveMessage) => {
    if (msg.type === "count" && msg.camera_id === cameraId) {
      setToday((t) => {
        const cur = t[msg.line] ?? { in: 0, out: 0 };
        return { ...t, [msg.line]: { ...cur, [msg.direction]: cur[msg.direction] + 1 } };
      });
      setPulses((p) => ({ ...p, [msg.line]: (p[msg.line] ?? 0) + 1 }));
    } else if (msg.type === "device") {
      const cam = msg.cameras.find((c) => c.camera_id === cameraId);
      if (cam) {
        setD((cur) => cur && {
          ...cur, device_online: true, state: cam.state, fps: cam.fps, applied_version: cam.applied_version,
          desired_version: Math.max(cur.desired_version, cam.desired_version), config_error: cam.config_error,
        });
      }
    } else if (msg.type === "config" && msg.camera_id === cameraId) {
      void load(false); // somebody saved (maybe in another tab)
    } else if (msg.type === "refresh") {
      void load(false);
    } else if (msg.type === "snapshot" && msg.camera_id === cameraId) {
      pollNow.current?.(); // the picture is ready: check now instead of at the next poll
    }
  }, [cameraId, load]);
  const live = useLive(org.id, onLive);

  // Fallback: refresh slowly (status, applied version) even without the live stream.
  React.useEffect(() => {
    const timer = setInterval(() => void load(false), live === "live" ? 30_000 : 10_000);
    return () => clearInterval(timer);
  }, [load, live]);

  // --- document changes with undo/redo --------------------------------------------------------
  const change = React.useCallback((next: CameraConfig, record = true) => {
    if (record) {
      setPast((p) => [...p.slice(-99), doc]);
      setFuture([]);
    }
    setDoc(next);
  }, [doc]);
  const beginChange = React.useCallback(() => {
    setPast((p) => [...p.slice(-99), doc]);
    setFuture([]);
  }, [doc]);
  const undo = React.useCallback(() => {
    if (!past.length) return;
    setFuture((f) => [doc, ...f]);
    setDoc(past[past.length - 1]);
    setPast((p) => p.slice(0, -1));
    setSel(null);
  }, [past, doc]);
  const redo = React.useCallback(() => {
    if (!future.length) return;
    setPast((p) => [...p, doc]);
    setDoc(future[0]);
    setFuture((f) => f.slice(1));
    setSel(null);
  }, [future, doc]);

  const selected = sel ? (sel.type === "line" ? doc.lines[sel.i] : doc.zones[sel.i]) : undefined;

  const flip = React.useCallback(() => {
    if (sel?.type !== "line" || !doc.lines[sel.i]) return;
    const lines = [...doc.lines];
    lines[sel.i] = { ...lines[sel.i], in_direction: lines[sel.i].in_direction === "to_right" ? "to_left" : "to_right" };
    change({ ...doc, lines });
  }, [sel, doc, change]);

  const remove = React.useCallback(() => {
    if (!sel) return;
    if (sel.type === "line") change({ ...doc, lines: doc.lines.filter((_, i) => i !== sel.i) });
    else change({ ...doc, zones: doc.zones.filter((_, i) => i !== sel.i) });
    setSel(null);
  }, [sel, doc, change]);

  function rename(name: string) {
    if (!sel || !selected || name === selected.name) return;
    if (sel.type === "line") {
      const lines = [...doc.lines];
      lines[sel.i] = { ...lines[sel.i], name };
      change({ ...doc, lines });
    } else {
      const zones = [...doc.zones];
      zones[sel.i] = { ...zones[sel.i], name };
      change({ ...doc, zones });
    }
  }

  function pickTool(t: Tool) {
    setTool(t);
    setPending(null);
    setHint(t === "line" ? "Drag across the door or path. The arrow shows the IN side."
      : t === "zone" ? "Click the corners of the area. Click the first corner again to finish."
        : "Click a line or zone to select it. Drag corners to move them.");
  }

  // keyboard shortcuts (not while typing in a field)
  React.useEffect(() => {
    if (!canEdit) return;
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select, [contenteditable=true]")) return;
      const key = e.key.toLowerCase();
      const mod = e.ctrlKey || e.metaKey;
      if (mod && key === "z") { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
      if (mod && key === "y") { e.preventDefault(); redo(); return; }
      if (mod) return;
      if (key === "v") pickTool("select");
      else if (key === "l") pickTool("line");
      else if (key === "z") pickTool("zone");
      else if (key === "f") flip();
      else if (key === "s") setSnapOn((v) => !v);
      else if (key === "delete" || key === "backspace") { if (sel) { e.preventDefault(); remove(); } }
      else if (key === "escape") { if (pending) setPending(null); else setSel(null); }
      else if (key === "enter" && pending && pending.length >= 3) {
        change({ ...doc, zones: [...doc.zones, { name: uniqueName(doc, "zone"), kind: "area", polygon: pending }] });
        setPending(null);
        setSel({ type: "zone", i: doc.zones.length });
        setTool("select");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // leaving with unsaved changes
  React.useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // --- snapshot -------------------------------------------------------------------------------
  async function takeSnapshot() {
    setMessage(null);
    try {
      const out = await api.post<SnapshotStatus>(`${base}/snapshot`);
      setShot({ status: "pending", req: out.request_id });
      const started = Date.now();
      let timer: ReturnType<typeof setTimeout> | undefined;
      const check = async () => {
        if (timer) clearTimeout(timer);
        try {
          const st = await api.get<SnapshotStatus>(`${base}/snapshot/${out.request_id}`);
          if (st.status === "ready") {
            setShot({ status: "ready", url: `${base}/snapshot/${out.request_id}/image`, at: new Date() });
            pollNow.current = null;
            return;
          }
          if (st.status === "error") {
            setShot({ status: "error", message: st.message ?? "The device could not take a picture." });
            pollNow.current = null;
            return;
          }
        } catch (e) {
          setShot({ status: "error", message: errorText(e) });
          pollNow.current = null;
          return;
        }
        if (Date.now() - started > 30_000) {
          setShot({ status: "error", message: "The device did not answer within 30 seconds. Is it online?" });
          pollNow.current = null;
          return;
        }
        timer = setTimeout(() => void check(), 1000);
      };
      pollNow.current = () => void check();
      timer = setTimeout(() => void check(), 600);
    } catch (e) {
      setShot({ status: "error", message: errorText(e) });
    }
  }

  // --- save -----------------------------------------------------------------------------------
  const issues = problems(doc);
  async function save() {
    if (issues.length) return;
    setSaving(true);
    setMessage(null);
    try {
      const out = await api.put<CameraDetail>(`${base}/config`, normalize(doc));
      setD(out);
      const cfg = out.config ? cloneDoc(out.config) : cloneDoc(doc);
      setSaved(cfg);
      setDoc(cloneDoc(cfg));
    } catch (e) {
      const fields = e instanceof ApiError ? Object.values(e.fields) : [];
      setMessage({ tone: "error", text: fields.length ? fields.join(" ") : errorText(e) });
    } finally {
      setSaving(false);
    }
  }
  function discard() {
    change(cloneDoc(saved));
    setSel(null);
    setPending(null);
  }

  if (loadError && !d) {
    return (
      <div className="max-w-[520px] space-y-4">
        <Notice tone="error">{loadError.missing ? "This camera does not exist (any more)." : loadError.text}</Notice>
        <Link href={`/app/orgs/${org.id}/devices`} className="text-accent">Back to devices</Link>
      </div>
    );
  }
  if (!d) return <Loading />;

  const st = cameraState(d);
  const sync = syncText(d, dirty);
  const aspect = d.frame_width && d.frame_height ? d.frame_width / d.frame_height : imgAspect ?? 16 / 9;
  const snapBlocked = !d.device_online ? "The device is offline." : d.snapshots_allowed === false
    ? "Snapshots are switched off on this device (privacy setting). Draw on the grid." : null;
  const schedule = doc.schedule;
  const readOnly = !canEdit;

  return (
    <>
      <p className="mb-2 text-sm">
        <Link href={`/app/orgs/${org.id}/devices/${d.device_id}`} className="text-muted no-underline hover:text-text">
          {d.device_name}
        </Link>
        <span className="text-faint">, {d.site_name}</span>
      </p>
      <div className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 className="text-2xl font-semibold leading-tight">{d.name}</h1>
        <span className="flex items-center gap-2 text-muted" data-testid="camera-state">
          <StatusDot health={st.health} /> {st.label}
        </span>
        <span className="ml-auto flex items-center gap-2 text-sm text-faint" data-testid="live-state">
          <StatusDot health={live === "live" ? "ok" : "idle"} /> {liveLabel(live)}
        </span>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px] lg:items-start">
        <div className="min-w-0">
          {canEdit ? (
            <div className="mb-2 flex flex-wrap items-center gap-1 rounded-[var(--radius-s)] border border-rule bg-panel p-1"
              role="toolbar" aria-label="Drawing tools">
              <ToolButton active={tool === "select"} onClick={() => pickTool("select")} keyHint="V" label="Select">Select</ToolButton>
              <ToolButton active={tool === "line"} onClick={() => pickTool("line")} keyHint="L" label="Line">Line</ToolButton>
              <ToolButton active={tool === "zone"} onClick={() => pickTool("zone")} keyHint="Z" label="Zone">Zone</ToolButton>
              <span className="mx-1 h-6 w-px bg-rule" aria-hidden />
              <ToolButton onClick={undo} disabled={!past.length} label="Undo">Undo</ToolButton>
              <ToolButton onClick={redo} disabled={!future.length} label="Redo">Redo</ToolButton>
              <ToolButton toggle active={snap} onClick={() => setSnapOn((v) => !v)} keyHint="S" label="Snap">Snap</ToolButton>
              <span className="ml-auto" />
              <Button size="sm" variant="secondary" onClick={() => void takeSnapshot()}
                disabled={!!snapBlocked} busy={shot.status === "pending"} title={snapBlocked ?? undefined}>
                {shot.status === "ready" ? "New snapshot" : "Take snapshot"}
              </Button>
            </div>
          ) : null}

          <CameraStage doc={doc} onChange={change} onBeginChange={beginChange} tool={tool} setTool={pickTool}
            selection={sel} setSelection={setSel} snap={snap} readOnly={readOnly} aspect={aspect}
            image={shot.status === "ready" ? shot.url : null}
            onImageSize={(w, h) => setImgAspect(w / h)} today={today} pulses={pulses} onHint={setHint}
            pending={pending} setPending={setPending} />

          <div className="mt-2 min-h-6 text-sm text-muted" aria-live="polite" data-testid="hint">
            {shot.status === "error" ? <span className="text-danger">Snapshot: {shot.message}</span>
              : shot.status === "ready" ? (
                <span>
                  Snapshot from {shot.at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}. People and
                  vehicles are pixelated on the device. The picture is not stored and is deleted after 10 minutes.
                  {hint ? <span className="block text-faint">{hint}</span> : null}
                </span>
              ) : hint || (canEdit
                ? snapBlocked ?? "Take a snapshot to see the camera picture (people are pixelated), or draw on the grid."
                : "You can see the lines and live counts. Members can change them.")}
          </div>
        </div>

        <Panel>{/* no overflow-hidden: the save bar below sticks to the bottom of the window */}
          <section className="px-4 py-4">
            <h2 className="mb-2 font-semibold">Lines and zones</h2>
            {doc.lines.length + doc.zones.length === 0 ? (
              <p className="text-sm text-muted">{canEdit ? "Choose Line and drag across the entrance." : "No lines yet."}</p>
            ) : (
              <ul className="-mx-2 space-y-0.5" aria-label="Lines and zones">
                {doc.lines.map((l, i) => {
                  const t = today[l.name] ?? { in: 0, out: 0 };
                  const on = sel?.type === "line" && sel.i === i;
                  return (
                    <li key={`l${i}`}>
                      <button type="button" onClick={() => { setSel({ type: "line", i }); pickTool("select"); }}
                        className={cn("grid w-full cursor-pointer grid-cols-[minmax(0,1fr)_auto_auto] items-baseline gap-3 rounded-[var(--radius-s)] px-2 py-1.5 text-left",
                          on ? "bg-raise" : "hover:bg-raise/60")} data-testid="shape-row">
                        <span className="truncate">{l.name}</span>
                        <span className="text-sm text-muted">in <b key={`i${t.in}`} className="cv-tick text-lg font-semibold text-text" data-testid="count-in">{t.in}</b></span>
                        <span className="text-sm text-muted">out <b key={`o${t.out}`} className="cv-tick text-lg font-semibold text-muted" data-testid="count-out">{t.out}</b></span>
                      </button>
                    </li>
                  );
                })}
                {doc.zones.map((z, i) => (
                  <li key={`z${i}`}>
                    <button type="button" onClick={() => { setSel({ type: "zone", i }); pickTool("select"); }}
                      className={cn("flex w-full cursor-pointer items-baseline gap-3 rounded-[var(--radius-s)] px-2 py-1.5 text-left",
                        sel?.type === "zone" && sel.i === i ? "bg-raise" : "hover:bg-raise/60")} data-testid="shape-row">
                      <span className="truncate">{z.name}</span>
                      <span className="ml-auto text-sm text-faint">{z.kind === "queue" ? "queue zone" : "zone"}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <p className="mt-2 text-xs text-faint">Counts since midnight, {d.site_timezone.replace("_", " ")}.</p>
          </section>

          {selected && canEdit ? (
            <section className="border-t border-rule bg-raise/40 px-4 py-4" aria-label="Selected shape">
              <Label htmlFor="shape-name">{sel?.type === "line" ? "Line name" : "Zone name"}</Label>
              <Input id="shape-name" key={`${sel?.type}${sel?.i}${selected.name}`} defaultValue={selected.name} maxLength={64}
                className="mt-1.5"
                onBlur={(e) => rename(e.currentTarget.value.trim() || selected.name)}
                onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />
              {sel?.type === "line" ? (
                <p className="mt-2 text-sm text-faint">Renaming starts a new count for this line.</p>
              ) : null}
              <div className="mt-3 flex flex-wrap items-center gap-2">
                {sel?.type === "line" ? (
                  <Button size="sm" variant="secondary" onClick={flip} title="F">Flip IN direction</Button>
                ) : (
                  <Seg label="Zone kind" value={doc.zones[sel!.i].kind} onChange={(kind) => {
                    const zones = [...doc.zones];
                    zones[sel!.i] = { ...zones[sel!.i], kind };
                    change({ ...doc, zones });
                  }} options={[{ value: "area", label: "Area" }, { value: "queue", label: "Queue" }]} />
                )}
                <Button size="sm" variant="danger" className="ml-auto" onClick={remove}>Delete</Button>
              </div>
            </section>
          ) : null}

          <Section title="What to count">
            <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
              {CLASSES.map((c) => {
                const on = doc.classes.includes(c.id);
                return (
                  <label key={c.id} className={cn("flex items-center gap-2.5", readOnly ? "" : "cursor-pointer")}>
                    <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={on} disabled={readOnly}
                      onChange={() => change({ ...doc, classes: on ? doc.classes.filter((x) => x !== c.id) : [...doc.classes, c.id] })} />
                    <span className="size-2.5 rounded-full" style={{ background: c.color }} aria-hidden />
                    {c.label}
                  </label>
                );
              })}
            </div>
          </Section>

          <Section title="Count point">
            <Seg label="Count point" disabled={readOnly} value={doc.anchor} onChange={(anchor) => change({ ...doc, anchor })}
              options={[{ value: "bottom_center", label: "Feet" }, { value: "center", label: "Middle" }]} />
            <p className="mt-2 text-sm text-faint">
              {doc.anchor === "bottom_center" ? "Someone counts when their feet cross the line. Best for cameras that see the floor."
                : "Someone counts when the middle of their body crosses. Use it when feet are not visible."}
            </p>
          </Section>

          <Section title="Counting hours">
            <Seg label="Counting hours" disabled={readOnly} value={schedule ? "hours" : "always"}
              onChange={(v) => change({ ...doc, schedule: v === "always" ? null : { days: [0, 1, 2, 3, 4, 5], start: "08:00", end: "20:00" } })}
              options={[{ value: "always", label: "All the time" }, { value: "hours", label: "Only at these times" }]} />
            {schedule ? (
              <div className="mt-3 space-y-3">
                <div className="grid grid-cols-7 gap-1" role="group" aria-label="Days">
                  {DAYS.map((day, i) => {
                    const on = schedule.days.includes(i);
                    return (
                      <button key={day} type="button" aria-pressed={on} disabled={readOnly}
                        onClick={() => change({ ...doc, schedule: { ...schedule, days: on ? schedule.days.filter((x) => x !== i) : [...schedule.days, i].sort((a, b) => a - b) } })}
                        className={cn("h-8 min-w-0 cursor-pointer rounded-[var(--radius-s)] border text-sm",
                          on ? "border-accent/60 bg-accent/15 text-text" : "border-rule text-muted hover:text-text")}>
                        {day}
                      </button>
                    );
                  })}
                </div>
                <div className="flex items-end gap-3">
                  <div className="space-y-1">
                    <Label htmlFor="sched-start">From</Label>
                    <Input id="sched-start" type="time" className="w-[120px]" value={schedule.start} disabled={readOnly}
                      onChange={(e) => e.target.value && change({ ...doc, schedule: { ...schedule, start: e.target.value } })} />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="sched-end">To</Label>
                    <Input id="sched-end" type="time" className="w-[120px]" value={schedule.end} disabled={readOnly}
                      onChange={(e) => e.target.value && change({ ...doc, schedule: { ...schedule, end: e.target.value } })} />
                  </div>
                </div>
                <p className="text-sm text-faint">
                  Site time ({d.site_timezone.replace("_", " ")}). Outside these times the camera is not analysed.
                  {schedule.end < schedule.start ? " Over midnight: the night belongs to the day it starts." : ""}
                </p>
              </div>
            ) : null}
          </Section>

          <section className="sticky bottom-0 space-y-3 rounded-b-[var(--radius-m)] border-t border-rule bg-panel px-4 py-4" data-testid="save-bar">
            <Notice tone={sync.tone} data-testid="sync-state">
              <span className="flex items-center gap-2">
                {sync.busy ? <span className="size-3 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent" aria-hidden /> : null}
                {sync.text}
              </span>
            </Notice>
            {message ? <Notice tone={message.tone}>{message.text}</Notice> : null}
            {dirty && issues.length ? <Notice tone="error">{issues[0]}</Notice> : null}
            {canEdit ? (
              <div className="flex gap-2">
                <Button variant="ghost" onClick={discard} disabled={!dirty || saving}>Discard</Button>
                <Button className="ml-auto" onClick={() => void save()} disabled={!dirty || issues.length > 0} busy={saving}>
                  Save and send to device
                </Button>
              </div>
            ) : null}
          </section>
        </Panel>
      </div>
    </>
  );
}

export default function CameraPage() {
  const { orgId, cameraId } = useParams<{ orgId: string; cameraId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <Editor org={org} cameraId={cameraId} />}</OrgGate>;
}
