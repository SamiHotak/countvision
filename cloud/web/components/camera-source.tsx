"use client";

// Cameras that are added in the web app (CountVision Local's computer, or any device that manages
// its cameras here): where the picture comes from, the "add camera" form, a live status line and a
// test picture (one pixelated snapshot, kept 10 minutes in memory only).

import * as React from "react";

import { Seg } from "@/components/editor-ui";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/input";
import { Notice } from "@/components/ui/panel";
import { StatusDot } from "@/components/ui/status-dot";
import { api, errorText, fieldErrors, type Camera, type DeviceDetail, type SnapshotStatus } from "@/lib/api";
import { cameraHealth } from "@/lib/device";

export type SourceKind = "webcam" | "rtsp" | "http";

export const SOURCE_KINDS: { value: SourceKind; label: string }[] = [
  { value: "webcam", label: "Webcam" },
  { value: "rtsp", label: "IP camera" },
  { value: "http", label: "Phone / stream" },
];

const HELP: Record<SourceKind, { field: string; placeholder: string; hint: React.ReactNode; initial: string }> = {
  webcam: {
    field: "Webcam number",
    placeholder: "0",
    initial: "0",
    hint: "0 is the computer's own webcam. A USB webcam is usually 1.",
  },
  rtsp: {
    field: "Camera address (RTSP)",
    placeholder: "rtsp://user:password@192.168.1.20:554/stream1",
    initial: "",
    hint: "The camera's manual or app shows its RTSP address. The password is stored encrypted.",
  },
  http: {
    field: "Stream address (HTTP / MJPEG)",
    placeholder: "http://192.168.1.30:8080/video",
    initial: "",
    hint: (
      <>
        A phone works as a camera: install an &quot;IP Webcam&quot; app, start the server, and type the address
        it shows with <span className="text-muted">/video</span> at the end. Phone and computer must be in the
        same Wi-Fi.
      </>
    ),
  },
};

/** Kind + address (+ name) fields. Controlled, so the setup wizard and the dialog share it. */
export function SourceFields({ kind, setKind, address, setAddress, name, setName, errors, autoFocus }: {
  kind: SourceKind; setKind: (k: SourceKind) => void;
  address: string; setAddress: (a: string) => void;
  name?: string; setName?: (n: string) => void;
  errors: Record<string, string>; autoFocus?: boolean;
}) {
  const help = HELP[kind];
  return (
    <div className="space-y-4">
      {setName ? (
        <Field label="Camera name" name="camera-name" value={name} onChange={(e) => setName(e.target.value)}
          placeholder="Entrance" maxLength={120} required error={errors.name} autoFocus={autoFocus}
          hint="Where it looks, as you would say it: Entrance, Till, Back door." />
      ) : null}
      <div className="space-y-1.5">
        <span className="block text-sm font-medium text-muted" id="source-kind-label">Camera type</span>
        <Seg label="Camera type" value={kind} options={SOURCE_KINDS} onChange={(k) => {
          setKind(k);
          setAddress(HELP[k].initial);
        }} />
      </div>
      <Field label={help.field} name="camera-address" value={address} onChange={(e) => setAddress(e.target.value)}
        placeholder={help.placeholder} inputMode={kind === "webcam" ? "numeric" : "url"} autoComplete="off"
        spellCheck={false} error={errors.address} hint={help.hint} required />
    </div>
  );
}

/** One line of live state for a camera: Connecting / Counting 10 FPS / Reconnecting ... */
export function CameraState({ cam, online }: { cam: Camera | null; online: boolean }) {
  if (!cam) return null;
  const h = cameraHealth(cam, online);
  const waitingLong = cam.state === "connecting" || cam.state === "starting" || cam.state === null;
  return (
    <p className="flex flex-wrap items-center gap-2 text-sm" data-testid="camera-state" data-state={cam.state ?? ""}>
      <StatusDot health={h.health} />
      <span>{h.label}{cam.state === "running" && cam.fps != null ? `, ${cam.fps.toFixed(1)} FPS` : ""}</span>
      {waitingLong && cam.source_kind === "webcam" ? (
        <span className="text-faint">If this does not change: is another program using the webcam?</span>
      ) : null}
    </p>
  );
}

/** "Take a test picture": one snapshot from the camera, people pixelated. */
export function TestPicture({ orgId, cameraId, disabled }: { orgId: string; cameraId: string; disabled?: string | null }) {
  const [state, setState] = React.useState<{ status: "idle" | "pending" | "ready" | "error"; url?: string; text?: string }>(
    { status: "idle" });
  const base = `/api/orgs/${orgId}/cameras/${cameraId}`;

  async function take() {
    setState({ status: "pending" });
    try {
      const out = await api.post<SnapshotStatus>(`${base}/snapshot`);
      const started = Date.now();
      while (Date.now() - started < 15_000) {
        await new Promise((r) => setTimeout(r, 400));
        const st = await api.get<SnapshotStatus>(`${base}/snapshot/${out.request_id}`);
        if (st.status === "ready") {
          setState({ status: "ready", url: `${base}/snapshot/${out.request_id}/image` });
          return;
        }
        if (st.status === "error") {
          setState({ status: "error", text: st.message ?? "The camera sent no picture." });
          return;
        }
      }
      setState({ status: "error", text: "No picture after 15 seconds. Is the camera counting?" });
    } catch (e) {
      setState({ status: "error", text: errorText(e) });
    }
  }

  return (
    <div className="space-y-3">
      <Button type="button" variant="secondary" onClick={() => void take()} busy={state.status === "pending"}
        disabled={!!disabled} data-testid="test-picture">
        {state.status === "ready" ? "Take another picture" : "Take a test picture"}
      </Button>
      {disabled ? <p className="text-sm text-faint">{disabled}</p> : null}
      {state.status === "error" ? <Notice tone="error">{state.text}</Notice> : null}
      {state.status === "ready" && state.url ? (
        <figure>
          {/* eslint-disable-next-line @next/next/no-img-element -- a one-off snapshot, not a static asset */}
          <img src={state.url} alt="Test picture from the camera, people pixelated"
            className="w-full rounded-[var(--radius-s)] border border-rule" data-testid="test-picture-image" />
          <figcaption className="mt-1.5 text-sm text-faint">
            People are pixelated. The picture is not stored: it is gone after 10 minutes.
          </figcaption>
        </figure>
      ) : null}
    </div>
  );
}

/** Add a camera to a device that manages its cameras here. */
export function AddCameraDialog({ orgId, device, open, onClose, onAdded }: {
  orgId: string; device: DeviceDetail; open: boolean; onClose: () => void; onAdded: (cam: Camera) => void;
}) {
  const [kind, setKind] = React.useState<SourceKind>("webcam");
  const [address, setAddress] = React.useState("0");
  const [name, setName] = React.useState("");
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (!open) return;
    const used = new Set(device.cameras.filter((c) => c.source_kind === "webcam").map((c) => c.source_label));
    const free = [0, 1, 2, 3].find((n) => !used.has(`Webcam ${n}`)) ?? 0;
    setKind("webcam");
    setAddress(String(free));
    setName(device.cameras.length ? "" : "Entrance");
    setErrors({});
    setError(null);
  }, [open, device.cameras]);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setErrors({});
    try {
      const cam = await api.post<Camera>(`/api/orgs/${orgId}/devices/${device.id}/cameras`,
        { name: name.trim(), kind, address: address.trim() });
      onAdded(cam);
    } catch (err) {
      const f = fieldErrors(err);
      setErrors(f);
      if (!Object.keys(f).length || f.key) setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title="Add a camera" className="w-[min(520px,calc(100vw-32px))]">
      <form onSubmit={submit} className="space-y-4" noValidate>
        {error ? <Notice tone="error">{error}</Notice> : null}
        <SourceFields kind={kind} setKind={setKind} address={address} setAddress={setAddress} name={name}
          setName={setName} errors={errors} autoFocus />
        <p className="text-sm text-faint">
          {device.name} starts counting it within a few seconds. Then draw the counting line on its picture.
        </p>
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy} data-testid="add-camera-submit">Add camera</Button>
        </div>
      </form>
    </Dialog>
  );
}

/** Change the address (e.g. a new camera password). The device restarts the camera with it. */
export function ChangeSourceDialog({ orgId, cam, open, onClose, onSaved }: {
  orgId: string; cam: Camera; open: boolean; onClose: () => void; onSaved: () => void;
}) {
  const [kind, setKind] = React.useState<SourceKind>((cam.source_kind as SourceKind) ?? "rtsp");
  const [address, setAddress] = React.useState("");
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    if (open) {
      setKind((cam.source_kind as SourceKind) ?? "rtsp");
      setAddress(cam.source_kind === "webcam" ? (cam.source_label ?? "").replace("Webcam ", "") : "");
      setErrors({});
    }
  }, [open, cam]);
  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.put(`/api/orgs/${orgId}/cameras/${cam.id}/source`, { kind, address: address.trim() });
      onSaved();
    } catch (err) {
      setErrors(Object.keys(fieldErrors(err)).length ? fieldErrors(err) : { address: errorText(err) });
    } finally {
      setBusy(false);
    }
  }
  return (
    <Dialog open={open} onClose={onClose} title={`Address of ${cam.name}`} className="w-[min(520px,calc(100vw-32px))]">
      <form onSubmit={submit} className="space-y-4" noValidate>
        <p className="text-sm text-muted">Now: {cam.source_label}. Type the whole address again (with the password).</p>
        <SourceFields kind={kind} setKind={setKind} address={address} setAddress={setAddress} errors={errors} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" busy={busy}>Save address</Button>
        </div>
      </form>
    </Dialog>
  );
}
