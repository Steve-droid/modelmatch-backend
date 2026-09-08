"""The run-time config contract (HLD §3b.1) against a STUB of the P38e API.

A tiny in-process HTTP server plays `GET /projects/{id}/agent-config` and
`POST /projects/{id}/ci-runs` with the same auth semantics as the backend
(`X-CI-Token`; 401 on a bad token; 404 unknown project). It records what the
agent sends so the tests can pin the exact payload — above all that tokens.total
and cache reads never reach `/ci-runs`.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent.errors import AgentConfigError
from agent.providers import opencode_model, review_llm_client
from agent.remote import RemoteError, build_ci_run_payload, fetch_agent_config
from tests.agent.conftest import invocations, recorded_argv

TOKEN = "mm_ci_testtoken_0123456789abcdef"
PROJECT = 7

SECURITY_CFG = {
    "projectId": PROJECT,
    "task": "security",
    "taskType": "security_analysis",
    "model": {"name": "DeepSeek V4 Flash", "provider": "deepseek",
              "providerModelId": "deepseek-v4-flash", "authMode": "api_key",
              "credentialEnvVar": "DEEPSEEK_API_KEY"},
    "reviewPreferences": None,
}
REVIEW_CFG = {
    "projectId": PROJECT,
    "task": "review",
    "taskType": "ci_review",
    "model": {"name": "Claude Haiku 4.5", "provider": "anthropic",
              "providerModelId": "claude-haiku-4-5", "authMode": "api_key",
              "credentialEnvVar": "ANTHROPIC_API_KEY"},
    "reviewPreferences": "Flag any use of eval(). Ignore import ordering.",
}


class StubApi:
    def __init__(self, config: dict):
        self.config = config
        self.posts: list[dict] = []
        self.gets = 0
        self.post_status = 201
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):  # quiet
                pass

            def _auth(self) -> bool:
                return self.headers.get("X-CI-Token") == TOKEN

            def _send(self, code: int, body: dict):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == f"/projects/{PROJECT}/agent-config":
                    if not self._auth():
                        return self._send(401, {"detail": "Invalid or missing CI token"})
                    srv.gets += 1
                    return self._send(200, srv.config)
                return self._send(404, {"detail": "Project not found"})

            def do_POST(self):
                if self.path == f"/projects/{PROJECT}/ci-runs":
                    if not self._auth():
                        return self._send(401, {"detail": "Invalid or missing CI token"})
                    n = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(n) or b"{}")
                    srv.posts.append(body)
                    if srv.post_status != 201:
                        return self._send(srv.post_status, {"detail": "rejected"})
                    return self._send(201, {"id": 33, "gate": body.get("gate")})
                return self._send(404, {"detail": "Project not found"})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def api():
    def _mk(config):
        s = StubApi(config)
        made.append(s)
        return s
    made: list[StubApi] = []
    yield _mk
    for s in made:
        s.close()


def _remote_env(stub: StubApi, **extra) -> dict[str, str]:
    env = {
        "MODELMATCH_API_URL": stub.url + "/",   # trailing slash must not matter
        "MODELMATCH_PROJECT_ID": str(PROJECT),
        "MODELMATCH_CI_TOKEN": TOKEN,
    }
    env.update({k: str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------- fetch (unit)


def test_fetch_parses_the_contract(api):
    stub = api(SECURITY_CFG)
    cfg = fetch_agent_config(stub.url, PROJECT, TOKEN)
    assert cfg.task == "security" and cfg.task_type == "security_analysis"
    assert cfg.model.provider == "deepseek" and cfg.model.provider_model_id == "deepseek-v4-flash"
    assert cfg.model.credential_env_var == "DEEPSEEK_API_KEY"
    assert cfg.review_preferences is None


def test_fetch_with_a_bad_token_is_a_remote_error_without_the_token_in_it(api):
    stub = api(SECURITY_CFG)
    with pytest.raises(RemoteError) as exc:
        fetch_agent_config(stub.url, PROJECT, "wrong-token-value")
    assert "401" in str(exc.value) and "wrong-token-value" not in str(exc.value)


def test_fetch_unknown_project_is_404(api):
    stub = api(SECURITY_CFG)
    with pytest.raises(RemoteError, match="404"):
        fetch_agent_config(stub.url, 999, TOKEN)


def test_fetch_rejects_an_unknown_task(api):
    stub = api({**SECURITY_CFG, "task": "linting"})
    with pytest.raises(RemoteError, match="linting"):
        fetch_agent_config(stub.url, PROJECT, TOKEN)


def test_fetch_unreachable_api_is_a_remote_error():
    with pytest.raises(RemoteError):
        fetch_agent_config("http://127.0.0.1:9", PROJECT, TOKEN, timeout=2)


# ---------------------------------------------------------------- provider maps


@pytest.mark.parametrize("provider, mid, expected", [
    ("deepseek", "deepseek-v4-flash", "deepseek/deepseek-v4-flash"),
    ("openai", "gpt-5.5", "openai/gpt-5.5"),
    ("gemini", "gemini-3.5-flash", "google/gemini-3.5-flash"),
    ("bedrock", "amazon.nova-2-lite-v1:0", "amazon-bedrock/amazon.nova-2-lite-v1:0"),
    ("anthropic", "claude-opus-5", "anthropic/claude-opus-5"),
])
def test_opencode_model_composition(provider, mid, expected):
    assert opencode_model(provider, mid) == expected


def test_opencode_model_rejects_prefixed_ids_and_unknown_providers():
    with pytest.raises(AgentConfigError):
        opencode_model("deepseek", "deepseek/deepseek-v4-flash")
    with pytest.raises(AgentConfigError):
        opencode_model("moonshot", "kimi")


def test_review_llm_client_map():
    assert review_llm_client("anthropic") == "anthropic"
    assert review_llm_client("gemini") == "gemini"
    with pytest.raises(AgentConfigError, match="not runnable for the review"):
        review_llm_client("deepseek")


# ---------------------------------------------------------------- end to end (CLI)


def test_security_project_config_drives_the_run_and_the_post(api, run_agent, workspace, fake_opencode):
    stub = api(SECURITY_CFG)
    env = _remote_env(stub, AGENT_WORKSPACE=str(workspace), DEEPSEEK_API_KEY="sk-test",
                      MODELMATCH_POST_RESULT="true", BUILD_TAG="jenkins-sec-vuln-demo-12",
                      FAKE_STEPS=3, FAKE_STEP_TOKENS=1000, FAKE_CACHE_READ=7000, **fake_opencode)
    run = run_agent(env)
    assert run.returncode == 1, run.stderr           # critical → gate fail, and it was posted
    assert stub.gets == 1
    [argv] = recorded_argv(fake_opencode)
    assert argv[4] == "deepseek/deepseek-v4-flash"     # composed from the BARE id
    [payload] = stub.posts
    assert set(payload) == {"findings", "tokensIn", "tokensOut", "model", "gate", "gateReason", "jenkinsBuildId"}
    assert payload["jenkinsBuildId"] == "jenkins-sec-vuln-demo-12"
    assert payload["tokensIn"] == 3000 and payload["tokensOut"] == 300
    assert "cacheReadTokens" not in json.dumps(payload) and "24300" not in json.dumps(payload)
    assert payload["findings"][0]["cwe"] == "CWE-89: SQL Injection"
    assert set(payload["findings"][0]) == {"severity", "category", "file", "line", "message", "cwe"}
    assert "posted run id=33" in run.stderr
    assert TOKEN not in run.stderr and TOKEN not in run.stdout


def test_api_task_wins_over_env_task(api, run_agent, workspace, fake_opencode):
    stub = api(SECURITY_CFG)
    env = _remote_env(stub, MODELMATCH_TASK="review", AGENT_WORKSPACE=str(workspace),
                      DEEPSEEK_API_KEY="sk-test", **fake_opencode)
    run = run_agent(env, stdin="--- a\n+++ b\n")
    assert run.returncode == 1 and invocations(fake_opencode) == 1   # ran the security loop
    assert "MODELMATCH_TASK=review ignored" in run.stderr


def test_missing_credential_variable_fails_before_spending(api, run_agent, workspace, fake_opencode):
    stub = api(SECURITY_CFG)
    run = run_agent(_remote_env(stub, AGENT_WORKSPACE=str(workspace), **fake_opencode))
    assert run.returncode == 4
    assert "DEEPSEEK_API_KEY" in run.error["detail"]
    assert invocations(fake_opencode) == 0


def test_fetch_failure_is_exit_4_never_a_guessed_task(api, run_agent, workspace, fake_opencode):
    stub = api(SECURITY_CFG)
    run = run_agent(_remote_env(stub, MODELMATCH_CI_TOKEN="nope", AGENT_WORKSPACE=str(workspace),
                                DEEPSEEK_API_KEY="sk-test", **fake_opencode))
    assert run.returncode == 4 and run.error["error"] == "config_error"
    assert "401" in run.error["detail"]
    assert invocations(fake_opencode) == 0


def test_review_project_config_maps_provider_and_appends_preferences(api, run_agent, monkeypatch):
    stub = api(REVIEW_CFG)
    # No Anthropic SDK call: the fake client is selected by LLM_CLIENT only when no
    # remote config… so prove the mapping via the config error the missing SDK/key path
    # gives us, and the prompt via the unit test in test_review_preferences. Here:
    # the credential preflight must name ANTHROPIC_API_KEY.
    run = run_agent(_remote_env(stub), stdin="--- a\n+++ b\n")
    assert run.returncode == 4 and "ANTHROPIC_API_KEY" in run.error["detail"]


def test_review_project_on_a_non_review_provider_is_a_config_error(api, run_agent):
    stub = api({**REVIEW_CFG, "model": {**REVIEW_CFG["model"], "provider": "deepseek",
                                          "credentialEnvVar": "DEEPSEEK_API_KEY"}})
    run = run_agent(_remote_env(stub, DEEPSEEK_API_KEY="sk-test"), stdin="--- a\n+++ b\n")
    assert run.returncode == 4 and "not runnable for the review" in run.error["detail"]


def test_post_failure_turns_the_stage_red(api, run_agent, workspace, fake_opencode):
    stub = api(SECURITY_CFG)
    stub.post_status = 409
    env = _remote_env(stub, AGENT_WORKSPACE=str(workspace), DEEPSEEK_API_KEY="sk-test",
                      MODELMATCH_POST_RESULT="true", BUILD_TAG="jenkins-x-1", FAKE_MODE="empty",
                      **fake_opencode)
    run = run_agent(env)
    assert run.returncode == 4                             # a clean scan that never reached the dashboard is not green
    assert "409" in run.error["detail"] and "gate=pass" in run.error["detail"]
    assert run.stdout.strip() != ""                        # the result JSON was still printed


def test_post_without_build_id_is_a_config_error(api, run_agent, workspace, fake_opencode):
    stub = api(SECURITY_CFG)
    env = _remote_env(stub, AGENT_WORKSPACE=str(workspace), DEEPSEEK_API_KEY="sk-test",
                      MODELMATCH_POST_RESULT="true", FAKE_MODE="empty", **fake_opencode)
    run = run_agent(env)
    assert run.returncode == 4 and "BUILD_TAG" in run.error["detail"]
    assert stub.posts == []


def test_partial_remote_config_falls_back_to_env(run_agent, workspace, fake_opencode):
    env = {"MODELMATCH_API_URL": "http://127.0.0.1:9", "MODELMATCH_TASK": "security",
           "AGENT_MODEL": "deepseek/deepseek-v4-flash", "AGENT_WORKSPACE": str(workspace),
           "FAKE_MODE": "empty", **fake_opencode}
    run = run_agent(env)
    assert run.returncode == 0 and "partial MODELMATCH_" in run.stderr


# ---------------------------------------------------------------- payload (unit)


def test_build_ci_run_payload_is_exactly_the_v1_contract_plus_cwe():
    result = {"findings": [{"severity": "low", "category": "security", "file": "a.py", "line": 1,
                            "message": "m", "cwe": "CWE-1"}],
              "tokensIn": 10, "tokensOut": 2, "model": "deepseek/deepseek-v4-flash",
              "gate": "pass", "gateReason": None,
              "cacheReadTokens": 999, "tokensTotal": 1011}          # would be dropped if present
    p = build_ci_run_payload(result, "jenkins-1")
    assert set(p) == {"findings", "tokensIn", "tokensOut", "model", "gate", "gateReason", "jenkinsBuildId"}
    assert p["findings"][0]["cwe"] == "CWE-1"
