#!/usr/bin/env bash
#
# Live smoke for the CI code-review agent against a REAL model (Bedrock Nova 2 Lite
# by default). GATED / MANUAL — this spends real Bedrock tokens and is deliberately
# NOT part of pytest. It runs the agent on the golden-eval diff fixtures
# (docs/tests/*.diff), prints each AgentResult JSON + the gate, and OPTIONALLY POSTs
# each result to a local backend /ci-runs using a minted per-project CI token.
#
# Usage:
#   scripts/agent-live-smoke.sh                 # container runner (what Jenkins runs)
#   RUNNER=python scripts/agent-live-smoke.sh   # no docker; via `uv run --extra llm`
#   BACKEND_URL=http://localhost:8000 PROJECT_ID=1 CI_TOKEN=<tok> scripts/agent-live-smoke.sh
#
# Cost guards (low by default, override via env):
#   AGENT_MAX_TOKENS=512  AGENT_TOKEN_CEILING=4000
#   AGENT_MODEL=global.amazon.nova-2-lite-v1:0  AWS_REGION=ap-south-1
#   (Nova 2 Lite is on-demand-only via the `global.…` inference profile; the bare
#    amazon.nova-2-lite-v1:0 is rejected by Converse.)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURES_DIR="${FIXTURES_DIR:-$REPO_ROOT/docs/tests}"

# --- provider + cost guards (LOW by default; Nova 2 Lite on tiny diffs) ---
export LLM_CLIENT="${LLM_CLIENT:-bedrock}"
# Nova 2 Lite is on-demand-only via an inference profile (the bare id is rejected
# by Converse) — `global.…` is the ACTIVE profile in ap-south-1.
export AGENT_MODEL="${AGENT_MODEL:-global.amazon.nova-2-lite-v1:0}"
export AWS_REGION="${AWS_REGION:-ap-south-1}"
export AGENT_MAX_TOKENS="${AGENT_MAX_TOKENS:-512}"
export AGENT_TOKEN_CEILING="${AGENT_TOKEN_CEILING:-4000}"

RUNNER="${RUNNER:-container}"            # container | python
IMAGE="${IMAGE:-modelmatch-agent:smoke}"

# Optional ingest: set ALL THREE to POST results into a local backend.
BACKEND_URL="${BACKEND_URL:-}"
PROJECT_ID="${PROJECT_ID:-}"
CI_TOKEN="${CI_TOKEN:-}"

FIXTURES=("vulnerable" "clean" "style_only")

log() { printf '\n\033[1m== %s ==\033[0m\n' "$*" >&2; }

if [[ "$RUNNER" == "container" ]]; then
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    log "building agent image ($IMAGE)"
    docker build -f "$REPO_ROOT/agent/Dockerfile" -t "$IMAGE" "$REPO_ROOT" >&2
  fi
fi

run_agent() {  # $1 = diff file -> AgentResult JSON on stdout, gate as exit code (0 pass / 1 fail)
  local diff_file="$1"
  if [[ "$RUNNER" == "container" ]]; then
    # creds via mounted ~/.aws (profile chain) and/or AWS_* env; reads diff on stdin.
    docker run -i --rm \
      -e LLM_CLIENT -e AGENT_MODEL -e AWS_REGION \
      -e AGENT_MAX_TOKENS -e AGENT_TOKEN_CEILING \
      -e AWS_PROFILE -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN \
      -v "$HOME/.aws:/home/appuser/.aws:ro" \
      "$IMAGE" < "$diff_file"
  else
    ( cd "$REPO_ROOT" && uv run --extra llm python -m agent --diff "$diff_file" )
  fi
}

for name in "${FIXTURES[@]}"; do
  diff_file="$FIXTURES_DIR/$name.diff"
  log "fixture: $name  (model=$AGENT_MODEL maxTokens=$AGENT_MAX_TOKENS ceiling=$AGENT_TOKEN_CEILING)"

  set +e
  result_json="$(run_agent "$diff_file")"
  rc=$?
  set -e

  if [[ -z "$result_json" ]]; then
    echo "  (no output — agent errored; rc=$rc)" >&2
    continue
  fi

  # pretty-print if jq is available; gate from the agent's exit code
  if command -v jq >/dev/null 2>&1; then
    echo "$result_json" | jq .
  else
    echo "$result_json"
  fi
  [[ $rc -eq 0 ]] && echo "  gate: PASS (rc=0)" >&2 || echo "  gate: FAIL (rc=$rc)" >&2

  # optional ingest into a local backend. Keep the CI token (and body) out of argv:
  # token → a 0600 curl config (heredoc, not a -H arg); body → a temp file via --data @.
  if [[ -n "$BACKEND_URL" && -n "$PROJECT_ID" && -n "$CI_TOKEN" ]]; then
    build_id="smoke-$name"
    log "POST $BACKEND_URL/projects/$PROJECT_ID/ci-runs  (jenkinsBuildId=$build_id)"
    cfg="$(mktemp)"; body="$(mktemp)"
    cat > "$cfg" <<CFGEOF
header = "X-CI-Token: $CI_TOKEN"
CFGEOF
    echo "$result_json" | jq --arg b "$build_id" '. + {jenkinsBuildId: $b}' > "$body"
    set +e
    curl -fsS --config "$cfg" -X POST "$BACKEND_URL/projects/$PROJECT_ID/ci-runs" \
      -H "Content-Type: application/json" --data @"$body" \
      | { command -v jq >/dev/null 2>&1 && jq . || cat; }
    set -e
    rm -f "$cfg" "$body"   # always clean up the secret-bearing config + body
  fi
done

log "done"
