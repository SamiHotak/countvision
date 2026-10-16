// Live camera picture of CountVision Local (GET/PUT /api/orgs/{org}/live-view, live.jpg, live.mjpg).
// The cloud edition never shows video: it has none of these endpoints.

import * as React from "react";

import { api } from "@/lib/api";

export type LiveMode = "full" | "blur" | "off";

export interface LiveViewSetting {
  mode: LiveMode;
  admins_only: boolean;
  can_watch: boolean; // this person may watch (mode is not off, and the role is enough)
  can_change: boolean; // the owner
}

const cache = new Map<string, Promise<LiveViewSetting>>();
const listeners = new Set<() => void>();

function load(orgId: string): Promise<LiveViewSetting> {
  let p = cache.get(orgId);
  if (!p) {
    p = api.get<LiveViewSetting>(`/api/orgs/${orgId}/live-view`);
    p.catch(() => cache.delete(orgId));
    cache.set(orgId, p);
  }
  return p;
}

/** The owner changed the setting: every live picture on the page reloads it. */
export function liveViewChanged(orgId: string, value?: LiveViewSetting) {
  cache.set(orgId, value ? Promise.resolve(value) : api.get<LiveViewSetting>(`/api/orgs/${orgId}/live-view`));
  listeners.forEach((fn) => fn());
}

/** The installation's live picture setting (null while loading, or in the cloud edition). */
export function useLiveViewSetting(orgId: string, enabled = true): LiveViewSetting | null {
  const [value, setValue] = React.useState<LiveViewSetting | null>(null);
  React.useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const read = () => load(orgId).then((v) => alive && setValue(v)).catch(() => alive && setValue(null));
    read();
    listeners.add(read);
    return () => {
      alive = false;
      listeners.delete(read);
    };
  }, [orgId, enabled]);
  return value;
}

export function liveUrl(orgId: string, cameraId: string, kind: "mjpg" | "jpg",
  opts: { geometry?: boolean; width?: number; fps?: number } = {}): string {
  const q = new URLSearchParams();
  if (opts.geometry === false) q.set("geometry", "false");
  if (opts.width) q.set("w", String(Math.round(opts.width)));
  if (kind === "mjpg" && opts.fps) q.set("fps", String(opts.fps));
  const qs = q.toString();
  return `/api/orgs/${orgId}/cameras/${cameraId}/live.${kind}${qs ? `?${qs}` : ""}`;
}

export const MODE_TEXT: Record<LiveMode, string> = {
  blur: "people blurred",
  full: "full picture",
  off: "off",
};

/** Per viewer: hide the picture of one camera (remembered in this browser only). */
export function useHiddenPicture(cameraId: string): [boolean, (hidden: boolean) => void] {
  const key = `cv.live.hidden.${cameraId}`;
  const [hidden, setHidden] = React.useState(false);
  React.useEffect(() => {
    try {
      setHidden(window.localStorage.getItem(key) === "1");
    } catch {
      /* private window: the picture is shown */
    }
  }, [key]);
  const set = React.useCallback((value: boolean) => {
    setHidden(value);
    try {
      if (value) window.localStorage.setItem(key, "1");
      else window.localStorage.removeItem(key);
    } catch {
      /* not remembered */
    }
  }, [key]);
  return [hidden, set];
}

/** True while the page is visible (a hidden tab stops the stream: nothing is made for nobody). */
export function usePageVisible(): boolean {
  const [visible, setVisible] = React.useState(true);
  React.useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}
