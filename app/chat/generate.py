"""The two LLM calls of the chat pipeline (S14b).

`generate_sql` is LLM #1 (question → one catalog SELECT, or NO_QUERY, or
CANNOT_ANSWER). `generate_answer` is LLM #2 (grounded answer from the spend summary +
retrieved rows). Both take an injected `LLMClient`, so the pipeline runs against the
fake client (offline) or — per Steve's S14b directive — the real Bedrock Nova client.

The LLMClient seam here is `complete(system, user, max_tokens) -> LLMResponse`
(ModelMatch's signature), not ABC's `complete(prompt, system=...)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.chat.prompts import (
    NO_QUERY_TOKEN,
    REFUSAL_TOKEN,
    build_answer_prompt,
    build_sql_prompt,
)
from app.llm import LLMClient

# A leading "SQL:" / "Output:" label the model may echo from the few-shot format.
_LABEL_RE = re.compile(r"^(sql|output)\s*:\s*", re.IGNORECASE)


@dataclass(frozen=True)
class SqlGeneration:
    sql: str | None       # cleaned SELECT, or None for NO_QUERY / refusal
    no_query: bool        # answerable from the spend summary alone (no catalog lookup)
    refused: bool         # off-topic / unanswerable
    prompt_id: str
    tokens_in: int
    tokens_out: int
    model: str
    raw_output: str       # exactly what the model returned (for debugging)


def _clean_output(text: str) -> str:
    """Normalize LLM #1's raw output to a bare SQL string or trichotomy token.

    Real Nova (verified live) wraps SQL in ```sql ... ``` and/or echoes the few-shot's
    `SQL:` / `Output:` label despite instructions — both would break the gate's parse.
    Strip the fence, then a single leading label."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return _LABEL_RE.sub("", t, count=1).strip()


def generate_sql(
    question: str,
    savings_summary: str,
    client: LLMClient,
    *,
    max_tokens: int,
    history: str = "",
    retry_context: str = "",
) -> SqlGeneration:
    """Ask LLM #1 to decide: a catalog SELECT, NO_QUERY, or CANNOT_ANSWER.

    `retry_context` (set after a failed execution) is fed back into the prompt so the
    model can correct its previous query."""
    prompt = build_sql_prompt(question, savings_summary, history, retry_context)
    resp = client.complete(prompt.system, prompt.user, max_tokens)
    cleaned = _clean_output(resp.text)
    upper = cleaned.upper()
    refused = upper.startswith(REFUSAL_TOKEN)
    no_query = not refused and upper.startswith(NO_QUERY_TOKEN)
    return SqlGeneration(
        sql=None if (refused or no_query) else cleaned,
        no_query=no_query,
        refused=refused,
        prompt_id=prompt.prompt_id,
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
        model=resp.model,
        raw_output=resp.text,
    )


@dataclass(frozen=True)
class AnswerGeneration:
    answer: str
    prompt_id: str
    tokens_in: int
    tokens_out: int
    model: str


def generate_answer(
    question: str,
    savings_summary: str,
    sql: str | None,
    columns: list[str],
    rows: list[tuple],
    client: LLMClient,
    *,
    max_tokens: int,
) -> AnswerGeneration:
    """Ask LLM #2 for an answer grounded only in the spend summary + retrieved rows."""
    prompt = build_answer_prompt(question, savings_summary, sql, columns, rows)
    resp = client.complete(prompt.system, prompt.user, max_tokens)
    return AnswerGeneration(
        answer=resp.text.strip(),
        prompt_id=prompt.prompt_id,
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
        model=resp.model,
    )
