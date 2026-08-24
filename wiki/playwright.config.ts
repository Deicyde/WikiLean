import { defineConfig, devices } from "@playwright/test";

const port = Number.parseInt(process.env.WIKILEAN_E2E_PORT ?? "4173", 10);
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "list",
  outputDir: "test-results",
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "exec tsx e2e/server.ts",
    url: `${baseURL}/Test_Article`,
    reuseExistingServer: false,
    timeout: 30_000,
    env: { WIKILEAN_E2E_PORT: String(port) },
  },
});
