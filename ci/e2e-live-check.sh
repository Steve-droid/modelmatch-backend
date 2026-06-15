#!/usr/bin/env bash
# The SINGLE gated e2e-live real-Bedrock subcheck (P18). Runs ONLY on main / #e2e-live,
# AFTER the fake-LLM E2E, against the SAME throwaway compose stack but with the backend
# in LLM_CLIENT=bedrock mode. It makes ONE tiny real Nova call through the backend's
# in-cluster ingestion path (POST /benchmarks/ingest) — the canonical Bedrock surface —
# using creds resolved from the Jenkins EC2 instance profile (no static keys).
#
# It is a backend-owned API check (no browser): browser coverage is the fake smoke's
# job. Required-when-triggered: ANY failure (unreachable, auth, config, no rows) exits
# non-zero so a green build means the live path was actually proven. Token spend is
# bounded by the stack's INGEST_MAX_TOKENS + LLM_HOURLY_TOKEN_CAP. The source is a tiny
# synthetic line; we never log diff/secret content.
#
# Env (set by the pipeline):
#   E2E_API_BASE   backend base URL on its free host port (default http://localhost:8000)
set -euo pipefail

export E2E_API_BASE="${E2E_API_BASE:-http://localhost:8000}"
EMAIL="e2e-live+$(date +%s)@example.com"
PASSWORD="e2e-live-pw-123456"
# Tiny synthetic source (one stated benchmark figure) — clearly fixture text, cheap to
# extract. NOT a real cited benchmark. Exported so the inline python can read it.
export SOURCE='FIXTURE source (e2e-live smoke): Amazon Nova Lite scored 38 percent pass@1 on SWE-bench Verified at about 0.06 USD per million tokens, 300k context, measured 2026-01-20.'

echo "e2e-live: backend in bedrock mode at ${E2E_API_BASE} — one real Nova ingest call."

# health gate (the bedrock-mode backend actually came up)
curl -fsS "${E2E_API_BASE}/healthz" >/dev/null

# register + login (ignore 409 on a rerun)
curl -fsS -X POST "${E2E_API_BASE}/auth/register" \
  -H 'content-type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}" >/dev/null 2>&1 || true
TOKEN=$(curl -fsS -X POST "${E2E_API_BASE}/auth/login" \
  -H 'content-type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["accessToken"])')
[ -n "${TOKEN}" ] || { echo "e2e-live FAIL: no access token" >&2; exit 1; }

# build the request body (SOURCE passed via env, never interpolated into argv)
REQ_BODY=$(python3 -c 'import json,os; print(json.dumps({"sourceText": os.environ["SOURCE"], "kind": "model_card"}))')

# the one real Nova call (the body is sourceText only — never logged verbatim by the app)
RESP_BODY=$(curl -fsS -X POST "${E2E_API_BASE}/benchmarks/ingest" \
  -H "authorization: Bearer ${TOKEN}" \
  -H 'content-type: application/json' \
  --data "${REQ_BODY}")

# assert the live extraction actually ran: status ingested + real tokens spent. The
# response body is passed via env (RESP_BODY) — stdin is the heredoc program itself.
RESP_BODY="${RESP_BODY}" python3 - <<'PY'
import json, os
body = json.loads(os.environ["RESP_BODY"])
status = body.get("status")
created = body.get("rowsCreated", 0)
tin, tout = body.get("tokensIn", 0), body.get("tokensOut", 0)
print(f"e2e-live: status={status} rowsCreated={created} tokensIn={tin} tokensOut={tout}")
assert status == "ingested", f"expected status=ingested, got {status!r}"
assert created >= 1, f"expected >=1 row created, got {created}"
assert tout > 0, "expected real output tokens from Nova (>0)"
print("e2e-live: real Bedrock Nova path PROVEN.")
PY
