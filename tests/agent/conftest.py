"""Fixtures for the agent v2 suite — everything runs on stubs, $0, no Postgres.

`fake_opencode` points AGENT_OPENCODE_BIN at tests/agent/fixtures/fake-opencode.py;
`workspace` is a tiny throwaway checkout; `run_agent` runs the real CLI
(`python -m agent`) in a subprocess with a CLEAN agent env (no MODELMATCH_* /
AGENT_* / provider keys leaking in from the developer's shell) so every test states
its whole configuration.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
FAKE_OPENCODE = FIXTURES / "fake-opencode.py"

_AGENT_PREFIXES = ("MODELMATCH_", "AGENT_", "FAKE_", "LLM_", "BUILD_TAG")
_CRED_NAMES = {
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION", "AWS_REGION",
    "AWS_PROFILE",
}


def clean_env() -> dict[str, str]:
    return {
        k: v for k, v in os.environ.items()
        if not k.startswith(_AGENT_PREFIXES) and k not in _CRED_NAMES
    }


@dataclass
class AgentRun:
    returncode: int
    stdout: str
    stderr: str

    @property
    def result(self) -> dict:
        return json.loads(self.stdout)

    @property
    def error(self) -> dict:
        """The structured error is the LAST non-empty stderr line."""
        return json.loads(self.stderr.strip().splitlines()[-1])

    @property
    def summary(self) -> dict | None:
        for line in self.stderr.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "agent_security_summary" in obj:
                return obj["agent_security_summary"]
        return None


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "checkout"
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "main.py").write_text("def index(name):\n    return f'<h1>{name}</h1>'\n")
    (ws / "app" / "config.py").write_text("TOKEN = 'x'\n")
    (ws / "README.md").write_text("demo\n")
    return ws


@pytest.fixture
def fake_opencode(tmp_path: Path) -> dict[str, str]:
    """Env that routes the security loop to the stub. Includes a counter file so
    tests can assert how many times OpenCode was invoked."""
    return {
        "AGENT_OPENCODE_BIN": str(FAKE_OPENCODE),
        "FAKE_COUNTER_FILE": str(tmp_path / "calls.txt"),
        "FAKE_ARGV_FILE": str(tmp_path / "argv.jsonl"),
    }


def invocations(env: dict[str, str]) -> int:
    try:
        return int(Path(env["FAKE_COUNTER_FILE"]).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def recorded_argv(env: dict[str, str]) -> list[list[str]]:
    p = Path(env["FAKE_ARGV_FILE"])
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


@pytest.fixture
def run_agent():
    def _run(env: dict[str, str], *, stdin: str = "", args: list[str] | None = None, timeout: int = 60) -> AgentRun:
        proc = subprocess.run(
            [sys.executable, "-m", "agent", *(args or [])],
            input=stdin,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env={**clean_env(), **env},
            timeout=timeout,
        )
        return AgentRun(proc.returncode, proc.stdout, proc.stderr)

    return _run
