import { expect, test } from "@playwright/test";

import { createOrg, linkFromMail, PASSWORD, signUp, uniqueEmail } from "./helpers";

test("sign up, confirm email, create organization, log out and in", async ({ page, request }) => {
  const email = uniqueEmail("owner");
  await page.goto("/app");
  await expect(page).toHaveURL(/\/login\?next=%2Fapp/);

  await signUp(page, "Olga Owner", email);
  await createOrg(page, "Café Sonne");
  await expect(page.getByText("Please confirm your email address")).toBeVisible();

  await page.goto(await linkFromMail(request, email, "verify-email"));
  await expect(page.getByText("Your email address is confirmed.")).toBeVisible();
  await page.getByRole("link", { name: "Continue to CountVision" }).click();
  await expect(page.getByRole("heading", { name: "Café Sonne" })).toBeVisible();
  await expect(page.getByText("Please confirm your email address")).toHaveCount(0);

  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("wrong-password-123");
  await page.getByRole("button", { name: "Log in" }).click();
  // (Next.js also has an invisible role="alert" route announcer, so filter by text.)
  await expect(page.getByRole("alert").filter({ hasText: "Wrong email or password." })).toBeVisible();
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("heading", { name: "Café Sonne" })).toBeVisible();
});

test("forgot password, reset, log in with the new one", async ({ page, request }) => {
  const email = uniqueEmail("forgot");
  await signUp(page, "Fritz", email);
  await createOrg(page, "Kiosk Nord");
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page.getByRole("heading", { name: "Log in" })).toBeVisible();

  await page.getByRole("link", { name: "Forgot your password?" }).click();
  await expect(page.getByRole("heading", { name: "Reset your password" })).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByRole("button", { name: "Send reset link" }).click();
  await expect(page.getByText(/If an account exists/)).toBeVisible();

  await page.goto(await linkFromMail(request, email, "reset-password"));
  await page.getByLabel("New password", { exact: true }).fill("my-brand-new-password");
  await page.getByLabel("Repeat the new password").fill("my-brand-new-password");
  await page.getByRole("button", { name: "Save new password" }).click();
  await expect(page.getByText("Your new password is saved.")).toBeVisible();

  await page.getByRole("link", { name: "Log in with the new password" }).click();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("my-brand-new-password");
  await page.getByRole("button", { name: "Log in" }).click();
  await expect(page.getByRole("heading", { name: "Kiosk Nord" })).toBeVisible();
});

test("signup shows field errors", async ({ page }) => {
  await page.goto("/signup");
  await page.getByLabel("Your name").fill("Short");
  await page.getByLabel("Email").fill(uniqueEmail("weak"));
  await page.getByLabel("Password").fill("short");
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page.locator("#password-error")).toContainText("at least 10 characters");
});
