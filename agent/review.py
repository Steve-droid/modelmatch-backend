"""The review task: diff → strict prompt → LLMClient → validated findings + gate.

Pure-ish and read-only: it takes a diff string + an LLMClient and returns an
AgentRunResult. It never touches the filesystem or the repo. The model is told to
return STRICT JSON; anything else is rejected (MalformedFindings) rather than
guessed — unless it reads as a refusal, which gets its own outcome (ModelRefused,
exit 3) so it can never be mistaken for a clean review. A per-run token ceiling
aborts the run before it can run away on the user's key.

Since 1.1.0 the project's review preferences (from the API, HLD §3b.1) are appended
to the system prompt, so a project can steer the review without touching its
Jenkinsfile.
"""

from __future__ import annotations

import json
import re
import time

from pydantic import ValidationError

from agent.config import AgentConfig
from agent.errors import (  # noqa: F401 — re-exported for callers/tests
    CeilingExceeded,
    MalformedFindings,
    ModelRefused,
    TokenCeilingExceeded,
    looks_like_refusal,
)
from agent.schemas import AgentFinding, AgentRunResult
from app.llm.base import LLMClient, approx_tokens
from app.observability import LLMObservation, log_llm_call

SYSTEM_PROMPT = (
    "You are a strict CI code-review agent. Review ONLY the provided unified diff "
    "for security and style issues. Do not modify code. Respond with STRICT JSON "
    "and nothing else, in exactly this shape:\n"
    '{"findings": [{"severity": "low|medium|high|critical", '
    '"category": "security|style", "file": "path", "line": 123, '
    '"message": "..."}]}\n'
    "Use an empty findings array if there are no issues. Never include prose."
)

PREFERENCES_HEADING = "Project review preferences (set by the project owner; follow them):"
MAX_PREFERENCES_CHARS = 2000
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_preferences(text: str | None) -> str | None:
    """Bounded, control-character-free preference text (untrusted user input that
    lands in a prompt): strip control chars, collapse whitespace runs, cap length."""
    if text is None:
        return None
    t = _CONTROL_CHARS.sub("", text)
    t = re.sub(r"[ \t]+", " ", t).strip()
    if not t:
        return None
    return t[:MAX_PREFERENCES_CHARS]


def build_system_prompt(review_preferences: str | None = None) -> str:
    prefs = sanitize_preferences(review_preferences)
    if not prefs:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{PREFERENCES_HEADING}\n{prefs}"


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


def parse_findings(text: str) -> list[AgentFinding]:
    """Strictly parse the model's JSON into validated findings (untrusted output).

    Prose that reads as a refusal is ModelRefused (exit 3), not MalformedFindings —
    a declined review must never look like a clean one.
    """
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError as exc:
        if looks_like_refusal(text):
            raise ModelRefused("the model declined the review instead of returning findings") from exc
        raise MalformedFindings(f"response was not valid JSON: {exc}") from exc

    raw = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise MalformedFindings("expected a 'findings' array")
    try:
        return [AgentFinding.model_validate(item) for item in raw]
    except ValidationError as exc:
        raise MalformedFindings(f"finding failed validation: {exc}") from exc


def apply_gate(
    findings: list[AgentFinding], fail_severities: list[str]
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


def review(diff: str, client: LLMClient, config: AgentConfig) -> AgentRunResult:
    """Run one review pass and return the result. Read-only; no filesystem writes."""
    system_prompt = build_system_prompt(config.review_preferences)
    user_prompt = build_user_prompt(diff)
    provider = config.llm_client
    token_ceiling = config.effective_token_ceiling("review")
    fail_severities = config.effective_fail_severities("review")

    # Preflight: estimate worst-case usage (prompt in + max output) and abort BEFORE
    # calling the provider, so an oversized diff never spends on the user's key.
    estimated = (
        approx_tokens(system_prompt) + approx_tokens(user_prompt) + config.max_tokens
    )
    if estimated > token_ceiling:
        _log_agent_call(
            model=config.model_id, tokens_in=0, tokens_out=0, provider=provider,
            status="error", error_kind="TokenCeilingExceeded",
        )
        raise CeilingExceeded(
            "token",
            f"estimated {estimated} tokens exceeds ceiling {token_ceiling} "
            "(aborted before the provider call)",
        )

    try:
        started = time.perf_counter()
        resp = client.complete(system_prompt, user_prompt, config.max_tokens)
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
    if used > token_ceiling:
        _log_agent_call(
            model=resp.model, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
            provider=provider, latency_ms=latency_ms,
            status="error", error_kind="TokenCeilingExceeded",
        )
        raise CeilingExceeded("token", f"run used {used} tokens, ceiling is {token_ceiling}")

    try:
        findings = parse_findings(resp.text)
    except (MalformedFindings, ModelRefused) as exc:
        _log_agent_call(
            model=resp.model, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
            provider=provider, latency_ms=latency_ms,
            status="error", error_kind=type(exc).__name__,
        )
        raise

    # Success: the provider call worked and its output parsed.
    _log_agent_call(
        model=resp.model, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
        provider=provider, latency_ms=latency_ms, status="ok",
    )

    gate, reason = apply_gate(findings, fail_severities)
    return AgentRunResult(
        findings=findings,
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
        model=resp.model,
        gate=gate,
        gate_reason=reason,
    )
