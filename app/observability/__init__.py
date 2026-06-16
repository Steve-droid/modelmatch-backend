"""Observability (S16): per-request LLM log line + Prometheus token metrics.

This package __init__ re-exports the **logging** half ONLY — it is deliberately free
of the metrics import so the standalone CI agent can `from app.observability import
log_llm_call` without dragging in prometheus_client (or a /metrics endpoint it has no
use for). The backend imports `app.observability.metrics` explicitly for the metrics
seams (`record_llm_call` = metrics + log; `record_llm_metrics` = metrics only).
"""

from app.observability.logging import (
    LLMObservation,
    build_log_record,
    configure_logging,
    count_redactions,
    log_llm_call,
)

__all__ = [
    "LLMObservation",
    "build_log_record",
    "configure_logging",
    "count_redactions",
    "log_llm_call",
]
