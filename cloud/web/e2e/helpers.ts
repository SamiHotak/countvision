import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const PASSWORD = "a-long-test-password";

/** A unique email per test run, so tests never clash. */
export function uniqueEmail(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
}

/** The newest link with this path from the dev outbox (memory email backend). */
export async function linkFromMail(request: APIRequestContext, to: string, path: string): Promise<string> {
  let found: string | null = null;
  await expect
    .poll(async () => {
      const resp = await request.get(`/api/dev/outbox?to=${encodeURIComponent(to)}`);
      const mails: { text: string }[] = await resp.json();
      for (const m of mails) {
        const match = m.text.match(new RegExp(`https?://[^\\s]+/${path}[^\\s]*`));
        if (match) {
          found = match[0];
          return true;
        }
      }
      return false;
    }, { message: `no ${path} email to ${to}` })
    .toBe(true);
  const url = new URL(found!);
  return url.pathname + url.search;
}

export async function signUp(page: Page, name: string, email: string) {
  await page.goto("/signup");
  await page.getByLabel("Your name").fill(name);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();
}

export async function createOrg(page: Page, name: string) {
  await expect(page.getByRole("heading", { name: /Name your business|New organization/ })).toBeVisible();
  await page.getByLabel("Organization name").fill(name);
  await page.getByRole("button", { name: "Create organization" }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
}

/** Line crossings as the edge sends them ("today" in the cloud counts these events). */
export function crossings(camera: string, line: string, n: number, direction: "in" | "out", tag: string) {
  const now = Date.now() / 1000;
  return Array.from({ length: n }, (_, i) => ({
    event_id: `${tag}-${direction}-${i}-${Math.random().toString(36).slice(2)}`, camera_id: camera, kind: "line_cross",
    ts: now - 5 + i * 0.01, name: line, class_name: "person", direction,
  }));
}

/** Acts like the edge agent: pairs with the code, then uploads one batch. Returns the token. */
export async function fakeAgent(request: APIRequestContext, code: string, edgeId = "edge-e2e"): Promise<string> {
  const pair = await request.post("/api/device/pair", {
    data: { code, edge_device_id: edgeId, agent_version: "0.6.0" },
  });
  expect(pair.ok()).toBeTruthy();
  const { token } = await pair.json();
  const now = Math.floor(Date.now() / 1000);
  const minute = now - (now % 60) - 60;
  const upload = await request.post("/api/device/ingest", {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      batch_id: `b-${now}-${edgeId}`,
      agent_version: "0.6.0",
      status: { detector: { name: "yolox_tiny", license: "Apache-2.0" }, upload: { pending: 0 } },
      cameras: [{
        id: "door", name: "Entrance", state: "running", connected: true, fps: 9.8, lines: ["entrance"],
        config: {
          lines: [{ name: "entrance", p1: [0.2, 0.6], p2: [0.8, 0.6], in_direction: "to_right" }],
          zones: [], classes: ["person"], anchor: "bottom_center", schedule: null,
        },
        config_version: 0, snapshots_allowed: true, frame: [1280, 720],
      }],
      line_counts: [{ camera_id: "door", window_start: minute, line: "entrance", class_name: "*", in_count: 7, out_count: 3 }],
      events: [...crossings("door", "entrance", 7, "in", edgeId), ...crossings("door", "entrance", 3, "out", edgeId)],
    },
  });
  expect(upload.ok()).toBeTruthy();
  return token;
}
