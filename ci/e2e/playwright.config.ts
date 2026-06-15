import { defineConfig, devices } from "@playwright/test";

// Thin backend-pipeline E2E smoke (P18). Runs against the throwaway compose stack
// (frontend image + backend CANDIDATE image + Postgres). baseURL is the frontend the
// pipeline publishes on a free host port; the spec also calls the backend API directly
// (E2E_API_BASE) to prove the candidate image works against a real Postgres. Kept
// deliberately minimal — the exhaustive browser suite lives in the frontend repo.
export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.ts/,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
