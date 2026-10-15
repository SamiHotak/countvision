"use client";

// CountVision Local: backup and restore (owners only). A backup = one .cvbackup file with the whole
// database and the installation's secret key. Write it to a USB stick now and then.

import * as React from "react";

import { Loading } from "@/components/me";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/input";
import { Notice } from "@/components/ui/panel";
import { api, errorText, fmtDateTime, type Org } from "@/lib/api";

interface BackupFile {
  name: string; size: number; created_at: string | null; version: string | null; organization: string | null;
  kind: "manual" | "before-restore" | "upload" | string;
}
interface BackupList { folder: string; default_folder: string; free_bytes: number | null; backups: BackupFile[] }

const mb = (n: number) => (n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} kB` : `${(n / 1024 / 1024).toFixed(1)} MB`);

export function Backups({ org }: { org: Org }) {
  const [list, setList] = React.useState<BackupList | null>(null);
  const [folder, setFolder] = React.useState("");
  const [shown, setShown] = React.useState(""); // the folder the list is from
  const [msg, setMsg] = React.useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [restoring, setRestoring] = React.useState<BackupFile | null>(null);
  const upload = React.useRef<HTMLInputElement>(null);
  const q = (f: string) => (f.trim() ? `?folder=${encodeURIComponent(f.trim())}` : "");

  const load = React.useCallback(async (f: string) => {
    try {
      setList(await api.get<BackupList>(`/api/orgs/${org.id}/backups${q(f)}`));
      setShown(f);
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    }
  }, [org.id]);
  React.useEffect(() => void load(""), [load]);

  async function create() {
    setBusy(true);
    setMsg(null);
    try {
      const out = await api.post<BackupFile & { folder: string }>(`/api/orgs/${org.id}/backups`,
        { folder: folder.trim() || null });
      setMsg({ tone: "success", text: `Backup written: ${out.name} (${mb(out.size)}) in ${out.folder}` });
      await load(folder);
    } catch (e) {
      setMsg({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  async function sendFile(file: File) {
    setBusy(true);
    setMsg(null);
    try {
      const resp = await fetch(`/api/orgs/${org.id}/backups/upload?name=${encodeURIComponent(file.name)}`, {
        method: "PUT", body: file, credentials: "same-origin",
        headers: { "X-CountVision": "1", "Content-Type": "application/octet-stream" },
      });
      const data = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(data?.error?.message ?? `Upload failed (${resp.status}).`);
      setMsg({ tone: "success", text: `${data.name} is ready to restore below.` });
      setFolder("");
      await load("");
    } catch (e) {
      setMsg({ tone: "error", text: e instanceof Error ? e.message : "Upload failed." });
    } finally {
      setBusy(false);
      if (upload.current) upload.current.value = "";
    }
  }

  return (
    <div className="space-y-5 p-5">
      <p className="max-w-[66ch] text-muted">
        A backup is one file with all your numbers, cameras, lines, members and settings. Make one before big
        changes, and copy one to a USB stick now and then: if this computer breaks, a new one gets everything back.
      </p>
      {msg ? <Notice tone={msg.tone}>{msg.text}</Notice> : null}
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-[240px] flex-1">
          <Field label="Folder" name="backup-folder" value={folder} onChange={(e) => setFolder(e.target.value)}
            placeholder={list?.default_folder ?? ""} autoComplete="off" spellCheck={false}
            hint="Empty = the CountVision backup folder. For a USB stick type its drive, for example E:\" />
        </div>
        <Button onClick={() => void create()} busy={busy} data-testid="backup-create">Create backup now</Button>
        <Button variant="secondary" onClick={() => void load(folder)} disabled={busy}>Show this folder</Button>
      </div>

      {!list ? <Loading /> : (
        <div>
          <p className="mb-2 text-sm text-faint">
            {list.folder}{list.free_bytes != null ? `, ${mb(list.free_bytes)} free` : ""}
          </p>
          {list.backups.length === 0 ? (
            <p className="text-muted">No backup in this folder yet.</p>
          ) : (
            <ul className="divide-y divide-rule rounded-[var(--radius-s)] border border-rule" aria-label="Backups">
              {list.backups.map((b) => (
                <li key={b.name} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5" data-testid="backup-row">
                  <span className="min-w-0 flex-1 basis-64">
                    <span className="block truncate">{b.created_at ? fmtDateTime(b.created_at) : b.name}</span>
                    <span className="block truncate text-sm text-faint">
                      {b.kind === "before-restore" ? "Made automatically before a restore. " : ""}
                      {b.organization ?? "?"}, {mb(b.size)}{b.version ? `, version ${b.version}` : ""}
                    </span>
                  </span>
                  <a href={`/api/orgs/${org.id}/backups/${encodeURIComponent(b.name)}/download${q(shown)}`}
                    className="text-sm text-accent" download>Download</a>
                  <button className="cursor-pointer text-sm text-muted hover:text-text" onClick={() => setRestoring(b)}
                    data-testid="backup-restore">Restore</button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="border-t border-rule pt-4">
        <p className="text-sm text-muted">A backup file from another computer or a stick that is not in the list:</p>
        <input ref={upload} type="file" accept=".cvbackup" className="mt-2 block text-sm text-muted file:mr-3 file:h-8 file:cursor-pointer file:rounded-[var(--radius-s)] file:border file:border-rule file:bg-raise file:px-3 file:text-text"
          onChange={(e) => e.target.files?.[0] && void sendFile(e.target.files[0])} disabled={busy}
          data-testid="backup-upload" aria-label="Upload a backup file" />
      </div>

      {restoring ? (
        <RestoreDialog org={org} backup={restoring} folder={shown} onClose={() => setRestoring(null)} />
      ) : null}
    </div>
  );
}

function RestoreDialog({ org, backup, folder, onClose }: { org: Org; backup: BackupFile; folder: string; onClose: () => void }) {
  const [typed, setTyped] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/orgs/${org.id}/backups/${encodeURIComponent(backup.name)}/restore`,
        { confirm: typed, folder: folder.trim() || null });
      window.location.assign("/login?restored=1"); // everybody logs in again (sessions come from the backup)
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  }
  return (
    <Dialog open onClose={onClose} title="Restore this backup?">
      <form onSubmit={submit} className="space-y-4">
        {error ? <Notice tone="error">{error}</Notice> : null}
        <p className="text-muted">
          ALL current data is replaced by the backup from{" "}
          <span className="text-text">{backup.created_at ? fmtDateTime(backup.created_at) : backup.name}</span>.
          Numbers counted after that are lost, unless you restore the automatic backup made right before.
          Everybody has to log in again.
        </p>
        <Field label={`Type "${org.name}" to confirm`} name="confirm" value={typed} onChange={(e) => setTyped(e.target.value)}
          autoComplete="off" autoFocus />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="danger" busy={busy} disabled={typed.trim() !== org.name}
            data-testid="backup-restore-confirm">Restore</Button>
        </div>
      </form>
    </Dialog>
  );
}
