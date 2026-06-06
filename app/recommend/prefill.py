"""S7 keyword pre-fill — deterministic free-text → form fields (NO LLM, NO model call).

A small, transparent keyword map. Word-boundary matched (so "top" doesn't fire on
"laptop"), case-insensitive. Returns *suggested* fields plus the terms that fired;
the user confirms before anything is submitted. Same text → same suggestion.

Conflict rule: a single-valued field (budgetSensitivity, latencyNeed) is only
suggested when the matched terms agree on one value — conflicting hits resolve to
None (we don't guess), though both terms still appear in matchedTerms.
"""

from __future__ import annotations

import re

from app.schemas.recommend import PrefillResult

# task type → trigger terms (multiple task types may be suggested at once)
TASK_TYPE_KEYWORDS: dict[str, list[str]] = {
    "agentic_coding": [
        "agentic", "agent", "coding agent", "swe-bench", "swe",
        "code review", "autonomous",
    ],
    "long_context": [
        "long context", "long-context", "rag", "retrieval",
        "large document", "many files",
    ],
}

# budget sensitivity (how cost-sensitive): high = very cost-sensitive (cheap),
# low = not cost-sensitive (best quality), medium = balanced.
BUDGET_KEYWORDS: dict[str, list[str]] = {
    "high": [
        "cheap", "budget", "low cost", "cost-effective", "save money",
        "affordable", "inexpensive",
    ],
    "low": [
        "best", "highest quality", "frontier", "sota", "state of the art",
        "top", "accuracy matters",
    ],
    "medium": ["balanced", "reasonable"],
}

# latency need (stored; does not affect S6 scoring)
LATENCY_KEYWORDS: dict[str, list[str]] = {
    "low": ["fast", "low latency", "realtime", "real-time", "interactive", "quick"],
    "high": ["batch", "offline", "can wait", "overnight"],
}


def _matches(term: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def _match_multi(text: str, table: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """All values whose terms fire, plus the matched terms (both in table order)."""
    values: list[str] = []
    terms: list[str] = []
    for value, keywords in table.items():
        hit = [kw for kw in keywords if _matches(kw, text)]
        if hit:
            values.append(value)
            terms.extend(hit)
    return values, terms


def prefill(text: str) -> PrefillResult:
    lowered = text.lower()

    task_types, tt_terms = _match_multi(lowered, TASK_TYPE_KEYWORDS)
    budget_values, b_terms = _match_multi(lowered, BUDGET_KEYWORDS)
    latency_values, l_terms = _match_multi(lowered, LATENCY_KEYWORDS)

    # single-valued fields: only suggest when the matches agree on one value
    budget = budget_values[0] if len(budget_values) == 1 else None
    latency = latency_values[0] if len(latency_values) == 1 else None

    # dedupe matched terms, preserving first-seen order (deterministic)
    seen: set[str] = set()
    matched_terms = [t for t in (*tt_terms, *b_terms, *l_terms) if not (t in seen or seen.add(t))]

    return PrefillResult(
        task_types=task_types,
        budget_sensitivity=budget,
        latency_need=latency,
        matched_terms=matched_terms,
    )
