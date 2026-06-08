"""Request/response models for the grounded Q&A chat endpoint (S14b; camelCase out).

The answer carries a `retrievalTrace[]` (which savings figure / catalog rows grounded
it — the architecture §4.2 contract) plus a `debug` block (the ABC-style SQL/tokens
audit: what query ran, how many rows, how many attempts, tokens spent)."""

from datetime import datetime
from typing import Optional

from pydantic import Field

from app.schemas.base import CamelModel


class ChatRequest(CamelModel):
    question: str = Field(min_length=1, max_length=2000)


class RetrievalTraceOut(CamelModel):
    """One grounding source the answer used (architecture §4.2 / `retrieval_trace`)."""

    kind: str          # 'savings' | 'benchmark_result'
    ref: str
    snippet: Optional[str] = None


class ChatDebug(CamelModel):
    """The audit trail behind an answer — the catalog query that ran (if any) + cost.

    Dev/test only (CHAT_DEBUG_ENABLED): `debug` is `null` for normal users so raw SQL
    and internals are never exposed. It deliberately carries NO secrets, secret refs,
    or raw DB errors (the user-facing answer is a clean message; the technical error
    stays server-side)."""

    sql: Optional[str] = None
    no_query: bool = False     # answered from the spend summary alone (no catalog SQL)
    row_count: int = 0
    columns: list[str] = []
    attempts: int = 0
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0


class ChatAnswerResponse(CamelModel):
    answer: str
    ok: bool                   # a grounded answer was produced
    refused: bool              # off-topic / unanswerable (honest refusal)
    retrieval_trace: list[RetrievalTraceOut] = []
    debug: Optional[ChatDebug] = None   # dev/test only (CHAT_DEBUG_ENABLED); null otherwise


class ChatMessageOut(CamelModel):
    id: int
    role: str                  # 'user' | 'assistant'
    text: Optional[str] = None
    created_at: Optional[datetime] = None
    # The grounding the assistant message was built on (opener + every prior answer),
    # so reloaded history keeps its trace. Empty for user messages.
    retrieval_trace: list[RetrievalTraceOut] = []


class ChatHistoryResponse(CamelModel):
    messages: list[ChatMessageOut]
