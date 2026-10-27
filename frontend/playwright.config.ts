import { randomBytes } from "node:crypto";

import { defineConfig, devices } from "@playwright/test";

// One end-to-end journey (required_test.md s19) against a fresh local database, the real
// console API and a production build of the app, all on loopback. The approver token is
// generated per run (never stored); workers re-read this file, so it travels in the env.
process.env.PRAXIS_E2E_TOKEN ??= randomBytes(24).toString("base64url");
const token = process.env.PRAXIS_E2E_TOKEN;
const pgPort = process.env.PG_PORT ?? "55432";
export const E2E_DB = `postgresql+psycopg://praxis@127.0.0.1:${pgPort}/praxis_console_e2e`;
const API_PORT = 8101;
const WEB_PORT = 3101;

export default defineConfig({
  testDir: "e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `.venv/bin/uvicorn --factory praxis.console.app:create_console_app --host 127.0.0.1 --port ${API_PORT}`,
      cwd: "..",
      url: `http://127.0.0.1:${API_PORT}/v1/console/system`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        PRAXIS_DATABASE_URL: E2E_DB,
        PRAXIS_WAREHOUSE_PATH: "data/e2e/absent.duckdb", // no warehouse: panels say so
        PRAXIS_CONSOLE_APPROVERS: `e2e-approver=${token}`,
        PRAXIS_LOG_LEVEL: "WARNING",
      },
    },
    {
      command: `npm run build && npx next start --hostname 127.0.0.1 --port ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}/system`,
      reuseExistingServer: false,
      timeout: 240_000,
      env: { PRAXIS_CONSOLE_API_URL: `http://127.0.0.1:${API_PORT}` },
    },
  ],
});
