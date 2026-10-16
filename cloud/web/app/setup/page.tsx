"use client";

// CountVision Local, first start: account + business + site, the first camera, then the counting line.
// Shown only while no account exists (GET /api/edition says needs_setup). Works without internet.

import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";

import { CameraState, SourceFields, type SourceKind } from "@/components/camera-source";
import { LivePicture } from "@/components/live-picture";
import { Wordmark } from "@/components/brand";
import { useEdition } from "@/components/edition";
import { Loading, useMe } from "@/components/me";
import { Button } from "@/components/ui/button";
import { Field, Label, Select } from "@/components/ui/input";
import { Notice } from "@/components/ui/panel";
import { api, COMMON_TIMEZONES, errorText, fieldErrors, type Camera, type DeviceDetail, type Me } from "@/lib/api";
import { cn } from "@/lib/utils";

const STEPS = [
  { title: "You and your business", text: "The owner account. It works only on this computer and your network." },
  { title: "Your first camera", text: "The webcam, an IP camera or a phone." },
  { title: "The counting line", text: "Draw where people walk in and out." },
];

interface Progress { step: number; orgId?: string; deviceId?: string; cameraId?: string }
const KEY = "cv:setup";

function load(): Progress {
  try {
    return JSON.parse(sessionStorage.getItem(KEY) ?? "") as Progress;
  } catch {
    return { step: 0 };
  }
}

function save(p: Progress) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    /* storage blocked: the wizard still works until the page is reloaded */
  }
}

/** The steps as stops on a counting line: done = filled, now = ring, later = hollow. */
function Rail({ step }: { step: number }) {
  return (
    <ol className="relative space-y-9 pl-9" aria-label="Setup steps">
      <span className="absolute bottom-2 left-[9px] top-2 w-[3px] rounded-full bg-rule" aria-hidden />
      <span className="absolute left-[9px] top-2 w-[3px] rounded-full bg-accent transition-[height] duration-500 motion-reduce:transition-none"
        style={{ height: `${Math.min(step, STEPS.length - 1) * 50}%` }} aria-hidden />
      {STEPS.map((s, i) => (
        <li key={s.title} className="relative" aria-current={i === step ? "step" : undefined}>
          <span aria-hidden className={cn("absolute -left-9 top-0.5 grid size-[21px] place-items-center rounded-full border-[3px]",
            i < step ? "border-accent bg-accent" : i === step ? "border-accent bg-ink" : "border-rule bg-ink")}>
            {i < step ? (
              <svg viewBox="0 0 12 12" className="size-3 text-accent-ink"><path d="M2.5 6.2 5 8.5l4.5-5" fill="none" stroke="currentColor" strokeWidth="2" /></svg>
            ) : null}
          </span>
          <p className={cn("font-semibold", i > step && "text-muted")}>{s.title}</p>
          <p className="mt-0.5 text-sm text-faint">{s.text}</p>
        </li>
      ))}
    </ol>
  );
}

function AccountStep({ onDone }: { onDone: (p: Progress) => void }) {
  const { setMe } = useMe();
  const { refresh } = useEdition();
  const [fields, setFields] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  // Europe/Berlin is chosen, not the browser's zone: a shop PC's clock is often set to some other
  // zone. The browser's zone is offered in the list.
  const browserTz = React.useMemo(() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch {
      return "Europe/Berlin";
    }
  }, []);
  const zones = COMMON_TIMEZONES.includes(browserTz) ? COMMON_TIMEZONES : [...COMMON_TIMEZONES, browserTz];

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    if (f.get("password") !== f.get("password2")) {
      setFields({ password2: "The two passwords are different." });
      return;
    }
    setBusy(true);
    setError(null);
    setFields({});
    try {
      const out = await api.post<{ me: Me; org_id: string; site_id: string; device_id: string }>("/api/local/setup", {
        name: f.get("name"), email: f.get("email"), password: f.get("password"), business: f.get("business"),
        site_name: f.get("site_name"), timezone: f.get("timezone"),
      });
      setMe(out.me);
      void refresh();
      onDone({ step: 1, orgId: out.org_id, deviceId: out.device_id });
    } catch (err) {
      const fe = fieldErrors(err);
      setFields(fe);
      if (!Object.keys(fe).length) setError(errorText(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-7" noValidate>
      {error ? <Notice tone="error">{error}</Notice> : null}
      <fieldset className="space-y-4">
        <legend className="mb-3 text-lg font-semibold">The owner</legend>
        <Field label="Your name" name="name" autoComplete="name" required autoFocus error={fields.name} />
        <Field label="Email" name="email" type="email" autoComplete="email" required error={fields.email}
          hint="Your login name. No email is sent: CountVision Local works without internet." />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Password" name="password" type="password" autoComplete="new-password" required
            minLength={10} error={fields.password} hint="At least 10 characters." />
          <Field label="Password again" name="password2" type="password" autoComplete="new-password" required
            error={fields.password2} />
        </div>
      </fieldset>
      <fieldset className="space-y-4">
        <legend className="mb-3 text-lg font-semibold">The business</legend>
        <Field label="Business name" name="business" required placeholder="Café Sonne" error={fields.business} />
        <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <Field label="Location" name="site_name" required placeholder="Main street" error={fields.site_name}
            hint="The shop or branch these cameras are in." />
          <div className="space-y-1.5">
            <Label htmlFor="timezone">Time zone</Label>
            <Select id="timezone" name="timezone" defaultValue="Europe/Berlin">
              {zones.map((z) => <option key={z} value={z}>{z.replace("_", " ")}</option>)}
            </Select>
            {fields.timezone ? <p className="text-sm text-danger">{fields.timezone}</p>
              : <p className="text-sm text-faint">Where the shop is. It decides when &quot;today&quot; starts.</p>}
          </div>
        </div>
      </fieldset>
      <Button type="submit" size="lg" busy={busy} data-testid="setup-submit">Create account and continue</Button>
    </form>
  );
}

function CameraStep({ progress, onDone }: { progress: Progress; onDone: (p: Progress) => void }) {
  const [kind, setKind] = React.useState<SourceKind>("webcam");
  const [address, setAddress] = React.useState("0");
  const [name, setName] = React.useState("Entrance");
  const [errors, setErrors] = React.useState<Record<string, string>>({});
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [cameraId, setCameraId] = React.useState<string | null>(progress.cameraId ?? null);
  const [device, setDevice] = React.useState<DeviceDetail | null>(null);
  const base = `/api/orgs/${progress.orgId}/devices/${progress.deviceId}`;

  React.useEffect(() => {
    if (!cameraId) return;
    let stop = false;
    const tick = async () => {
      try {
        const d = await api.get<DeviceDetail>(base);
        if (!stop) setDevice(d);
      } catch {
        /* next try */
      }
    };
    void tick();
    const timer = setInterval(tick, 1500);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [cameraId, base]);

  async function add(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy(true);
    setErrors({});
    setError(null);
    try {
      const cam = await api.post<Camera>(`${base}/cameras`, { name: name.trim(), kind, address: address.trim() });
      setCameraId(cam.id);
      save({ ...progress, cameraId: cam.id });
    } catch (err) {
      const fe = fieldErrors(err);
      setErrors(fe);
      if (!Object.keys(fe).length) setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const cam = device?.cameras.find((c) => c.id === cameraId) ?? null;
  const counting = cam?.state === "running";
  if (cameraId) {
    return (
      <div className="space-y-6">
        <div>
          <h2 className="text-lg font-semibold">{cam?.name ?? name}</h2>
          <p className="mt-0.5 text-sm text-muted">{cam?.source_label}</p>
        </div>
        <div className="rounded-[var(--radius-m)] border border-rule bg-panel p-4">
          {cam ? <CameraState cam={cam} online={device?.online ?? false} /> : <p className="text-sm text-muted">Starting the camera…</p>}
          {!device?.online ? (
            <p className="mt-2 text-sm text-faint">
              Waiting for the counting service on this computer. It starts together with CountVision.
            </p>
          ) : null}
        </div>
        {counting ? (
          <div>
            <LivePicture orgId={progress.orgId!} cameraId={cameraId} name={cam?.name ?? name} kind="stream" />
            <p className="mt-2 max-w-[62ch] text-sm text-muted">
              The live picture of this camera, with people blurred. Nothing is recorded. The owner can change this in
              Settings, Privacy. Put up a sign at the camera so visitors know it counts.
            </p>
          </div>
        ) : null}
        <div className="flex flex-wrap gap-3 pt-2">
          <Button size="lg" onClick={() => onDone({ ...progress, step: 2, cameraId })} data-testid="setup-camera-next">
            Continue
          </Button>
          <Button size="lg" variant="ghost" onClick={() => { setCameraId(null); save({ ...progress, cameraId: undefined }); }}>
            Add another camera
          </Button>
        </div>
      </div>
    );
  }
  return (
    <form onSubmit={add} className="space-y-6" noValidate>
      {error ? <Notice tone="error">{error}</Notice> : null}
      <SourceFields kind={kind} setKind={setKind} address={address} setAddress={setAddress} name={name}
        setName={setName} errors={errors} autoFocus />
      <div className="flex flex-wrap gap-3">
        <Button type="submit" size="lg" busy={busy} data-testid="setup-add-camera">Add camera</Button>
        <Button type="button" size="lg" variant="ghost" onClick={() => onDone({ ...progress, step: 2 })}>
          Skip for now
        </Button>
      </div>
    </form>
  );
}

function LineStep({ progress }: { progress: Progress }) {
  const router = useRouter();
  const finish = (href: string) => {
    try {
      sessionStorage.removeItem(KEY);
      localStorage.setItem("cv:last-org", progress.orgId ?? "");
    } catch {
      /* ignore */
    }
    router.replace(href);
  };
  const editor = progress.cameraId ? `/app/orgs/${progress.orgId}/cameras/${progress.cameraId}` : null;
  return (
    <div className="space-y-6">
      <p className="max-w-[56ch] text-muted">
        CountVision counts a person when they cross a line you draw on the camera picture, for example across the
        door. Draw it where people walk through, not where they stand still. You can change it any time.
      </p>
      <Notice>
        Only numbers are stored on this computer. No pictures, no video.
      </Notice>
      <div className="flex flex-wrap gap-3">
        {editor ? (
          <Button size="lg" onClick={() => finish(editor)} data-testid="setup-draw-line">Draw the counting line</Button>
        ) : null}
        <Button size="lg" variant={editor ? "ghost" : "primary"} onClick={() => finish(`/app/orgs/${progress.orgId}`)}
          data-testid="setup-finish">
          {editor ? "Later, open the overview" : "Open the overview"}
        </Button>
      </div>
    </div>
  );
}

export default function SetupPage() {
  const router = useRouter();
  const { edition, loaded } = useEdition();
  const { me, loading } = useMe();
  const [progress, setProgress] = React.useState<Progress | null>(null);

  React.useEffect(() => {
    if (!loaded || loading) return;
    const saved = load();
    if (edition.edition !== "local") {
      router.replace("/login");
    } else if (edition.needs_setup) {
      setProgress({ step: 0 });
    } else if (me && saved.step > 0 && saved.orgId) {
      setProgress(saved); // reloaded in the middle of the wizard
    } else {
      router.replace(me ? "/app" : "/login");
    }
  }, [loaded, loading, edition, me, router]);

  const go = (p: Progress) => {
    save(p);
    setProgress(p);
    window.scrollTo({ top: 0 });
  };

  if (!progress) return <Loading />;
  const step = STEPS[progress.step];
  return (
    <div className="grid min-h-dvh lg:grid-cols-[360px_minmax(0,1fr)]">
      <aside className="border-b border-rule bg-panel px-5 py-6 sm:px-10 lg:border-b-0 lg:border-r lg:py-10">
        <Wordmark href="/setup" />
        <p className="mt-6 max-w-[34ch] text-muted lg:mt-10">
          Welcome to CountVision Local. Three steps, about five minutes. Everything stays on this computer.
        </p>
        <div className="mt-8 hidden lg:block">
          <Rail step={progress.step} />
        </div>
        <p className="mt-6 text-sm text-faint lg:hidden">Step {progress.step + 1} of {STEPS.length}</p>
      </aside>
      <main className="px-5 py-8 sm:px-10 lg:py-14">
        <div className="w-full max-w-[560px]">
          <h1 className="text-2xl font-semibold leading-tight">{step.title}</h1>
          <p className="mb-8 mt-2 text-muted">{step.text}</p>
          {progress.step === 0 ? <AccountStep onDone={go} /> : null}
          {progress.step === 1 ? <CameraStep progress={progress} onDone={go} /> : null}
          {progress.step === 2 ? <LineStep progress={progress} /> : null}
          {progress.step === 0 ? (
            <p className="mt-10 text-sm text-faint">
              Already set up? <Link href="/login" className="text-muted">Log in</Link>
            </p>
          ) : null}
        </div>
      </main>
    </div>
  );
}
