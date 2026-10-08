import { defineConfig, devices } from "@playwright/test";

// Browser tests of the main flows. They need the API (memory email backend) and the web app:
//   API:  CV_EMAIL_BACKEND=memory CV_EMAIL_DELIVERY=sync countvision-cloud serve --port 8001
//   Web:  npm run build && npm start
// Then: npx playwright test   (BASE_URL changes the address, default http://localhost:3000)
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 7_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] }, testIgnore: /phone\.spec\.ts/ },
    { name: "phone", use: { ...devices["Pixel 7"] }, testMatch: /phone\.spec\.ts/ },
  ],
});
