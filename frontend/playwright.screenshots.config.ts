import { defineConfig, devices } from "@playwright/test";

// `make screenshots`: снимает docs/img/*.png против поднятого compose.
export default defineConfig({
  testDir: "./screenshots",
  timeout: 420_000,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    viewport: { width: 1280, height: 800 },
    deviceScaleFactor: 1,
    locale: "ru-RU",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } }],
});
