"""The review itself: diff → strict prompt → LLMClient → validated findings + gate.

Pure-ish and read-only: it takes a diff string + an LLMClient and returns an
AgentResult. It never touches the filesystem or the repo. The model is told to
return STRICT JSON; anything else is rejected (MalformedFindings) rather than
guessed. A per-run token ceiling aborts the run before it can run away on the
user's key.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from agent.config import AgentConfig
from app.llm.base import LLMClient, approx_tokens
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


def review(diff: str, client: LLMClient, config: AgentConfig) -> AgentResult:
    """Run one review pass and return the result. Read-only; no filesystem writes."""
    user_prompt = build_user_prompt(diff)

    # Preflight: estimate worst-case usage (prompt in + max output) and abort BEFORE
    # calling the provider, so an oversized diff never spends on the user's key.
    estimated = (
        approx_tokens(SYSTEM_PROMPT) + approx_tokens(user_prompt) + config.max_tokens
    )
    if estimated > config.token_ceiling:
        raise TokenCeilingExceeded(
            f"estimated {estimated} tokens exceeds ceiling {config.token_ceiling} "
            "(aborted before the provider call)"
        )

    resp = client.complete(SYSTEM_PROMPT, user_prompt, config.max_tokens)

    # Post-call: enforce against actual usage too.
    used = resp.tokens_in + resp.tokens_out
    if used > config.token_ceiling:
        raise TokenCeilingExceeded(
            f"run used {used} tokens, ceiling is {config.token_ceiling}"
        )

    findings = parse_findings(resp.text)
    gate, reason = apply_gate(findings, config.fail_severities)
    return AgentResult(
        findings=findings,
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
        model=resp.model,
        gate=gate,
        gate_reason=reason,
    )
