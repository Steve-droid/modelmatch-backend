"""S10a agent tests: review a diff offline (fake client) → findings + gate.

Covers the done-criteria: runs on the fake client, produces findings JSON, applies
the gate, enforces the token ceiling, rejects malformed output, and never writes files.
Plus a CLI smoke test (`python -m agent`).
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _installed(modname: str) -> bool:
    try:
        return importlib.util.find_spec(modname) is not None
    except ModuleNotFoundError:
        return False


def _run_cli(diff: str, env_overrides: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "agent"],
        input=diff,
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env={**os.environ, **env_overrides},
    )

from agent.config import AgentConfig
from agent.review import (
    MalformedFindings,
    TokenCeilingExceeded,
    apply_gate,
    parse_findings,
    review,
)
from app.llm.fake import FakeLLMClient
from app.schemas.findings import Finding

ROOT = Path(__file__).resolve().parent.parent
def _structured_error(stderr: str) -> dict:
    """The CLI contract puts the S16 per-request LLM log line(s) AND the final
    structured-error JSON on stderr, so the error is the LAST non-empty line — not the
    whole stream. (On paths that fail before any LLM call, stderr is just that one line.)"""
    return json.loads(stderr.strip().splitlines()[-1])


DIFF = "--- a/app.py\n+++ b/app.py\n@@\n-pass\n+exec(user_input)\n"

HIGH = '{"findings":[{"severity":"high","category":"security","file":"app.py","line":4,"message":"exec on user input"}]}'
LOW = '{"findings":[{"severity":"low","category":"style","file":"app.py","line":2,"message":"naming"}]}'


def _config(**kw) -> AgentConfig:
    base = dict(llm_client="fake", max_tokens=1024, token_ceiling=100_000, fail_severities=["high", "critical"])
    base.update(kw)
    return AgentConfig(**base)


def test_review_parses_findings_and_reports_tokens_and_model():
    res = review(DIFF, FakeLLMClient(HIGH, model="m1"), _config())
    assert len(res.findings) == 1
    assert res.findings[0].category == "security"
    assert res.findings[0].severity == "high"
    assert res.tokens_in > 0 and res.tokens_out > 0
    assert res.model == "m1"


def test_gate_fails_on_blocking_severity():
    res = review(DIFF, FakeLLMClient(HIGH), _config())
    assert res.gate == "fail"
    assert res.gate_reason and "high" in res.gate_reason


def test_gate_passes_on_non_blocking_severity():
    res = review(DIFF, FakeLLMClient(LOW), _config())
    assert res.gate == "pass"
    assert res.gate_reason is None


def test_empty_findings_passes_clean():
    res = review(DIFF, FakeLLMClient(), _config())  # default = empty findings
    assert res.findings == []
    assert res.gate == "pass"


# Real models (e.g. Bedrock Nova) wrap the JSON in a markdown fence — surfaced by
# the live smoke (docs/tests/). The parser must tolerate it; tested offline here.
@pytest.mark.parametrize("fenced", [
    '```json\n{"findings":[{"severity":"high","category":"security","file":"app.py","line":4,"message":"exec on user input"}]}\n```',
    '```\n{"findings":[{"severity":"high","category":"security","file":"app.py","line":4,"message":"exec on user input"}]}\n```',
])
def test_parse_findings_tolerates_markdown_fence(fenced):
    findings = parse_findings(fenced)
    assert len(findings) == 1 and findings[0].severity == "high"


def test_review_parses_fenced_model_output():
    fenced = "```json\n" + HIGH + "\n```"
    res = review(DIFF, FakeLLMClient(fenced), _config())
    assert res.gate == "fail" and len(res.findings) == 1


@pytest.mark.parametrize("bad", [
    "not json at all",
    '{"foo": 1}',                                   # no findings key, not a list
    '{"findings": [{"severity": "high"}]}',         # missing required fields
    '{"findings": [{"severity": "nope", "category": "security", "file": "x", "message": "m"}]}',  # bad enum
])
def test_malformed_model_output_is_rejected(bad):
    with pytest.raises(MalformedFindings):
        review(DIFF, FakeLLMClient(bad), _config())


def test_token_ceiling_aborts_the_run():
    with pytest.raises(TokenCeilingExceeded):
        review(DIFF, FakeLLMClient(HIGH), _config(token_ceiling=1))


def test_review_never_writes_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(os.listdir(tmp_path))
    review(DIFF, FakeLLMClient(HIGH), _config())
    assert set(os.listdir(tmp_path)) == before  # nothing written


def test_agent_env_vars_are_read():
    """The documented AGENT_* env names actually configure the agent."""
    import os
    from unittest import mock

    env = {
        "AGENT_MAX_TOKENS": "256",
        "AGENT_TOKEN_CEILING": "500",
        "AGENT_FAIL_SEVERITIES": "critical",
        "AGENT_MODEL": "claude-haiku-4",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        cfg = AgentConfig(_env_file=None)
    assert cfg.max_tokens == 256
    assert cfg.token_ceiling == 500
    assert cfg.fail_severities == ["critical"]
    assert cfg.model_id == "claude-haiku-4"


def test_agent_reads_bedrock_region_from_standard_aws_env():
    """The agent's Bedrock path must honor the standard AWS region env exported by
    the generated Jenkins snippet."""
    import os
    from unittest import mock

    env = {"LLM_CLIENT": "bedrock", "AWS_DEFAULT_REGION": "ap-south-1"}
    with mock.patch.dict(os.environ, env, clear=False):
        cfg = AgentConfig(_env_file=None)
    assert cfg.llm_client == "bedrock"
    assert cfg.aws_region == "ap-south-1"


def test_cli_main_passes_bedrock_region_to_client(monkeypatch, capsys):
    import agent.__main__ as agent_main

    calls = {}
    fake_client = object()

    monkeypatch.setattr(agent_main, "_read_diff", lambda _path: DIFF)

    def _build(name, *, model="fake-model", api_key=None, region=None, client=None):
        calls.update({"name": name, "model": model, "region": region})
        return fake_client

    def _review(diff, client, config):
        assert diff == DIFF
        assert client is fake_client
        return SimpleNamespace(
            gate="pass",
            model_dump_json=lambda by_alias=True: json.dumps(
                {
                    "findings": [],
                    "tokensIn": 0,
                    "tokensOut": 0,
                    "model": config.model_id,
                    "gate": "pass",
                    "gateReason": None,
                }
            ),
        )

    monkeypatch.setattr(agent_main, "build_llm_client", _build)
    monkeypatch.setattr(agent_main, "review", _review)
    monkeypatch.setattr(
        agent_main.logging, "basicConfig", lambda **_kwargs: None
    )  # keep test stderr quiet

    from unittest import mock

    env = {
        "LLM_CLIENT": "bedrock",
        "AGENT_MODEL": "global.amazon.nova-2-lite-v1:0",
        "AWS_DEFAULT_REGION": "ap-south-1",
    }
    with mock.patch.dict(os.environ, env, clear=False):
        code = agent_main.main([])

    out = capsys.readouterr().out
    assert code == 0
    assert calls == {
        "name": "bedrock",
        "model": "global.amazon.nova-2-lite-v1:0",
        "region": "ap-south-1",
    }
    assert '"gate": "pass"' in out


class _SpyClient:
    """Records whether the provider was called (to prove preflight short-circuits)."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system, user, max_tokens):
        from app.llm.base import LLMResponse

        self.calls += 1
        return LLMResponse('{"findings": []}', 1, 1, "spy")


def test_oversized_diff_aborts_before_calling_the_provider():
    spy = _SpyClient()
    big_diff = "+" + ("x" * 100_000)
    with pytest.raises(TokenCeilingExceeded):
        review(big_diff, spy, _config(token_ceiling=50, max_tokens=10))
    assert spy.calls == 0  # never spent on the user's key


def test_apply_gate_unit():
    high = Finding(severity="high", category="security", file="x", message="m")
    assert apply_gate([high], ["high", "critical"])[0] == "fail"
    assert apply_gate([], ["high", "critical"]) == ("pass", None)


def test_cli_runs_on_fake_and_exits_zero():
    proc = subprocess.run(
        [sys.executable, "-m", "agent"],
        input=DIFF,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={**os.environ, "LLM_CLIENT": "fake"},
    )
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)  # valid JSON on stdout
    assert body["findings"] == []
    assert body["gate"] == "pass"
    assert "tokensIn" in body and "tokensOut" in body  # camelCase contract


def test_cli_invalid_llm_client_exits_4_with_structured_error():
    proc = _run_cli(DIFF, {"LLM_CLIENT": "nope"})
    assert proc.returncode == 4
    assert proc.stdout == ""  # nothing half-written to stdout
    assert "Traceback" not in proc.stderr  # no raw traceback
    err = _structured_error(proc.stderr)  # structured JSON on the last stderr line
    assert err["error"] == "llm_client_error"
    assert "nope" in err["detail"]
    assert "exec(user_input)" not in proc.stderr  # diff never leaked


def test_cli_missing_diff_file_exits_4_with_structured_error(tmp_path):
    missing = tmp_path / "no-such-file.patch"
    proc = subprocess.run(
        [sys.executable, "-m", "agent", "--diff", str(missing)],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        env={**os.environ, "LLM_CLIENT": "fake"},
    )
    assert proc.returncode == 4
    assert proc.stdout == ""
    assert "Traceback" not in proc.stderr  # no raw traceback
    err = _structured_error(proc.stderr)
    assert err["error"] == "diff_read_error"


@pytest.mark.skipif(_installed("anthropic"), reason="anthropic installed → would attempt a real call")
def test_cli_missing_sdk_exits_4_with_structured_error():
    # anthropic isn't installed in the dev venv → RuntimeError at call time
    proc = _run_cli(DIFF, {"LLM_CLIENT": "anthropic", "AGENT_MODEL": "claude-haiku-4"})
    assert proc.returncode == 4
    assert proc.stdout == ""
    assert "Traceback" not in proc.stderr
    err = _structured_error(proc.stderr)
    assert err["error"] == "llm_client_error"
    assert "exec(user_input)" not in proc.stderr  # diff never leaked
