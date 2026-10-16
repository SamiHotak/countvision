"use client";

// The live camera picture of CountVision Local: the real video from the counting service on this
// computer, with boxes, track ids, counting lines and zones drawn on it. "stream" = an MJPEG
// stream (smooth, one connection), "still" = a new picture every few seconds (tiles, many cameras).
// Nothing is recorded: the pictures pass through memory only. A hidden tab stops the stream.

import * as React from "react";

import { ApiError } from "@/lib/api";
import {
  liveUrl,
  MODE_TEXT,
  useHiddenPicture,
  useLiveViewSetting,
  usePageVisible,
  type LiveViewSetting,
} from "@/lib/live-picture";
import { cn } from "@/lib/utils";

type Status = "loading" | "live" | "error";

export interface LivePictureProps {
  orgId: string;
  cameraId: string;
  name: string;
  kind?: "stream" | "still";
  aspect?: number | null; // width / height when known (else 16:9 until the first picture)
  geometry?: boolean; // draw lines and zones (the camera editor draws its own)
  width?: number;
  fps?: number;
  stillEveryMs?: number;
  /** Why the camera does not count right now (offline, paused ...), shown over the picture. */
  stateText?: string | null;
  /** Small tiles: no hide button, shorter texts. */
  compact?: boolean;
  className?: string;
  onAspect?: (aspect: number) => void;
  /** Use this setting instead of loading it (the camera editor already has it). */
  setting?: LiveViewSetting | null;
}

const RETRY_MS = [1500, 3000, 6000, 10000];

async function readError(url: string): Promise<string> {
  try {
    const resp = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (resp.ok) return "";
    const body = await resp.json().catch(() => null);
    return body?.error?.message ?? `The picture could not be loaded (${resp.status}).`;
  } catch {
    return "CountVision is not reachable. Is it still running?";
  }
}

export function LivePicture(props: LivePictureProps) {
  const { orgId, cameraId, name, kind = "stream", geometry = true, compact = false } = props;
  const loaded = useLiveViewSetting(orgId, props.setting === undefined);
  const setting = props.setting === undefined ? loaded : props.setting;
  const visible = usePageVisible();
  const [hidden, setHidden] = useHiddenPicture(cameraId);
  const [aspect, setAspect] = React.useState<number | null>(props.aspect ?? null);
  const [status, setStatus] = React.useState<Status>("loading");
  const [message, setMessage] = React.useState("");
  const [attempt, setAttempt] = React.useState(0);
  const [still, setStill] = React.useState<string | null>(null);
  const imgRef = React.useRef<HTMLImageElement>(null);
  const width = props.width ?? 960;
  const watching = !!setting?.can_watch && !hidden && visible;
  const onAspect = props.onAspect;

  React.useEffect(() => {
    if (props.aspect) setAspect(props.aspect);
  }, [props.aspect]);

  const noteSize = React.useCallback((w: number, h: number) => {
    if (w > 0 && h > 0) {
      setAspect(w / h);
      onAspect?.(w / h);
    }
  }, [onAspect]);

  // --- MJPEG stream ----------------------------------------------------------------------------
  const streamSrc = kind === "stream" && watching
    ? `${liveUrl(orgId, cameraId, "mjpg", { geometry, width, fps: props.fps ?? 8 })}${attempt ? `&n=${attempt}` : ""}`
    : undefined;

  React.useEffect(() => {
    if (!streamSrc) return;
    setStatus("loading");
    // the first frame of a multipart image does not always fire "load": watch its size instead
    const timer = window.setInterval(() => {
      const img = imgRef.current;
      if (img && img.naturalWidth > 0) {
        setStatus("live");
        noteSize(img.naturalWidth, img.naturalHeight);
        window.clearInterval(timer);
      }
    }, 250);
    return () => window.clearInterval(timer);
  }, [streamSrc, noteSize]);

  const retryLater = React.useCallback((errorText: string) => {
    setStatus("error");
    setMessage(errorText || "The picture stopped. Trying again.");
    const delay = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)];
    window.setTimeout(() => setAttempt((n) => n + 1), delay);
  }, [attempt]);

  async function onStreamError() {
    retryLater(await readError(liveUrl(orgId, cameraId, "jpg", { geometry, width: 320 })));
  }

  // --- stills (tiles) --------------------------------------------------------------------------
  React.useEffect(() => {
    if (kind !== "still" || !watching) return;
    let alive = true;
    let current: string | null = null;
    let timer = 0;
    const every = props.stillEveryMs ?? 2000;
    async function next() {
      const url = liveUrl(orgId, cameraId, "jpg", { geometry, width });
      try {
        const resp = await fetch(url, { credentials: "same-origin", cache: "no-store" });
        if (!resp.ok) {
          const body = await resp.json().catch(() => null);
          throw new ApiError(resp.status, body?.error?.code ?? "", body?.error?.message ?? "No picture.");
        }
        const blob = await resp.blob();
        if (!alive) return;
        const objectUrl = URL.createObjectURL(blob);
        setStill(objectUrl);
        if (current) URL.revokeObjectURL(current);
        current = objectUrl;
        setStatus("live");
        timer = window.setTimeout(next, every);
      } catch (e) {
        if (!alive) return;
        setStatus("error");
        setMessage(e instanceof ApiError ? e.message : "CountVision is not reachable. Is it still running?");
        timer = window.setTimeout(next, Math.max(every, 4000));
      }
    }
    void next();
    return () => {
      alive = false;
      window.clearTimeout(timer);
      if (current) URL.revokeObjectURL(current);
    };
  }, [kind, watching, orgId, cameraId, geometry, width, props.stillEveryMs]);

  const ratio = aspect ?? 16 / 9;
  const box = cn("relative overflow-hidden rounded-[var(--radius-s)] bg-[#05090c]", props.className);

  if (setting === null) {
    return <div className={box} style={{ aspectRatio: ratio }} aria-hidden />;
  }
  if (!setting.can_watch) {
    return (
      <div className={cn(box, "grid place-items-center border border-rule bg-panel p-4 text-center")}
        style={{ aspectRatio: ratio }} data-testid="live-off">
        <p className="max-w-[40ch] text-sm text-muted">
          {setting.mode === "off"
            ? setting.can_change
              ? "The live picture is switched off. You can switch it on in Settings, Privacy."
              : "The live picture is switched off by the owner. Counting goes on."
            : "Only admins and the owner can watch the live picture."}
        </p>
      </div>
    );
  }
  if (hidden) {
    return (
      <div className={cn(box, "grid place-items-center border border-rule bg-panel")} style={{ aspectRatio: ratio }}>
        <button type="button" onClick={() => setHidden(false)} data-testid="show-live"
          className="cursor-pointer rounded-[var(--radius-s)] border border-rule bg-raise px-3 py-1.5 text-sm text-text hover:border-faint">
          Show live picture
        </button>
      </div>
    );
  }

  const src = kind === "stream" ? streamSrc : still ?? undefined;
  const alt = `Live picture of ${name}${setting.mode === "blur" ? ", people blurred" : ""}`;
  return (
    <figure className={cn(box, "group m-0")} style={{ aspectRatio: ratio }} data-testid="live-picture"
      data-status={status} data-mode={setting.mode}>
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element -- a live stream, never cached or optimised
        <img ref={imgRef} key={kind === "stream" ? src : "still"} src={src} alt={alt}
          className="absolute inset-0 size-full object-contain"
          onLoad={(e) => {
            if (kind === "stream") setStatus("live");
            noteSize(e.currentTarget.naturalWidth, e.currentTarget.naturalHeight);
          }}
          onError={kind === "stream" ? () => void onStreamError() : undefined} />
      ) : null}

      {status !== "live" ? (
        <div className="absolute inset-0 grid place-items-center p-4 text-center">
          <p className={cn("max-w-[44ch] text-sm", status === "error" ? "text-text" : "text-muted")}
            role={status === "error" ? "status" : undefined} data-testid="live-message">
            {status === "loading" ? "Connecting to the camera" : message}
          </p>
        </div>
      ) : null}

      <figcaption className="pointer-events-none absolute bottom-2 left-2 flex items-center gap-1.5 rounded-[4px] bg-[rgba(5,9,12,0.78)] px-2 py-1 text-xs text-text">
        <span aria-hidden className={cn("size-2 rounded-full", status === "live" ? "cv-live-dot bg-danger" : "bg-faint")} />
        {status === "live" ? (compact ? "Live" : `Live, ${MODE_TEXT[setting.mode]}`) : "Not live"}
      </figcaption>

      {props.stateText ? (
        <p className="pointer-events-none absolute right-2 bottom-2 max-w-[60%] truncate rounded-[4px] bg-[rgba(5,9,12,0.78)] px-2 py-1 text-xs text-warn">
          {props.stateText}
        </p>
      ) : null}

      {!compact ? (
        <button type="button" onClick={() => setHidden(true)}
          className="absolute top-2 right-2 cursor-pointer rounded-[4px] bg-[rgba(5,9,12,0.78)] px-2 py-1 text-xs text-muted opacity-0 transition-opacity hover:text-text focus-visible:opacity-100 group-hover:opacity-100">
          Hide picture
        </button>
      ) : null}
    </figure>
  );
}
