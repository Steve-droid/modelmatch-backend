#!/usr/bin/env bash
# Container Integration smoke for the BACKEND pipeline (P31).
#
# This is the boundary check that the in-process pytest suite cannot give us: the
# freshly-built candidate BACKEND IMAGE is running in a container, talking to a real
# Postgres in a sibling container, and we exercise it ONLY over its HTTP API — no Python
# imports, no test client, no shared process. If the image's entrypoint / gunicorn
# config / DB driver / migrations / seed data are broken, this stage fails (the pytest
# suite would still pass).
#
# What it proves (mapped to the P31 brief):
#   1. /readyz returns 200 and reports DB ready (image + DB driver + migrations work).
#   2. POST /auth/register inserts a user (write path + argon2 hashing in the image).
#   3. POST /auth/login authenticates that user (read path + JWT signing).
#   4. POST /recommendations reads the SEEDED catalog (recommender + seed data work).
#   5. POST /projects writes + GET /projects/{id} reads back (one basic write/read path
#      through the API).
#
# Fake-LLM only: the compose stack defaults LLM_CLIENT=fake. The single real-Bedrock
# call lives in ci/e2e-live-check.sh, NOT here.
#
# Env (set by the pipeline):
#   E2E_API_BASE   backend base URL on its free host port (default http://localhost:8000)
set -euo pipefail

export E2E_API_BASE="${E2E_API_BASE:-http://localhost:8000}"
# Unique email per run so reruns against a persistent DB don't 409 on register.
EMAIL="integ+$(date +%s)@example.com"
PASSWORD="integ-pw-123456"

echo "container-integration: backend at ${E2E_API_BASE} (fake LLM, real Postgres)"

# 1. /readyz — backend image is up AND its DB driver can reach the seeded compose db.
READYZ=$(curl -fsS "${E2E_API_BASE}/readyz")
echo "  readyz body: ${READYZ}"
echo "${READYZ}" | python3 -c 'import sys,json; b=json.load(sys.stdin); assert b["status"]=="ready" and b["db"]=="ok", b'

# 2. register the user (idempotent on rerun: 409 is acceptable).
curl -fsS -o /dev/null -w "  register status: %{http_code}\n" \
  -X POST "${E2E_API_BASE}/auth/register" \
  -H 'content-type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}" \
  || true
echo

# 3. log in -> bearer token (proves the user we just registered is readable + auth works).
TOKEN=$(curl -fsS -X POST "${E2E_API_BASE}/auth/login" \
  -H 'content-type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASSWORD}\"}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["accessToken"])')
[ -n "${TOKEN}" ] || { echo "container-integration FAIL: no access token" >&2; exit 1; }
echo "  login ok"

AUTH_H="authorization: Bearer ${TOKEN}"

# 4. POST /recommendations against the seeded catalog. The deterministic recommender
#    needs at least one benchmark_result row in the right task_type group to produce a
#    suggestion + baseline; that the result is non-empty proves the migrate/seed step
#    actually populated the catalog in the candidate image's view of the DB.
REC=$(curl -fsS -X POST "${E2E_API_BASE}/recommendations" \
  -H "${AUTH_H}" -H 'content-type: application/json' \
  -d '{"taskTypes":["agentic_coding"],"budgetSensitivity":"medium"}')
# Candidate option IDs in rank order: the top suggestion first, then the shortlist.
# We do NOT assume the #1-ranked model is project-createable: the catalog lists
# data-only models (e.g. GPT-5 Mini) for recommender breadth that have NO enabled CI
# runtime config, so POST /projects returns 422 for them by design. The smoke proves
# the write/read path works for a *runnable* pick — so it tries candidates in order
# and accepts the first the API allows.
CANDIDATE_OPTION_IDS=$(echo "${REC}" | python3 -c '
import sys, json
r = json.load(sys.stdin)
ids, seen = [], set()
for opt in [r["suggested"], *r.get("shortlist", [])]:
    oid = opt["recommendationOptionId"]
    if oid not in seen:
        seen.add(oid); ids.append(str(oid))
print(" ".join(ids))
')
BASELINE_MODEL_ID=$(echo "${REC}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["baseline"]["modelId"])')
[ -n "${CANDIDATE_OPTION_IDS}" ] && [ -n "${BASELINE_MODEL_ID}" ] \
  || { echo "container-integration FAIL: empty recommendation (catalog not seeded?)" >&2; exit 1; }
echo "  recommend ok: candidateOptionIds=[${CANDIDATE_OPTION_IDS}] baselineModelId=${BASELINE_MODEL_ID}"

# 5. write/read: create a project from the first createable pick, then GET it back.
#    Proves an authenticated write that depends on multiple tables (user,
#    recommendation_option, model, agent_runtime_config) plus a read on what we wrote.
export PROJECT_NAME="integ-${BUILD_NUMBER:-local}-$(date +%s)"
PROJ_RESP=$(mktemp -t integ-proj-resp.XXXXXXXX)
trap 'rm -f "${PROJ_RESP}"' EXIT
PROJECT_ID=""
for OPT_ID in ${CANDIDATE_OPTION_IDS}; do
  export OPT_ID BASELINE_MODEL_ID
  PROJECT_BODY=$(python3 -c 'import json,os
print(json.dumps({
  "name": os.environ["PROJECT_NAME"],
  "selectedOptionId": int(os.environ["OPT_ID"]),
  "baselineModelId": int(os.environ["BASELINE_MODEL_ID"]),
}))')
  # No -f: we want to inspect the status code (422 = data-only model, try the next).
  CODE=$(curl -sS -o "${PROJ_RESP}" -w '%{http_code}' -X POST "${E2E_API_BASE}/projects" \
    -H "${AUTH_H}" -H 'content-type: application/json' --data "${PROJECT_BODY}")
  if [ "${CODE}" = "201" ]; then
    PROJECT_ID=$(python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])' <"${PROJ_RESP}")
    echo "  project create ok: optionId=${OPT_ID} id=${PROJECT_ID}"
    break
  elif [ "${CODE}" = "422" ]; then
    echo "  option ${OPT_ID} not runnable (422, no enabled CI runtime config) — trying next"
  else
    echo "container-integration FAIL: POST /projects optionId=${OPT_ID} -> HTTP ${CODE}" >&2
    cat "${PROJ_RESP}" >&2; exit 1
  fi
done
[ -n "${PROJECT_ID}" ] \
  || { echo "container-integration FAIL: no candidate option was project-createable" >&2; exit 1; }

FETCHED=$(curl -fsS -H "${AUTH_H}" "${E2E_API_BASE}/projects/${PROJECT_ID}")
echo "${FETCHED}" | python3 -c '
import sys, os, json
b = json.load(sys.stdin)
assert b["name"] == os.environ["PROJECT_NAME"], f"name mismatch: {b}"
assert b["selectedOptionModel"], f"selectedOptionModel empty: {b}"
assert b["baselineModel"], f"baselineModel empty: {b}"
# Pull values out FIRST: backslash escapes are illegal inside f-string braces, so
# avoid nested quotes in the f-string entirely.
name, model, baseline = b["name"], b["selectedOptionModel"], b["baselineModel"]
print(f"  project read ok: name={name} model={model} baseline={baseline}")
'

echo "container-integration: PASS"
