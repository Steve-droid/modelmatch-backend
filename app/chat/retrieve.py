"""Question → catalog rows: the retrieval half of the chat pipeline (LLM #1 + gate +
read-only execute), mirroring the ABC SQL-RAG retrieve loop.

The savings half is deterministic (the S14 engine) and lives in the pipeline; this
module is only the UNTRUSTED catalog-SQL path: LLM #1 decides, the gate validates, the
read-only role executes, and a DB execution error is fed back for up to `max_retries`
fresh attempts. Two outcomes terminate without retry:
- `refused`  — LLM #1 said the question is off-topic (CANNOT_ANSWER).
- `no_query` — answerable from the spend summary alone (NO_QUERY); no catalog lookup.
A gate rejection (unsafe SQL) is also terminal — it is not retried.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.chat.execute import run_readonly
from app.chat.gate import check_sql
from app.chat.generate import generate_sql
from app.chat.prompts import build_retry_context
from app.llm import LLMClient


@dataclass(frozen=True)
class RetrievalResult:
    ok: bool
    no_query: bool = False           # answerable from the spend summary alone
    refused: bool = False            # off-topic / unanswerable
    sql: str | None = None           # the SQL that ran (or last attempted)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    attempts: int = 0
    error: str | None = None         # failure reason (when not ok and not refused)
    # trace / cost accounting (summed across attempts):
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    prompt_id: str = ""


def retrieve_catalog(
    question: str,
    savings_summary: str,
    client: LLMClient,
    *,
    engine: Engine | None = None,
    max_tokens: int,
    max_rows: int,
    history: str = "",
    max_retries: int = 1,
) -> RetrievalResult:
    """Generate catalog SQL, gate it, run it read-only; retry on DB error up to N."""
    retry_context = ""
    in_tokens = out_tokens = attempts = 0
    model = prompt_id = ""
    last_error: str | None = None
    last_sql: str | None = None

    for _ in range(max_retries + 1):
        attempts += 1
        gen = generate_sql(
            question,
            savings_summary,
            client,
            max_tokens=max_tokens,
            history=history,
            retry_context=retry_context,
        )
        in_tokens += gen.tokens_in
        out_tokens += gen.tokens_out
        model, prompt_id = gen.model, gen.prompt_id

        def _result(**kw) -> RetrievalResult:
            return RetrievalResult(
                attempts=attempts,
                tokens_in=in_tokens,
                tokens_out=out_tokens,
                model=model,
                prompt_id=prompt_id,
                **kw,
            )

        if gen.refused:
            return _result(ok=False, refused=True)
        if gen.no_query:
            # No catalog lookup needed — the spend summary grounds the answer.
            return _result(ok=True, no_query=True)

        gate = check_sql(gen.sql, max_limit=max_rows)
        if not gate.ok:
            # Unsafe SQL is terminal — do not retry (reject → user error).
            return _result(
                ok=False,
                sql=gen.sql,
                error=f"The generated SQL was blocked by the safety gate: {gate.reason}",
            )

        last_sql = gate.sql
        try:
            res = run_readonly(gate.sql, engine=engine)
        except SQLAlchemyError as exc:
            last_error = str(getattr(exc, "orig", None) or exc).strip()
            retry_context = build_retry_context(gate.sql, last_error)
            continue

        return _result(
            ok=True,
            sql=gate.sql,
            columns=res.columns,
            rows=res.rows,
            row_count=res.row_count,
        )

    return RetrievalResult(
        ok=False,
        sql=last_sql,
        error=f"Query failed after {attempts} attempts: {last_error}",
        attempts=attempts,
        tokens_in=in_tokens,
        tokens_out=out_tokens,
        model=model,
        prompt_id=prompt_id,
    )
