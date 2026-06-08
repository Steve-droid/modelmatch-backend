"""S16 observability tests — the per-request LLM log line + /metrics.

Layers:
- PURE (no DB, no LLM): the log record carries provider/model/status/tokens/latency/
  retrieved-size; inputs appear ONLY as derived metadata (chars/sha256/redactions),
  never raw; the agent omits inputs entirely (so the diff can NEVER appear); failures
  carry status + error_kind (class name only, no message).
- METRICS: record_llm_metrics increments counters (no log); record_llm_call also logs;
  /metrics serves them, including purpose="agent" folded in from /ci-runs.
- INTEGRATION (real call sites, fake LLM): success + failure paths for agent and
  ingestion log a line, redact inputs, and never leak the diff/source/secret/error text.
  (The chat lines live with the chat fixtures in test_chat.py.)
"""

import json
import logging

import pytest
from prometheus_client import REGISTRY

from app.observability import (
    LLMObservation,
    build_log_record,
    count_redactions,
    log_llm_call,
)
from app.observability.metrics import (
    record_llm_call,
    record_llm_metrics,
    render_metrics,
)

# ---------------------------------------------------------------------------
# PURE — redaction counting
# ---------------------------------------------------------------------------

SECRETS = [
    "sk-ant-api03-DEADBEEFdeadbeef1234567890",   # Anthropic key
    "sk-proj-ABCDEFGHIJKLMNOP1234567890",        # generic provider key
    "AKIAIOSFODNN7EXAMPLE",                       # AWS access key id
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc",  # JWT
    "dev@example.com",                            # email (PII)
    "password=hunter2",                           # key=value secret
]


@pytest.mark.parametrize("secret", SECRETS)
def test_count_redactions_detects_known_secret_shapes(secret):
    assert count_redactions(f"prefix {secret} suffix") >= 1


def test_count_redactions_zero_for_clean_text():
    assert count_redactions("just a normal sentence about savings") == 0


# ---------------------------------------------------------------------------
# PURE — the log record shape (no raw inputs)
# ---------------------------------------------------------------------------

def test_log_record_carries_core_fields_and_provider():
    rec = build_log_record(
        LLMObservation(
            purpose="chat",
            provider="bedrock",
            model="apac.amazon.nova-lite-v1:0",
            tokens_in=120,
            tokens_out=42,
            latency_ms=350,
            retrieved_context_size=3,
            status="ok",
        )
    )
    assert rec["event"] == "llm_call"
    for key in (
        "purpose", "provider", "model", "status",
        "tokens_in", "tokens_out", "latency_ms", "retrieved_context_size",
    ):
        assert key in rec
    assert rec["provider"] == "bedrock"
    assert rec["retrieved_context_size"] == 3
    # no failure → no error_kind key
    assert "error_kind" not in rec


def test_log_record_reduces_inputs_to_metadata_only():
    secret = "sk-ant-api03-SECRETSECRETSECRET12345"
    rec = build_log_record(
        LLMObservation(
            purpose="ingestion",
            provider="bedrock",
            model="nova",
            tokens_in=1,
            tokens_out=1,
            prompt=f"contact dev@example.com key {secret} " + "y" * 400,
            query="SELECT 1 -- mail@corp.io",
        )
    )
    blob = json.dumps(rec)
    # NO raw text of any kind
    assert secret not in blob
    assert "dev@example.com" not in blob
    assert "mail@corp.io" not in blob
    assert "SELECT 1" not in blob
    assert "yyyy" not in blob
    # only safe derived metadata
    assert rec["prompt_chars"] > 400
    assert len(rec["prompt_sha256"]) == 12
    assert rec["prompt_redactions"] >= 2   # email + key
    assert rec["query_chars"] == len("SELECT 1 -- mail@corp.io")
    assert rec["query_redactions"] == 1    # the email in the comment


def test_agent_observation_omits_inputs_entirely():
    # The agent never passes prompt/query (the diff must never be logged) → omitted.
    rec = build_log_record(
        LLMObservation(
            purpose="agent", provider="anthropic",
            model="claude-haiku-4-5", tokens_in=9, tokens_out=2,
        )
    )
    assert not any(k.startswith(("prompt_", "query_")) for k in rec)


def test_failure_record_carries_status_and_error_kind_only():
    rec = build_log_record(
        LLMObservation(
            purpose="ingestion", provider="bedrock", model="nova",
            tokens_in=0, tokens_out=0, status="error", error_kind="RuntimeError",
        )
    )
    assert rec["status"] == "error"
    assert rec["error_kind"] == "RuntimeError"


def test_log_llm_call_emits_one_json_line(caplog):
    with caplog.at_level(logging.INFO, logger="modelmatch.llm"):
        log_llm_call(
            LLMObservation(purpose="agent", provider="fake", model="m",
                           tokens_in=1, tokens_out=1)
        )
    lines = [r.getMessage() for r in caplog.records if r.name == "modelmatch.llm"]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event"] == "llm_call" and parsed["purpose"] == "agent"


# ---------------------------------------------------------------------------
# METRICS
# ---------------------------------------------------------------------------

def _sample(name: str, **labels) -> float:
    val = REGISTRY.get_sample_value(name, labels)
    return val if val is not None else 0.0


def test_record_llm_metrics_increments_without_logging(caplog):
    labels = {"model": "metric-only-model", "purpose": "ingestion"}
    before = _sample("modelmatch_llm_tokens_in_total", **labels)
    with caplog.at_level(logging.INFO, logger="modelmatch.llm"):
        record_llm_metrics(
            LLMObservation(purpose="ingestion", model="metric-only-model",
                           tokens_in=50, tokens_out=10, status="ok")
        )
    assert _sample("modelmatch_llm_tokens_in_total", **labels) == before + 50
    # metrics-only: NO log line emitted
    assert [r for r in caplog.records if r.name == "modelmatch.llm"] == []


def test_record_llm_call_increments_counters_and_labels_status():
    labels = {"model": "metric-test-model", "purpose": "ingestion"}
    before_in = _sample("modelmatch_llm_tokens_in_total", **labels)
    before_out = _sample("modelmatch_llm_tokens_out_total", **labels)
    before_ok = _sample("modelmatch_llm_calls_total", **labels, status="ok")

    record_llm_call(
        LLMObservation(purpose="ingestion", model="metric-test-model",
                       tokens_in=100, tokens_out=25, latency_ms=120, status="ok")
    )

    assert _sample("modelmatch_llm_tokens_in_total", **labels) == before_in + 100
    assert _sample("modelmatch_llm_tokens_out_total", **labels) == before_out + 25
    assert _sample("modelmatch_llm_calls_total", **labels, status="ok") == before_ok + 1


def test_record_llm_call_failure_increments_error_status():
    labels = {"model": "err-model", "purpose": "chat"}
    before = _sample("modelmatch_llm_calls_total", **labels, status="error")
    record_llm_call(
        LLMObservation(purpose="chat", model="err-model", tokens_in=0, tokens_out=0,
                       status="error", error_kind="RuntimeError")
    )
    assert _sample("modelmatch_llm_calls_total", **labels, status="error") == before + 1


def test_render_metrics_exposes_the_series_with_labels():
    record_llm_call(
        LLMObservation(purpose="chat", model="render-test-model",
                       tokens_in=7, tokens_out=3)
    )
    payload, content_type = render_metrics()
    text = payload.decode()
    assert "text/plain" in content_type
    assert "modelmatch_llm_tokens_in_total" in text
    assert 'model="render-test-model"' in text and 'purpose="chat"' in text


def test_metrics_endpoint_serves_prometheus_text():
    from fastapi.testclient import TestClient

    from app.main import app

    resp = TestClient(app).get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "modelmatch_llm_tokens_in_total" in resp.text


# ---------------------------------------------------------------------------
# METRICS — purpose="agent" folded in from /ci-runs (deterministic, no LLM)
# ---------------------------------------------------------------------------

def test_ci_runs_ingest_feeds_agent_token_metrics(client, db_session):
    from app.catalog.seed import load_seed
    from tests.test_ci import _agent_result, _project_with_token

    labels = {"model": "claude-haiku-4-5", "purpose": "agent"}
    before = _sample("modelmatch_llm_tokens_in_total", **labels)

    load_seed(db_session)
    pid, token = _project_with_token(client, db_session, "obs_ci@example.com")
    resp = client.post(
        f"/projects/{pid}/ci-runs",
        json=_agent_result("obs-build-1"),
        headers={"X-CI-Token": token},
    )
    assert resp.status_code == 201

    # The agent's tokensIn (1200) is now on /metrics under purpose="agent".
    assert _sample("modelmatch_llm_tokens_in_total", **labels) == before + 1200
    assert _sample("modelmatch_llm_calls_total", **labels, status="ok") >= 1
    assert "modelmatch_llm_tokens_in_total" in client.get("/metrics").text


# ---------------------------------------------------------------------------
# INTEGRATION — the agent: success + failures, never the diff
# ---------------------------------------------------------------------------

DIFF = "--- a/secrets.py\n+++ b/secrets.py\n@@\n+API_KEY = 'sk-ant-LEAKED-DIFF-CONTENT'\n+exec(user_input)\n"


def _agent_config(**kw):
    from agent.config import AgentConfig

    base = dict(
        llm_client="fake", model_id="fake-model", max_tokens=512,
        token_ceiling=100_000, fail_severities=["high", "critical"],
    )
    base.update(kw)
    return AgentConfig(**base)


def _agent_lines(caplog):
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "modelmatch.llm"
    ]


def _assert_no_diff_leak(caplog):
    assert "exec(user_input)" not in caplog.text
    assert "sk-ant-LEAKED-DIFF-CONTENT" not in caplog.text
    assert "secrets.py" not in caplog.text


def test_agent_success_logs_provider_and_status_without_diff(caplog):
    from agent.review import review
    from app.llm.fake import FakeLLMClient

    with caplog.at_level(logging.INFO, logger="modelmatch.llm"):
        review(DIFF, FakeLLMClient('{"findings": []}', model="haiku-test"),
               _agent_config(llm_client="fake"))

    lines = _agent_lines(caplog)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["purpose"] == "agent" and rec["status"] == "ok"
    assert rec["provider"] == "fake" and rec["model"] == "haiku-test"
    assert not any(k.startswith(("prompt_", "query_")) for k in rec)
    _assert_no_diff_leak(caplog)


def test_agent_provider_error_logs_error_without_message_or_diff(caplog):
    from agent.review import review

    class _Raising:
        def complete(self, system, user, max_tokens):
            raise RuntimeError("boom from provider sk-ant-SHOULD-NOT-LEAK")

    with caplog.at_level(logging.INFO, logger="modelmatch.llm"):
        with pytest.raises(RuntimeError):
            review(DIFF, _Raising(), _agent_config())

    rec = _agent_lines(caplog)[-1]
    assert rec["status"] == "error" and rec["error_kind"] == "RuntimeError"
    assert "boom from provider" not in caplog.text       # no exception message
    assert "sk-ant-SHOULD-NOT-LEAK" not in caplog.text   # no secret from the message
    _assert_no_diff_leak(caplog)


def test_agent_malformed_output_logs_error(caplog):
    from agent.review import MalformedFindings, review
    from app.llm.fake import FakeLLMClient

    with caplog.at_level(logging.INFO, logger="modelmatch.llm"):
        with pytest.raises(MalformedFindings):
            review(DIFF, FakeLLMClient("not json at all", model="m"), _agent_config())

    rec = _agent_lines(caplog)[-1]
    assert rec["status"] == "error" and rec["error_kind"] == "MalformedFindings"
    assert rec["model"] == "m"   # post-call → the provider's reported model
    _assert_no_diff_leak(caplog)


def test_agent_post_call_token_ceiling_logs_error(caplog):
    from agent.review import (
        SYSTEM_PROMPT,
        TokenCeilingExceeded,
        build_user_prompt,
        review,
    )
    from app.llm.base import approx_tokens
    from app.llm.fake import FakeLLMClient

    # Pre-call estimate (max_tokens=1) passes the ceiling, but the actual big response
    # blows it → the POST-call check fires.
    estimate = approx_tokens(SYSTEM_PROMPT) + approx_tokens(build_user_prompt(DIFF)) + 1
    big = "y" * 8000  # ~2000 output tokens, well over the ceiling
    config = _agent_config(max_tokens=1, token_ceiling=estimate + 5)

    with caplog.at_level(logging.INFO, logger="modelmatch.llm"):
        with pytest.raises(TokenCeilingExceeded):
            review(DIFF, FakeLLMClient(big, model="ceil-model"), config)

    rec = _agent_lines(caplog)[-1]
    assert rec["status"] == "error" and rec["error_kind"] == "TokenCeilingExceeded"
    assert rec["model"] == "ceil-model"  # post-call path saw the response
    _assert_no_diff_leak(caplog)


# ---------------------------------------------------------------------------
# INTEGRATION — ingestion: success + failures, source redacted
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

NOVA_ROWS = (
    Path(__file__).resolve().parent / "fixtures" / "nova_response.json.txt"
).read_text()
SOURCE_WITH_SECRET = (
    "Model card. Maintainer: dev@example.com  key=sk-ant-api03-SOURCESECRET123456"
)


def _ingest_lines(caplog):
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "modelmatch.llm"
    ]


def test_ingest_success_logs_provider_with_source_redacted(db_session, caplog):
    from app.blob_store import InMemoryBlobStore
    from app.ingest.service import ingest_source
    from app.llm.fake import FakeLLMClient
    from app.schemas.ingest import IngestRequest

    with caplog.at_level(logging.INFO, logger="modelmatch.llm"):
        ingest_source(
            db_session,
            IngestRequest(source_text=SOURCE_WITH_SECRET, kind="model_card"),
            FakeLLMClient(responses=NOVA_ROWS, model="nova-ingest-test"),
            blob=InMemoryBlobStore(),
        )

    rec = _ingest_lines(caplog)[-1]
    assert rec["purpose"] == "ingestion" and rec["status"] == "ok"
    assert rec["provider"] == "fake" and rec["model"] == "nova-ingest-test"
    # source reduced to metadata only — its secret + PII never appear
    assert "sk-ant-api03-SOURCESECRET123456" not in caplog.text
    assert "dev@example.com" not in caplog.text
    assert "Model card" not in caplog.text
    assert rec["prompt_redactions"] >= 2


def test_ingest_provider_error_logs_error_without_message(db_session, caplog):
    from app.ingest.service import ingest_source
    from app.schemas.ingest import IngestRequest

    class _Raising:
        def complete(self, system, user, max_tokens):
            raise RuntimeError("nova exploded dev@example.com")

    with caplog.at_level(logging.INFO, logger="modelmatch.llm"):
        with pytest.raises(RuntimeError):
            ingest_source(
                db_session,
                IngestRequest(source_text=SOURCE_WITH_SECRET, kind="model_card"),
                _Raising(),
            )

    rec = _ingest_lines(caplog)[-1]
    assert rec["purpose"] == "ingestion" and rec["status"] == "error"
    assert rec["error_kind"] == "RuntimeError"
    assert "nova exploded" not in caplog.text          # no exception message
    assert "dev@example.com" not in caplog.text         # no PII from message/source


def test_ingest_hourly_cap_logs_throttled(db_session, caplog):
    from app.ingest.service import ingest_source
    from app.llm.fake import FakeLLMClient
    from app.llm_budget import HourlyTokenCapExceeded
    from app.schemas.ingest import IngestRequest

    with caplog.at_level(logging.INFO, logger="modelmatch.llm"):
        with pytest.raises(HourlyTokenCapExceeded):
            ingest_source(
                db_session,
                IngestRequest(source_text="a tiny source", kind="model_card"),
                FakeLLMClient(responses=NOVA_ROWS, model="nova"),
                hourly_cap=1,  # any real call busts a 1-token hour → throttled, no call
            )

    rec = _ingest_lines(caplog)[-1]
    assert rec["status"] == "throttled"
    assert rec["error_kind"] == "HourlyTokenCapExceeded"
    assert rec["tokens_in"] == 0 and rec["tokens_out"] == 0
