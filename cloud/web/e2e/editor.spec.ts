import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { createOrg, crossings, fakeAgent, signUp, uniqueEmail } from "./helpers";

// A small real JPEG (160 x 90), standing in for the device's pixelated snapshot.
const JPEG = Buffer.from(
  "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNSMj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAARCABaAKADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDlqKKKwNAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiirOnf8f0f4/yNDArUV0tFRzlcpzVFdLRRzhynNUV0tFHOHKc1RXS0Uc4cpzVFdLRRzhynNUV0tFHOHKc1RVnUf+P6T8P5Cq1WiQooooAKKKKACrOnf8f0f4/yNVqktpfInWXbu254zjtQ9gOhorM/tb/ph/4//wDWo/tb/ph/4/8A/WrLlZd0adFZn9rf9MP/AB//AOtR/a3/AEw/8f8A/rUcrC6NOisz+1v+mH/j/wD9aj+1v+mH/j//ANajlYXRp0Vmf2t/0w/8f/8ArUf2t/0w/wDH/wD61HKwujTorM/tb/ph/wCP/wD1qP7W/wCmH/j/AP8AWo5WF0adFZn9rf8ATD/x/wD+tR/a3/TD/wAf/wDrUcrC6K2o/wDH9J+H8hVapLmXz52l27d2OM57VHWq2ICiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAP/2Q==",
  "base64",
);

/** Site + pairing code through the API (the UI for these is tested in devices.spec.ts). */
async function pairDevice(page: Page, request: APIRequestContext, edgeId: string): Promise<{ orgId: string; token: string }> {
  const orgId = page.url().split("/app/orgs/")[1].split("/")[0];
  const headers = { "X-CountVision": "1" };
  const site = await (await page.request.post(`/api/orgs/${orgId}/sites`, {
    headers, data: { name: "Bäckerei", timezone: "Europe/Berlin" },
  })).json();
  const code = await (await page.request.post(`/api/orgs/${orgId}/pairing-codes`, {
    headers, data: { site_id: site.id, device_name: "Mini PC" },
  })).json();
  const token = await fakeAgent(request, code.code, edgeId);
  return { orgId, token };
}

async function ingest(request: APIRequestContext, token: string, body: Record<string, unknown>) {
  const resp = await request.post("/api/device/ingest", {
    headers: { Authorization: `Bearer ${token}` },
    data: { batch_id: `b-${Date.now()}-${Math.random()}`, agent_version: "0.6.0", ...body },
  });
  expect(resp.ok()).toBeTruthy();
}

async function poll(request: APIRequestContext, token: string) {
  const resp = await request.get("/api/device/poll?rev=-1&wait=0", { headers: { Authorization: `Bearer ${token}` } });
  expect(resp.ok()).toBeTruthy();
  return resp.json();
}

/** Drag on the stage from / to positions given in 0..1 of its size. */
async function dragOnStage(page: Page, from: [number, number], to: [number, number]) {
  const box = (await page.getByTestId("stage").boundingBox())!;
  await page.mouse.move(box.x + from[0] * box.width, box.y + from[1] * box.height);
  await page.mouse.down();
  await page.mouse.move(box.x + ((from[0] + to[0]) / 2) * box.width, box.y + ((from[1] + to[1]) / 2) * box.height, { steps: 4 });
  await page.mouse.move(box.x + to[0] * box.width, box.y + to[1] * box.height, { steps: 4 });
  await page.mouse.up();
}

async function clickStage(page: Page, at: [number, number]) {
  const box = (await page.getByTestId("stage").boundingBox())!;
  await page.mouse.click(box.x + at[0] * box.width, box.y + at[1] * box.height);
}

test("draw lines and a zone in the browser, the device gets them, counts arrive live, snapshot", async ({ page, request }) => {
  await signUp(page, "Ezra Editor", uniqueEmail("editor"));
  await createOrg(page, "Bäckerei Korn");
  const { orgId, token } = await pairDevice(page, request, "edge-editor");

  // device page -> editor
  await page.goto(`/app/orgs/${orgId}/devices`);
  await page.getByTestId("device-row").click();
  await expect(page.getByTestId("camera-row")).toContainText("Entrance");
  await expect(page.getByTestId("live-state")).toContainText("Live");
  await page.getByTestId("edit-lines").click();

  await expect(page.getByRole("heading", { name: "Entrance" })).toBeVisible();
  await expect(page.getByTestId("sync-state")).toContainText("come from the device's own config");
  const entrance = page.getByTestId("shape-row").filter({ hasText: "entrance" });
  await expect(entrance.getByTestId("count-in")).toHaveText("7");
  await expect(entrance.getByTestId("count-out")).toHaveText("3");
  await expect(page.getByTestId("live-state")).toContainText("Live");

  // a crossing on the device shows up without reloading (SSE through the web server)
  await ingest(request, token, { events: crossings("door", "entrance", 2, "in", "live") });
  await expect(entrance.getByTestId("count-in")).toHaveText("9", { timeout: 5_000 });

  // draw a new line, rename it, flip it; undo / redo
  await page.getByRole("button", { name: "Line", exact: true }).click();
  await dragOnStage(page, [0.15, 0.3], [0.85, 0.32]);
  const line1 = page.getByTestId("shape-row").filter({ hasText: "line 1" });
  await expect(line1).toBeVisible();
  await page.keyboard.press("Control+z");
  await expect(line1).toHaveCount(0);
  await page.keyboard.press("Control+Shift+z");
  await expect(line1).toBeVisible();
  await line1.click();
  await page.getByLabel("Line name").fill("exit");
  await page.getByLabel("Line name").press("Enter");
  await page.getByRole("button", { name: "Flip IN direction" }).click();
  await expect(page.getByTestId("shape-row").filter({ hasText: "exit" })).toBeVisible();

  // a zone with 4 corners (closed by clicking the first corner again)
  await page.getByRole("button", { name: "Zone", exact: true }).click();
  await clickStage(page, [0.1, 0.65]);
  await clickStage(page, [0.4, 0.65]);
  await clickStage(page, [0.4, 0.9]);
  await clickStage(page, [0.1, 0.9]);
  await clickStage(page, [0.1, 0.65]);
  await expect(page.getByTestId("shape-row").filter({ hasText: "zone 1" })).toBeVisible();
  await page.getByRole("radio", { name: "Queue" }).click();

  // what and when
  await page.getByLabel("Cars").check();
  await page.getByRole("radio", { name: "Only at these times" }).click();
  await page.getByRole("button", { name: "Sun" }).click(); // Mon-Sat + Sun
  await expect(page.getByTestId("sync-state")).toContainText("Unsaved changes");

  await page.getByRole("button", { name: "Save and send to device" }).click();
  await expect(page.getByTestId("sync-state")).toContainText(/Sending version 1|Version 1 saved/);

  // the device's long-poll returns the new config at once
  const state = await poll(request, token);
  expect(state.changed).toBe(true);
  const cam = state.cameras.find((c: { id: string }) => c.id === "door");
  expect(cam.version).toBe(1);
  expect(cam.timezone).toBe("Europe/Berlin");
  expect(cam.config.lines.map((l: { name: string }) => l.name)).toEqual(["entrance", "exit"]);
  expect(cam.config.lines[1].in_direction).toBe("to_left");
  expect(cam.config.zones[0]).toMatchObject({ name: "zone 1", kind: "queue" });
  expect(cam.config.zones[0].polygon).toHaveLength(4);
  expect(cam.config.classes).toEqual(["person", "car"]);
  expect(cam.config.schedule).toEqual({ days: [0, 1, 2, 3, 4, 5, 6], start: "08:00", end: "20:00" });

  // the device applies it and says so with its next upload -> live "running" message
  await ingest(request, token, {
    cameras: [{ id: "door", name: "Entrance", state: "running", connected: true, fps: 9.7, lines: ["entrance", "exit"],
      zones: ["zone 1"], config: cam.config, config_version: 1, snapshots_allowed: true, frame: [1280, 720] }],
  });
  await expect(page.getByTestId("sync-state")).toContainText("Version 1 is running on the device", { timeout: 5_000 });

  // snapshot on request: the device answers the poll with a JPEG
  await page.getByRole("button", { name: "Take snapshot" }).click();
  const req = (await poll(request, token)).snapshots[0];
  expect(req.camera_id).toBe("door");
  const sent = await request.post(`/api/device/snapshots/${req.request_id}`, {
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "image/jpeg" }, data: JPEG,
  });
  expect(sent.status()).toBe(204);
  const img = page.getByRole("img", { name: "Camera snapshot, people pixelated" });
  await expect(img).toBeVisible({ timeout: 6_000 });
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(160);
  await expect(page.getByTestId("hint")).toContainText("not stored");

  // reload: the saved config is what the editor shows
  await page.reload();
  await expect(page.getByTestId("shape-row")).toHaveCount(3);
  await expect(page.getByLabel("Cars")).toBeChecked();
});
