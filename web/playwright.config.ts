import { defineConfig } from "@playwright/test";

const port = process.env.REPOMIND_E2E_PORT ?? "7322";
const python = process.env.REPOMIND_PYTHON ?? (process.platform === "win32" ? "../.venv/Scripts/python.exe" : "../.venv/bin/python");

// An installed browser is used, so nothing has to be downloaded. Set REPOMIND_E2E_BROWSER=chrome to change it.
export default defineConfig({
  testDir: "e2e",
  // The tests share one server, and they change its model state and its list of repositories.
  workers: 1,
  fullyParallel: false,
  timeout: 45_000,
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    channel: process.env.REPOMIND_E2E_BROWSER ?? "msedge",
    headless: true,
    viewport: { width: 1440, height: 900 },
    colorScheme: "dark",
  },
  webServer: {
    command: `"${python}" e2e/server.py`,
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 90_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
