import { expect, test, type Page } from "@playwright/test";

// CountVision Local from the first start to a counted walk, a licence key, an on-screen alert,
// an invitation link and a backup + restore. Needs a fresh installation (see playwright.local.config.ts).

const PASSWORD = "a-long-local-password";
const EMAIL = "owner@shop.example";
const MJPEG = process.env.CV_E2E_MJPEG ?? "http://127.0.0.1:8090/video";
const LICENCE = process.env.CV_E2E_LICENCE ?? "";

async function dragOnStage(page: Page, from: [number, number], to: [number, number]) {
  const box = (await page.getByTestId("stage").boundingBox())!;
  await page.mouse.move(box.x + from[0] * box.width, box.y + from[1] * box.height);
  await page.mouse.down();
  await page.mouse.move(box.x + ((from[0] + to[0]) / 2) * box.width, box.y + ((from[1] + to[1]) / 2) * box.height, { steps: 4 });
  await page.mouse.move(box.x + to[0] * box.width, box.y + to[1] * box.height, { steps: 4 });
  await page.mouse.up();
}

test("first start, camera, counting line, live count, licence, alerts, team, backup", async ({ page, request }) => {
  // nothing set up yet: every way in leads to the wizard; no public sign-up
  await page.goto("/");
  await expect(page).toHaveURL(/\/setup$/);
  await page.goto("/signup");
  await expect(page).toHaveURL(/\/setup$/);
  await expect(page.getByRole("heading", { name: "You and your business" })).toBeVisible();

  // step 1: owner, business, location
  await page.getByLabel("Your name").fill("Olga Owner");
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Password again").fill("something-else-1");
  await page.getByLabel("Business name").fill("Café Sonne");
  await page.getByLabel("Location").fill("Main street");
  await page.getByTestId("setup-submit").click();
  await expect(page.getByText("The two passwords are different.")).toBeVisible();
  await page.getByLabel("Password again").fill(PASSWORD);
  await page.getByTestId("setup-submit").click();

  // step 2: the first camera (a phone / MJPEG stream); the built-in agent starts it within seconds
  await expect(page.getByRole("heading", { name: "Your first camera" })).toBeVisible();
  await page.getByRole("radio", { name: "Phone / stream" }).click();
  await page.getByLabel("Camera name").fill("Entrance");
  await page.getByLabel("Stream address (HTTP / MJPEG)").fill("ftp://nope");
  await page.getByTestId("setup-add-camera").click();
  await expect(page.getByText(/must start with http:\/\//)).toBeVisible();
  await page.getByLabel("Stream address (HTTP / MJPEG)").fill(MJPEG);
  await page.getByTestId("setup-add-camera").click();
  await expect(page.getByTestId("camera-state")).toHaveAttribute("data-state", "running", { timeout: 60_000 });
  await expect(page.getByTestId("camera-state")).toContainText("Counting");
  // the live camera picture (people blurred by default) appears as soon as it counts
  const wizardLive = page.getByTestId("live-picture");
  await expect(wizardLive).toHaveAttribute("data-status", "live", { timeout: 20_000 });
  await expect(wizardLive).toHaveAttribute("data-mode", "blur");
  await expect(wizardLive).toContainText("Live, people blurred");
  await page.getByTestId("setup-camera-next").click();

  // step 3 -> the camera editor: draw the door line across the walkway and save
  await expect(page.getByRole("heading", { name: "The counting line" })).toBeVisible();
  await page.getByTestId("setup-draw-line").click();
  await expect(page.getByRole("heading", { name: "Entrance" })).toBeVisible();
  await expect(page.getByTestId("editor-live")).toContainText("Live, people blurred"); // live, no snapshot
  await expect(page.getByRole("button", { name: "Take snapshot" })).toHaveCount(0);
  await expect(page.getByRole("img", { name: "Live picture of Entrance" })).toBeVisible();
  await page.getByRole("button", { name: "Line", exact: true }).click();
  await dragOnStage(page, [0.1, 0.5], [0.9, 0.5]);
  await expect(page.getByTestId("shape-row").filter({ hasText: "line 1" })).toBeVisible();
  await page.getByRole("button", { name: "Save and send to device" }).click();
  await expect(page.getByTestId("sync-state")).toContainText("Version 1 is running on the device", { timeout: 15_000 });

  // people walk through the demo scene: the count goes up live on the device page
  await page.goto("/app");
  await expect(page.getByTestId("business-name")).toHaveText("Café Sonne");
  const nav = page.getByRole("navigation", { name: "Main" });
  await nav.getByRole("link", { name: "Devices" }).click();
  await expect(page.getByRole("button", { name: "Add device" })).toHaveCount(0); // no pairing in local
  await page.getByTestId("device-row").filter({ hasText: "This computer" }).click();
  const row = page.getByTestId("camera-row").filter({ hasText: "Entrance" });
  await expect(row.getByTestId("camera-source")).toHaveText(MJPEG);
  await expect.poll(async () => Number(await row.getByTestId("count-in").first().innerText()),
    { timeout: 60_000, message: "nobody counted" }).toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: "Remove device" })).toHaveCount(0); // the built-in device stays
  await expect(row.getByTestId("live-picture")).toHaveAttribute("data-status", "live", { timeout: 20_000 });

  // the overview shows a live tile of the camera
  await nav.getByRole("link", { name: "Overview" }).click();
  await expect(page.getByTestId("camera-tile").getByTestId("live-picture"))
    .toHaveAttribute("data-status", "live", { timeout: 20_000 });

  // Settings -> Privacy: the owner switches the live picture to full, then off (counting goes on)
  await nav.getByRole("link", { name: "Settings" }).click();
  const privacy = page.getByTestId("privacy-settings");
  await privacy.getByText("Full picture", { exact: true }).click();
  await privacy.getByRole("button", { name: "Save privacy setting" }).click();
  await expect(privacy.getByText(/Saved/)).toBeVisible();
  await nav.getByRole("link", { name: "Devices" }).click();
  await page.getByTestId("device-row").filter({ hasText: "This computer" }).click();
  await expect(row.getByTestId("live-picture")).toHaveAttribute("data-mode", "full");
  await expect(row.getByTestId("live-picture")).toHaveAttribute("data-status", "live", { timeout: 20_000 });
  await nav.getByRole("link", { name: "Settings" }).click();
  await expect(page.getByText(/set the live picture to full picture/)).toBeVisible(); // in the activity log
  await expect(page.getByText(/watched the live picture of the camera "Entrance"/).first()).toBeVisible();
  await privacy.getByText("Off", { exact: true }).click();
  await privacy.getByRole("button", { name: "Save privacy setting" }).click();
  await expect(privacy.getByText(/Saved/)).toBeVisible();
  await nav.getByRole("link", { name: "Devices" }).click();
  await page.getByTestId("device-row").filter({ hasText: "This computer" }).click();
  await expect(row.getByTestId("live-off")).toContainText("switched off");
  await expect(row).toContainText("Counting");

  // cloud-only parts are hidden; the licence page shows the trial
  await expect(nav.getByRole("link", { name: /^Billing/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Admin" })).toHaveCount(0);
  await nav.getByRole("link", { name: /Licence/ }).click();
  await expect(page.getByTestId("licence-state")).toContainText("Free trial, 14 days left");
  if (LICENCE) {
    await page.getByTestId("licence-key").fill("CVL1-not-a-real-key.abc");
    await page.getByTestId("licence-save").click();
    await expect(page.getByText("This licence key is not valid.")).toBeVisible();
    await page.getByTestId("licence-key").fill(LICENCE);
    await page.getByTestId("licence-save").click();
    await expect(page.getByTestId("licence-state")).toContainText("Licensed for 4 cameras");
    await expect(page.getByTestId("licence-facts")).toContainText("Café Sonne GmbH");
  }

  // an alert that only shows on screen; Telegram is not offered
  await nav.getByRole("link", { name: "Alerts" }).click();
  await page.getByRole("button", { name: "Add channel" }).click();
  await expect(page.getByRole("radio", { name: "Telegram" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "New alert" }).click();
  const dialog = page.getByRole("dialog");
  await expect(async () => {
    await dialog.getByText("Camera not counting", { exact: true }).click();
    await expect(dialog.getByRole("radio", { name: /Camera not counting/ })).toBeChecked({ timeout: 1_000 });
  }).toPass();
  await expect(page.getByText(/Every alert also shows on screen/)).toBeVisible();
  await dialog.getByRole("button", { name: "Create alert" }).click();
  await expect(page.getByTestId("rule-row")).toContainText("To the screen only");
  await page.getByTestId("alert-bell").filter({ visible: true }).click();
  await expect(page.getByText("No alert on now")).toBeVisible();
  await page.keyboard.press("Escape");

  // a team member: the invitation link is shown (no mail server)
  await nav.getByRole("link", { name: "Members" }).click();
  await page.getByRole("button", { name: "Invite someone" }).click();
  await page.getByLabel("Email address").fill("tom@shop.example");
  await page.getByRole("button", { name: "Create invitation link" }).click();
  await expect(page.getByTestId("invite-link")).toHaveValue(/\/invite\//);
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("invite-row")).toContainText("tom@shop.example");

  // backup, a change, restore -> the change is gone, log in again
  await nav.getByRole("link", { name: "Settings" }).click();
  await page.getByTestId("backup-create").click();
  await expect(page.getByText(/Backup written: CountVision-backup-/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("backup-row")).toHaveCount(1);
  const orgUrl = page.url().replace(/\/settings$/, "");
  const sites = await page.request.post(`${orgUrl.replace("/app/", "/api/")}/sites`, {
    headers: { "X-CountVision": "1" }, data: { name: "Second shop" } });
  expect(sites.status()).toBe(201);
  await page.getByTestId("backup-restore").first().click();
  await page.getByLabel('Type "Café Sonne" to confirm').fill("Café Sonne");
  await page.getByTestId("backup-restore-confirm").click();
  await expect(page).toHaveURL(/\/login/, { timeout: 60_000 });
  await expect(page.getByTestId("no-email-reset")).toBeVisible();
  await page.getByLabel("Email").fill(EMAIL);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByTestId("business-name")).toHaveText("Café Sonne");
  const main = page.getByRole("main");
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Sites" }).click();
  await expect(main.getByRole("heading", { name: "Sites", level: 1 })).toBeVisible();
  await expect(main.getByText("Main street").first()).toBeVisible();
  await expect(main.getByText("Second shop")).toHaveCount(0);
  // the camera is back and counting again (the agent got its camera list from the restored data)
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Devices" }).click();
  await page.getByTestId("device-row").filter({ hasText: "This computer" }).click();
  await expect(page.getByTestId("camera-row").filter({ hasText: "Entrance" })).toContainText("Counting", { timeout: 30_000 });
});
