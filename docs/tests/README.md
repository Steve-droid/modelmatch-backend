# Golden-eval fixtures + live agent smoke (GATED / MANUAL)

Throwaway diffs for sanity-checking the **CI code-review agent** against a *real*
model (Bedrock Nova 2 Lite) before we trust it in the user's Jenkins. These are
**not** part of `pytest` — the default suite uses the fake LLM client and the
fixtures in `tests/`. Running the agent here spends **real Bedrock tokens**, so it
is a deliberate, manual step (see "Run it" below).

> Why throwaway code? Per the HLD provider-data-policy note, only non-confidential
> / disposable code may go to a model that may log or train on inputs. These three
> tiny snippets are invented for the eval — never real product code or a real diff.

## The fixtures & expected high-level behavior

The agent reviews the diff for **security + style** and returns an `AgentResult`
(findings + token usage + a `gate`). The gate **fails** on any finding at a
blocking severity (`AGENT_FAIL_SEVERITIES`, default `high,critical`).

| Fixture | What it introduces | Expected findings | Expected gate |
|---|---|---|---|
| `vulnerable.diff` | SQL built by string-concatenating user input (SQL injection) | ≥1 **security**, **high/critical** | **fail** |
| `clean.diff` | A small, well-typed, documented pure function | none (or only trivial) | **pass** |
| `style_only.diff` | Unused import, non-PEP8 name (`Calc`), no spaces around operators | one or more **style** (low/medium); **no** security | **pass** |

These are **high-level expectations**, not exact assertions — a real model's exact
wording, count, and line numbers vary run to run. The eval is "did it flag the
injection and block it, stay quiet on clean code, and treat style as non-blocking?"

## Run it (manual, gated)

Real Bedrock call, low cost caps. From the repo root:

```bash
# container (what Jenkins runs); needs AWS creds (mounted ~/.aws or AWS_* env)
scripts/agent-live-smoke.sh

# or without docker, via uv:
RUNNER=python scripts/agent-live-smoke.sh

# also POST each result into a local backend /ci-runs with a minted CI token:
BACKEND_URL=http://localhost:8000 PROJECT_ID=1 CI_TOKEN=<minted-token> \
  scripts/agent-live-smoke.sh
```

Cost guards (overridable env, low by default): `AGENT_MAX_TOKENS=512`,
`AGENT_TOKEN_CEILING=4000`, `AGENT_MODEL=global.amazon.nova-2-lite-v1:0`,
`AWS_REGION=ap-south-1`. Nova 2 Lite is on-demand-only via an **inference
profile** — the bare `amazon.nova-2-lite-v1:0` is rejected by Converse, so the
default is the `global.…` profile (the ACTIVE one in ap-south-1).

The script prints each `AgentResult` JSON to stdout (and the gate per fixture), so
you can eyeball whether the live model matches the table above.

## Last live run (2026-06-07, `global.amazon.nova-2-lite-v1:0`, maxTokens=512)

Matched all three expectations:

| Fixture | Findings (live) | Gate |
|---|---|---|
| `vulnerable.diff` | 1 × high/security — "SQL injection … direct concatenation of user input" | **fail** |
| `clean.diff` | none | **pass** |
| `style_only.diff` | 2 × low/style — unused `os` import; `Calc` not snake_case | **pass** |

Token usage was tiny (in ≈ 226–269, out ≈ 15–126 per call). Note: the live run
surfaced that Nova wraps its JSON in a ` ```json ` fence — the agent parser now
strips fences (`agent/review.py`, covered offline in `tests/test_agent_review.py`).
