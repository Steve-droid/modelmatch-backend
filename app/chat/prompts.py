"""Prompts as code for the grounded Q&A chat (S14b) — the two LLM calls.

Same philosophy as the ingestion prompts: versioned, inspectable templates with an
explicit `name@version` id, and `.replace()`-based substitution (NOT `str.format`) —
the substituted values (user questions, savings figures, query rows) are untrusted
and routinely contain literal `{`/`}`.

Two prompts:
- `chat-sql@v1`    — LLM #1: question (+ spend summary) → one read-only SELECT over
  `chat_catalog`, or `NO_QUERY` (answerable from the spend summary alone), or
  `CANNOT_ANSWER` (off-topic). The three-way decision is what makes the hybrid work:
  savings questions need no catalog lookup, catalog questions do, off-topic gets
  refused with no answer-gen call.
- `chat-answer@v2` — LLM #2: question + spend summary + retrieved catalog rows → an
  answer grounded ONLY in those, or an honest "I don't have that."

Product framing baked into both: Modicum proves a cheaper LLM is good enough to run
as a CI **code-review agent** that flags BOTH security risks AND coding-style bad
practices in PR diffs — it is NOT a "summarize what changed" tool.

Placeholders are `<<UPPER>>` sentinels filled by `render(**fields)`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.chat.schema_context import build_schema_context

NO_QUERY_TOKEN = "NO_QUERY"
REFUSAL_TOKEN = "CANNOT_ANSWER"


@dataclass(frozen=True)
class RenderedPrompt:
    system: str
    user: str
    prompt_id: str  # "<name>@<version>"


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    version: str
    system: str
    user_template: str

    def render(self, **fields: str) -> RenderedPrompt:
        user = self.user_template
        for key, value in fields.items():
            user = user.replace(f"<<{key.upper()}>>", value)
        return RenderedPrompt(
            system=self.system, user=user, prompt_id=f"{self.name}@{self.version}"
        )


# Few-shot: teach the three-way decision. Catalog questions → a SELECT over the view
# (comparability-correct); personal spend/quality → NO_QUERY; off-topic → CANNOT_ANSWER.
FEW_SHOT_EXAMPLES: list[tuple[str, str]] = [
    (
        "which model is the cheapest?",
        "SELECT model, vendor, cost_per_mtok\n"
        "FROM chat_catalog\n"
        "WHERE cost_per_mtok IS NOT NULL\n"
        "ORDER BY cost_per_mtok ASC;",
    ),
    (
        "what does Amazon Nova Lite score on CodeReviewBench?",
        "SELECT model, benchmark, metric, score\n"
        "FROM chat_catalog\n"
        "WHERE model ILIKE '%nova lite%' AND benchmark ILIKE '%codereviewbench%';",
    ),
    (
        "which models score highest on CodeReviewBench pass@1?",
        "SELECT model, vendor, score\n"
        "FROM chat_catalog\n"
        "WHERE benchmark ILIKE '%codereviewbench%' AND metric = 'pass@1'\n"
        "ORDER BY score DESC;",
    ),
    ("how much have I saved so far?", NO_QUERY_TOKEN),
    ("is my review quality good enough to bank the savings?", NO_QUERY_TOKEN),
    ("what's the weather today?", REFUSAL_TOKEN),
]


def format_examples(examples: list[tuple[str, str]] = FEW_SHOT_EXAMPLES) -> str:
    # Label the answer "Output:" to match the final prompt cue — using "SQL:" here
    # primed the model to echo a "SQL:" prefix (verified live), breaking the gate.
    return "\n\n".join(f"Question: {q}\nOutput: {sql}" for q, sql in examples)


def format_rows(columns: list[str], rows: list[tuple], max_rows: int = 50) -> str:
    """A compact text rendering of the catalog result set for the answer prompt."""
    if not rows:
        return "(no rows)"
    lines = [" | ".join(columns)]
    for row in rows[:max_rows]:
        lines.append(" | ".join("" if v is None else str(v) for v in row))
    if len(rows) > max_rows:
        lines.append(f"... ({len(rows) - max_rows} more rows)")
    return "\n".join(lines)


SQL_GEN = PromptTemplate(
    name="chat-sql",
    version="v1",
    system=(
        "You are a careful PostgreSQL analyst for Modicum. Modicum helps a "
        "developer pick a cost-effective LLM to run as a CI code-review agent — an "
        "agent that reviews pull-request diffs and flags BOTH security risks AND "
        "coding-style bad practices, with the pass/fail gate staying in their CI. "
        "Given the user's question, decide ONE of three outputs:\n"
        "1. If it asks about MODELS, vendors, benchmarks, scores, or model prices, "
        "write exactly ONE read-only SELECT over the chat_catalog view that answers "
        "it, using ONLY the listed columns and following the rules. Output ONLY the "
        "SQL — no markdown, no code fences, no comments, no prose.\n"
        f"2. If it asks about the USER'S OWN spend, savings, cost so far, or review "
        f"quality, that is answered from the spend summary (not the catalog) — output "
        f"exactly: {NO_QUERY_TOKEN}\n"
        f"3. If it is unrelated to Modicum, models, spend, or CI review — output "
        f"exactly: {REFUSAL_TOKEN}"
    ),
    user_template=(
        "<<SCHEMA>>\n\n"
        "The user's current spend summary (context for deciding NO_QUERY; do not put "
        "these figures in SQL):\n<<SAVINGS>>\n\n"
        "Example questions and the correct output:\n"
        "<<EXAMPLES>>\n\n"
        "<<HISTORY>>"
        "<<RETRY>>"
        "Now decide the output for this question.\n"
        "Question: <<QUESTION>>\n"
        "Output:"
    ),
)


def build_retry_context(failed_sql: str, error: str) -> str:
    """Feedback injected into the next SQL-gen attempt after a DB execution error."""
    return (
        "Your previous SQL failed when executed. Correct it.\n"
        f"Previous SQL: {failed_sql}\n"
        f"Database error: {error}\n\n"
    )


ANSWER_GEN = PromptTemplate(
    name="chat-answer",
    # v2 (P38 B3): added the saved-vs-spend nudge below — the live opener had been
    # presenting the period spend as if it were the savings.
    version="v2",
    system=(
        "You are the Modicum assistant. Modicum proves a cheaper LLM is good "
        "enough to run as a CI code-review agent (it flags security risks and "
        "coding-style bad practices in PR diffs) and shows the money saved versus a "
        "baseline model. Answer the user's question using ONLY the spend summary and "
        "the catalog query results provided below — never invent models, numbers, "
        "vendors, or fields not present. If the data provided does not contain the "
        "answer, say so plainly. If there are no catalog rows, say there were no "
        "matching results. Be concise and factual. "
        "'saved' always refers to the Cumulative saved vs baseline figure; 'spend' is "
        "the actual amount paid; never present spend as savings. "
        "Write in plain punctuation: use commas, colons or separate sentences, never "
        "em dashes."
    ),
    user_template=(
        "Question: <<QUESTION>>\n\n"
        "Spend summary:\n<<SAVINGS>>\n\n"
        "Catalog query that was executed (NO_QUERY means none was needed):\n"
        "<<SQL>>\n\n"
        "Catalog results — <<ROW_COUNT>> row(s), columns: <<COLUMNS>>\n"
        "<<ROWS>>\n\n"
        "Answer the question using only the spend summary and these results."
    ),
)

TEMPLATES = {t.name: t for t in (SQL_GEN, ANSWER_GEN)}


def build_sql_prompt(
    question: str,
    savings_summary: str,
    history: str = "",
    retry_context: str = "",
) -> RenderedPrompt:
    """Assemble the LLM #1 prompt (schema + spend summary + few-shot + question).

    `retry_context` carries the previous failed SQL + error on a retry; empty on the
    first attempt (so the rendered prompt is unchanged in the common case)."""
    return SQL_GEN.render(
        schema=build_schema_context(),
        savings=savings_summary,
        examples=format_examples(),
        history=history,
        retry=retry_context,
        question=question,
    )


def build_answer_prompt(
    question: str,
    savings_summary: str,
    sql: str | None,
    columns: list[str],
    rows: list[tuple],
) -> RenderedPrompt:
    """Assemble the grounded-answer prompt from the question, spend summary, and rows."""
    return ANSWER_GEN.render(
        question=question,
        savings=savings_summary,
        sql=sql if sql else NO_QUERY_TOKEN,
        row_count=str(len(rows)),
        columns=", ".join(columns) if columns else "(none)",
        rows=format_rows(columns, rows),
    )
