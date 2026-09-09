# CLAUDE.md — modelmatch-backend

> Modicum was previously ModelMatch. Repository and infrastructure identifiers retain `modelmatch` for compatibility.

**Status: ACTIVE.** FastAPI backend for Modicum **+ the CI-agent image**. See the umbrella
`../CLAUDE.md` and the spec in `../docs/planning/` (esp. `architecture.md` and `module-reconciliation.md`).

## Responsibilities

- **Deterministic recommender (NO LLM):** form → filter catalog → weighted score
  (`rank_score = w_q·quality + w_c·(1 − cost)`) → suggested model + baseline (+ shortlist). Pure
  function: same inputs → same output. Optional keyword pre-fill (rule-based).
- **Catalog ingestion (#3, LLM, in-cluster):** unstructured model/benchmark sources → **Bedrock Nova**
  extract → **validated** structured `benchmark_result` rows; **idempotent** (content-hash → skip
  unchanged); sources to **S3**. Fills the catalog; the formula still ranks.
- **Savings engine + quality gate:** per `ci_run`, `actual/baseline/savings`; acceptance-rate gate
  (failing runs excluded + surfaced). Baseline demo = **Sonnet, computed**.
- **Grounded Q&A chat (#4, LLM, in-cluster):** question → retrieve (savings + catalog) → **Bedrock
  Nova** → answer + **retrieval trace**; out-of-scope → honest refusal; SQL parameterized. Opening
  message = "explain my spend".
- **CI agent (`agent/`, the proof):** diff → AI code review via the **provider-agnostic `LLMClient`**
  (BYOK; adapters Bedrock·Anthropic·Gemini) → findings JSON + tokens → POST `/ci-runs`. Review +
  pass/fail **gate stay in CI**; never edits the repo. Built as a **second image** with its **own
  pipeline** (`Jenkinsfile.agent`, **P19**) — **publish-only, NOT an EKS workload** (it runs in a *user's*
  Jenkins via `docker run`): source→build→test→**Trivy**→publish the **same tested digest** to **private
  ECR (instance profile) + a public registry** (likely Docker Hub; separate `modelmatch/jenkins/*`
  credential), immutable tags. **No GitOps bump / ArgoCD / kubectl / cluster deploy.** Changing the
  `/ci-setup` default (`DEFAULT_AGENT_IMAGE=docker.io/…:<tag>`) is a **backend** release via P18/GitOps,
  not an agent deploy. **Trigger:** a **separate multibranch job** on `Jenkinsfile.agent` — **no
  long-lived `agent` branch**; the normal `feature/* → PR → main` model applies. The job wakes on
  webhook/SCM events, then a first **`Detect agent changes`** stage skips (exits a green **`skipped`**,
  not failure) unless an **agent-relevant** path changed (`agent/**`, `Jenkinsfile.agent`, agent build
  files, dep lockfiles `pyproject.toml`/`uv.lock`, shared `app/llm/**` + CI-run/finding schema/client,
  `tests/agent/**`) **or** `FORCE_AGENT_BUILD=true`. Feature/PR → build/test/Trivy only; `main` →
  also publish. See `../docs/planning/mentor-notes-2026-06-15.md` §8.
- **Auth:** register/login, JWT (argon2), owner-scoping. CI-run ingest authed by a **per-project
  token**, not the user JWT.

## Layout (target)

```
app/{api, models, schemas, recommend, ingest (#3), chat (#4), projects, ci, savings, observability,
     llm (LLMClient + adapters: bedrock / anthropic / gemini + fake)}
agent/        (CI-agent image: diff → LLMClient review → findings JSON; shares app.llm + the findings contract)
migrations/   (alembic; run as a Job/Helm hook, not on startup)
tests/        (unit: no containers · integration: testcontainers Postgres · all fake LLM + fixtures; live LLM only in the gated `e2e-live` E2E path on main/#e2e-live — see ../docs/planning/mentor-notes-2026-06-15.md)
```

## Rules

- **Postgres 16, in-cluster, NO pgvector** — no embeddings anywhere.
- **Two-surface model rule:** in-cluster LLM (ingestion + chat) = **Bedrock Nova via IRSA** (no static
  keys); the **agent** is **BYOK** multi-provider. Demo: Haiku live, Sonnet computed, Gemini free-tier
  (non-confidential code only).
- `/healthz` + `/readyz` split; `/metrics` (tokens, labeled by model+purpose — **not** $).
- **Per-request LLM log line** (model, latency, tokens in/out, retrieved-context size, truncated
  prompt+query, PII-redacted) for all three uses. **Never log diff content or secrets.**
- Config via `pydantic-settings` from env. Pydantic schemas, camelCase out. Errors 401/403/422/429/504.
- `uv` packaging. Branching: `feature/<story-id>-<desc>`; never commit to `main`.

Before asking Steve to approve a Story commit, Claude Code must:

1. Run the tests (focused area + full suite) and get them green on compose Postgres.
2. Present the **"What I built in this slice"** summary and STOP for Steve's explicit approval.

**Do NOT run `/pre-commit-scan`** — it has been removed from the workflow (2026-06-08): each run took
~10 min and it surfaced no meaningful logical bugs, while **Steve reviews with Codex** (faster, catches
more). Steve runs any review he wants **manually**; Claude does not invoke a review agent. Only
commit / merge / tag / push after Steve's explicit approval.

The flow:

```
Implement story
   |
Run tests  ->  not green? fix
   |
Show Steve the "What I built in this slice" summary
   |
Steve reviews (Codex, manual) + explicitly approves
   |
commit / merge / tag / push
```

## Build order touching this repo

S1 · S2 · S3 · S4 · S5 catalog · **S5b ingestion (#3)** · S6 pick · S7 pre-fill · S8 project · S9
Jenkins BYOK · **S10 CI agent (`agent/`, multi-provider)** · S11 ingest · S12 savings · S13 quality
gate · **S14b grounded chat (#4)** · S16 observability · S17 tests.
