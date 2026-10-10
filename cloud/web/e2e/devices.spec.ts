import { expect, test } from "@playwright/test";

import { createOrg, fakeAgent, signUp, uniqueEmail } from "./helpers";

test("add a site, pair a device with a code, see its camera and counts, remove it", async ({ page, request }) => {
  await signUp(page, "Dana Device", uniqueEmail("devices"));
  await createOrg(page, "Kiosk am Markt");

  // Devices without a site: the page sends you to Sites first.
  await page.getByRole("link", { name: "Devices" }).first().click();
  await page.getByRole("link", { name: "Add a site first" }).click();
  await page.getByRole("button", { name: "Add your first site" }).click();
  await page.getByLabel("Name").fill("Markt 5");
  await page.getByRole("dialog").getByRole("button", { name: "Add site" }).click();
  await expect(page.getByTestId("site-row")).toContainText("Markt 5");

  await page.getByRole("link", { name: "Devices" }).first().click();
  await page.getByRole("button", { name: "Add your first device" }).click();
  await page.getByLabel("Device name").fill("Mini PC Kasse");
  await page.getByRole("button", { name: "Create pairing code" }).click();
  const code = (await page.getByTestId("pairing-code").textContent())!.trim();
  expect(code).toMatch(/^[A-Z2-9]{4}-[A-Z2-9]{4}$/);
  await expect(page.getByText(`--code ${code}`).first()).toBeVisible();
  await page.getByRole("tab", { name: "Docker" }).click();
  await expect(page.getByText(/docker compose run --rm countvision pair --url http:\/\/host\.docker\.internal/)).toBeVisible();

  await fakeAgent(request, code);
  await expect(page.getByText("Mini PC Kasse is connected.")).toBeVisible({ timeout: 10_000 });
  await page.getByRole("link", { name: "Open device" }).click();

  await expect(page.getByRole("heading", { name: "Mini PC Kasse" })).toBeVisible();
  await expect(page.getByTestId("device-state")).toContainText("Online");
  const cam = page.getByTestId("camera-row");
  await expect(cam).toContainText("Entrance");
  await expect(cam).toContainText("Counting, 9.8 FPS");
  await expect(cam.getByRole("row", { name: /entrance/ })).toContainText("7");
  await expect(page.getByText("yolox_tiny")).toBeVisible();

  // Overview shows the real numbers now
  await page.getByRole("link", { name: "Overview" }).first().click();
  await expect(page.getByText("cameras counting")).toBeVisible();
  await expect(page.getByRole("link", { name: /1\/1\s*cameras counting/ })).toBeVisible();

  // Remove the device (owner = admin)
  await page.getByRole("link", { name: "Devices" }).first().click();
  await page.getByTestId("device-row").click();
  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Remove device" }).click();
  await expect(page.getByText("This device was removed.")).toBeVisible();
  await expect(page.getByTestId("device-state")).toContainText("Removed");
});
