"""CLI entrypoint: `python -m agent` (the image's command).

Reads a unified diff from --diff FILE or stdin, runs the review, prints the
AgentResult JSON to stdout, and sets the exit code from the gate so the Jenkins
stage passes/fails naturally. Failures emit STRUCTURED JSON on stderr — never a
raw traceback, and never the prompt/diff/secrets:

    0  gate pass
    1  gate fail (blocking findings)
    2  malformed model output
    3  token ceiling exceeded
    4  LLM client/config/provider failure
"""

from __future__ import annotations

import argparse
import json
import sys

from agent.config import AgentConfig
from agent.review import MalformedFindings, TokenCeilingExceeded, review
from app.llm.factory import build_llm_client


def _read_diff(path: str | None) -> str:
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return sys.stdin.read()


def _fail(code: int, error: str, detail: str) -> int:
    """Emit a structured error on stderr (no traceback, no prompt/diff/secrets)."""
    json.dump({"error": error, "detail": detail}, sys.stderr)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="ModelMatch CI code-review agent")
    parser.add_argument("--diff", help="path to a unified diff (default: stdin)")
    args = parser.parse_args(argv)

    try:
        config = AgentConfig()
    except Exception as exc:  # invalid AGENT_* env / config
        return _fail(4, "config_error", f"{type(exc).__name__}: {exc}")

    diff = _read_diff(args.diff)

    try:
        client = build_llm_client(config.llm_client, model=config.model_id)
        result = review(diff, client, config)
    except MalformedFindings as exc:
        return _fail(2, "malformed_findings", str(exc))
    except TokenCeilingExceeded as exc:
        return _fail(3, "token_ceiling_exceeded", str(exc))
    except ValueError as exc:  # unsupported LLM_CLIENT / bad config value
        return _fail(4, "llm_client_error", str(exc))
    except RuntimeError as exc:  # missing SDK / missing 'llm' extra
        return _fail(4, "llm_client_error", str(exc))
    except Exception as exc:  # provider SDK call failure — type+message only, no diff
        return _fail(4, "llm_provider_error", f"{type(exc).__name__}: {exc}")

    print(result.model_dump_json(by_alias=True))
    return 0 if result.gate == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
