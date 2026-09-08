"""Agent outcomes that are NOT a pass, and the one exit-code table both modes share.

One image, two modes (review / security), ONE table. The Jenkins stage branches on
the code, so every non-pass outcome must be distinguishable — above all a refusal
(`3`), which returns no findings and would otherwise read as "no vulnerabilities
found" over code the model never examined.

    0    gate pass / clean scan
    1    gate fail — a blocking finding → the stage fails (the demo beat)
    2    the model's output did not parse (security: after AGENT_MAX_ATTEMPTS)
    3    the model REFUSED the task — never a clean result
    4    config / input / credential / API-fetch / provider failure
    124  a ceiling aborted the run (tokens, steps or wall-clock)

Changed in 1.1.0: v1 used 3 for the token ceiling and had no refusal code. The
ceiling moved to 124 (the conventional "timed out" code, also what the P38b spike
used) so 3 can carry the one outcome a security gate must never mistake for clean.
"""

from __future__ import annotations

EXIT_PASS = 0
EXIT_GATE_FAIL = 1
EXIT_MALFORMED = 2
EXIT_REFUSED = 3
EXIT_CONFIG = 4
EXIT_CEILING = 124

# Every non-zero code, for tests that assert "only 0 is a pass".
NON_PASS_CODES = frozenset({EXIT_GATE_FAIL, EXIT_MALFORMED, EXIT_REFUSED, EXIT_CONFIG, EXIT_CEILING})


class AgentError(Exception):
    """Base for every non-pass outcome the CLI maps to an exit code."""

    exit_code = EXIT_CONFIG
    error = "agent_error"


class MalformedFindings(AgentError):
    """The model returned something that isn't the agreed findings JSON."""

    exit_code = EXIT_MALFORMED
    error = "malformed_findings"


class ModelRefused(AgentError):
    """The model declined the task instead of doing it. NOT a clean result."""

    exit_code = EXIT_REFUSED
    error = "model_refused"


class CeilingExceeded(AgentError):
    """A per-run ceiling (tokens / steps / wall-clock) aborted the run."""

    exit_code = EXIT_CEILING
    error = "ceiling_exceeded"

    def __init__(self, which: str, detail: str) -> None:
        super().__init__(f"{which} ceiling tripped: {detail}")
        self.which = which

    @property
    def log_kind(self) -> str:
        """The `error_kind` on the per-request LLM log line — precise per ceiling, and
        `TokenCeilingExceeded` stays byte-identical to v1 for existing log queries."""
        return {"token": "TokenCeilingExceeded", "step": "StepCeilingExceeded",
                "wall-clock": "WallClockCeilingExceeded"}.get(self.which, "CeilingExceeded")


# Backward-compatible name: v1 raised TokenCeilingExceeded from review.py.
TokenCeilingExceeded = CeilingExceeded


class AgentConfigError(AgentError):
    """Bad or missing configuration, credential, workspace or API config fetch."""

    exit_code = EXIT_CONFIG
    error = "config_error"


class ProviderError(AgentError):
    """The provider / runtime failed before producing any output (auth, network…)."""

    exit_code = EXIT_CONFIG
    error = "llm_provider_error"


_REFUSAL_MARKERS = (
    "cannot fulfill", "can't fulfill", "unable to perform", "i cannot perform",
    "i'm unable to", "i am unable to", "cannot assist", "can't assist",
    "i cannot help", "i can't help", "cannot comply", "can't comply",
    "i won't", "i will not", "not able to assist",
)


def looks_like_refusal(text: str) -> bool:
    """Did the model decline instead of answering?

    Measured on Gemini 3.5 Flash during P38b: same prompt, same repo, a refusal in
    prose roughly two runs in three. Tool-call count is deliberately NOT a guard —
    observed refusals often glob the repo once and *then* decline. The wording is the
    reliable signal. A genuine clean result is valid JSON with an empty array and
    never reaches this check.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(m in t for m in _REFUSAL_MARKERS)
