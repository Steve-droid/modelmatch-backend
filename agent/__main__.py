"""CLI entrypoint: `python -m agent` (the image's command).

Reads a unified diff from --diff FILE or stdin, runs the review, prints the
AgentResult JSON to stdout, and sets the exit code from the gate so the Jenkins
stage passes/fails naturally:

    0  gate pass
    1  gate fail (blocking findings)
    2  malformed model output
    3  token ceiling exceeded
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="ModelMatch CI code-review agent")
    parser.add_argument("--diff", help="path to a unified diff (default: stdin)")
    args = parser.parse_args(argv)

    config = AgentConfig()
    diff = _read_diff(args.diff)
    client = build_llm_client(config.llm_client, model=config.model_id)

    try:
        result = review(diff, client, config)
    except MalformedFindings as exc:
        json.dump({"error": "malformed_findings", "detail": str(exc)}, sys.stderr)
        return 2
    except TokenCeilingExceeded as exc:
        json.dump({"error": "token_ceiling_exceeded", "detail": str(exc)}, sys.stderr)
        return 3

    print(result.model_dump_json(by_alias=True))
    return 0 if result.gate == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
