import { defineConfig, devices } from "@playwright/test";

// Browser test of CountVision Local (CV_EDITION=local): a FRESH installation (no account yet), the
// API without Redis, the real edge agent started as soon as the setup wizard wrote its files, and a
// camera stream (MJPEG of the demo scene). See .github/workflows/ci.yml job "local".
//   npx playwright test -c playwright.local.config.ts
// Env: CV_E2E_MJPEG (camera stream), CV_E2E_LICENCE (a licence key signed for the API's public key).
export default defineConfig({
  testDir: "./e2e-local",
  timeout: 240_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0, // the test sets the installation up once: a retry would find it set up already
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "desktop", use: { ...devices["Desktop Chrome"] } }],
});
