import fs from "node:fs";

import { expect, test, type Page } from "@playwright/test";

import { createOrg, fakeAgent, linkFromMail, signUp, uniqueEmail } from "./helpers";

// The API runs with the dev fakes: CV_TELEGRAM_API_URL=http://127.0.0.1:8001/api/dev/telegram and the
// webhook sink at /api/dev/webhook-sink. Video jobs run in a thread with the blob detector.
const TG_TOKEN = "123456789:AAfake-telegram-token-for-tests";
const SINK = process.env.CV_E2E_WEBHOOK_URL ?? "http://127.0.0.1:8001/api/dev/webhook-sink";
const CSRF = { "X-CountVision": "1" };

async function verifiedOrg(page: Page, request: import("@playwright/test").APIRequestContext, prefix: string, org: string) {
  const email = uniqueEmail(prefix);
  await signUp(page, "Ida Integration", email);
  await page.goto(await linkFromMail(request, email, "verify-email"));
  await page.goto("/app");
  await createOrg(page, org);
  return page.url().split("/orgs/")[1].split("/")[0];
}

test("alerts: Telegram + webhook channels, a queue alert fires and reaches both", async ({ page, request }) => {
  const orgId = await verifiedOrg(page, request, "alerts", "Kiosk Alarm");
  const site = await (await page.request.post(`/api/orgs/${orgId}/sites`, { headers: CSRF, data: { name: "Kiosk" } })).json();
  const code = await (await page.request.post(`/api/orgs/${orgId}/pairing-codes`, {
    headers: CSRF, data: { site_id: site.id, device_name: "Pi" } })).json();
  const token = await fakeAgent(request, code.code, `edge-alert-${Date.now()}`);
  // the camera now reports a queue zone, and 3 minutes with 6 people waiting
  const now = Math.floor(Date.now() / 1000);
  const minute = (back: number) => now - (now % 60) - 60 * back;
  const up = await request.post("/api/device/ingest", {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      batch_id: `q-${now}`,
      cameras: [{ id: "door", name: "Entrance", state: "running", connected: true, config: {
        lines: [{ name: "entrance", p1: [0.2, 0.6], p2: [0.8, 0.6] }],
        zones: [{ name: "Till", polygon: [[0.1, 0.1], [0.4, 0.1], [0.4, 0.5]], kind: "queue" }], classes: ["person"] } }],
      zone_stats: [3, 2, 1].map((b) => ({ camera_id: "door", window_start: minute(b), zone: "Till", class_name: "*",
        sample_s: 60, occ_avg: 6, occ_max: 7, occ_last: 6, queue_avg: 6, queue_max: 7, visits: 0, dwell_sum_s: 0, dwell_max_s: 0 })),
    },
  });
  expect(up.ok()).toBeTruthy();

  await page.getByRole("link", { name: "Alerts" }).first().click();
  await expect(page.getByRole("heading", { name: "Alerts", exact: true })).toBeVisible();

  // Telegram channel: token -> find my chat -> add -> test
  await page.getByRole("button", { name: "Add channel" }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("Bot token").fill(TG_TOKEN);
  await dialog.getByRole("button", { name: "Find my chat" }).click();
  await expect(dialog.getByLabel("Send alerts to")).toContainText("Test phone");
  await dialog.getByRole("button", { name: "Add channel" }).click();
  const tgRow = page.getByTestId("channel-row").filter({ hasText: "My phone" });
  await expect(tgRow).toContainText("Chat: Test phone");
  await tgRow.getByRole("button", { name: "Send test" }).click();
  await expect(page.getByText("Test message sent to Test phone.")).toBeVisible();

  // webhook channel: the signing secret is shown once
  await page.getByRole("button", { name: "Add channel" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("radio", { name: "Webhook" }).click();
  await dialog.getByLabel("Address (URL)").fill(SINK);
  await dialog.getByRole("button", { name: "Add channel" }).click();
  await expect(page.getByTestId("signing-secret")).toContainText("whsec_");
  await page.getByRole("button", { name: "I copied it" }).click();
  const hookRow = page.getByTestId("channel-row").filter({ hasText: "Our system" });
  await hookRow.getByRole("button", { name: "Send test" }).click();
  await expect(page.getByText("Webhook answered 200.")).toBeVisible();

  // a queue alert to both channels
  await page.getByRole("button", { name: "New alert" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByText("Long queue", { exact: true }).click();
  await expect(dialog.getByLabel("Queue zone", { exact: true })).toContainText("Till");
  await dialog.getByLabel("People waiting (at least)", { exact: true }).fill("5");
  await dialog.getByLabel("For at least (minutes)", { exact: true }).fill("2");
  await dialog.getByRole("checkbox", { name: /Our system/ }).check();
  await dialog.getByRole("button", { name: "Create alert" }).click();
  await expect(page.getByTestId("rule-row")).toContainText("5 or more people waiting for 2 min");

  expect((await (await request.post("/api/dev/run-alerts", { headers: CSRF })).json()).fired).toBeGreaterThanOrEqual(1);
  await page.reload();
  await expect(page.getByTestId("rule-row")).toContainText("Active since");
  await expect(page.getByRole("list", { name: "Alert history" })).toContainText("6 people waiting in all queue zones (limit 5, for 2 min)");
  const sent = await (await request.get("/api/dev/telegram/sent")).json();
  expect(sent[0].text).toContain("Kiosk: Long queue");
  const hooks = await (await request.get("/api/dev/webhook-sink")).json();
  const fired = hooks.map((h: { body: string }) => JSON.parse(h.body)).find((b: { type: string; data: { site_name: string } }) =>
    b.type === "alert.fired" && b.data.site_name === "Kiosk");
  expect(fired.data.value).toBe(6);
});

test("API key: create, read the public API, revoke", async ({ page, request }) => {
  const orgId = await verifiedOrg(page, request, "apikey", "Data Shop");
  await page.request.post(`/api/orgs/${orgId}/sites`, { headers: CSRF, data: { name: "Main street" } });
  await page.getByRole("link", { name: "API", exact: true }).first().click();
  await page.getByRole("button", { name: "New key" }).click();
  await page.getByRole("dialog").getByLabel("Name (what uses it)").fill("Power BI");
  await page.getByRole("dialog").getByRole("button", { name: "Create key" }).click();
  const key = (await page.getByTestId("new-api-key").textContent())!.trim();
  expect(key).toMatch(/^cvk_/);
  await expect(page.getByLabel("List sites")).toContainText(key);

  const sites = await request.get("/api/v1/sites", { headers: { Authorization: `Bearer ${key}` } });
  expect(sites.status()).toBe(200);
  expect((await sites.json())[0].name).toBe("Main street");

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Revoke Power BI" }).click();
  await expect(page.getByTestId("key-row")).toContainText("Revoked");
  expect((await request.get("/api/v1/sites", { headers: { Authorization: `Bearer ${key}` } })).status()).toBe(401);
});

const VIDEO = process.env.CV_E2E_VIDEO;

test("video analysis: upload, draw a line, count, results", async ({ page, request }) => {
  test.skip(!VIDEO || !fs.existsSync(VIDEO), "set CV_E2E_VIDEO to the demo video (countvision-edge demo)");
  test.setTimeout(120_000);
  await verifiedOrg(page, request, "video", "Traffic Study");
  await page.getByRole("link", { name: "Video analysis" }).first().click();
  await page.getByTestId("video-file").setInputFiles(VIDEO!);
  await expect(page.getByRole("button", { name: "Start counting" })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/640x360, 15 FPS, 0:18/)).toBeVisible();

  const stage = page.locator("svg.touch-none").first();
  const box = (await stage.boundingBox())!;
  await page.mouse.move(box.x + 0.1 * box.width, box.y + 0.5 * box.height);
  await page.mouse.down();
  await page.mouse.move(box.x + 0.9 * box.width, box.y + 0.5 * box.height, { steps: 10 });
  await page.mouse.up();
  await page.getByRole("checkbox", { name: "Cars" }).check();
  await page.getByLabel("Frames per second").selectOption("0"); // every frame (the demo video is short)
  await page.getByRole("button", { name: "Start counting" }).click();

  await expect(page.getByTestId("job-status")).toContainText("Done", { timeout: 90_000 });
  const line = page.getByTestId("result-line");
  await expect(line).toContainText("line 1");
  await expect(line.locator("td").nth(1)).toHaveText("4");
  await expect(line.locator("td").nth(2)).toHaveText("2");
  await expect(line).toContainText("People 3/2");
  const [pdf] = await Promise.all([page.waitForEvent("download"), page.getByRole("link", { name: "PDF report" }).click()]);
  expect(pdf.suggestedFilename()).toMatch(/report\.pdf$/);
  await page.getByRole("link", { name: "Video analysis" }).first().click();
  await expect(page.getByTestId("job-row").first()).toContainText("IN 4, OUT 2");
});
