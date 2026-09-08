"""Security task on fake-opencode.py + golden Semgrep JSON. $0, no provider.

Covers the P38d done-criteria for the loop: the OpenCode invocation (-m
provider/model, the verbatim auditor prompt), Semgrep → findings with CWE, the
critical gate, retries on refusal AND malformed output, the three ceilings, the
empty-mount guard, and the token accounting (input + output, never total).
"""

from __future__ import annotations

import json

import pytest

from agent.security import (
    _balanced_json_blocks,
    extract_results,
    map_severity,
    to_findings,
)
from tests.agent.conftest import FIXTURES, invocations, recorded_argv

DEEPSEEK_GOLDEN = FIXTURES / "sample-run-deepseek.json"
GEMINI_GOLDEN = FIXTURES / "sample-run-gemini.json"


def _sec_env(workspace, fake_opencode, **extra) -> dict[str, str]:
    env = {
        "MODELMATCH_TASK": "security",
        "AGENT_MODEL": "deepseek/deepseek-v4-flash",
        "AGENT_WORKSPACE": str(workspace),
        **fake_opencode,
    }
    env.update({k: str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------- the happy paths


def test_critical_finding_fails_the_gate_with_cwe(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode))
    assert run.returncode == 1, run.stderr
    body = run.result
    assert body["gate"] == "fail" and "critical" in body["gateReason"]
    [f] = body["findings"]
    assert f["severity"] == "critical"
    assert f["category"] == "security"
    assert f["file"] == "app/main.py" and f["line"] == 42
    assert f["cwe"] == "CWE-89: SQL Injection"
    assert body["model"] == "deepseek/deepseek-v4-flash"


def test_clean_scan_exits_zero(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_MODE="empty"))
    assert run.returncode == 0, run.stderr
    assert run.result["findings"] == [] and run.result["gate"] == "pass"


def test_opencode_is_invoked_with_the_model_and_the_verbatim_auditor_prompt(
    run_agent, workspace, fake_opencode
):
    run_agent(_sec_env(workspace, fake_opencode))
    [argv] = recorded_argv(fake_opencode)
    assert argv[:4] == ["run", "--format", "json", "-m"]
    assert argv[4] == "deepseek/deepseek-v4-flash"
    prompt = argv[5]
    assert prompt.startswith("# Security Code Auditor")            # RealVuln's prompt, verbatim
    assert "1. List all Python files in this repo" in prompt         # RealVuln's scaffold
    assert "Output ONLY the JSON findings object" in prompt


def test_golden_deepseek_run_maps_four_findings(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_MODE="prose_fence",
                             FAKE_RESULTS_FILE=str(DEEPSEEK_GOLDEN)))
    assert run.returncode == 1, run.stderr
    sev = [f["severity"] for f in run.result["findings"]]
    assert sev == ["critical", "medium", "low", "low"]
    cwes = [f["cwe"] for f in run.result["findings"]]
    assert cwes[0] == "CWE-1336: Server-Side Template Injection"
    assert all(c and c.startswith("CWE-") for c in cwes)


def test_golden_gemini_run_keeps_first_cwe_of_many(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_RESULTS_FILE=str(GEMINI_GOLDEN)))
    assert run.returncode == 1
    first, second = run.result["findings"]
    assert first["cwe"].startswith("CWE-94")
    assert second["severity"] == "low" and second["cwe"].startswith("CWE-798")


def test_braces_inside_a_finding_message_are_not_a_parse_failure(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_MODE="braces"))
    assert run.returncode == 1, run.stderr
    assert "{{7*7}}" in run.result["findings"][0]["message"]


# ---------------------------------------------------------------- tokens


def test_tokens_are_input_plus_output_never_total(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_STEPS=4, FAKE_STEP_TOKENS=1000,
                             FAKE_CACHE_READ=5000))
    body = run.result
    assert body["tokensIn"] == 4 * 1000
    assert body["tokensOut"] == 4 * 100
    # `total` (input+output+cache) would be 4 * 6100 = 24400 — it never appears.
    assert "24400" not in run.stdout
    assert "cacheReadTokens" not in body and "tokensTotal" not in body and "total" not in body
    # The cache reads are still visible — on stderr, for the humans.
    assert run.summary["cache_read_tokens"] == 4 * 5000
    assert run.summary["total_reported_tokens"] == 4 * 6100


def test_every_attempt_the_key_paid_for_is_counted(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_MODE="sequence",
                             FAKE_SEQUENCE="refusal,success", FAKE_STEPS=2, FAKE_STEP_TOKENS=500))
    assert run.returncode == 1, run.stderr                # recovered on attempt 2
    assert invocations(fake_opencode) == 2
    assert run.result["tokensIn"] == 2 * 2 * 500           # both attempts
    assert run.summary["attempts"] == 2 and run.summary["refusals"] == 1


# ---------------------------------------------------------------- non-pass outcomes


def test_refusal_on_every_attempt_exits_3_never_clean(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_MODE="refusal"))
    assert run.returncode == 3
    assert run.stdout == ""                                 # no result JSON → nothing to mistake for clean
    assert run.error["error"] == "model_refused"
    assert "NOT a clean result" in run.error["detail"]
    assert invocations(fake_opencode) == 3                  # AGENT_MAX_ATTEMPTS default 3


def test_malformed_output_on_every_attempt_exits_2(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_MODE="malformed", AGENT_MAX_ATTEMPTS=2))
    assert run.returncode == 2
    assert run.error["error"] == "malformed_findings"
    assert invocations(fake_opencode) == 2


def test_provider_failure_before_any_output_exits_4_without_retry(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_MODE="fail"))
    assert run.returncode == 4
    assert run.error["error"] == "llm_provider_error"
    assert invocations(fake_opencode) == 1                  # not a flake → no retry on the user's key


@pytest.mark.parametrize("knob, value, which", [
    ("AGENT_TOKEN_CEILING", "2500", "token"),        # 3 steps × 1100 billable = 3300 > 2500
    ("AGENT_MAX_STEPS", "2", "step"),
])
def test_token_and_step_ceilings_abort_mid_run_with_124(run_agent, workspace, fake_opencode, knob, value, which):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_STEPS=6, **{knob: value}))
    assert run.returncode == 124, run.stderr
    assert run.error["error"] == "ceiling_exceeded"
    assert which in run.error["detail"]
    assert run.summary["ceiling"] == which


def test_wall_clock_ceiling_aborts_with_124(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_STEPS=30, FAKE_DELAY="0.2",
                             AGENT_MAX_SECONDS="1"))
    assert run.returncode == 124, run.stderr
    assert "wall-clock" in run.error["detail"]


def test_empty_workspace_is_a_config_error_not_a_clean_scan(run_agent, tmp_path, fake_opencode):
    empty = tmp_path / "empty"
    empty.mkdir()
    run = run_agent(_sec_env(empty, fake_opencode))
    assert run.returncode == 4
    assert "bind mount" in run.error["detail"]
    assert invocations(fake_opencode) == 0                  # never even started OpenCode


def test_missing_workspace_is_a_config_error(run_agent, tmp_path, fake_opencode):
    run = run_agent(_sec_env(tmp_path / "nope", fake_opencode))
    assert run.returncode == 4 and invocations(fake_opencode) == 0


def test_security_model_must_be_provider_slash_model(run_agent, workspace, fake_opencode):
    run = run_agent(_sec_env(workspace, fake_opencode, AGENT_MODEL="deepseek-v4-flash"))
    assert run.returncode == 4 and "opencode-provider" in run.error["detail"]


def test_gate_severity_is_critical_only_by_default(run_agent, workspace, fake_opencode, tmp_path):
    high_only = {"version": "1.0.0", "results": [{
        "check_id": "x", "path": "app/main.py", "start": {"line": 3},
        "extra": {"message": "ERROR without HIGH confidence → high, not critical",
                  "severity": "ERROR", "metadata": {"cwe": ["CWE-79"], "confidence": "MEDIUM"}},
    }]}
    p = tmp_path / "high.json"
    p.write_text(json.dumps(high_only))
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_RESULTS_FILE=str(p)))
    assert run.returncode == 0                              # high does not block the security stage
    assert run.result["findings"][0]["severity"] == "high"
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_RESULTS_FILE=str(p),
                             AGENT_FAIL_SEVERITIES="high,critical"))
    assert run.returncode == 1                              # unless the user says so


def test_stderr_never_carries_the_key(run_agent, workspace, fake_opencode):
    env = _sec_env(workspace, fake_opencode, FAKE_MODE="fail", DEEPSEEK_API_KEY="sk-verysecretkey-12345678")
    run = run_agent(env)
    assert "sk-verysecretkey" not in run.stderr and "sk-verysecretkey" not in run.stdout


# ---------------------------------------------------------------- units


@pytest.mark.parametrize("sev, conf, expected", [
    ("ERROR", "HIGH", "critical"),
    ("ERROR", "MEDIUM", "high"),
    ("ERROR", "", "high"),
    ("WARNING", "HIGH", "medium"),
    ("INFO", "HIGH", "low"),
    ("weird", "HIGH", "low"),
])
def test_map_severity_table(sev, conf, expected):
    assert map_severity(sev, conf) == expected


def test_extract_results_tolerates_prose_and_fences_and_takes_the_last_object():
    body = json.dumps({"version": "1.0.0", "results": []})
    text = 'Thinking… {"not": "it"}\n```json\n' + body + "\n```\nDone."
    assert extract_results(text) == []


def test_extract_results_rejects_unrepairable_output():
    assert extract_results('{"results": [ {"path": "a", "extra": {"message": "x["y"]"}} ]}') is None
    assert extract_results("no json here") is None


def test_extract_results_repairs_a_bad_escape_only_when_needed():
    text = '{"version":"1.0.0","results":[{"path":"a.py","extra":{"message":"the \\\'name\\\' param"}}]}'
    [r] = extract_results(text)
    assert r["extra"]["message"] == "the 'name' param"


def test_balanced_blocks_ignore_braces_inside_strings():
    text = 'x {"m": "{{7*7}} and }"} y {"n": 1}'
    assert _balanced_json_blocks(text) == ['{"m": "{{7*7}} and }"}', '{"n": 1}']


def test_to_findings_relativises_paths_and_drops_ingest_rejects():
    rows = [
        {"path": "/workspace/app/main.py", "start": {"line": 3},
         "extra": {"message": "abs path", "severity": "INFO", "metadata": {"cwe": ["CWE-1"]}}},
        {"path": "./app/x.py", "start": {"line": 0},
         "extra": {"message": "dot-slash", "severity": "WARNING"}},
        {"path": "../etc/passwd", "extra": {"message": "traversal", "severity": "ERROR"}},
        {"path": "", "extra": {"message": "no path", "severity": "ERROR"}},
        {"path": "app/y.py", "extra": {"message": "", "severity": "ERROR"}},
        "not-a-dict",
    ]
    findings, dropped = to_findings(rows, "/workspace")
    assert [f.file for f in findings] == ["app/main.py", "app/x.py"]
    assert findings[0].cwe == "CWE-1" and findings[1].cwe is None
    assert dropped == 4


# ---------------------------------------------------------------- real stream replay


def test_replay_of_a_real_deepseek_stream(run_agent, workspace, fake_opencode):
    """A byte-for-byte replay of a recorded live DeepSeek V4 Flash run (P38b, ds1):
    the genuine OpenCode 1.18.20 event shape, with the tokens the spike measured —
    11,501 in / 2,618 out / 69,376 cache-read, 6 steps. `total` there was 91,949."""
    run = run_agent(_sec_env(workspace, fake_opencode, FAKE_MODE="replay",
                             FAKE_REPLAY_FILE=str(FIXTURES / "deepseek-run-events.jsonl")))
    assert run.returncode == 1, run.stderr
    body = run.result
    assert body["tokensIn"] == 11501 and body["tokensOut"] == 2618
    assert "91949" not in run.stdout                        # total never posted
    assert run.summary["cache_read_tokens"] == 69376 and run.summary["steps"] == 6
    assert body["gate"] == "fail"
    assert any(f["severity"] == "critical" and f["cwe"].startswith("CWE-1336") for f in body["findings"])
    assert all(f["cwe"] for f in body["findings"])
