import { test, expect } from "@playwright/test";

// Thin backend-pipeline E2E smoke (P18). Proves the freshly-built BACKEND IMAGE works
// end-to-end through the real FE/API path against a real Postgres, WITHOUT duplicating
// the frontend's exhaustive suite:
//   1. the backend image answers /healthz (it booted + reached Postgres),
//   2. it registers + authenticates a user (auth + DB writes work in the image),
//   3. the frontend image serves the SPA and the SPA logs in against that backend.
//
// fake-LLM only — it never asks a live question (that would spend Bedrock tokens). The
// single real-model path is the separate e2e-live API subcheck (main / #e2e-live).
//
// CI CONTRACT: the Jenkins E2E stage sets E2E_REQUIRE_BACKEND=true, so an unreachable
// backend is a HARD FAILURE, not a skip — a required gate must never go green by
// silently skipping. Locally (flag unset) it self-skips so it's a no-op without a stack.

const API = process.env.E2E_API_BASE ?? "http://localhost:8000";
const REQUIRE_BACKEND = process.env.E2E_REQUIRE_BACKEND === "true";
const CREDS = { email: `be-smoke+${Date.now()}@example.com`, password: "be-smoke-pw-123456" };

test("backend image: healthy + auth works, and the SPA logs in against it", async ({
  page,
  request,
}) => {
  // --- 1. backend image is up (booted + reached Postgres) ---
  let up = false;
  try {
    up = (await request.get(`${API}/healthz`, { timeout: 2000 })).ok();
  } catch {
    // unreachable → `up` stays false; require-or-skip below.
  }
  if (REQUIRE_BACKEND) {
    expect(up, `E2E_REQUIRE_BACKEND set but backend not reachable at ${API}`).toBeTruthy();
  } else {
    test.skip(!up, `backend not reachable at ${API} — bring up the compose stack first`);
  }

  // --- 2. the candidate image registers + authenticates a user (auth + DB writes) ---
  const reg = await request.post(`${API}/auth/register`, { data: CREDS });
  expect([201, 409]).toContain(reg.status()); // 409 if a rerun reused the email
  const login = await request.post(`${API}/auth/login`, { data: CREDS });
  expect(login.ok(), `API login -> ${login.status()}`).toBeTruthy();

  // --- 3. the FE image serves the SPA and the SPA logs in against the backend ---
  await page.goto("/");
  await page.getByLabel("Email").fill(CREDS.email);
  await page.getByLabel("Password").fill(CREDS.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  // authenticated home (the SPA reached the backend, got a token, rendered the hub)
  await expect(page.getByRole("button", { name: "Create a new CI-Agent" })).toBeVisible();
});
