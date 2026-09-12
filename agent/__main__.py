"""CLI entrypoint: `python -m agent` (the image's command).

Two tasks, one image, one exit-code table (agent/errors.py):

    review    reads a unified diff (--diff FILE or stdin), one LLM call, gate on
              high/critical.
    security  OpenCode agentic loop over the read-only workspace, RealVuln auditor
              prompt, Semgrep JSON → findings with CWE, gate on critical.

Which task, which model and (review) which preferences come from the Driftplain
API when MODELMATCH_API_URL + MODELMATCH_PROJECT_ID + MODELMATCH_CI_TOKEN are set
(HLD §3b.1); otherwise from env (MODELMATCH_TASK / AGENT_MODEL) for local and
fixture runs. The result JSON goes to stdout; with MODELMATCH_POST_RESULT=true it is
also POSTed to /ci-runs. Failures emit STRUCTURED JSON on stderr — never a raw
traceback, never the prompt/diff/secrets.

    0  gate pass / clean          3  model refused (never clean)
    1  gate fail                  4  config / credential / fetch / provider failure
    2  malformed model output   124  a ceiling aborted the run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from agent.config import AgentConfig
from agent.errors import (
    EXIT_CONFIG,
    EXIT_GATE_FAIL,
    EXIT_PASS,
    AgentConfigError,
    AgentError,
)
from agent.providers import opencode_model, review_llm_client
from agent.remote import RemoteConfig, build_ci_run_payload, fetch_agent_config, post_ci_run
from agent.review import review
from agent.security import print_diagnostics, run_security


def _read_diff(path: str | None) -> str:
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return sys.stdin.read()


def _fail(code: int, error: str, detail: str) -> int:
    """Emit a structured error on stderr (no traceback, no prompt/diff/secrets)."""
    json.dump({"error": error, "detail": detail}, sys.stderr)
    sys.stderr.write("\n")
    return code


def _note(msg: str) -> None:
    print(json.dumps({"agent_note": msg}), file=sys.stderr)


def _remap_credentials() -> None:
    """OpenCode's `google` provider reads GOOGLE_GENERATIVE_AI_API_KEY; the catalog stores
    the provider-truthful GOOGLE_API_KEY (that string is injected verbatim into the
    generated Jenkinsfile, and the same row serves review mode through our own
    google-genai adapter). The one-line adaptation lives here, not in the catalog and
    not in a shell entrypoint (the images ship no shell script). GEMINI_API_KEY is
    the name our adapter + RealVuln's .env use; accept it too. Everything else
    (ANTHROPIC/DEEPSEEK/OPENAI keys, the AWS chain) is read under its standard name."""
    if not os.environ.get("GOOGLE_GENERATIVE_AI_API_KEY"):
        for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
            if os.environ.get(name):
                os.environ["GOOGLE_GENERATIVE_AI_API_KEY"] = os.environ[name]
                break


def _check_image_task(task: str) -> None:
    """Two images, one codebase: `modelmatch-agent` (review, the LLM SDKs) and
    `modelmatch-agent-security` (OpenCode). Each bakes AGENT_IMAGE_TASK; a project
    whose task does not match the image it was run with is a config error, not a
    mysterious ImportError or a missing binary three attempts later."""
    baked = os.environ.get("AGENT_IMAGE_TASK")
    if baked and baked != task:
        other = "modelmatch-agent-security" if task == "security" else "modelmatch-agent"
        raise AgentConfigError(
            f"this image runs the {baked!r} task only; the project needs {task!r} — "
            f"use the {other} image"
        )


def _preflight_credential(remote: RemoteConfig) -> None:
    """Fail BEFORE spending a token when the BYOK variable is missing — the
    alternative is a provider 401 after OpenCode has started, or a confusing
    'unauthorized' three attempts later."""
    m = remote.model
    if m.auth_mode == "api_key":
        if not m.credential_env_var:
            raise AgentConfigError("runtime config declares api_key auth but no credentialEnvVar")
        if not os.environ.get(m.credential_env_var):
            raise AgentConfigError(
                f"credential variable {m.credential_env_var} is not set in the agent's "
                "environment (bind the Jenkins credential and pass it with -e by name)"
            )


def _resolve(config: AgentConfig) -> tuple[AgentConfig, str, RemoteConfig | None]:
    """Decide task + model (+ preferences): the API wins when configured."""
    if not config.remote_configured:
        if config.api_url or config.project_id is not None or config.ci_token:
            _note("partial MODELMATCH_* config (need API_URL + PROJECT_ID + CI_TOKEN); "
                  "falling back to env for task/model")
        return config, config.task, None

    assert config.api_url and config.ci_token and config.project_id is not None
    remote = fetch_agent_config(
        config.api_url, config.project_id, config.ci_token.get_secret_value(), config.http_timeout
    )
    _preflight_credential(remote)
    update: dict = {"review_preferences": remote.review_preferences}
    if remote.task == "review":
        update["llm_client"] = review_llm_client(remote.model.provider)
        update["model_id"] = remote.model.provider_model_id
    else:
        update["model_id"] = opencode_model(remote.model.provider, remote.model.provider_model_id)
    if os.environ.get("MODELMATCH_TASK") and os.environ["MODELMATCH_TASK"] != remote.task:
        _note(f"MODELMATCH_TASK={os.environ['MODELMATCH_TASK']} ignored; the API says {remote.task}")
    return config.model_copy(update=update), remote.task, remote


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="Driftplain CI agent (review | security)")
    parser.add_argument("--diff", help="review: path to a unified diff (default: stdin)")
    args = parser.parse_args(argv)

    # The S16 LLM log line goes to STDERR — stdout is reserved for the result JSON the
    # Jenkins stage parses. INFO so the per-request line is emitted.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")

    try:
        config = AgentConfig()
    except Exception as exc:  # invalid env / config
        return _fail(EXIT_CONFIG, "config_error", f"{type(exc).__name__}: {exc}")

    _remap_credentials()
    try:
        config, task, remote = _resolve(config)
        _check_image_task(task)
    except AgentError as exc:
        return _fail(exc.exit_code, exc.error, str(exc))

    diag = None
    try:
        if task == "review":
            try:
                diff = _read_diff(args.diff)
            except OSError as exc:  # missing/unreadable --diff file (no traceback, no leak)
                return _fail(EXIT_CONFIG, "diff_read_error", f"{type(exc).__name__}: {exc}")
            # Lazy: the SDK-bearing factory exists only in the review image.
            from app.llm.factory import build_llm_client

            client = build_llm_client(
                config.llm_client, model=config.model_id, region=config.aws_region
            )
            result = review(diff, client, config)
        elif task == "security":
            result, diag = run_security(config, config.model_id)
        else:  # unreachable: config validates the literal
            return _fail(EXIT_CONFIG, "config_error", f"unknown task {task!r}")
    except AgentError as exc:
        diag = getattr(exc, "diagnostics", None)
        if diag is not None:
            print_diagnostics(diag)
        return _fail(exc.exit_code, exc.error, str(exc))
    except ValueError as exc:  # unsupported LLM_CLIENT / bad config value
        return _fail(EXIT_CONFIG, "llm_client_error", str(exc))
    except RuntimeError as exc:  # missing SDK / missing 'llm' extra
        return _fail(EXIT_CONFIG, "llm_client_error", str(exc))
    except Exception as exc:  # provider SDK call failure — type+message only, no diff
        return _fail(EXIT_CONFIG, "llm_provider_error", f"{type(exc).__name__}: {exc}")

    result_json = json.loads(result.model_dump_json(by_alias=True))
    result_json["cacheReadTokens"] = diag.cache_read_tokens if diag is not None else None
    print(json.dumps(result_json))
    if diag is not None:
        print_diagnostics(diag)

    if config.post_result:
        if remote is None:
            return _fail(EXIT_CONFIG, "config_error",
                         "MODELMATCH_POST_RESULT needs MODELMATCH_API_URL + PROJECT_ID + CI_TOKEN")
        if not config.build_id:
            return _fail(EXIT_CONFIG, "config_error",
                         "MODELMATCH_POST_RESULT needs BUILD_TAG (or MODELMATCH_BUILD_ID)")
        assert config.api_url and config.ci_token and config.project_id is not None
        payload = build_ci_run_payload(result_json, config.build_id)
        try:
            posted = post_ci_run(
                config.api_url, config.project_id, config.ci_token.get_secret_value(),
                payload, config.http_timeout,
            )
        except AgentError as exc:
            # The gate result is known, but a run that never reached the dashboard is
            # the silent failure class this product exists to rule out → red, loudly.
            return _fail(exc.exit_code, exc.error, f"gate={result.gate}; POST /ci-runs failed: {exc}")
        _note(f"posted run id={posted.get('id') if isinstance(posted, dict) else '?'} "
              f"gate={result.gate} findings={len(result.findings)}")

    return EXIT_PASS if result.gate == "pass" else EXIT_GATE_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
