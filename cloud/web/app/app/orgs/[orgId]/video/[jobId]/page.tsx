"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import * as React from "react";

import { CameraStage, type Selection, type Tool } from "@/components/camera-stage";
import { HBars } from "@/components/charts";
import { Section, Seg, ToolButton } from "@/components/editor-ui";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { StatusDot } from "@/components/ui/status-dot";
import { api, CLASSES, errorText, roleRank, type CameraConfig, type Org, type Pt } from "@/lib/api";
import { fmtDuration, fmtNum } from "@/lib/dashboard";
import { uniqueName } from "@/lib/geometry";
import {
  fmtBytes, fmtClock, JOB_STATUS, jobHealth, uploadFile, type VideoConfig, type VideoJob, type VideoResult,
} from "@/lib/integrations";
import { useLive } from "@/lib/live";
import { cn } from "@/lib/utils";

const START_DOC: VideoConfig = {
  lines: [], zones: [], classes: ["person"], anchor: "bottom_center", schedule: null, blur: true, sample_fps: 10,
  annotated: true,
};

function toDoc(cfg: VideoConfig | null): VideoConfig {
  return cfg ? { ...START_DOC, ...cfg, schedule: null } : { ...START_DOC };
}

// --- results -----------------------------------------------------------------------------------

function MinuteBars({ timeline }: { timeline: VideoResult["timeline"] }) {
  const step = Math.max(1, Math.ceil(timeline.length / 60));
  const bars: { label: string; value: number; in: number; out: number }[] = [];
  for (let i = 0; i < timeline.length; i += step) {
    const part = timeline.slice(i, i + step);
    const n_in = part.reduce((n, p) => n + p.in, 0);
    const n_out = part.reduce((n, p) => n + p.out, 0);
    bars.push({ label: fmtClock(part[0].minute * 60), value: n_in + n_out, in: n_in, out: n_out });
  }
  const top = Math.max(1, ...bars.map((b) => b.value));
  const [tip, setTip] = React.useState<number | null>(null);
  return (
    <div>
      <div className="flex h-36 items-end gap-[2px] border-b border-rule" role="img"
        aria-label={`Crossings per ${step === 1 ? "minute" : `${step} minutes`} of video`} onMouseLeave={() => setTip(null)}>
        {bars.map((b, i) => (
          <div key={i} className="flex h-full min-w-0 flex-1 items-end" onMouseEnter={() => setTip(i)}>
            <div className={cn("w-full rounded-t-[3px] bg-accent", tip !== null && tip !== i && "opacity-50")}
              style={{ height: `${(b.value / top) * 100}%`, minHeight: b.value ? 2 : 0, maxWidth: 24, margin: "0 auto" }} />
          </div>
        ))}
      </div>
      <div className="mt-1.5 flex justify-between text-xs text-faint">
        <span>{bars[0]?.label}</span>
        <span className="text-text">{tip !== null ? `${bars[tip].label}: ${bars[tip].in} in, ${bars[tip].out} out` : `Crossings (in + out) per ${step === 1 ? "minute" : `${step} minutes`}`}</span>
        <span>{bars[bars.length - 1]?.label}</span>
      </div>
    </div>
  );
}

function Results({ job, base }: { job: VideoJob; base: string }) {
  const r = job.result!;
  const file = (n: string, dl = true) => `${base}/files/${n}${dl ? "?download=true" : ""}`;
  const link = "inline-flex h-9 items-center rounded-[var(--radius-s)] border border-rule bg-raise px-3 text-sm no-underline hover:border-faint";
  return (
    <div className="space-y-6">
      <Panel className="grid grid-cols-2 gap-px overflow-hidden bg-rule sm:grid-cols-4 [&>*]:bg-panel">
        {[
          { label: "In (all lines)", value: fmtNum(r.totals.in) },
          { label: "Out (all lines)", value: fmtNum(r.totals.out) },
          { label: "Video length", value: fmtClock(job.duration_s) },
          { label: "Average visit in zones", value: fmtDuration(r.dwell.avg_s) },
        ].map((s) => (
          <div key={s.label} className="px-5 py-4">
            <p className="text-sm text-muted">{s.label}</p>
            <p className="mt-1 text-[30px] font-semibold leading-tight" style={{ fontVariantNumeric: "proportional-nums" }} data-testid="result-stat">{s.value}</p>
          </div>
        ))}
      </Panel>

      <div className="flex flex-wrap gap-2">
        {job.files["report.pdf"] ? <a className={link} href={file("report.pdf")} download>PDF report</a> : null}
        {job.files["counts.csv"] ? <a className={link} href={file("counts.csv")} download>CSV (per minute)</a> : null}
        {job.files["annotated.mp4"] ? <a className={link} href={file("annotated.mp4")} download>Annotated video</a> : null}
      </div>

      {job.files["annotated.mp4"] ? (
        <Panel className="overflow-hidden">
          <video controls preload="metadata" className="block w-full bg-black" src={file("annotated.mp4", false)}
            poster={job.files["thumbnail.jpg"] ? file("thumbnail.jpg", false) : undefined} data-testid="annotated-video" />
          <p className="px-4 py-2.5 text-sm text-faint">
            {job.config?.blur === false ? "People are not pixelated in this video." : "People and vehicles are pixelated."}
            {job.video_expires_at ? ` The video is deleted on ${new Date(job.video_expires_at).toLocaleDateString()}; the numbers stay.` : ""}
            {r.video_codec === "mp4v" ? " Your browser may not play this file (no H.264 encoder on the server): download it." : ""}
          </p>
        </Panel>
      ) : null}

      <Panel>
        <PanelHead title="Lines" />
        <table className="w-full text-sm">
          <thead className="text-left text-muted">
            <tr className="border-b border-rule"><th scope="col" className="px-5 py-2.5 font-medium">Line</th>
              <th scope="col" className="py-2.5 text-right font-medium">In</th><th scope="col" className="py-2.5 text-right font-medium">Out</th>
              <th scope="col" className="px-5 py-2.5 font-medium">By class</th></tr>
          </thead>
          <tbody className="divide-y divide-rule">
            {r.lines.map((l) => (
              <tr key={l.name} data-testid="result-line">
                <td className="px-5 py-2.5 font-medium">{l.name}</td>
                <td className="py-2.5 text-right">{fmtNum(l.in)}</td>
                <td className="py-2.5 text-right">{fmtNum(l.out)}</td>
                <td className="px-5 py-2.5 text-muted">
                  {Object.entries(l.classes).map(([c, v]) => `${CLASSES.find((x) => x.id === c)?.label ?? c} ${v.in}/${v.out}`).join(", ") || "–"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {r.timeline.length > 1 ? (
        <Panel>
          <PanelHead title="Over the video" />
          <div className="px-5 py-4"><MinuteBars timeline={r.timeline} /></div>
        </Panel>
      ) : null}

      {r.zones.length ? (
        <div className="grid gap-6 md:grid-cols-2">
          <Panel>
            <PanelHead title="Zones" />
            <ul className="divide-y divide-rule text-sm">
              {r.zones.map((z) => (
                <li key={z.name} className="px-5 py-3">
                  <p className="font-medium">{z.name} <span className="font-normal text-faint">{z.kind === "queue" ? "queue" : "area"}</span></p>
                  <p className="text-muted">
                    {z.kind === "queue" ? `${fmtNum(z.queue_avg)} waiting on average, most ${z.queue_max}` : `${fmtNum(z.occ_avg)} inside on average, most ${z.occ_max}`}
                    , {z.visits} visits, average {fmtDuration(z.dwell_avg_s)}
                  </p>
                </li>
              ))}
            </ul>
          </Panel>
          <Panel>
            <PanelHead title="Length of visits" />
            <div className="px-5 py-4">
              {r.dwell.count ? <HBars rows={r.dwell.buckets.map((b) => ({ label: b.label, value: b.count, text: fmtNum(b.count) }))} />
                : <p className="text-sm text-muted">No visits in zones.</p>}
            </div>
          </Panel>
        </div>
      ) : null}

      <p className="text-sm text-faint">
        Counted with {r.detector === "onnx" ? "YOLOX-Tiny" : r.detector}: {fmtNum(r.frames_processed)} of {fmtNum(r.frames_read)} frames in{" "}
        {fmtDuration(r.processing_s)}{r.speed_x ? ` (${r.speed_x}x real time)` : ""}. Check a part of the annotated video by eye:
        counts depend on the camera angle and where the lines are.
      </p>
    </div>
  );
}

// --- editor ------------------------------------------------------------------------------------

function Editor({ job, base, onStarted, readOnly }: { job: VideoJob; base: string; onStarted: (j: VideoJob) => void; readOnly: boolean }) {
  const [doc, setDoc] = React.useState<VideoConfig>(() => toDoc(job.config));
  const [past, setPast] = React.useState<VideoConfig[]>([]);
  const [future, setFuture] = React.useState<VideoConfig[]>([]);
  const [tool, setTool] = React.useState<Tool>(job.config ? "select" : "line");
  const [sel, setSel] = React.useState<Selection>(null);
  const [snap, setSnap] = React.useState(true);
  const [pending, setPending] = React.useState<Pt[] | null>(null);
  const [hint, setHint] = React.useState(job.config ? "" : "Drag a line across the path people (or cars) take. The arrow shows the IN side.");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const change = (next: CameraConfig, record = true) => {
    if (record) { setPast((p) => [...p.slice(-99), doc]); setFuture([]); }
    setDoc({ ...doc, ...next });
  };
  const beginChange = () => { setPast((p) => [...p.slice(-99), doc]); setFuture([]); };
  const undo = () => { if (!past.length) return; setFuture((f) => [doc, ...f]); setDoc(past[past.length - 1]); setPast((p) => p.slice(0, -1)); setSel(null); };
  const redo = () => { if (!future.length) return; setPast((p) => [...p, doc]); setDoc(future[0]); setFuture((f) => f.slice(1)); setSel(null); };
  const pick = (t: Tool) => {
    setTool(t);
    setPending(null);
    setHint(t === "line" ? "Drag across the door or path. The arrow shows the IN side."
      : t === "zone" ? "Click the corners of the area. Click the first corner again to finish." : "Click a line or zone to select it.");
  };
  const selected = sel ? (sel.type === "line" ? doc.lines[sel.i] : doc.zones[sel.i]) : undefined;

  React.useEffect(() => {
    if (readOnly) return;
    function onKey(e: KeyboardEvent) {
      if ((e.target as HTMLElement).closest("input, textarea, select")) return;
      const k = e.key.toLowerCase();
      if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return; }
      if (e.ctrlKey || e.metaKey) return;
      if (k === "v") pick("select");
      else if (k === "l") pick("line");
      else if (k === "z") pick("zone");
      else if (k === "s") setSnap((v) => !v);
      else if ((k === "delete" || k === "backspace") && sel) { e.preventDefault(); remove(); }
      else if (k === "escape") { if (pending) setPending(null); else setSel(null); }
      else if (k === "enter" && pending && pending.length >= 3) {
        change({ ...doc, zones: [...doc.zones, { name: uniqueName(doc, "zone"), kind: "area", polygon: pending }] });
        setPending(null);
        setTool("select");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function remove() {
    if (!sel) return;
    if (sel.type === "line") change({ ...doc, lines: doc.lines.filter((_, i) => i !== sel.i) });
    else change({ ...doc, zones: doc.zones.filter((_, i) => i !== sel.i) });
    setSel(null);
  }

  function rename(name: string) {
    if (!sel || !selected || !name || name === selected.name) return;
    if (sel.type === "line") change({ ...doc, lines: doc.lines.map((l, i) => (i === sel.i ? { ...l, name } : l)) });
    else change({ ...doc, zones: doc.zones.map((z, i) => (i === sel.i ? { ...z, name } : z)) });
  }

  async function saveAndStart() {
    setBusy(true);
    setError(null);
    try {
      const { schedule: _schedule, ...cfg } = doc;
      void _schedule;
      await api.put(`${base}/config`, cfg);
      onStarted(await api.post<VideoJob>(`${base}/start`));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const empty = doc.lines.length + doc.zones.length === 0;
  const minutes = job.duration_s ? job.duration_s / 60 : 0;
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_320px] lg:items-start">
      <div className="min-w-0">
        {!readOnly ? (
          <div className="mb-2 flex flex-wrap items-center gap-1 rounded-[var(--radius-s)] border border-rule bg-panel p-1" role="toolbar" aria-label="Drawing tools">
            <ToolButton active={tool === "select"} onClick={() => pick("select")} keyHint="V" label="Select">Select</ToolButton>
            <ToolButton active={tool === "line"} onClick={() => pick("line")} keyHint="L" label="Line">Line</ToolButton>
            <ToolButton active={tool === "zone"} onClick={() => pick("zone")} keyHint="Z" label="Zone">Zone</ToolButton>
            <span className="mx-1 h-6 w-px bg-rule" aria-hidden />
            <ToolButton onClick={undo} disabled={!past.length} label="Undo">Undo</ToolButton>
            <ToolButton onClick={redo} disabled={!future.length} label="Redo">Redo</ToolButton>
            <ToolButton toggle active={snap} onClick={() => setSnap((v) => !v)} keyHint="S" label="Snap">Snap</ToolButton>
          </div>
        ) : null}
        <CameraStage doc={doc} onChange={change} onBeginChange={beginChange} tool={tool} setTool={pick} selection={sel}
          setSelection={setSel} snap={snap} readOnly={readOnly} aspect={(job.width ?? 16) / (job.height ?? 9)}
          image={job.files["thumbnail.jpg"] ? `${base}/files/thumbnail.jpg` : null} today={{}} pulses={{}} onHint={setHint}
          pending={pending} setPending={setPending} />
        <p className="mt-2 min-h-6 text-sm text-muted" aria-live="polite" data-testid="hint">{hint || "A frame from 1 second into your video."}</p>
      </div>

      <Panel>
        <section className="px-4 py-4">
          <h2 className="mb-2 font-semibold">Lines and zones</h2>
          {empty ? <p className="text-sm text-muted">Draw at least one line (to count in/out) or zone (people inside, visits).</p> : (
            <ul className="-mx-2 space-y-0.5">
              {doc.lines.map((l, i) => (
                <li key={`l${i}`}><button type="button" onClick={() => { setSel({ type: "line", i }); pick("select"); }}
                  className={cn("flex w-full cursor-pointer gap-3 rounded-[var(--radius-s)] px-2 py-1.5 text-left", sel?.type === "line" && sel.i === i ? "bg-raise" : "hover:bg-raise/60")}>
                  <span className="truncate">{l.name}</span><span className="ml-auto text-sm text-faint">line</span></button></li>
              ))}
              {doc.zones.map((z, i) => (
                <li key={`z${i}`}><button type="button" onClick={() => { setSel({ type: "zone", i }); pick("select"); }}
                  className={cn("flex w-full cursor-pointer gap-3 rounded-[var(--radius-s)] px-2 py-1.5 text-left", sel?.type === "zone" && sel.i === i ? "bg-raise" : "hover:bg-raise/60")}>
                  <span className="truncate">{z.name}</span><span className="ml-auto text-sm text-faint">{z.kind === "queue" ? "queue zone" : "zone"}</span></button></li>
              ))}
            </ul>
          )}
        </section>
        {selected && !readOnly ? (
          <section className="border-t border-rule bg-raise/40 px-4 py-4" aria-label="Selected shape">
            <Label htmlFor="shape-name">{sel?.type === "line" ? "Line name" : "Zone name"}</Label>
            <Input id="shape-name" key={`${sel?.type}${sel?.i}${selected.name}`} defaultValue={selected.name} maxLength={64} className="mt-1.5"
              onBlur={(e) => rename(e.currentTarget.value.trim())} onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} />
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {sel?.type === "line" ? (
                <Button size="sm" variant="secondary" onClick={() => change({ ...doc, lines: doc.lines.map((l, i) => (i === sel.i ? { ...l, in_direction: l.in_direction === "to_right" ? "to_left" : "to_right" } : l)) })}>
                  Flip IN direction
                </Button>
              ) : (
                <Seg label="Zone kind" value={doc.zones[sel!.i].kind} onChange={(kind) => change({ ...doc, zones: doc.zones.map((z, i) => (i === sel!.i ? { ...z, kind } : z)) })}
                  options={[{ value: "area", label: "Area" }, { value: "queue", label: "Queue" }]} />
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
                <label key={c.id} className="flex cursor-pointer items-center gap-2.5">
                  <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={on} disabled={readOnly}
                    onChange={() => change({ ...doc, classes: on ? doc.classes.filter((x) => x !== c.id) : [...doc.classes, c.id] })} />
                  <span className="size-2.5 rounded-full" style={{ background: c.color }} aria-hidden />{c.label}
                </label>
              );
            })}
          </div>
        </Section>
        <Section title="Count point">
          <Seg label="Count point" disabled={readOnly} value={doc.anchor} onChange={(anchor) => change({ ...doc, anchor })}
            options={[{ value: "bottom_center", label: "Feet" }, { value: "center", label: "Middle" }]} />
        </Section>
        <Section title="Analysis">
          <Label htmlFor="fps">Frames per second</Label>
          <select id="fps" className="mt-1.5 h-9 w-full rounded-[var(--radius-s)] border border-rule bg-ink px-2" disabled={readOnly}
            value={doc.sample_fps ?? 0} onChange={(e) => setDoc({ ...doc, sample_fps: Number(e.target.value) || null })}>
            <option value={10}>10 (good for people, faster)</option>
            <option value={15}>15</option>
            <option value={0}>Every frame (fast cars, slowest)</option>
          </select>
          <label className="mt-3 flex cursor-pointer items-center gap-2.5">
            <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={doc.annotated} disabled={readOnly}
              onChange={() => setDoc({ ...doc, annotated: !doc.annotated })} />
            Make an annotated video
          </label>
          <label className="mt-1.5 flex cursor-pointer items-center gap-2.5">
            <input type="checkbox" className="size-4 accent-[var(--color-accent)]" checked={doc.blur} disabled={readOnly || !doc.annotated}
              onChange={() => setDoc({ ...doc, blur: !doc.blur })} />
            Pixelate people and vehicles in it
          </label>
        </Section>
        {!readOnly ? (
          <div className="sticky bottom-0 border-t border-rule bg-panel px-4 py-3">
            {error ? <p className="mb-2 text-sm text-danger" role="alert">{error}</p> : null}
            <Button className="w-full" onClick={() => void saveAndStart()} busy={busy} disabled={empty || doc.classes.length === 0}>
              Start counting
            </Button>
            <p className="mt-2 text-xs text-faint">
              {minutes ? `${fmtClock(job.duration_s)} of video. ` : ""}On a normal server this takes about as long as the video, or less.
              The uploaded video is deleted afterwards.
            </p>
          </div>
        ) : null}
      </Panel>
    </div>
  );
}

// --- page ------------------------------------------------------------------------------------

function JobView({ org, jobId }: { org: Org; jobId: string }) {
  const router = useRouter();
  const base = `/api/orgs/${org.id}/video-jobs/${jobId}`;
  const [job, setJob] = React.useState<VideoJob | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [resume, setResume] = React.useState<number | null>(null);
  const [editing, setEditing] = React.useState(false);
  const canEdit = roleRank(org.my_role) >= roleRank("member");

  const load = React.useCallback(async () => {
    try {
      setJob(await api.get<VideoJob>(base));
    } catch (e) {
      setError(errorText(e));
    }
  }, [base]);
  React.useEffect(() => { void load(); }, [load]);
  useLive(org.id, React.useCallback((m) => {
    if (m.type === "video_job" && m.job_id === jobId) {
      setJob((j) => (j ? { ...j, status: m.status as VideoJob["status"], progress: m.progress } : j));
      if (!["running", "queued"].includes(m.status)) void load();
    }
  }, [jobId, load]));
  const status = job?.status;
  React.useEffect(() => {  // fallback polling while something is happening
    if (!status || !["probing", "queued", "running"].includes(status)) return;
    const id = setInterval(() => void load(), 3000);
    return () => clearInterval(id);
  }, [status, load]);

  async function act(path: string, method: "post" | "del" = "post") {
    try {
      if (method === "del") {
        if (!window.confirm("Delete this video job and all its files?")) return;
        await api.del(base);
        router.push(`/app/orgs/${org.id}/video`);
        return;
      }
      setJob(await api.post<VideoJob>(`${base}/${path}`));
    } catch (e) {
      setError(errorText(e));
    }
  }

  async function continueUpload(f: File) {
    if (!job) return;
    if (f.size !== job.size_bytes) { setError("This is not the same file (the size is different)."); return; }
    setError(null);
    setResume(job.received_bytes / job.size_bytes);
    try {
      await uploadFile(`/api/orgs/${org.id}/video-jobs`, job, f, setResume);
      setJob(await api.post<VideoJob>(`${base}/complete`));
    } catch (e) {
      setError(e instanceof Error ? e.message : errorText(e));
    } finally {
      setResume(null);
    }
  }

  if (!job) return error ? <Notice tone="error">{error}</Notice> : <Loading />;
  const showEditor = job.status === "ready" || (editing && job.input_kept) ||
    ((job.status === "failed" || job.status === "cancelled") && job.input_kept && !!job.files["thumbnail.jpg"]);
  return (
    <>
      <nav className="mb-2 text-sm text-muted" aria-label="Breadcrumb">
        <Link href={`/app/orgs/${org.id}/video`} className="no-underline hover:text-text">Video analysis</Link>
      </nav>
      <div className="mb-5 flex flex-wrap items-center gap-x-4 gap-y-2">
        <h1 className="mr-auto min-w-0 truncate text-2xl font-semibold leading-tight">{job.name}</h1>
        <span className="inline-flex items-center gap-2 text-sm text-muted" data-testid="job-status">
          <StatusDot health={jobHealth(job)} />{JOB_STATUS[job.status]}
        </span>
        {canEdit && job.status !== "running" ? <Button size="sm" variant="ghost" onClick={() => void act("", "del")}>Delete</Button> : null}
      </div>
      <p className="-mt-3 mb-5 text-sm text-faint">
        {job.width ? `${job.width}x${job.height}, ${job.fps} FPS, ${fmtClock(job.duration_s)}, ` : ""}{fmtBytes(job.size_bytes)}
      </p>
      {error ? <Notice tone="error" className="mb-5">{error}</Notice> : null}
      {job.error && job.status !== "done" ? <Notice tone={job.status === "cancelled" ? "info" : "error"} className="mb-5">{job.error}</Notice> : null}

      {job.status === "uploading" ? (
        <Panel className="p-5">
          <p className="font-medium">The upload stopped at {Math.round((job.received_bytes / job.size_bytes) * 100)} %.</p>
          {resume !== null ? (
            <p className="mt-2 text-sm text-muted" aria-live="polite">Uploading: {Math.round(resume * 100)} %</p>
          ) : canEdit ? (
            <label className="mt-3 inline-flex h-10 cursor-pointer items-center rounded-[var(--radius-s)] bg-accent px-4 font-medium text-accent-ink">
              Choose the same file to continue
              <input type="file" className="sr-only" onChange={(e) => { const f = e.target.files?.[0]; if (f) void continueUpload(f); }} />
            </label>
          ) : null}
        </Panel>
      ) : job.status === "probing" ? (
        <Panel className="p-5"><p className="text-muted">Reading the video… a few seconds.</p></Panel>
      ) : job.status === "queued" || job.status === "running" ? (
        <Panel className="p-5">
          <p className="font-medium">{job.status === "queued" ? "Waiting for the video worker…" : `Counting: ${Math.round(job.progress * 100)} %`}</p>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-raise" role="progressbar" aria-valuemin={0} aria-valuemax={100}
            aria-valuenow={Math.round(job.progress * 100)} aria-label="Analysis">
            <div className="h-full rounded-full bg-accent transition-[width] duration-700" style={{ width: `${Math.max(2, job.progress * 100)}%` }} />
          </div>
          <p className="mt-2 text-sm text-muted">You can close this page; the analysis continues on the server.</p>
          {canEdit ? <Button size="sm" variant="ghost" className="mt-2" onClick={() => void act("cancel")}>Stop</Button> : null}
        </Panel>
      ) : null}

      {job.status === "done" && job.result && !editing ? (
        <>
          <Results job={job} base={base} />
          {canEdit && job.input_kept ? <Button variant="secondary" className="mt-6" onClick={() => setEditing(true)}>Change lines and count again</Button> : null}
        </>
      ) : null}

      {showEditor ? (
        <Editor job={job} base={base} readOnly={!canEdit} onStarted={(j) => { setEditing(false); setJob(j); }} />
      ) : null}
    </>
  );
}

export default function VideoJobPage() {
  const { orgId, jobId } = useParams<{ orgId: string; jobId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <JobView org={org} jobId={jobId} />}</OrgGate>;
}
