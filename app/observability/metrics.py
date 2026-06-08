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

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

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
    """The Prometheus exposition payload + its content type, for the /metrics route."""
    return generate_latest(), CONTENT_TYPE_LATEST
