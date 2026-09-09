# Modicum — Runbook & Demo Walkthrough

> A single, reviewer-facing guide to **understand**, **run**, and **demo** Modicum. It spans both
> active repos (`modelmatch-backend`, `modelmatch-frontend`). It lives in the backend repo because the
> umbrella workspace is **not** a git repo and the backend holds the product core + the CI agent.
>
> Authoritative product spec: [`../../docs/planning/`](../../docs/planning/) (`prd-1.md`,
> `architecture.md`, `module-reconciliation.md`, `00-backlog.md`).

**Status snapshot (this doc is truthful to committed code):**
backend **v0.20.0**, frontend **v0.5.0**. Built through **S16** (observability) plus **S15d** project
lifecycle on the backend, and **S15a–S15d** on the frontend. Items marked **[planned]** below are **not
yet merged** — do not assume they run.

---

## 1. What Modicum is

**One-liner:** *Prove a cheaper LLM is good enough for your CI — and show the money saved.*

**Demo story.** A team runs an AI code-review agent in their Jenkins CI and defaults to an expensive
model "to be safe." Modicum:

1. **Recommends** a cost-effective model + an expensive **baseline** to compare against — using a
   **deterministic** formula over a benchmark catalog (no LLM in the ranking).
2. Wires the chosen model into the team's **Jenkins** pipeline as a containerised **AI code-review
   agent** that reviews each PR diff on the team's **own API key (BYOK)**.
3. Shows a **savings dashboard vs the expensive baseline** — counting savings **only when a quality
   signal holds**.
4. Lets the user **ask** about their spend in a **grounded chat** (answers tied to retrieved data, with
   a visible trace).

The proof is the agent: you cannot keyword-match *"this diff adds a SQL injection"* — a real model has
to read the diff. The savings are honest because they are gated on review quality.

---

## 2. The deterministic recommender (NO LLM in the ranking)

The recommender is a **pure function** of (form inputs + benchmark catalog). Same inputs → same output;
`rank_score` is stored for audit.

```
form inputs (task type, budget sensitivity, latency)
        ▼
① filter catalog   WHERE task_type ∈ selected AND comparable benchmark+metric
        ▼
② weighted score   rank_score = w_q·quality + w_c·(1 − cost)     (w_q from the budget preset)
        ▼
③ pick   suggested model + a baseline (+ ranked shortlist); rank_score persisted
```

- **No model call, no embeddings, no RAG on this path.** The LLM *fills* the catalog (capability #3
  below) and *answers questions* (capability #4); the **formula ranks**.
- Budget-sensitivity presets (env-tunable): `low → w_q 0.85`, `medium → 0.60`, `high → 0.40`
  (`w_c = 1 − w_q`).
- **Comparability rule:** only compare rows with the same `benchmark` + `metric`.
- **Current onboarding UI (S15b/S15c)** scopes the task to **`ci_review`** (the proof path) — shown as a
  fixed pill, with **budget** and **agent-speed (latency)** selectors. The backend recommender still
  accepts other task types and has a keyword pre-fill endpoint (`POST /recommendations/prefill`); the FE
  doesn't surface those yet.

---

## 3. The two-surface model rule (important)

Modicum uses an LLM in **three** places, governed by **where the model runs and whose key pays**:

| Surface | Used for | Models | Auth |
|---|---|---|---|
| **In-cluster backend** | catalog ingestion (#3), grounded chat (#4) | **Bedrock Nova Lite / 2-Lite only** | **IRSA** — no static keys |
| **CI agent** (the proof) | PR-diff code review in the user's Jenkins | **BYOK — any provider** | the **user's** key (provider-agnostic `LLMClient`) |

Why split: the in-cluster backend runs on our AWS account, so it is bound to the account's permitted
Bedrock models — and Nova is plenty for ingestion/chat. The **agent** runs in the *user's* Jenkins with
the *user's* key, so it is **not** bound to our account's model list — that is the variance escape hatch
that lets us demo a real **Nova → Haiku → Sonnet** cost/quality spread.

**Demo spectrum:** the agent runs on Steve's own **Anthropic** key — **Haiku live**, **Sonnet baseline
computed** (`tokens × price`, never run for ranking) — plus **Gemini free-tier** as a 3rd vendor (Amazon
= Nova in-cluster). The catalog also *lists* models like GPT/DeepSWE (data-only) for recommender
breadth. **OpenAI is out** as a runtime provider.

> ### ⚠️ Gemini free-tier data-policy caveat
> Gemini's **free** tier **trains on inputs and allows human review**. Anthropic and Bedrock do **not**
> train on API inputs. So the demo feeds Gemini **only non-confidential / throwaway code** (our demo
> fixtures). Production Gemini use would need the paid tier / Vertex AI. This is documented as a
> deliberate provider-data-policy choice — a portfolio asset, not an oversight.

---

## 4. LLM capability #3 — catalog ingestion

The backend's first in-cluster LLM use, and a real product feature (the catalog stays current as new
models drop).

```
[unstructured source: model card / leaderboard / eval report]  → blob store
        ▼  LLMClient → Bedrock Nova (IRSA)
   extract structured benchmark_result rows (model, benchmark, task_type, score, metric,
   cost_per_mtok, context_window, source)
        ▼  validate (Pydantic + sanity bounds; treated as UNTRUSTED)
        ▼  idempotent: content_hash → unchanged source does NO re-work
   upsert into the catalog  →  feeds the deterministic recommender (§2)
```

- **Endpoint:** `POST /benchmarks/ingest`.
- **Idempotent:** re-running on an unchanged source (same content hash) skips the LLM call and the
  upsert — proven offline against the fake client.
- **Validated, untrusted:** the model's output is schema- and bounds-checked before persisting; invalid
  output is rejected, never stored.
- **Cost guard:** every in-cluster Nova call passes through the single `LLMClient`, which enforces a
  **hard hourly token cap** (`LLM_HOURLY_TOKEN_CAP`) that **aborts** (HTTP 429) past budget — counted
  via a shared Postgres `llm_usage` tally so it holds across replicas.
- **[in progress] real S3 backend:** `BLOB_STORE` only supports `fake` (in-process) today; the `s3`
  backend (via IRSA) lands with the infra stories.

---

## 5. LLM capability #4 — grounded Q&A chat

The backend's second in-cluster LLM use: a natural-language chat where the user **asks** about their
spend / quality / models and the answer is **grounded** in retrieved data.

```
POST /projects/{id}/chat   { question }
   │ ① retrieve grounding: the project's savings aggregates + relevant catalog rows
   │ ② build a STRICTLY-GROUNDED prompt (system rules + retrieved context only)
   │ ③ LLMClient → Bedrock Nova (IRSA)
   ▼
   { answer, retrievalTrace[] → the rows/figures used }
```

- **Endpoints:** `POST /projects/{id}/chat` (ask), `GET /projects/{id}/chat` (history).
- **Grounded, with a visible trace.** Answers only from retrieved savings + catalog content; the
  response surfaces **which rows/figures** it used.
- **Honest refusal.** Out-of-scope questions get *"I can't answer that from the data,"* never a
  hallucination. The chat distinguishes three outcomes: answer-from-SQL / no-query-needed /
  cannot-answer.
- **Safe SQL.** Any generated query over the curated catalog view is **SELECT-only**, gated by a
  `sqlglot` parse check, run through a **read-only DB role** over a **curated view** — never
  string-built, never a write.
- **Opening message** = the auto **"explain my spend"** summary; the user then asks follow-ups.
- **Debug trace** (raw SQL etc.) is hidden unless `CHAT_DEBUG_ENABLED` — never shown to end users by
  default.

---

## 6. The CI code-review agent — the proof path

The agent is a **standalone image** built from `agent/` in the backend repo. It runs **in the user's
Jenkins**, not in our cluster.

```
[user Jenkins PR build]
   │  ① git diff origin/<target>...HEAD  →  pr.diff
   │  ② docker run modelmatch-agent  → LLMClient (BYOK) reviews the diff: security + style
   │  ③ findings JSON + token usage + a PASS/FAIL gate (exit code)
   ▼  POST /projects/{id}/ci-runs   (per-project CI token, NOT the user JWT)
[backend]  savings (actual vs baseline) + quality gate = pure arithmetic, ZERO tokens spent by us
```

- **Review + pass/fail gate stay in CI** — that is the point of CI (block bad code before prod). The
  agent **never edits the repo**.
- **What leaves the user's CI:** only **findings + token counts + build metadata** — **never the diff
  content**. So a user's CI run **costs us zero tokens** (the savings math is deterministic arithmetic).
- **Exit codes** (the gate): `0` pass · `1` fail (blocking findings) · `2` malformed model output ·
  `3` token ceiling exceeded · `4` config/input/provider failure. See [`../agent/README.md`](../agent/README.md).
- **Per-run cost ceiling** on the user's key: `AGENT_TOKEN_CEILING` aborts the run; `AGENT_MAX_TOKENS`
  caps per-call output.

### Metadata-only Jenkins connection + the two credentials

The backend stores **only safe metadata** for a Jenkins connection — **base URL + job name**. It does
**not** store (or read) the user's Jenkins token or model API key: those live as **Jenkins credentials**
in the user's own Jenkins, and the generated CI snippet references them by id.

| Jenkins credential id | type | what it is |
|---|---|---|
| `modelmatch-ci-token` | Secret text | the per-project ingest token (from `GET /ci-setup`) the agent uses to POST results back |
| `modelmatch-model-api-key` | Secret text | the user's BYOK provider key the agent calls the model with |

- **Connect:** `PUT /projects/{id}/jenkins` accepts `baseUrl + jobName` **without** any secret. The
  connection's status becomes `configured`.
- **Mint-once CI token:** `GET /projects/{id}/ci-setup` mints the per-project ingest token **once**,
  stores only its **hash**, and returns the **plaintext on that first fetch only**. Later fetches return
  `token: null` — a token already minted can never be re-shown. Copy it when shown.
- **Lost-token recovery (rotation):** `POST /projects/{id}/ci-setup/rotate` issues a **fresh** token
  (and invalidates the old one), returning the new plaintext once. The FE surfaces this as
  **"Regenerate token."**
- **Project lifecycle (S15d):** edit / re-pick the model + baseline with `PATCH /projects/{id}`; remove
  a project with `DELETE /projects/{id}` (204, **FK cascade** over the Jenkins connection / ci_runs /
  findings / recommendation rows). The onboarding wizard **defers project creation to commit**, so
  abandoning it leaves **no orphaned project**.
- The CI snippet keeps secrets out of any command's argv: the BYOK key is passed to `docker run` by name
  (`-e VAR`), and the CI token is written to a `0600` curl config file, never a CLI argument.

---

## 7. Savings + the quality gate

Per `ci_run`, on the tokens the agent reports:

```
actual_cost   = tokens × selected_model.price       (split in/out pricing)
baseline_cost = tokens × baseline_model.price        baseline demo = Sonnet, COMPUTED (never run)
savings       = baseline_cost − actual_cost
quality_ok    = acceptance_rate ≥ QUALITY_THRESHOLD  (default 0.8)
```

- **Quality signal:** each finding gets accept/reject feedback (`POST /findings/{id}/feedback`); the
  per-project **acceptance rate** is the quality metric.
- **Quality-gated savings:** cumulative savings **count only runs that clear the threshold**. Runs that
  fail are **excluded from the banked total and surfaced as "quality risk"** — the honest-savings
  guardrail.
- **Dashboard data:** `GET /projects/{id}/savings?range=…` returns time-bucketed `{ kpis, series, runs }`
  aggregates. Pure read; all math is the savings engine.
- Baseline default is `BASELINE_MODEL_ID` (demo = `Claude Sonnet 4.5`, a catalog model in the
  `ci_review` comparability group).

---

## 8. Run it locally

### Prerequisites
- **Backend:** Python 3.12 + [`uv`](https://docs.astral.sh/uv/); Docker + Docker Compose.
- **Frontend:** Node.js 20+ and npm.
- Copy each repo's `.env.example` → `.env` (never commit `.env`).

### Backend + Postgres (from `modelmatch-backend/`)

Run Postgres in Docker and the backend on the host. The one value you **must** change is `JWT_SECRET`:
the placeholder in `.env.example` is **rejected at startup** (there is no built-in default).

```bash
cp .env.example .env
# REQUIRED: set a real JWT_SECRET, e.g.
#   echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env   # then remove the placeholder line

docker compose up -d db                 # local PostgreSQL on :5432
uv sync
uv run alembic upgrade head             # build the schema — a SEPARATE step, never on startup
uv run uvicorn app.main:app --reload    # http://localhost:8000
```

Health: `GET /healthz` (liveness), `GET /readyz` (readiness, checks the DB), `GET /metrics` (Prometheus).
Interactive API docs at `http://localhost:8000/docs`.

> **The compose `backend` service is not the local-dev path (yet).** It builds the production image
> (gunicorn, no reload), but it isn't passed a `JWT_SECRET` and the runtime image doesn't carry the
> Alembic migrations — so the schema still has to be built from the host with `alembic upgrade head`.
> Use the host backend + compose Postgres above for local dev; the full in-container stack lands with
> the infra/compose work.

> **The LLM/blob/secret backends are offline + free by default.** `LLM_CLIENT=fake`, `BLOB_STORE=fake`,
> `SECRET_STORE=fake` — no cloud creds needed and **zero tokens spent**. Real Bedrock/S3/Secrets
> backends activate with the infra stories. (You still set a real `JWT_SECRET` as above.)

### Frontend (from `modelmatch-frontend/`)

```bash
cp .env.example .env                    # VITE_API_BASE_URL=http://localhost:8000
npm install
npm run dev                             # http://localhost:5173
```

The SPA talks to the backend over HTTPS/JSON only. In production the API base URL is injected via a
templated `/config.js` served by nginx; for local dev Vite reads `VITE_`-prefixed vars.

---

## 9. Environment variables

### Backend (`modelmatch-backend/.env`)

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://modelmatch:modelmatch@localhost:5432/modelmatch` | Postgres connection |
| `JWT_SECRET` | `change-me-in-env` | JWT signing secret (set a real value) |
| `JWT_ALGORITHM` / `JWT_EXPIRE_MINUTES` | `HS256` / `60` | JWT params |
| `BASELINE_MODEL_ID` | `Claude Sonnet 4.5` | demo baseline (computed, not run) — a catalog model name in the `ci_review` group |
| `QUALITY_THRESHOLD` | `0.8` | acceptance-rate gate for banking savings |
| `RANK_WEIGHT_LOW/MEDIUM/HIGH` | `0.85 / 0.60 / 0.40` | budget-sensitivity `w_q` presets (quality↔cost weight) |
| `RECOMMENDATION_SHORTLIST_SIZE` | `3` | ranked options returned/persisted |
| `FEATURE_PROACTIVE_ADVISOR` | `false` | separable advisor (bonus); off by default |
| `AWS_REGION` | `ap-south-1` | region for Bedrock / S3 |
| `BEDROCK_MODEL_ID` | `apac.amazon.nova-lite-v1:0` | in-cluster ingestion + chat model (ap-south-1 needs the `apac.` inference profile) |
| `S3_BUCKET` | `modelmatch-ingestion-sources-957261948820` | ingestion source docs (when `BLOB_STORE=s3`) |
| `LLM_CLIENT` | `fake` | **in-cluster** surface: `fake` \| `bedrock` (Nova via IRSA) |
| `LLM_FIXTURES_DIR` | `tests/fixtures/llm_responses` | recorded fixtures for the fake client |
| `BLOB_STORE` | `fake` | ingestion blob backend: `fake` today; `s3` later |
| `INGEST_MAX_TOKENS` | `2048` | per-extraction output cap |
| `LLM_HOURLY_TOKEN_CAP` | `200000` | **hard** hourly cap on our Nova spend — aborts (429), shared Postgres tally |
| `SECRET_STORE` | `fake` | Jenkins/BYOK secret-ref backend: `fake` today; `aws` (Secrets Manager via IRSA) later |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | where the user's Jenkins POSTs `ci-runs` back (baked into the snippet) |
| `AGENT_IMAGE` | `modelmatch-agent:latest` | CI-agent image the snippet pulls (real ECR ref lands with infra) |
| `CI_AGENT_LLM_CLIENT` | `anthropic` | provider baked into the CI snippet: `anthropic` \| `gemini` \| `bedrock` (**never `fake`**) |
| `CI_AGENT_MODEL` | `claude-haiku-4-5` | model id the snippet passes as `AGENT_MODEL` |
| `CI_AGENT_MAX_TOKENS` / `CI_AGENT_TOKEN_CEILING` | `1024` / `20000` | per-call / per-run token caps on the user's key |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173` | allowed FE origin |

**CI-agent runtime vars** (set by the snippet, consumed by the `agent/` image): `LLM_CLIENT`,
`AGENT_MODEL`, `AGENT_MAX_TOKENS`, `AGENT_TOKEN_CEILING`, `AGENT_FAIL_SEVERITIES`, plus the provider
creds (`ANTHROPIC_API_KEY` / `GEMINI_API_KEY` / AWS via IRSA). See [`../agent/README.md`](../agent/README.md).

### Frontend (`modelmatch-frontend/.env`)

| Variable | Default | Purpose |
|---|---|---|
| `VITE_API_BASE_URL` | `http://localhost:8000` | backend base URL (prod injects via templated `/config.js`) |

---

## 10. Tests

All LLM uses sit behind one `LLMClient` with a **fake client + recorded fixtures**, so the suites run
**offline at zero token cost**.

```bash
# Backend (from modelmatch-backend/) — needs Postgres up (docker compose up -d db)
uv run pytest                       # full suite (fake LLM, offline)
uv run pytest tests/test_savings.py # a focused module

# Live, GATED (spends real tokens — deliberate only)
RUN_LLM_LIVE=1 ANTHROPIC_API_KEY=... uv run pytest tests/test_llm_live.py
```

Coverage today (~355 backend tests passing): deterministic-pick scoring, savings math, ingestion
idempotency + output validation, chat grounding + honest refusal + SELECT-only SQL, quality gate,
auth/owner-scoping, project lifecycle (edit/delete cascade + token rotation), migration round-trip,
observability log line + `/metrics`, agent review.

```bash
# Frontend (from modelmatch-frontend/)
npm run test        # Vitest (unit/component — 68 tests)
npm run typecheck   # tsc --noEmit
npm run build       # production build
npm run e2e         # Playwright happy path (hermetic — backend mocked via route interception)
```

**S17 e2e (Playwright).** One CI-able **happy path** drives the real SPA end-to-end with the backend
**fully mocked** (`page.route`): login → home hub → *Create a new CI-Agent* → recommend (ci_review) →
pick → defer-create at the Jenkins step → CI-setup token → dashboard (seeded by a mocked CI run) →
grounded-chat opener → one grounded question. A second **optional real-stack smoke**
(`npm run e2e:all`, project `real-stack`) runs the same flow against a **real backend** (compose
Postgres + app on `:8000`) and exercises the genuine **`POST /ci-runs`** ingest with a real per-project
token; it **self-skips** when `:8000` is unreachable (no-op in plain CI). Both cost **$0** — the CI run
is a *mocked* agent result (deterministic savings, no LLM).

> The **real-Jenkins** version of the proof path (the CI-Agent reviewing a real PR on the cowsay app on
> an EC2 Jenkins, ingested to the dashboard) is a **manual, gated** demo-rehearsal — see
> [`ec2-jenkins-cowsay-smoke.md`](ec2-jenkins-cowsay-smoke.md).

---

## 11. Demo script (end-to-end click path)

The smallest fully-grounded walkthrough. Backend + Postgres + frontend running per §8; `LLM_CLIENT=fake`
is fine for everything except the live agent call.

1. **Register / log in** in the SPA → token-gated dashboard.
2. **Ingest a few catalog rows** — `POST /benchmarks/ingest` on a sample source (offline fake client).
   Re-run the same source → **no re-work** (idempotency).
3. **Recommend** — the onboarding form (task fixed to **ci_review**; pick **budget** + **agent-speed**)
   → get a **suggested model + a Sonnet baseline + a shortlist**. Same inputs → same pick
   (deterministic).
4. **Create a project** from the pick.
5. **Connect Jenkins** — enter **base URL + job name** only (validated; no secrets). Fetch **CI setup**
   → copy the **one-time CI token** and the **stage snippet** (lost it? **Regenerate token**). In the
   user's Jenkins, add the two credentials (`modelmatch-ci-token`, `modelmatch-model-api-key`). The
   project isn't persisted until you commit the wizard — abandoning it leaves no orphan.
6. **Run the agent** — on a PR diff. Demo: Anthropic **Haiku live**; **Sonnet baseline computed**. The
   agent posts findings + tokens to `/ci-runs` (the diff never leaves CI). For an offline demo, a mocked
   `/ci-runs` POST works too.
7. **Dashboard** — see **cost/run, cumulative saved vs baseline, quality over time**; accept/reject
   findings and watch a sub-threshold run drop out of the banked total as **quality risk**.
8. **Chat** — the panel opens with the auto **"explain my spend"** summary; ask a follow-up and see the
   answer with its **retrieval trace**; ask something out-of-scope and see the **honest refusal**.

**Smallest slice if behind:** ingest a couple of rows → form → pick → create project → one CI run
(mocked agent OK) → dashboard shows savings → ask the chat one grounded question.

---

## 12. What's implemented vs planned

| Area | State |
|---|---|
| Auth, recommender, catalog, ingestion #3, savings + quality gate, chat #4, observability, CI agent, metadata-only Jenkins + mint-once token | **Done** (backend v0.19.0, frontend v0.4.0) |
| FE: login, dashboard + chat panel, recommender/project/Jenkins onboarding | **Done** (S15a/S15b/S15c) |
| **Project lifecycle** — edit / re-pick (`PATCH`), delete (`DELETE`, FK cascade), defer-create (no orphan), Jenkins URL validation, CI-token regenerate (`/ci-setup/rotate`) | **Done** (S15d — backend v0.20.0, frontend v0.5.0) |
| **e2e + real-Jenkins smoke** — Playwright happy path + real-stack `/ci-runs` smoke + the **FE+BE+DB-on-ECR** cowsay smoke on real EC2 Jenkins | **Done (S17a)** — automated e2e green + the [real-Jenkins smoke](ec2-jenkins-cowsay-smoke.md) **ran green** (fake agent). **S17b** owes the live-BYOK Haiku run + the response-rating UI |
| **Infra** — Terraform (EKS, VPC, ECR, IRSA, S3 state) | **[planned]** — `modelmatch-infra` is a SHELL repo |
| **GitOps** — Helm umbrella + ArgoCD, cert-manager, ingress, monitoring/logging | **[planned]** — `modelmatch-gitops` is a SHELL repo |
| Real `BLOB_STORE=s3` / `SECRET_STORE=aws` / `LLM_CLIENT=bedrock` live | **[planned]** — only `fake` backends wired today |
| **Proactive advisor** (S19–S20) | **[planned, separable]** — droppable bonus |
| **Repo auto-profile** (S21–S22) | **[planned, optional]** |

---

## 13. Related docs

- Per-repo READMEs: [`../README.md`](../README.md) (backend), `../../modelmatch-frontend/README.md`.
- CI agent details: [`../agent/README.md`](../agent/README.md).
- Real-Jenkins cowsay smoke (manual, gated): [`ec2-jenkins-cowsay-smoke.md`](ec2-jenkins-cowsay-smoke.md).
- Product spec: [`../../docs/planning/`](../../docs/planning/) — `prd-1.md`, `architecture.md`,
  `module-reconciliation.md`, `mentor-notes-2026-06-04.md`, `00-backlog.md`.
- Architecture diagrams: [`./diagrams/`](./diagrams/) (draw.io sources).
</content>
</invoke>
