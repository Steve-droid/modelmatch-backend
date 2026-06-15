#!/usr/bin/env bash
# Throwaway CI compose-stack lifecycle for the BACKEND pipeline (P18). Uses
# docker-compose.ci.yaml (FE+BE+Postgres+migrate, BE = the candidate image under test).
# It NEVER points at the cluster/dev DB — the compose Postgres is self-contained and
# disposable, and every `down` drops the volume so the next run starts empty.
#
# Subcommands:
#   ci/e2e-stack.sh up-db   # only Postgres — for the Integration stage (pytest builds
#                           #   its own throwaway DB on it via tests/conftest.py)
#   ci/e2e-stack.sh up      # full stack: db -> migrate+seed -> backend -> frontend,
#                           #   then wait for the backend /healthz (for the E2E stage)
#   ci/e2e-stack.sh down    # docker compose down -v --remove-orphans (volume gone)
#
# Caller provides via env (the pipeline sets these; local defaults are fine):
#   COMPOSE_PROJECT_NAME   isolates networks/volumes for concurrent builds (default mm-be-ci)
#   BACKEND_IMAGE          backend image under test          (default modelmatch-backend:latest)
#   FRONTEND_IMAGE         frontend image pulled from ECR    (default modelmatch-frontend:latest)
#   JWT_SECRET             REQUIRED for up/up-db (compose interpolates ${JWT_SECRET:?})
#   BACKEND_PORT           host port to poll for /healthz    (default 8000)
#   POSTGRES_PORT          host port Postgres binds          (default 5432)
#   LLM_CLIENT             fake (default) | bedrock (gated e2e-live only)
set -euo pipefail

cmd="${1:-}"
cd "$(dirname "$0")/.."

COMPOSE_FILE="docker-compose.ci.yaml"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-mm-be-ci}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-8080}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
HEALTH_URL="http://localhost:${BACKEND_PORT}/healthz"
FRONTEND_URL="http://localhost:${FRONTEND_PORT}/"

# Pick the available Compose CLI: prefer the v2 plugin (`docker compose`), fall back to
# the v1 standalone (`docker-compose`). The graded controller has the v2 plugin
# (installed in P17); locally (OrbStack) it's v2 too. DC is intentionally unquoted at
# call sites so "docker compose" word-splits into two argv tokens.
if docker compose version >/dev/null 2>&1; then
  DC="docker compose -f ${COMPOSE_FILE}"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose -f ${COMPOSE_FILE}"
else
  echo "ERROR: no Docker Compose found ('docker compose' v2 plugin or 'docker-compose' v1)" >&2
  exit 1
fi
echo "Using Compose CLI: ${DC}"

down() {
  # Compose interpolates ${JWT_SECRET:?...} on EVERY command (down included), so provide
  # a throwaway value when the caller's env no longer has it (e.g. a Jenkins post{} block
  # outside the test's withEnv). It is never used — down starts nothing.
  export JWT_SECRET="${JWT_SECRET:-teardown}"
  $DC down -v --remove-orphans
}

wait_for() {
  local what="$1" probe="$2"
  echo "Waiting for ${what} ..."
  for i in $(seq 1 60); do
    if eval "$probe" >/dev/null 2>&1; then
      echo "${what} ready after ${i} attempt(s)."
      return 0
    fi
    sleep 2
  done
  echo "ERROR: ${what} did not become ready in time. Recent state:" >&2
  $DC ps >&2 || true
  $DC logs --tail=80 >&2 || true
  down || true
  exit 1
}

case "$cmd" in
  up-db)
    : "${JWT_SECRET:?set JWT_SECRET (a throwaway value for the CI stack)}"
    down >/dev/null 2>&1 || true
    $DC up -d db
    wait_for "Postgres at localhost:${POSTGRES_PORT}" \
      "$DC exec -T db pg_isready -U modelmatch -d modelmatch"
    ;;
  up)
    : "${JWT_SECRET:?set JWT_SECRET (a throwaway value for the CI stack)}"
    down >/dev/null 2>&1 || true
    $DC up -d
    wait_for "backend /healthz at ${HEALTH_URL}" "curl -fsS ${HEALTH_URL}"
    # The Playwright smoke hits the FE port first — make sure nginx is actually serving
    # before the next stage runs, or the browser navigation can flake on a cold start.
    wait_for "frontend at ${FRONTEND_URL}" "curl -fsS ${FRONTEND_URL}"
    ;;
  down)
    down
    ;;
  *)
    echo "usage: $0 {up-db|up|down}" >&2
    exit 2
    ;;
esac
