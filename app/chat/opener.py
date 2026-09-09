"""The deterministic "explain my spend" opener + savings grounding (S14b).

The chat's first assistant message — and the grounding for every spend/savings/quality
question — comes from the S14 savings aggregate, NOT from the LLM. These are pure
functions over the `SavingsResponse` DTO (no DB, no model call, $0, fully testable):

- `format_savings_snapshot` → a compact, factual block injected into both LLM prompts
  as the authoritative spend figures (the model may cite these but never invent them).
- `build_opener` → the friendly plain-language opening summary the user sees first.
- `savings_trace` → the single `retrieval_trace` row (kind='savings') recording which
  figures grounded a savings answer.

Product framing: the model these figures describe runs as a CI **code-review agent**
that flags security risks AND coding-style bad practices in PR diffs — not a
"summarize the changes" tool. The opener says so.
"""

from __future__ import annotations

from decimal import Decimal

from app.schemas.savings import SavingsResponse
from app.tasks import SECURITY_ANALYSIS, TASK_LABELS

_SAVINGS_REF = "savings:project"

# E20: what the agent DOES per task — the spend summary and the opener name it, so
# the chat never describes a security scan as "reviewing PR diffs" (or vice versa).
_TASK_DESCRIPTION = {
    "ci_review": (
        "one call per pull request: reviews the PR diff for security risks and "
        "coding-style issues; findings gate the build on high/critical"
    ),
    SECURITY_ANALYSIS: (
        "an agentic scan of the whole checkout for vulnerabilities; findings carry "
        "CWE ids and a critical one fails the build"
    ),
}


def task_line(task_type: str | None) -> str:
    """'PR code review (ci_review)' — the label + the catalog vocabulary."""
    if not task_type:
        return "CI code review"
    label = TASK_LABELS.get(task_type, task_type.replace("_", " "))
    return f"{label} ({task_type})"


def _task_verb(task_type: str | None) -> str:
    """What the selected model has been doing, for the opener sentence."""
    if task_type == SECURITY_ANALYSIS:
        return "scanned your repository for vulnerabilities (agentic security analysis, CWE-tagged findings)"
    return "reviewed your PR diffs (security + coding-style)"


def _money(value: Decimal | None) -> str:
    """USD with 4 dp (the demo's per-run costs are sub-cent); '—' when unknown."""
    if value is None:
        return "—"
    return f"${value:.4f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def format_savings_snapshot(savings: SavingsResponse) -> str:
    """The authoritative spend figures, as a compact block for the LLM to ground on.

    Only figures the S14 engine computed — the model answers spend questions from
    these or says it doesn't have the data. Never invent or recompute."""
    k = savings.kpis
    selected = savings.selected_model or "the selected model"
    baseline = savings.baseline_model or "the baseline"
    # acceptance_rate is a 0–1 fraction (see app.quality.service); scale for display.
    rate = "not yet rated" if k.acceptance_rate is None else f"{k.acceptance_rate * 100:.0f}%"
    task = savings.task_type
    quality_label = {"banking": "quality target met", "quality_risk": "below quality target",
                     "unrated": "not yet rated"}[k.quality_status]
    lines = [
        "Spend summary (authoritative, computed by Modicum, not by you):",
        f"- Task: {task_line(task)} — "
        f"{_TASK_DESCRIPTION.get(task or '', 'the CI agent task for this project')}",
        f"- Selected model (runs the CI agent for this task): {selected}",
        f"- Baseline model (the expensive default, costed but not run): {baseline}",
        f"- CI runs in range: {k.runs_count} "
        f"({k.banked_runs} {'run' if k.banked_runs == 1 else 'runs'} counted toward savings, "
        f"quality-risk {k.quality_risk_runs}, "
        f"unrated {k.unrated_runs})",
        f"- Cumulative saved vs baseline (quality-passing runs only): "
        f"{_money(k.cumulative_saved)}"
        + (f" ({_pct(k.saved_pct)} of baseline)" if k.saved_pct is not None else ""),
        f"- Spend this period (actual): {_money(k.spend_this_period)}",
        f"- Savings at quality risk (excluded from the headline): {_money(k.quality_risk)}",
        f"- Finding acceptance rate: {rate} "
        f"(quality threshold {k.threshold * 100:.0f}%, status: {quality_label})",
    ]
    if k.projected_monthly_savings is not None:
        lines.append(
            f"- Projected monthly savings (linear): {_money(k.projected_monthly_savings)}"
        )
    return "\n".join(lines)


def build_opener(savings: SavingsResponse) -> str:
    """The deterministic opening 'explain my spend' message (plain language)."""
    k = savings.kpis
    if k.runs_count == 0:
        return (
            "Hi! I'm your Modicum assistant. Once your Jenkins pipeline runs the "
            "CI code-review agent (it flags security risks and coding-style issues in "
            "your PR diffs), I'll explain your spend here: how much the recommended "
            "model is saving you versus the baseline, and whether review quality is "
            "holding up. No runs yet, so there's nothing to total. Ask me about the "
            "model catalog any time."
        )

    selected = savings.selected_model or "your selected model"
    baseline = savings.baseline_model or "the baseline"
    saved = _money(k.cumulative_saved)
    pct = f" ({_pct(k.saved_pct)} of what {baseline} would have cost)" if k.saved_pct is not None else ""

    if k.quality_status == "banking":
        quality_line = (
            f"Review quality is holding. Finding acceptance is "
            f"{_pct(None if k.acceptance_rate is None else k.acceptance_rate * 100)}, "
            f"at or above your {k.threshold * 100:.0f}% threshold, so those savings count."
        )
    elif k.quality_status == "quality_risk":
        quality_line = (
            f"Heads up: finding acceptance is "
            f"{_pct(None if k.acceptance_rate is None else k.acceptance_rate * 100)}, below your "
            f"{k.threshold * 100:.0f}% threshold, so {_money(k.quality_risk)} of savings is "
            "flagged as quality risk and kept out of the headline."
        )
    else:
        quality_line = (
            "No findings have been rated yet, so no runs count toward your savings total "
            "until review quality is confirmed."
        )

    return (
        f"Here's your spend so far on {task_line(savings.task_type)}. Across "
        f"{k.runs_count} CI run(s), {selected} has {_task_verb(savings.task_type)} for "
        f"{_money(k.spend_this_period)}, versus running {baseline}, saving you {saved}{pct}. "
        f"{quality_line} Ask me anything about these numbers, the quality, or the model catalog."
    )


def savings_trace(savings: SavingsResponse) -> tuple[str, str]:
    """The (ref, snippet) for the kind='savings' retrieval-trace row of an answer."""
    return _SAVINGS_REF, format_savings_snapshot(savings)
