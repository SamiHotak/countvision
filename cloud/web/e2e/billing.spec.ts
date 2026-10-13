import { expect, test } from "@playwright/test";

import { createOrg, signUp, uniqueEmail } from "./helpers";

// Billing with the FAKE Stripe of the API (CV_STRIPE_API_URL=http://127.0.0.1:8001/api/dev/stripe).
// Real Stripe is never called in tests.

const CSRF = { "X-CountVision": "1" };

test("trial -> Stripe Checkout -> plan active -> portal downgrade -> admin grant", async ({ page, request }) => {
  const email = uniqueEmail("billing");
  await signUp(page, "Bea Billing", email);
  const orgName = `Kiosk ${Date.now() % 100000}`;
  await createOrg(page, orgName);
  const orgId = page.url().split("/app/orgs/")[1].split("/")[0];

  // the free trial
  await page.getByRole("link", { name: /Billing/ }).first().click();
  await expect(page.getByTestId("plan-name")).toHaveText("Free trial");
  await expect(page.getByTestId("plan-state")).toHaveText("14 days left");
  await expect(page.getByText(/Stripe test mode/)).toBeVisible();
  await expect(page.getByTestId("camera-usage")).toHaveText("0 of 2 counting");
  await expect(page.locator("[data-testid=plan-starter], [data-testid=plan-shop], [data-testid=plan-business]")).toHaveCount(3);

  // pay for Shop on the (fake) Stripe page, come back
  await page.getByTestId("choose-shop").click();
  await expect(page.getByText("FAKE Stripe")).toBeVisible();
  await expect(page.getByText(/Free until .* then paid monthly/)).toBeVisible(); // rest of the trial carried over
  await page.getByTestId("fake-pay").click();
  await expect(page).toHaveURL(new RegExp(`/app/orgs/${orgId}/billing$`));
  await expect(page.getByTestId("billing-notice")).toContainText("Your plan is now Shop");
  await expect(page.getByTestId("plan-name")).toHaveText("Shop");
  await expect(page.getByTestId("plan-state")).toHaveText("Active");
  await expect(page.getByTestId("camera-usage")).toHaveText("0 of 4 counting");
  await expect(page.getByTestId("plan-shop")).toContainText("Your plan");

  // Manage billing (customer portal): switch to Starter
  await page.getByTestId("manage-billing").click();
  await page.getByTestId("fake-switch-starter").click();
  await page.getByTestId("fake-return").click();
  await expect(page.getByTestId("plan-name")).toHaveText("Starter");

  // the customer's activity log shows it
  await page.goto(`/app/orgs/${orgId}/settings`);
  await expect(page.getByText(/plan changed: starter/).first()).toBeVisible();

  // the operator's admin page: grant Business for a pilot
  expect((await (await request.post("/api/dev/make-admin", { headers: CSRF, data: { email } })).json()).ok).toBe(true);
  await page.goto("/app/admin");
  await expect(page.getByRole("heading", { name: "Admin" })).toBeVisible();
  await page.getByLabel("Search organizations").fill(orgName);
  const row = page.locator("tr", { hasText: orgName });
  await expect(row).toContainText("Starter");
  await row.getByRole("button", { name: "Open" }).click();
  const dialog = page.getByTestId("admin-org-dialog");
  await dialog.getByLabel("Plan", { exact: true }).selectOption("business");
  await dialog.getByLabel(/Note/).fill("Pilot Kiosk");
  await dialog.getByTestId("admin-grant").click();
  await expect(dialog.getByText("Plan granted.")).toBeVisible();
  await expect(dialog.getByText(/Granted business until/)).toBeVisible();
  await page.keyboard.press("Escape");
  await page.goto(`/app/orgs/${orgId}/billing`);
  await expect(page.getByTestId("plan-name")).toHaveText("Business");
  await expect(page.getByTestId("plan-state")).toHaveText("Granted");
  await expect(page.getByText(/Pilot Kiosk/)).toBeVisible();
});

test("an ended trial shows 'choose a plan' but keeps billing and members open", async ({ page, request }) => {
  await signUp(page, "Tim Trial", uniqueEmail("trial"));
  await createOrg(page, "Bäckerei Ende");
  const orgId = page.url().split("/app/orgs/")[1].split("/")[0];
  await request.post("/api/dev/billing/end-trial", { headers: CSRF, data: { org_id: orgId } });

  await page.goto(`/app/orgs/${orgId}/sites`);
  await expect(page.getByTestId("plan-gate")).toContainText("Your free trial has ended");
  await page.getByRole("link", { name: "See plans" }).click();
  await expect(page.getByTestId("plan-state")).toHaveText("Trial ended");
  await expect(page.getByTestId("plan-gate")).toHaveCount(0);
  await page.goto(`/app/orgs/${orgId}/members`);
  await expect(page.getByTestId("plan-gate")).toHaveCount(0);
  // the API agrees
  const dash = await page.request.get(`/api/orgs/${orgId}/dashboard`);
  expect(dash.status()).toBe(402);
});
