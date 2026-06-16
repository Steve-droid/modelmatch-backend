"""Prometheus metrics for the in-cluster LLM uses (S16) — tokens, NOT dollars.

Per the build module: Prometheus counts **tokens** (labeled by model + purpose) and
Grafana multiplies by a configmap $/1k rate — cost is never a metric here. `/metrics`
(see `app.main`) serves these for scraping.

Two backend seams:
- `record_llm_call` — increment the counters AND emit the per-request log line (so a
  call site can't update one without the other). Used by the in-cluster LLM uses
  (ingestion #3, chat #4), success and failure alike.
- `record_llm_metrics` — increment the counters ONLY (no log line). Used by the
  deterministic `/ci-runs` ingest to fold the AGENT's token counts (purpose="agent")
  into the backend's metrics: no LLM call happens there, so emitting an `llm_call` log
  line would be misleading — but the agent's tokens still belong on our `/metrics`.

The CI agent itself uses `log_llm_call` directly — it has no /metrics endpoint to
scrape, and importing this module is the only thing that pulls in prometheus_client.
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from app.observability.logging import LLMObservation, log_llm_call

_LABELS = ("model", "purpose")

LLM_TOKENS_IN = Counter(
    "modelmatch_llm_tokens_in_total",
    "LLM input (prompt) tokens, by model and purpose.",
    _LABELS,
)
LLM_TOKENS_OUT = Counter(
    "modelmatch_llm_tokens_out_total",
    "LLM output (completion) tokens, by model and purpose.",
    _LABELS,
)
LLM_CALLS = Counter(
    "modelmatch_llm_calls_total",
    "LLM calls, by model, purpose and status (ok / error / throttled).",
    (*_LABELS, "status"),
)
LLM_LATENCY = Histogram(
    "modelmatch_llm_latency_seconds",
    "LLM call latency in seconds, by model and purpose.",
    _LABELS,
)

# --- HTTP request signals (P20 app dashboard: rate / latency / error rate) ----------
# `path` is the ROUTE TEMPLATE (e.g. "/projects/{project_id}/ci-runs"), never the raw
# URL — so an id in the path can't explode label cardinality. `status` is the numeric
# HTTP status code as a string. Error rate is derived in Grafana as the 5xx share.
_HTTP_LABELS = ("method", "path", "status")
# Web-shaped buckets (5ms … 10s) — finer at the low end where our endpoints live.
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "modelmatch_http_requests_total",
    "HTTP requests, by method, route template and status code.",
    _HTTP_LABELS,
)
HTTP_LATENCY = Histogram(
    "modelmatch_http_request_duration_seconds",
    "HTTP request duration in seconds, by method and route template.",
    ("method", "path"),
    buckets=_HTTP_BUCKETS,
)

# --- DB query timing (P20 app dashboard) --------------------------------------------
# `operation` is the leading SQL verb (SELECT / INSERT / UPDATE / DELETE / OTHER) — a
# bounded label, not the statement text (which could carry values/PII).
_DB_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)

DB_QUERY_DURATION = Histogram(
    "modelmatch_db_query_duration_seconds",
    "Database query duration in seconds, by SQL operation.",
    ("operation",),
    buckets=_DB_BUCKETS,
)


def observe_http(method: str, path: str, status: int, duration_s: float) -> None:
    """Record one served HTTP request: bump the per-status counter and the
    method+route latency histogram. Called from the metrics middleware."""
    status_str = str(status)
    HTTP_REQUESTS.labels(method, path, status_str).inc()
    HTTP_LATENCY.labels(method, path).observe(duration_s)


def observe_db_query(operation: str, duration_s: float) -> None:
    """Record one DB statement's wall-clock duration under its SQL verb."""
    DB_QUERY_DURATION.labels(operation).observe(duration_s)


def record_llm_metrics(obs: LLMObservation) -> None:
    """Increment the token/call/latency counters for one LLM call. Metrics ONLY — no
    log line. `provider` is intentionally NOT a label (cardinality control); it appears
    in the log line instead. `status` labels the call counter (ok / error / throttled)."""
    LLM_TOKENS_IN.labels(obs.model, obs.purpose).inc(obs.tokens_in)
    LLM_TOKENS_OUT.labels(obs.model, obs.purpose).inc(obs.tokens_out)
    LLM_CALLS.labels(obs.model, obs.purpose, obs.status).inc()
    if obs.latency_ms is not None:
        LLM_LATENCY.labels(obs.model, obs.purpose).observe(obs.latency_ms / 1000.0)


def record_llm_call(obs: LLMObservation) -> dict[str, object]:
    """Increment the metrics for one LLM call, then emit its log line. Returns the log
    record. The combined observability seam for the backend's in-cluster LLM uses."""
    record_llm_metrics(obs)
    return log_llm_call(obs)


def render_metrics() -> tuple[bytes, str]:
    """The Prometheus exposition payload + its content type, for the /metrics route.

    Under gunicorn the app runs with multiple worker processes, each of which would
    otherwise keep a PRIVATE in-process registry — so Prometheus scrapes whichever
    worker the load-balancer picks and the counters appear to jump backwards between
    workers, breaking `rate()`. When ``PROMETHEUS_MULTIPROC_DIR`` is set (the container),
    every worker writes to a shared mmap dir and we AGGREGATE across them here via the
    MultiProcessCollector. Unset (tests / local uvicorn) → the default in-process
    registry, unchanged."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess  # lazy: only when multiproc is on

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
