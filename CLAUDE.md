# CLAUDE.md — modelmatch-backend

**Status: ACTIVE.** FastAPI backend for ModelMatch **+ the CI-agent image**. See the umbrella
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
  pipeline** (`Jenkinsfile.agent`: source→build→test→package→publish, no cluster-deploy).
- **Auth:** register/login, JWT (argon2), owner-scoping. CI-run ingest authed by a **per-project
  token**, not the user JWT.

## Layout (target)

```
app/{api, models, schemas, recommend, ingest (#3), chat (#4), projects, ci, savings, observability,
     llm (LLMClient + adapters: bedrock / anthropic / gemini + fake)}
agent/        (CI-agent image: diff → LLMClient review → findings JSON; shares app.llm + the findings contract)
migrations/   (alembic; run as a Job/Helm hook, not on startup)
tests/        (pytest + testcontainers; fake LLM + fixtures; gated test-llm-live)
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

## Build order touching this repo

S1 · S2 · S3 · S4 · S5 catalog · **S5b ingestion (#3)** · S6 pick · S7 pre-fill · S8 project · S9
Jenkins BYOK · **S10 CI agent (`agent/`, multi-provider)** · S11 ingest · S12 savings · S13 quality
gate · **S14b grounded chat (#4)** · S16 observability · S17 tests.
