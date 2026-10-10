"use client";

// Live updates from the cloud (Server-Sent Events): line crossings, device status, config
// versions, snapshot results. Only numbers and names, never pictures.
//
// The browser's EventSource reconnects by itself (the server ends every stream after 10
// minutes and asks for a reconnect after 3 s). Pages still poll slowly as a fallback, so they
// also work when a proxy blocks streaming.

import * as React from "react";

import type { LiveMessage } from "@/lib/api";

export type LiveState = "connecting" | "live" | "off";

export function useLive(orgId: string, onMessage: (msg: LiveMessage) => void): LiveState {
  const [state, setState] = React.useState<LiveState>("connecting");
  const handler = React.useRef(onMessage);
  React.useEffect(() => {
    handler.current = onMessage;
  }, [onMessage]);

  React.useEffect(() => {
    if (typeof EventSource === "undefined") {
      setState("off");
      return;
    }
    const source = new EventSource(`/api/orgs/${orgId}/live`);
    let failures = 0;
    source.onopen = () => {
      failures = 0;
      setState("live");
    };
    source.onmessage = (ev) => {
      try {
        handler.current(JSON.parse(ev.data) as LiveMessage);
      } catch {
        // a broken message must not stop the page
      }
    };
    source.onerror = () => {
      failures += 1;
      // CONNECTING = the browser retries by itself; CLOSED = it gave up (e.g. 403/404).
      setState(source.readyState === EventSource.CLOSED || failures > 3 ? "off" : "connecting");
    };
    return () => source.close();
  }, [orgId]);

  return state;
}

/** Small "Live" label for page headers. */
export function liveLabel(state: LiveState): string {
  return { connecting: "Connecting", live: "Live", off: "Updates every 10 s" }[state];
}

/**
 * Reload a page's data when the cloud reports activity (device uploads, new configs), at most
 * every ``minGapMs``. Falls back to polling every ``pollMs`` (slower while live).
 */
export function useLiveReload(orgId: string, reload: () => void, minGapMs = 5000, pollMs = 15_000): LiveState {
  const last = React.useRef(0);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const fn = React.useRef(reload);
  React.useEffect(() => {
    fn.current = reload;
  }, [reload]);
  const onMessage = React.useCallback((msg: LiveMessage) => {
    if (msg.type !== "device" && msg.type !== "refresh" && msg.type !== "config") return;
    if (timer.current) return;
    const wait = Math.max(0, last.current + minGapMs - Date.now());
    timer.current = setTimeout(() => {
      timer.current = null;
      last.current = Date.now();
      fn.current();
    }, wait);
  }, [minGapMs]);
  const state = useLive(orgId, onMessage);
  React.useEffect(() => {
    const id = setInterval(() => {
      last.current = Date.now();
      fn.current();
    }, state === "live" ? pollMs * 4 : pollMs);
    return () => clearInterval(id);
  }, [state, pollMs]);
  React.useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);
  return state;
}
