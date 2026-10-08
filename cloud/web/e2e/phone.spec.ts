import { expect, test } from "@playwright/test";

import { createOrg, signUp, uniqueEmail } from "./helpers";

test("phone: menu works and nothing scrolls sideways", async ({ page }) => {
  await signUp(page, "Phone User", uniqueEmail("phone"));
  await createOrg(page, "Late Night Kiosk");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.getByRole("button", { name: "Menu" }).click();
  await page.getByRole("link", { name: "Members" }).click();
  await expect(page.getByRole("heading", { name: "Members" })).toBeVisible();
  await expect(page.getByTestId("member-row")).toContainText("Phone User");
});
