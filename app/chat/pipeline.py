"""The grounded Q&A chat orchestrator (S14b) — the hybrid pipeline end to end.

`answer_question` ties the two grounding sources to a single grounded answer + trace:

    savings  (DETERMINISTIC — the trusted S14 engine, the user's own figures)
       +
    catalog  (UNTRUSTED LLM SQL → gate → read-only role → rows)   ← retrieve_catalog
       ▼
    LLM #2 grounded answer (spend summary + catalog rows only)    ← unless refused

Three terminal shapes, like ABC: a grounded answer; an honest off-topic refusal
(`CANNOT_ANSWER` → no answer-gen call); or a clear failure message when the catalog
query couldn't run. A NO_QUERY question is answered from the spend summary alone.

Safety/cost: every in-cluster Nova call is wrapped by the hourly token cap (reserve →
call → reconcile/release; busting it raises `HourlyTokenCapExceeded` → 429 with no
provider spend). Savings are read on the app's normal connection (trusted); only the
LLM-generated catalog SQL touches the read-only role. One `llm_call` row per turn
(`purpose='chat'`, `ci_run_id=NULL`); the turn (question + answer + the rows/figures
that grounded it) is persisted to `chat_message` + `retrieval_trace`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app import llm_budget
from app.chat import opener
from app.chat.generate import generate_answer
from app.chat.retrieve import retrieve_catalog
from app.config import get_settings
from app.llm import LLMClient, LLMResponse, approx_tokens, build_llm_client
from app.models import ChatMessage, LlmCall, RetrievalTrace, User
from app.observability import LLMObservation
from app.observability.metrics import record_llm_call
from app.savings import dashboard

_OFF_TOPIC_MESSAGE = (
    "I can only answer questions about your Driftplain spend, review quality, and the "
    "model catalog. Try rephrasing your question around those."
)
_FAILED_MESSAGE = (
    "I wasn't able to look that up in the model catalog. Please try rephrasing your "
    "question."
)
_MAX_CATALOG_TRACES = 50  # bound persisted trace rows (results are already LIMIT-capped)


def build_chat_client() -> LLMClient:
    """The in-cluster LLM client for chat, per config (fake offline; Bedrock Nova via
    IRSA in-cluster) — same surface as ingestion. A FastAPI dependency tests override."""
    s = get_settings()
    return build_llm_client(s.llm_client, model=s.bedrock_model_id, region=s.aws_region)


class _CappedClient:
    """Wraps an LLMClient so every `complete()` is fenced by the hourly token cap:
    reserve the worst case → call → reconcile to actual (release on error). Raises
    `HourlyTokenCapExceeded` (→ 429) before the provider call when over budget.
    Records each response so the pipeline can total tokens for the trace + llm_call."""

    def __init__(self, inner: LLMClient, db: Session, cap: int) -> None:
        self._inner = inner
        self._db = db
        self._cap = cap
        self.responses: list[LLMResponse] = []
        self.latency_ms = 0  # summed across every complete() this turn (for S16)

    def complete(self, system: str, user: str, max_tokens: int) -> LLMResponse:
        estimate = approx_tokens(system) + approx_tokens(user) + max_tokens
        reservation = llm_budget.reserve_tokens(self._db, estimate, self._cap)
        try:
            started = time.perf_counter()
            resp = self._inner.complete(system, user, max_tokens)
            self.latency_ms += int((time.perf_counter() - started) * 1000)
        except Exception:
            llm_budget.release(self._db, reservation)
            raise
        llm_budget.reconcile(self._db, reservation, resp.tokens_in + resp.tokens_out)
        self.responses.append(resp)
        return resp


@dataclass(frozen=True)
class TraceEntry:
    kind: str   # 'savings' | 'benchmark_result'
    ref: str
    snippet: str


@dataclass(frozen=True)
class ChatResult:
    answer: str             # user-facing
    ok: bool                # a grounded answer was produced
    refused: bool           # off-topic / unanswerable
    no_query: bool          # answered from the spend summary alone (no catalog SQL)
    traces: list[TraceEntry] = field(default_factory=list)
    sql: str | None = None
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    attempts: int = 0
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    assistant_message_id: int | None = None


def _catalog_traces(columns: list[str], rows: list[tuple]) -> list[TraceEntry]:
    """One trace row per returned catalog row (kind='benchmark_result')."""
    id_idx = columns.index("id") if "id" in columns else None
    out: list[TraceEntry] = []
    for i, row in enumerate(rows[:_MAX_CATALOG_TRACES]):
        ref = f"chat_catalog:{row[id_idx]}" if id_idx is not None else f"chat_catalog:#{i}"
        snippet = " | ".join(
            f"{c}={'' if v is None else v}" for c, v in zip(columns, row)
        )
        out.append(TraceEntry(kind="benchmark_result", ref=ref, snippet=snippet))
    return out


def _observe_chat_turn(
    capped: "_CappedClient",
    provider: str,
    model_fallback: str,
    question: str,
    sql: str | None,
    row_count: int | None,
    *,
    status: str,
    error_kind: str | None,
) -> None:
    """Emit ONE per-request LLM log line + metrics for a chat turn, totalling whatever
    calls happened (SQL-gen + answer-gen, or a partial prefix on failure). The question
    + generated SQL appear only as derived metadata — never raw."""
    record_llm_call(
        LLMObservation(
            purpose="chat",
            provider=provider,
            model=capped.responses[-1].model if capped.responses else model_fallback,
            tokens_in=sum(r.tokens_in for r in capped.responses),
            tokens_out=sum(r.tokens_out for r in capped.responses),
            latency_ms=capped.latency_ms,
            retrieved_context_size=row_count,
            status=status,
            error_kind=error_kind,
            prompt=question,
            query=sql,
        )
    )


def answer_question(
    db: Session,
    project_id: int,
    current_user: User,
    question: str,
    client: LLMClient,
    *,
    engine: Engine | None = None,
    history: str = "",
    persist: bool = True,
) -> ChatResult:
    """Run the full hybrid pipeline and return a grounded answer + trace.

    Owner-scoped via `dashboard.project_savings` (404 missing / 403 not-yours). Raises
    `llm_budget.HourlyTokenCapExceeded` if the hourly cap would be busted (→ 429); no
    state is persisted in that case (it raises before any answer is formed)."""
    settings = get_settings()
    provider = settings.llm_client
    # Deterministic, trusted, owner-scoped: the user's own spend figures. NO LLM.
    savings = dashboard.project_savings(db, project_id, current_user)
    summary = opener.format_savings_snapshot(savings)

    capped = _CappedClient(client, db, settings.llm_hourly_token_cap)

    # The spend summary grounds every answered turn — always traced (kind='savings').
    sav_ref, sav_snippet = opener.savings_trace(savings)
    traces: list[TraceEntry] = []
    error_kind: str | None = None

    try:
        retrieval = retrieve_catalog(
            question,
            summary,
            capped,
            engine=engine,
            max_tokens=settings.chat_max_tokens,
            max_rows=settings.chat_max_rows,
            history=history,
            max_retries=settings.chat_retry_max,
        )

        if retrieval.refused:
            answer, ok, refused, status = _OFF_TOPIC_MESSAGE, False, True, "ok"
        elif not retrieval.ok:
            # SQL gen/exec failed after retries — a controlled failure (not an exception);
            # surface it as an errored turn. retrieval.error text is NEVER logged.
            answer, ok, refused, status = _FAILED_MESSAGE, False, False, "error"
            error_kind = "retrieval_failed"
        else:
            gen = generate_answer(
                question,
                summary,
                retrieval.sql,
                retrieval.columns,
                retrieval.rows,
                capped,
                max_tokens=settings.chat_max_tokens,
            )
            answer, ok, refused, status = gen.answer, True, False, "ok"
            traces.append(TraceEntry(kind="savings", ref=sav_ref, snippet=sav_snippet))
            traces.extend(_catalog_traces(retrieval.columns, retrieval.rows))
    except llm_budget.HourlyTokenCapExceeded:
        # Over the hourly cap → aborted (→ 429). No turn is persisted; emit a throttled
        # line (status only — the message's token numbers are never logged) and re-raise.
        _observe_chat_turn(
            capped, provider, settings.bedrock_model_id, question, None, None,
            status="throttled", error_kind="HourlyTokenCapExceeded",
        )
        raise
    except Exception as exc:
        _observe_chat_turn(
            capped, provider, settings.bedrock_model_id, question, None, None,
            status="error", error_kind=type(exc).__name__,
        )
        raise

    tokens_in = sum(r.tokens_in for r in capped.responses)
    tokens_out = sum(r.tokens_out for r in capped.responses)
    model = capped.responses[-1].model if capped.responses else retrieval.model

    # Per-request LLM log line + token metrics (S16): one line per chat turn, totalling
    # the SQL-gen + answer-gen calls. The question + generated SQL appear only as derived
    # metadata; retrieved_context_size is the catalog rows that grounded it.
    _observe_chat_turn(
        capped, provider, model, question, retrieval.sql, retrieval.row_count,
        status=status, error_kind=error_kind,
    )

    assistant_message_id: int | None = None
    if persist:
        assistant_message_id = _persist_turn(
            db, project_id, question, answer, traces, tokens_in, tokens_out,
            latency_ms=capped.latency_ms, status=status,
        )

    return ChatResult(
        answer=answer,
        ok=ok,
        refused=refused,
        no_query=retrieval.no_query,
        traces=traces,
        sql=retrieval.sql,
        columns=list(retrieval.columns),
        row_count=retrieval.row_count,
        attempts=retrieval.attempts,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        error=retrieval.error,
        assistant_message_id=assistant_message_id,
    )


def _persist_turn(
    db: Session,
    project_id: int,
    question: str,
    answer: str,
    traces: list[TraceEntry],
    tokens_in: int,
    tokens_out: int,
    *,
    latency_ms: int | None = None,
    status: str = "ok",
) -> int:
    """Persist the user + assistant messages, the answer's retrieval trace, and one
    llm_call row (purpose='chat', ci_run_id NULL). Returns the assistant message id."""
    db.add(ChatMessage(project_id=project_id, role="user", text=question))
    assistant = ChatMessage(project_id=project_id, role="assistant", text=answer)
    db.add(assistant)
    db.flush()  # assign the assistant message id for the trace FKs

    for t in traces:
        db.add(
            RetrievalTrace(
                chat_message_id=assistant.id, kind=t.kind, ref=t.ref, snippet=t.snippet
            )
        )
    db.add(
        LlmCall(
            ci_run_id=None,
            purpose="chat",
            model_id=None,  # Nova's model string isn't a catalog model row (S16 logs it)
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            status=status,
        )
    )
    db.commit()
    return assistant.id


def ensure_opener(db: Session, project_id: int, current_user: User) -> ChatMessage | None:
    """Seed the deterministic 'explain my spend' opener as the first assistant message
    if this project has no chat history yet. Returns the opener message (or None if
    history already exists). NO LLM — pure template over the S14 savings aggregate."""
    from sqlalchemy import select

    existing = db.scalar(
        select(ChatMessage).where(ChatMessage.project_id == project_id).limit(1)
    )
    if existing is not None:
        return None

    savings = dashboard.project_savings(db, project_id, current_user)  # owner-scoped
    text = opener.build_opener(savings)
    message = ChatMessage(project_id=project_id, role="assistant", text=text)
    db.add(message)
    db.flush()
    sav_ref, sav_snippet = opener.savings_trace(savings)
    db.add(
        RetrievalTrace(
            chat_message_id=message.id, kind="savings", ref=sav_ref, snippet=sav_snippet
        )
    )
    db.commit()
    return message
