"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import * as React from "react";

import { PageTitle } from "@/components/app-shell";
import { Loading } from "@/components/me";
import { OrgGate } from "@/components/org-gate";
import { Button } from "@/components/ui/button";
import { Notice, Panel, PanelHead } from "@/components/ui/panel";
import { StatusDot } from "@/components/ui/status-dot";
import { api, errorText, roleRank, timeAgo, type Org } from "@/lib/api";
import { fmtBytes, fmtClock, JOB_STATUS, jobHealth, uploadFile, type VideoJob } from "@/lib/integrations";
import { useLive } from "@/lib/live";
import { cn } from "@/lib/utils";

const ACCEPT = ".mp4,.mov,.avi,.mkv,.m4v,.webm,.mpg,.mpeg,.ts,.mts,.wmv,video/*";

function Uploader({ org, onDone }: { org: Org; onDone: (job: VideoJob) => void }) {
  const [file, setFile] = React.useState<File | null>(null);
  const [progress, setProgress] = React.useState<number | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [over, setOver] = React.useState(false);
  const abort = React.useRef<AbortController | null>(null);
  const base = `/api/orgs/${org.id}/video-jobs`;

  async function start(f: File) {
    setFile(f);
    setError(null);
    setProgress(0);
    abort.current = new AbortController();
    try {
      const job = await api.post<VideoJob>(base, { name: f.name, size_bytes: f.size });
      await uploadFile(base, job, f, setProgress, abort.current.signal);
      onDone(await api.post<VideoJob>(`${base}/${job.id}/complete`));
    } catch (e) {
      if ((e as Error).name === "AbortError") setError("Upload stopped.");
      else setError(e instanceof Error && !("status" in e) ? e.message : errorText(e));
      setProgress(null);
    }
  }

  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files[0]; if (f && progress === null) void start(f); }}
      className={cn("rounded-[var(--radius-m)] border border-dashed px-5 py-7 text-center transition-colors",
        over ? "border-accent bg-accent/5" : "border-rule bg-panel")}>
      {progress !== null && file ? (
        <div className="mx-auto max-w-[460px]" aria-live="polite">
          <p className="truncate font-medium">{file.name}</p>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-raise" role="progressbar" aria-valuemin={0} aria-valuemax={100}
            aria-valuenow={Math.round(progress * 100)} aria-label="Upload">
            <div className="h-full rounded-full bg-accent transition-[width]" style={{ width: `${progress * 100}%` }} />
          </div>
          <p className="mt-2 text-sm text-muted">Uploading: {Math.round(progress * 100)} % of {fmtBytes(file.size)}. Keep this page open.</p>
          <Button size="sm" variant="ghost" className="mt-2" onClick={() => abort.current?.abort()}>Stop upload</Button>
        </div>
      ) : (
        <>
          <p className="text-lg font-medium">Drop a video file here</p>
          <p className="mt-1 text-sm text-muted">MP4, MOV, AVI, MKV … up to 2 GB and 2 hours. Or</p>
          <label className="mt-3 inline-flex h-10 cursor-pointer items-center rounded-[var(--radius-s)] bg-accent px-4 font-medium text-accent-ink hover:bg-accent/85">
            Choose a video
            <input type="file" accept={ACCEPT} className="sr-only" data-testid="video-file"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) void start(f); e.target.value = ""; }} />
          </label>
          {error ? <p className="mt-3 text-sm text-danger" role="alert">{error}</p> : null}
        </>
      )}
    </div>
  );
}

function VideoList({ org }: { org: Org }) {
  const router = useRouter();
  const [jobs, setJobs] = React.useState<VideoJob[] | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const canEdit = roleRank(org.my_role) >= roleRank("member");
  const base = `/api/orgs/${org.id}/video-jobs`;

  const load = React.useCallback(async () => {
    try {
      setJobs(await api.get<VideoJob[]>(base));
    } catch (e) {
      setError(errorText(e));
    }
  }, [base]);
  React.useEffect(() => { void load(); }, [load]);
  useLive(org.id, React.useCallback((m) => { if (m.type === "video_job") void load(); }, [load]));

  return (
    <>
      <PageTitle title="Video analysis" />
      <p className="mb-5 max-w-[72ch] text-muted">
        Count a recorded video: a traffic study, an event, a camera without internet. Upload the file, draw lines and
        zones on it, and get the numbers, an annotated video (people pixelated) and a PDF. The uploaded video is
        deleted when the analysis ends. You need a legal basis to record and analyse the people in it.
      </p>
      {error ? <Notice tone="error" className="mb-5">{error}</Notice> : null}
      {canEdit ? <Uploader org={org} onDone={(j) => router.push(`/app/orgs/${org.id}/video/${j.id}`)} /> : null}

      <Panel className="mt-6">
        <PanelHead title="Videos" />
        {!jobs ? <Loading /> : jobs.length === 0 ? <p className="px-5 py-5 text-muted">No video yet.</p> : (
          <ul className="divide-y divide-rule" aria-label="Video jobs">
            {jobs.map((j) => (
              <li key={j.id}>
                <Link href={`/app/orgs/${org.id}/video/${j.id}`} data-testid="job-row"
                  className="flex flex-wrap items-center gap-x-5 gap-y-1 px-5 py-3 no-underline hover:bg-raise/50">
                  <StatusDot health={jobHealth(j)} />
                  <span className="min-w-0 flex-1 truncate font-medium">{j.name}</span>
                  <span className="text-sm text-muted">
                    {JOB_STATUS[j.status]}{j.status === "running" ? ` ${Math.round(j.progress * 100)} %` : ""}
                    {j.status === "done" && j.result ? `: IN ${j.result.totals.in}, OUT ${j.result.totals.out}` : ""}
                  </span>
                  <span className="text-sm text-faint">{fmtClock(j.duration_s)}, {fmtBytes(j.size_bytes)}, {timeAgo(j.created_at)}</span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </>
  );
}

export default function VideoPage() {
  const { orgId } = useParams<{ orgId: string }>();
  return <OrgGate orgId={orgId}>{(org) => <VideoList org={org} />}</OrgGate>;
}
