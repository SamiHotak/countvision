"use client";

import Link from "next/link";
import * as React from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, Label, Select } from "@/components/ui/input";
import { Notice } from "@/components/ui/panel";
import { api, errorText, fieldErrors, type Org, type PairingCode, type Site } from "@/lib/api";
import { cn } from "@/lib/utils";

type Platform = "windows" | "docker";

function pairCommand(platform: Platform, origin: string, code: string): string[] {
  if (platform === "docker") {
    // Inside a container "localhost" is the container itself: use the host's name instead.
    const url = origin.replace(/\/\/(localhost|127\.0\.0\.1)(?=[:/]|$)/, "//host.docker.internal");
    return [
      `docker compose run --rm countvision pair --url ${url} --code ${code}`,
      "docker compose restart countvision",
    ];
  }
  return [
    `.venv\\Scripts\\countvision-edge pair --config edge\\configs\\local.yaml --url ${origin} --code ${code}`,
    ".\\start.bat --count",
  ];
}

function CopyLine({ text }: { text: string }) {
  const [copied, setCopied] = React.useState(false);
  return (
    <div className="flex items-start gap-2 rounded-[var(--radius-s)] border border-rule bg-ink p-2.5">
      <code className="min-w-0 flex-1 break-all text-sm leading-relaxed text-text">{text}</code>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className="shrink-0"
        onClick={() => {
          void navigator.clipboard?.writeText(text).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          });
        }}
      >
        {copied ? "Copied" : "Copy"}
      </Button>
    </div>
  );
}

/** "Add device": choose site and name, get a one-time code, wait until the device uses it. */
export function AddDeviceDialog({ org, sites, open, onClose, onPaired }: {
  org: Org; sites: Site[]; open: boolean; onClose: () => void; onPaired: () => void;
}) {
  const [code, setCode] = React.useState<PairingCode | null>(null);
  const [status, setStatus] = React.useState<PairingCode | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [busy, setBusy] = React.useState(false);
  const [platform, setPlatform] = React.useState<Platform>("windows");
  const [left, setLeft] = React.useState(0);
  const origin = typeof window === "undefined" ? "" : window.location.origin;

  React.useEffect(() => {
    if (open) {
      setCode(null);
      setStatus(null);
      setError(null);
      setFields({});
    }
  }, [open]);

  // While the code is open: poll every 2 s and count down, stop when the device used it.
  const onPairedRef = React.useRef(onPaired);
  onPairedRef.current = onPaired;
  React.useEffect(() => {
    if (!code || !open) return;
    let done = false;
    const tick = async () => {
      setLeft(Math.max(0, Math.round((new Date(code.expires_at).getTime() - Date.now()) / 1000)));
      try {
        const s = await api.get<PairingCode>(`/api/orgs/${org.id}/pairing-codes/${code.id}`);
        if (done) return;
        setStatus(s);
        if (s.status !== "pending") {
          done = true;
          clearInterval(timer);
          if (s.status === "used") onPairedRef.current();
        }
      } catch {
        /* network hiccup: keep trying */
      }
    };
    const timer = setInterval(() => void tick(), 2000);
    void tick();
    return () => {
      done = true;
      clearInterval(timer);
    };
  }, [code, open, org.id]);

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    setFields({});
    try {
      setCode(await api.post<PairingCode>(`/api/orgs/${org.id}/pairing-codes`, {
        site_id: form.get("site_id"), device_name: form.get("device_name"),
      }));
    } catch (err) {
      setError(errorText(err));
      setFields(fieldErrors(err));
    } finally {
      setBusy(false);
    }
  }

  const used = status?.status === "used";
  const expired = status?.status === "expired";

  return (
    <Dialog open={open} onClose={onClose} title={used ? "Device connected" : "Add a device"} className="w-[min(560px,calc(100vw-32px))]">
      {!code ? (
        <form onSubmit={submit} className="space-y-4" noValidate>
          <p className="text-muted">
            The device is the computer at the site that reads the cameras and counts. It sends only numbers here.
          </p>
          {error && !fields.device_name ? <Notice tone="error">{error}</Notice> : null}
          <div className="space-y-1.5">
            <Label htmlFor="dev-site">Site</Label>
            <Select id="dev-site" name="site_id" defaultValue={sites[0]?.id}>
              {sites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
          </div>
          <Field label="Device name" name="device_name" required maxLength={120} autoFocus
            placeholder="Mini PC at the till" error={fields.device_name} />
          <div className="flex justify-end gap-2 pt-1">
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" busy={busy}>Create pairing code</Button>
          </div>
        </form>
      ) : used ? (
        <div className="space-y-4">
          <Notice tone="success">{code.device_name} is connected. Its cameras appear after the first upload.</Notice>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={onClose}>Close</Button>
            <Link href={`/app/orgs/${org.id}/devices/${status?.device_id}`}
              className="inline-flex h-10 items-center rounded-[var(--radius-s)] bg-accent px-4 font-medium text-accent-ink no-underline hover:bg-accent/85">
              Open device
            </Link>
          </div>
        </div>
      ) : (
        <div className="space-y-5">
          <div>
            <p className="text-sm text-muted">Pairing code for {code.device_name}</p>
            <p className="mt-1 text-[44px] font-semibold leading-none tracking-[0.12em]" data-testid="pairing-code">{code.code}</p>
            <p className={cn("mt-2 text-sm", expired ? "text-warn" : "text-faint")} aria-live="polite">
              {expired ? "This code has expired. Close and create a new one."
                : `Works once, for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} more minutes.`}
            </p>
          </div>
          <div>
            <div className="mb-2 flex gap-1" role="tablist" aria-label="Where the counting runs">
              {(["windows", "docker"] as Platform[]).map((p) => (
                <button key={p} role="tab" aria-selected={platform === p} onClick={() => setPlatform(p)}
                  className={cn("h-8 cursor-pointer rounded-[var(--radius-s)] px-3 text-sm",
                    platform === p ? "bg-raise text-text" : "text-muted hover:text-text")}>
                  {p === "windows" ? "Windows (start.bat)" : "Docker"}
                </button>
              ))}
            </div>
            <p className="mb-2 text-sm text-muted">
              {platform === "windows"
                ? "On the device, in the countvision folder (PowerShell), run:"
                : "On the device, in the docker folder, run:"}
            </p>
            <div className="space-y-2">
              {pairCommand(platform, origin, code.code ?? "").map((line) => <CopyLine key={line} text={line} />)}
            </div>
          </div>
          <p className="flex items-center gap-2 text-sm text-muted" role="status">
            <span className="size-3 animate-spin rounded-full border-2 border-accent border-r-transparent" aria-hidden />
            Waiting for the device to use the code
          </p>
        </div>
      )}
    </Dialog>
  );
}
