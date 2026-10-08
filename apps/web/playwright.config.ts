import { defineConfig } from "@playwright/test";

// Start a seeded control plane first (see docs/ui/redesign-verification.md).
// WEB_E2E_URL can point at /app to exercise the production build and its CSP.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  use: {
    baseURL: process.env.WEB_E2E_URL ?? "http://localhost:5173",
    browserName: "chromium",
    trace: "retain-on-failure",
  },
  webServer: process.env.WEB_E2E_URL
    ? undefined
    : {
        command: "npm run dev -- --host 127.0.0.1",
        url: "http://localhost:5173",
        reuseExistingServer: true,
      },
});
