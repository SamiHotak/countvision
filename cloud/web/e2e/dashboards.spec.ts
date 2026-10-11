import { expect, test } from "@playwright/test";

import { createOrg, crossings, fakeAgent, linkFromMail, signUp, uniqueEmail } from "./helpers";

const CSRF = { "X-CountVision": "1" };

test("sample data: dashboard, periods, CSV + PDF download, report email with PDF", async ({ page, request }) => {
  const email = uniqueEmail("dash");
  await signUp(page, "Dora Dash", email);
  await page.goto(await linkFromMail(request, email, "verify-email")); // reports go to confirmed addresses only
  await page.goto("/app");
  await createOrg(page, "Bäckerei Sonne");

  // empty organization: the overview offers sample data
  await page.getByRole("button", { name: "Add sample data" }).click();
  await expect(page.getByRole("heading", { name: "Demo café (sample data)" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("invented sample data")).toBeVisible();
  const total = page.getByTestId("visitors-total");
  await expect(total).not.toHaveText("–");

  // 7 days: day buckets, comparison with last week, chart + table view
  await page.getByRole("button", { name: "7 days" }).click();
  await expect(page).toHaveURL(/from=\d{4}-\d{2}-\d{2}&to=/);
  await expect(page.getByRole("heading", { name: "Visitors per day" })).toBeVisible();
  await expect(page.getByTestId("visitors-change").locator("..")).toContainText(/vs (same time )?last week/);
  const week = Number((await total.textContent())!.replace(/\D/g, ""));
  expect(week).toBeGreaterThan(300);
  await page.getByRole("button", { name: "Show table" }).click();
  await expect(page.getByRole("columnheader", { name: "In", exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "Show chart" }).click();
  await expect(page.getByRole("heading", { name: "When visitors come" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Counter queue" })).toBeVisible();

  // downloads
  const [csv] = await Promise.all([page.waitForEvent("download"), page.getByTestId("download-csv").click()]);
  expect(csv.suggestedFilename()).toMatch(/^countvision_demo-caf-sample-data_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}_lines\.csv$/);
  const [pdf] = await Promise.all([page.waitForEvent("download"), page.getByTestId("download-pdf").click()]);
  expect(pdf.suggestedFilename()).toMatch(/_report\.pdf$/);
  const fs = await import("node:fs/promises");
  const bytes = await fs.readFile((await pdf.path())!);
  expect(bytes.subarray(0, 4).toString()).toBe("%PDF");

  // a weekly report email, sent now
  await page.getByRole("link", { name: "Reports" }).first().click();
  await page.getByRole("button", { name: "New report email" }).first().click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "weekly" }).click();
  await dialog.getByLabel("Send on").selectOption("0");
  await dialog.getByLabel("At", { exact: true }).selectOption("8");
  await dialog.getByLabel(/attach the numbers as a CSV/).check();
  await dialog.getByRole("button", { name: "Create report" }).click();
  const row = page.getByTestId("schedule-row");
  await expect(row).toContainText("Weekly report, Demo café (sample data)");
  await expect(row).toContainText("Every Monday at 08:00");
  await expect(row).toContainText("Dora Dash");
  await row.getByRole("button", { name: "Send now" }).click();
  await expect(page.getByText(/Sent the report for .* to 1 person/)).toBeVisible();
  const mails = await (await request.get(`/api/dev/outbox?to=${encodeURIComponent(email)}`)).json();
  const report = mails.find((m: { subject: string }) => m.subject.includes("weekly report"));
  expect(report.text).toContain("Visitors in:");
  expect(report.attachments.map((a: { filename: string }) => a.filename.split("_").pop())).toEqual(["report.pdf", "lines.csv"]);
  expect(report.attachments[0].size).toBeGreaterThan(5000);

  await row.getByRole("button", { name: "Pause" }).click();
  await expect(row).toContainText("Paused");
});

test("today counts live and lines can be excluded from visitors", async ({ page, request }) => {
  await signUp(page, "Lia Live", uniqueEmail("live"));
  await createOrg(page, "Kiosk Live");
  const orgId = page.url().split("/orgs/")[1].split("/")[0];
  const site = await (await page.request.post(`/api/orgs/${orgId}/sites`, {
    headers: CSRF, data: { name: "Kiosk", timezone: "Europe/Berlin" } })).json();
  const code = await (await page.request.post(`/api/orgs/${orgId}/pairing-codes`, {
    headers: CSRF, data: { site_id: site.id, device_name: "Pi" } })).json();
  const token = await fakeAgent(request, code.code, `edge-live-${Date.now()}`); // 7 in, 3 out

  await page.goto(`/app/orgs/${orgId}/sites/${site.id}`);
  const total = page.getByTestId("visitors-total");
  await expect(total).toHaveText("7");
  await expect(page.getByTestId("live-state")).toContainText("Live");
  await expect(page.getByText("Inside now (about)")).toBeVisible();

  // a new crossing appears without reloading the page
  const send = (batch: string, events: unknown[]) => request.post("/api/device/ingest", {
    headers: { Authorization: `Bearer ${token}` }, data: { batch_id: batch, events } });
  expect((await send(`live-${Date.now()}`, crossings("door", "entrance", 2, "in", "more"))).ok()).toBeTruthy();
  await expect(total).toHaveText("9", { timeout: 10_000 });

  // a second line (an aisle) is not a visitor line
  expect((await send(`aisle-${Date.now()}`, crossings("door", "aisle", 5, "in", "aisle"))).ok()).toBeTruthy();
  await expect(total).toHaveText("14", { timeout: 10_000 });
  await page.getByLabel("aisle counts as visitors").uncheck();
  await expect(total).toHaveText("9");
  await expect(page.getByTestId("line-row").filter({ hasText: "aisle" })).toContainText("No");

  // the overview shows the same number
  await page.getByRole("link", { name: "Overview" }).first().click();
  await expect(page.getByTestId("site-today")).toContainText("9");
});
