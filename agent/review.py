"""The review itself: diff → strict prompt → LLMClient → validated findings + gate.

Pure-ish and read-only: it takes a diff string + an LLMClient and returns an
AgentResult. It never touches the filesystem or the repo. The model is told to
return STRICT JSON; anything else is rejected (MalformedFindings) rather than
guessed. A per-run token ceiling aborts the run before it can run away on the
user's key.
"""

from __future__ import annotations

import json
import time

from pydantic import ValidationError

from agent.config import AgentConfig
from app.llm.base import LLMClient, approx_tokens
from app.observability import LLMObservation, log_llm_call
from app.schemas.findings import AgentResult, Finding

SYSTEM_PROMPT = (
    "You are a strict CI code-review agent. Review ONLY the provided unified diff "
    "for security and style issues. Do not modify code. Respond with STRICT JSON "
    "and nothing else, in exactly this shape:\n"
    '{"findings": [{"severity": "low|medium|high|critical", '
    '"category": "security|style", "file": "path", "line": 123, '
    '"message": "..."}]}\n'
    "Use an empty findings array if there are no issues. Never include prose."
)


class MalformedFindings(Exception):
    """The model returned something that isn't the agreed findings JSON."""


class TokenCeilingExceeded(Exception):
    """The run exceeded its cumulative token budget and was aborted."""


def build_user_prompt(diff: str) -> str:
    return f"Review this unified diff:\n\n{diff}"


def _strip_code_fence(text: str) -> str:
    """Unwrap a ```json … ``` (or bare ```) markdown fence many models emit.

    The contract asks for strict JSON, but real models (e.g. Bedrock Nova) routinely
    wrap it in a fence. Stripping a fenced block is safe and deterministic; anything
    that still isn't JSON is rejected as MalformedFindings below.
    """
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = t[3:]  # drop the opening ```
    newline = t.find("\n")
    if newline != -1 and t[:newline].strip().isalpha():  # optional language tag (json)
        t = t[newline + 1 :]
    t = t.rstrip()
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


def parse_findings(text: str) -> list[Finding]:
    """Strictly parse the model's JSON into validated Findings (untrusted output)."""
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as exc:
        raise MalformedFindings(f"response was not valid JSON: {exc}") from exc

    raw = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise MalformedFindings("expected a 'findings' array")
    try:
        return [Finding.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise MalformedFindings(f"finding failed validation: {exc}") from exc


def apply_gate(
    findings: list[Finding], fail_severities: list[str]
) -> tuple[str, str | None]:
    """Pass/fail decision (in CI). Fails on any finding at a fail-severity."""
    fail = [f for f in findings if f.severity in fail_severities]
    if fail:
        sevs = ", ".join(sorted({f.severity for f in fail}))
        return "fail", f"{len(fail)} finding(s) at blocking severity ({sevs})"
    return "pass", None


def _log_agent_call(
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
    provider: str,
    latency_ms: int | None = None,
    status: str = "ok",
    error_kind: str | None = None,
) -> None:
    """One per-request LLM log line for the agent. The diff is NEVER logged: prompt/query
    stay None — only provider/model/tokens/latency/status are emitted. Logging only — no
    metrics: the agent runs once in the user's CI and has no /metrics endpoint (the
    backend folds agent tokens into /metrics from the /ci-runs ingest instead)."""
    log_llm_call(
        LLMObservation(
            purpose="agent",
            provider=provider,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            retrieved_context_size=None,
            status=status,
            error_kind=error_kind,
        )
    )


def review(diff: str, client: LLMClient, config: AgentConfig) -> AgentResult:
    """Run one review pass and return the result. Read-only; no filesystem writes."""
    user_prompt = build_user_prompt(diff)
    provider = config.llm_client

    # Preflight: estimate worst-case usage (prompt in + max output) and abort BEFORE
    # calling the provider, so an oversized diff never spends on the user's key.
    estimated = (
        approx_tokens(SYSTEM_PROMPT) + approx_tokens(user_prompt) + config.max_tokens
    )
    if estimated > config.token_ceiling:
        _log_agent_call(
            model=config.model_id, tokens_in=0, tokens_out=0, provider=provider,
            status="error", error_kind="TokenCeilingExceeded",
        )
        raise TokenCeilingExceeded(
            f"estimated {estimated} tokens exceeds ceiling {config.token_ceiling} "
            "(aborted before the provider call)"
        )

    try:
        started = time.perf_counter()
        resp = client.complete(SYSTEM_PROMPT, user_prompt, config.max_tokens)
        latency_ms = int((time.perf_counter() - started) * 1000)
    except Exception as exc:
        # Provider/client failure — log the exception CLASS only (never its message,
        # which could carry prompt/diff/secret text), then let __main__ map it to an exit.
        _log_agent_call(
            model=config.model_id, tokens_in=0, tokens_out=0, provider=provider,
            status="error", error_kind=type(exc).__name__,
        )
        raise

    # Post-call: enforce against actual usage too.
    used = resp.tokens_in + resp.tokens_out
    if used > config.token_ceiling:
        _log_agent_call(
            model=resp.model, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
            provider=provider, latency_ms=latency_ms,
            status="error", error_kind="TokenCeilingExceeded",
        )
        raise TokenCeilingExceeded(
            f"run used {used} tokens, ceiling is {config.token_ceiling}"
        )

    try:
        findings = parse_findings(resp.text)
    except MalformedFindings:
        _log_agent_call(
            model=resp.model, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
            provider=provider, latency_ms=latency_ms,
            status="error", error_kind="MalformedFindings",
        )
        raise

    # Success: the provider call worked and its output parsed.
    _log_agent_call(
        model=resp.model, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
        provider=provider, latency_ms=latency_ms, status="ok",
    )

    gate, reason = apply_gate(findings, config.fail_severities)
    return AgentResult(
        findings=findings,
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
        model=resp.model,
        gate=gate,
        gate_reason=reason,
    )
