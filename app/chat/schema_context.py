"""The curated schema description handed to LLM #1 for catalog SQL generation (S14b).

Two design choices, mirroring the ABC SQL-RAG chat:

1. **One curated object only.** The LLM is told the database has a single readable
   view, `chat_catalog` — the same denormalized shape `catalog.service.list_catalog`
   returns. It is NOT told the base tables exist, so it cannot ask for columns it
   never learns about (`user.password_hash`, `jenkins_connection` secret refs, the
   per-tenant `ci_run` rows). This mirrors the GRANT in the read-only-role migration:
   the prompt-level boundary and the DB-level boundary describe the same surface.

2. **Static, deterministic.** The column list is hand-kept in sync with the view in
   the migration (a view isn't an ORM table to introspect). Hard-coded data rules
   below encode the conventions the model must follow (the comparability rule, what
   the cost column means, and that personal spend/savings come from a separate
   summary — never from this view).
"""

from __future__ import annotations

# Kept in sync with the `chat_catalog` view — created in
# migrations/.../a1b2c3d4e5f6_chat_readonly_role_and_view.py and extended with the
# benchmark-provenance columns in .../a7b8c9d0e1f2_benchmark_as_of_and_notes.py
CATALOG_VIEW = "chat_catalog"

_COLUMNS: list[tuple[str, str]] = [
    ("id", "integer — the catalog row id"),
    ("model", "text — the model name, e.g. 'Amazon Nova Lite', 'Claude Haiku 4.5'"),
    ("vendor", "text — the model's vendor, e.g. 'Amazon', 'Anthropic', 'Google'"),
    ("benchmark", "text — the benchmark name, e.g. 'CodeReviewBench', 'SWE-bench Verified'"),
    ("harness", "text — the eval harness/scaffold, or NULL if none"),
    ("harness_vendor", "text — the harness vendor, or NULL"),
    (
        "task_type",
        "text — the task the score measures: 'ci_review' (PR code review), "
        "'security_analysis' (agentic vulnerability scanning), or 'agentic_coding'. "
        "Each task type is measured by exactly ONE benchmark and metric.",
    ),
    ("metric", "text — the score's metric, e.g. 'pass@1', 'accuracy'"),
    ("score", "numeric — the benchmark score (higher is better; scale depends on metric)"),
    (
        "cost_per_mtok",
        "numeric — the blended ranking/display price in USD per MILLION tokens "
        "(lower is cheaper)",
    ),
    ("input_price_per_mtok", "numeric — USD per million INPUT tokens, or NULL if unpriced"),
    ("output_price_per_mtok", "numeric — USD per million OUTPUT tokens, or NULL if unpriced"),
    ("context_window", "integer — max context tokens, or NULL"),
    ("source", "text — where the figure came from (provenance), or NULL"),
    ("measured_at", "date — when the figure was measured, or NULL"),
    (
        "benchmark_as_of",
        "date — the date THIS BENCHMARK's figures were taken, or NULL if the "
        "benchmark is continuously refreshed. Use it to answer how current a score is.",
    ),
    (
        "benchmark_notes",
        "text — what the benchmark measures and any caveat about its age, or NULL",
    ),
]

_RULES = """\
Rules and conventions:
- This view is the ONLY table you may query. Do not reference any other table or view.
- Comparability: only compare scores for rows that share the SAME benchmark AND the
  SAME metric (never compare a pass@1 against an accuracy, or scores across different
  benchmarks). When ranking by quality, filter to one (benchmark, metric) pair.
- 'cost_per_mtok' (and the split input/output prices) are in USD per MILLION tokens;
  lower means cheaper. A NULL price means the model is unpriced — exclude it from
  cost rankings rather than treating NULL as 0.
- Cheapest / most expensive → ORDER BY cost_per_mtok (skip NULLs). Best quality on a
  benchmark → ORDER BY score DESC within one (benchmark, metric) pair.
- Scores are point-in-time readings, not live leaderboard values. When a row has a
  'benchmark_as_of' date, any claim about that score is only true as of that date —
  select it (and 'benchmark_notes') whenever the question is about how good, how
  current, or how trustworthy a score is.
- A score is an AVERAGE over the benchmark's whole suite (RealVuln averages 66
  repositories; CodeReviewBench a set of pull requests). It describes typical
  performance across that suite and does NOT predict what a model will find on any
  one repository or pull request — a single run can score well above or below it.
  Never present a benchmark score as an expected result for a specific CI run.
- This view has NO per-user, per-project, spend, savings, or CI-run data. Questions
  about the user's OWN spend, savings, cost so far, or review quality are answered
  from the separate spend summary, NOT from this view — for those, output NO_QUERY.
"""


def build_schema_context() -> str:
    """Render the curated `chat_catalog` view (columns + types) plus the rules block."""
    lines = [
        "Database schema (PostgreSQL). You may query EXACTLY ONE view:",
        f"\nView {CATALOG_VIEW} — one row per (model, benchmark, harness, metric):",
    ]
    for name, desc in _COLUMNS:
        lines.append(f"  - {name} ({desc})")
    return "\n".join(lines) + "\n\n" + _RULES
