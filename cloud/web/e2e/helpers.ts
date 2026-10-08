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
