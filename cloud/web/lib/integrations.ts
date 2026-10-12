// Types and texts for alerts, channels, API keys, webhooks and video jobs (Phase 4 B).

import type { CameraConfig } from "@/lib/api";

export type ChannelKind = "email" | "telegram" | "webhook";

export interface Channel {
  id: string;
  kind: ChannelKind;
  name: string;
  enabled: boolean;
  created_at: string;
  last_ok_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  secret_ok: boolean;
  recipients?: { user_id: string; name: string; email: string; email_verified: boolean }[];
  chat_id?: number | string;
  chat_title?: string;
  url?: string;
  events?: string[];
  signing_secret?: string | null; // only in the answer to "create" (shown once)
}

export type AlertKind = "occupancy" | "queue" | "camera_offline" | "traffic_drop";

export interface AlertRule {
  id: string;
  site_id: string;
  site_name: string;
  name: string;
  kind: AlertKind;
  camera_id: string | null;
  zone: string | null;
  threshold: number;
  duration_min: number;
  channel_ids: string[];
  enabled: boolean;
  state: "ok" | "firing";
  fired_at: string | null;
  last_value: number | null;
  last_eval_at: string | null;
  last_message: string | null;
  pending_since: string | null;
  created_at: string;
}

export interface AlertEvent {
  id: number;
  rule_id: string | null;
  site_id: string | null;
  rule_name: string;
  kind: AlertKind;
  status: "fired" | "resolved";
  value: number | null;
  message: string;
  created_at: string;
}

export interface ApiKeyRow {
  id: string;
  name: string;
  prefix: string;
  created_at: string;
  last_used_at: string | null;
  revoked: boolean;
  key?: string; // only right after creating
}

export interface Delivery {
  id: number;
  channel_id: string;
  event_id: string;
  event_type: string;
  status: "pending" | "ok" | "failed";
  attempts: number;
  response_code: number | null;
  error: string | null;
  created_at: string;
  delivered_at: string | null;
  next_try_at: string | null;
}

export const KIND_INFO: Record<AlertKind, { label: string; help: string; unit: string; thresholdLabel: string;
  durationLabel: string; defaultThreshold: number; defaultDuration: number }> = {
  occupancy: {
    label: "Too many people", help: "People inside an area zone reach a limit (safety, capacity, staff).",
    unit: "people", thresholdLabel: "People inside (at least)", durationLabel: "For at least (minutes)",
    defaultThreshold: 20, defaultDuration: 3,
  },
  queue: {
    label: "Long queue", help: "People waiting in a queue zone reach a limit: open another till.",
    unit: "people waiting", thresholdLabel: "People waiting (at least)", durationLabel: "For at least (minutes)",
    defaultThreshold: 5, defaultDuration: 2,
  },
  camera_offline: {
    label: "Camera not counting", help: "A camera or its computer stops counting (power, network, cable). Not checked outside counting hours.",
    unit: "cameras", thresholdLabel: "", durationLabel: "After (minutes)", defaultThreshold: 0, defaultDuration: 10,
  },
  traffic_drop: {
    label: "Fewer visitors than usual", help: "Visitors drop below a share of a normal week at the same time (a blocked door, a broken line, a quiet day). Needs 2 weeks of data.",
    unit: "% of normal", thresholdLabel: "Below (% of a normal week)", durationLabel: "Time window (minutes, 30 or more)",
    defaultThreshold: 50, defaultDuration: 60,
  },
};

export const EVENT_INFO: Record<string, string> = {
  crossing: "Every line crossing (sent in small batches, within seconds)",
  "counts.hourly": "Hourly counts per site (a few minutes after each hour)",
};

// --- video analysis ------------------------------------------------------------------------------

export type JobStatus = "uploading" | "probing" | "ready" | "queued" | "running" | "done" | "failed" | "cancelled";

export interface VideoConfig extends CameraConfig {
  blur: boolean;
  sample_fps: number | null;
  annotated: boolean;
}

export interface VideoResult {
  lines: { name: string; in: number; out: number; classes: Record<string, { in: number; out: number }> }[];
  zones: { name: string; kind: "area" | "queue"; occ_avg: number | null; occ_max: number; queue_avg: number | null;
    queue_max: number; visits: number; dwell_avg_s: number | null }[];
  totals: { in: number; out: number };
  timeline: { minute: number; in: number; out: number }[];
  dwell: { count: number; avg_s: number | null; buckets: { label: string; count: number }[] };
  frames_read: number;
  frames_processed: number;
  processing_s: number;
  speed_x: number | null;
  processed_fps: number | null;
  detector: string;
  video_codec: string;
  classes: string[];
}

export interface VideoJob {
  id: string;
  name: string;
  status: JobStatus;
  size_bytes: number;
  received_bytes: number;
  duration_s: number | null;
  fps: number | null;
  frame_count: number | null;
  width: number | null;
  height: number | null;
  config: VideoConfig | null;
  progress: number;
  error: string | null;
  result: VideoResult | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  files: Record<"thumbnail.jpg" | "annotated.mp4" | "report.pdf" | "counts.csv", boolean>;
  input_kept: boolean;
  video_expires_at: string | null;
  chunk_size: number;
}

export const JOB_STATUS: Record<JobStatus, string> = {
  uploading: "Uploading", probing: "Reading the video", ready: "Draw lines", queued: "Waiting",
  running: "Counting", done: "Done", failed: "Failed", cancelled: "Stopped",
};

export function jobHealth(j: VideoJob): "ok" | "warn" | "down" | "idle" {
  if (j.status === "done") return "ok";
  if (j.status === "failed") return "down";
  if (j.status === "cancelled") return "idle";
  return "warn";
}

export function fmtBytes(n: number): string {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(0)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

export function fmtClock(s: number | null | undefined): string {
  if (s === null || s === undefined) return "–";
  const t = Math.round(s);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const sec = t % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

/** Upload a file in pieces (resumes where the server stopped). Calls onProgress(0..1). */
export async function uploadFile(base: string, job: VideoJob, file: File, onProgress: (p: number) => void,
  signal?: AbortSignal): Promise<void> {
  let offset = job.received_bytes;
  while (offset < file.size) {
    if (signal?.aborted) throw new DOMException("Upload stopped", "AbortError");
    const piece = file.slice(offset, Math.min(file.size, offset + job.chunk_size));
    let resp: Response | null = null;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        resp = await fetch(`${base}/${job.id}/upload?offset=${offset}`, {
          method: "PUT", body: piece, credentials: "same-origin", signal,
          headers: { "X-CountVision": "1", "Content-Type": "application/octet-stream" },
        });
        if (resp.ok || resp.status === 409 || resp.status < 500) break;
      } catch (e) {
        if (signal?.aborted) throw e;
      }
      await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
    }
    if (!resp) throw new Error("The connection to the server was lost. Try again.");
    const data = await resp.json().catch(() => null);
    if (resp.status === 409 && data?.error?.code === "bad_offset") {
      // the server has a different amount (an earlier try did arrive): continue from there
      const fresh = await (await fetch(`${base}/${job.id}`, { credentials: "same-origin" })).json();
      offset = fresh.received_bytes;
      continue;
    }
    if (!resp.ok) throw new Error(data?.error?.message ?? `Upload failed (${resp.status}).`);
    offset = data.received_bytes;
    onProgress(offset / file.size);
  }
}
