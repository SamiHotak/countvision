"use client";

// Settings -> Privacy (CountVision Local): how the live camera picture is shown in this business.
// Only the owner changes it; every change is written to the activity log.

import * as React from "react";

import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui/panel";
import { api, errorText, type Org } from "@/lib/api";
import { liveViewChanged, useLiveViewSetting, type LiveMode, type LiveViewSetting } from "@/lib/live-picture";
import { cn } from "@/lib/utils";

const CHOICES: { mode: LiveMode; title: string; text: string }[] = [
  {
    mode: "blur",
    title: "People blurred",
    text: "Every detected person and vehicle is pixelated. You see the scene and the counting, not who it is.",
  },
  {
    mode: "full",
    title: "Full picture",
    text: "The camera picture as it is. Use it only when your staff and visitors know about the camera.",
  },
  {
    mode: "off",
    title: "Off",
    text: "No live picture anywhere. Counting goes on; lines are drawn on a test picture instead.",
  },
];

export function PrivacySettings({ org }: { org: Org }) {
  const current = useLiveViewSetting(org.id);
  const [mode, setMode] = React.useState<LiveMode>("blur");
  const [adminsOnly, setAdminsOnly] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [message, setMessage] = React.useState<{ tone: "error" | "success"; text: string } | null>(null);

  React.useEffect(() => {
    if (current) {
      setMode(current.mode);
      setAdminsOnly(current.admins_only);
    }
  }, [current]);

  if (!current) return <p className="px-5 py-4 text-muted">Loading</p>;
  const canChange = current.can_change;
  const changed = mode !== current.mode || adminsOnly !== current.admins_only;

  async function save() {
    setBusy(true);
    setMessage(null);
    try {
      const out = await api.put<LiveViewSetting>(`/api/orgs/${org.id}/live-view`, { mode, admins_only: adminsOnly });
      liveViewChanged(org.id, out);
      setMessage({ tone: "success", text: "Saved. Open pages use the new setting at once." });
    } catch (e) {
      setMessage({ tone: "error", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4 px-5 py-4" data-testid="privacy-settings">
      <p className="max-w-[68ch] text-muted">
        The live picture comes from the counting service on this computer. It is shown only to people who are
        logged in to this business, and it is never recorded or saved: only the numbers are stored.
      </p>
      <fieldset disabled={!canChange || busy}>
        <legend className="mb-2 font-medium">Live camera picture</legend>
        <div className="grid gap-2 md:grid-cols-3" role="radiogroup">
          {CHOICES.map((c) => (
            <label key={c.mode}
              className={cn("block cursor-pointer rounded-[var(--radius-s)] border p-3",
                mode === c.mode ? "border-accent bg-accent/8" : "border-rule hover:border-faint",
                !canChange && "cursor-default")}>
              <span className="flex items-center gap-2">
                <input type="radio" name="live-mode" value={c.mode} checked={mode === c.mode}
                  onChange={() => setMode(c.mode)} className="accent-[var(--color-accent)]" />
                <span className="font-medium">{c.title}</span>
              </span>
              <span className="mt-1 block text-sm text-muted">{c.text}</span>
            </label>
          ))}
        </div>
        <label className={cn("mt-3 flex items-center gap-2", mode === "off" && "opacity-50")}>
          <input type="checkbox" checked={adminsOnly} disabled={mode === "off"}
            onChange={(e) => setAdminsOnly(e.target.checked)} className="accent-[var(--color-accent)]" />
          Only admins and the owner may watch
        </label>
      </fieldset>
      {mode !== "off" ? (
        <Notice tone="warn">
          Showing camera pictures to staff is processing of personal data, also without recording. Put up a sign at
          the camera and make sure you have a legal basis (in Germany often Art. 6 (1) (f) GDPR; with employees, talk
          to the works council). CountVision gives you templates, but it is not legal advice: check with a lawyer.
        </Notice>
      ) : null}
      {message ? <Notice tone={message.tone}>{message.text}</Notice> : null}
      {canChange ? (
        <Button onClick={() => void save()} disabled={!changed} busy={busy}>Save privacy setting</Button>
      ) : (
        <p className="text-sm text-faint">Only the owner can change this.</p>
      )}
    </div>
  );
}
