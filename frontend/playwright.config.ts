import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

/**
 * End to end against the real pipeline: `repoatlas serve` on the index this
 * environment configures (yours locally; in CI, a scratch workspace indexed
 * from tests/fixtures/repos via REPOATLAS_CONFIG and REPOATLAS_DB), and a
 * production build of the explorer reading its connection file. Nothing is
 * mocked except where a test says so.
 */
const API_PORT = 7337;
const APP_PORT = 7338;
const APP = import.meta.dirname;
const CONNECTION_FILE = path.join(APP, "e2e/.state/api.json");
const PYTHON = process.env.REPOATLAS_PYTHON ?? path.join(APP, "../.venv/bin/python");

export default defineConfig({
  testDir: "e2e",
  // one API process answers every request on one thread; keep the load honest
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  reporter: [["list"]],
  timeout: 60_000,
  expect: { timeout: 30_000 },
  use: {
    baseURL: `http://localhost:${APP_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1536, height: 960 } },
    },
  ],
  webServer: [
    {
      command: `"${PYTHON}" -m repoatlas.cli.main serve --port ${API_PORT}`,
      cwd: path.join(APP, ".."),
      // an unauthenticated GET is refused with 401, which is how it says it is up
      url: `http://127.0.0.1:${API_PORT}/v1`,
      env: { REPOATLAS_API_CONNECTION_FILE: CONNECTION_FILE },
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `pnpm exec next build && pnpm exec next start --hostname 127.0.0.1 --port ${APP_PORT}`,
      cwd: APP,
      url: `http://localhost:${APP_PORT}`,
      env: { REPOATLAS_API_CONNECTION_FILE: CONNECTION_FILE, RA_DIST_DIR: ".next-e2e" },
      reuseExistingServer: false,
      timeout: 300_000,
    },
  ],
});
