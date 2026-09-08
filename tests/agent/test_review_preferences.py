"""Review task, 1.1.0 additions: per-project preferences in the system prompt, the
refusal outcome, and the reconciled exit codes — all on the fake client, $0."""

from __future__ import annotations

import pytest

from agent.config import AgentConfig
from agent.errors import ModelRefused
from agent.review import (
    MAX_PREFERENCES_CHARS,
    PREFERENCES_HEADING,
    SYSTEM_PROMPT,
    build_system_prompt,
    review,
    sanitize_preferences,
)
from app.llm.base import LLMResponse

DIFF = "--- a/app.py\n+++ b/app.py\n@@\n-pass\n+exec(user_input)\n"
HIGH = '{"findings":[{"severity":"high","category":"security","file":"app.py","line":4,"message":"exec"}]}'


class _CapturingClient:
    def __init__(self, text: str = '{"findings": []}') -> None:
        self.text = text
        self.system: str | None = None

    def complete(self, system, user, max_tokens):
        self.system = system
        return LLMResponse(self.text, 10, 5, "cap")


def test_preferences_are_appended_under_a_heading():
    prompt = build_system_prompt("Flag eval(). Ignore import order.")
    assert prompt.startswith(SYSTEM_PROMPT)
    assert PREFERENCES_HEADING in prompt
    assert prompt.endswith("Flag eval(). Ignore import order.")


def test_no_preferences_means_the_v1_prompt_byte_for_byte():
    assert build_system_prompt(None) == SYSTEM_PROMPT
    assert build_system_prompt("   \n\t ") == SYSTEM_PROMPT


def test_preferences_are_bounded_and_control_chars_stripped():
    raw = "a\x00b\x1bc" + "x" * (MAX_PREFERENCES_CHARS + 500)
    clean = sanitize_preferences(raw)
    assert clean is not None
    assert "\x00" not in clean and "\x1b" not in clean
    assert clean.startswith("abc")
    assert len(clean) == MAX_PREFERENCES_CHARS


def test_review_uses_the_config_preferences():
    client = _CapturingClient()
    cfg = AgentConfig(llm_client="fake", review_preferences="Be terse.")
    review(DIFF, client, cfg)
    assert client.system is not None and client.system.endswith("Be terse.")


def test_review_refusal_is_model_refused_not_malformed():
    client = _CapturingClient("I'm unable to review this code as it may be used for harmful purposes.")
    with pytest.raises(ModelRefused):
        review(DIFF, client, AgentConfig(llm_client="fake"))


def test_review_default_gate_and_ceiling_are_per_task():
    cfg = AgentConfig(llm_client="fake")
    assert cfg.effective_fail_severities("review") == ["high", "critical"]
    assert cfg.effective_fail_severities("security") == ["critical"]
    assert cfg.effective_token_ceiling("review") == 100_000
    assert cfg.effective_token_ceiling("security") == 1_000_000
    cfg = AgentConfig(llm_client="fake", token_ceiling=5, fail_severities=["low"])
    assert cfg.effective_token_ceiling("security") == 5
    assert cfg.effective_fail_severities("security") == ["low"]


def test_agent_config_ignores_a_dotenv_in_cwd(tmp_path, monkeypatch):
    """The review stage runs with the USER's checkout as cwd; their .env must not
    reconfigure the agent."""
    (tmp_path / ".env").write_text("AGENT_MODEL=from-the-users-repo\nLLM_CLIENT=anthropic\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("LLM_CLIENT", raising=False)
    cfg = AgentConfig()
    assert cfg.model_id == "fake-model" and cfg.llm_client == "fake"


# ---------------------------------------------------------------- CLI exit codes (review)


def test_cli_review_token_ceiling_exits_124_not_3(run_agent):
    run = run_agent({"LLM_CLIENT": "fake", "AGENT_TOKEN_CEILING": "1"}, stdin=DIFF)
    assert run.returncode == 124
    assert run.error["error"] == "ceiling_exceeded"
    assert "exec(user_input)" not in run.stderr


def test_cli_review_clean_exits_0_with_cwe_null(run_agent):
    run = run_agent({"LLM_CLIENT": "fake"}, stdin=DIFF)
    assert run.returncode == 0
    assert run.result["findings"] == [] and run.result["gate"] == "pass"


def test_cli_unknown_task_is_a_config_error(run_agent):
    run = run_agent({"MODELMATCH_TASK": "lint"}, stdin=DIFF)
    assert run.returncode == 4 and run.error["error"] == "config_error"


# ---------------------------------------------------------------- two images, one codebase


def test_image_task_guard_rejects_the_other_task(run_agent, workspace, fake_opencode):
    """Each image bakes AGENT_IMAGE_TASK; a project whose task does not match is a clear
    config error, not an ImportError (review image has no OpenCode; security image has
    no provider SDKs)."""
    run = run_agent({"AGENT_IMAGE_TASK": "review", "MODELMATCH_TASK": "security",
                     "AGENT_MODEL": "deepseek/deepseek-v4-flash", "AGENT_WORKSPACE": str(workspace),
                     **fake_opencode})
    assert run.returncode == 4
    assert "modelmatch-agent-security" in run.error["detail"]
    run = run_agent({"AGENT_IMAGE_TASK": "security", "MODELMATCH_TASK": "review", "LLM_CLIENT": "fake"},
                    stdin=DIFF)
    assert run.returncode == 4 and "modelmatch-agent image" in run.error["detail"]


def test_missing_opencode_binary_is_a_config_error(run_agent, workspace):
    run = run_agent({"MODELMATCH_TASK": "security", "AGENT_MODEL": "deepseek/deepseek-v4-flash",
                     "AGENT_WORKSPACE": str(workspace), "AGENT_OPENCODE_BIN": "/nonexistent/opencode"})
    assert run.returncode == 4 and "modelmatch-agent-security" in run.error["detail"]


def test_gemini_key_is_remapped_for_opencode_in_python(run_agent, workspace, fake_opencode, tmp_path):
    """The catalog names GOOGLE_API_KEY; OpenCode reads GOOGLE_GENERATIVE_AI_API_KEY. The
    remap happens in the agent (no shell entrypoint in the images)."""
    envfile = tmp_path / "seen-env.txt"
    # the stub records nothing about env, so prove it through a tiny wrapper that does
    wrapper = tmp_path / "oc.sh"
    wrapper.write_text(f'#!/bin/sh\necho "$GOOGLE_GENERATIVE_AI_API_KEY" > "{envfile}"\nexec "{fake_opencode["AGENT_OPENCODE_BIN"]}" "$@"\n')
    wrapper.chmod(0o755)
    run = run_agent({"MODELMATCH_TASK": "security", "AGENT_MODEL": "google/gemini-3.5-flash",
                     "AGENT_WORKSPACE": str(workspace), "GOOGLE_API_KEY": "g-test-key",
                     "FAKE_MODE": "empty", **fake_opencode, "AGENT_OPENCODE_BIN": str(wrapper)})
    assert run.returncode == 0, run.stderr
    assert envfile.read_text().strip() == "g-test-key"
