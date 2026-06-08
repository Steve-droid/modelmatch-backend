"""Grounded Q&A chat routes (S14b; architecture §4.2, #4).

- GET  /projects/{id}/chat        → the conversation so far (seeds the deterministic
  "explain my spend" opener on first visit). No LLM.
- POST /projects/{id}/chat {question} → a grounded answer + retrieval trace + debug.

Both are user-JWT + owner-scoped (a USER asking about their OWN project), NOT the
per-project CI-token path (that's agent ingest only). The LLM client and the read-only
catalog engine are injected dependencies so tests can substitute a real Bedrock client
+ the test database's read-only role. The hourly token cap maps to 429.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, get_db, require_owner
from app.chat import pipeline
from app.chat.execute import get_readonly_engine
from app.config import get_settings
from app.llm import LLMClient
from app.llm_budget import HourlyTokenCapExceeded
from app.models import ChatMessage, Project, RetrievalTrace, User
from app.schemas.chat import (
    ChatAnswerResponse,
    ChatDebug,
    ChatHistoryResponse,
    ChatMessageOut,
    ChatRequest,
    RetrievalTraceOut,
)

router = APIRouter(prefix="/projects", tags=["chat"])


def get_chat_llm_client() -> LLMClient:
    return pipeline.build_chat_client()


def get_chat_engine() -> Engine:
    return get_readonly_engine()


def _require_owned_project(db: Session, project_id: int, current_user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    require_owner(project.user_id, current_user)
    return project


@router.get("/{project_id}/chat", response_model=ChatHistoryResponse)
def get_chat_history(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChatHistoryResponse:
    _require_owned_project(db, project_id, current_user)
    pipeline.ensure_opener(db, project_id, current_user)  # seed opener if empty (no LLM)
    messages = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.project_id == project_id)
        .order_by(ChatMessage.id)
    ).all()

    # Each assistant message carries its persisted retrieval trace (the opener + every
    # prior answer keep their grounding when history is reloaded). One grouped query.
    traces: dict[int, list[RetrievalTrace]] = {}
    msg_ids = [m.id for m in messages]
    if msg_ids:
        for tr in db.scalars(
            select(RetrievalTrace)
            .where(RetrievalTrace.chat_message_id.in_(msg_ids))
            .order_by(RetrievalTrace.id)
        ).all():
            traces.setdefault(tr.chat_message_id, []).append(tr)

    return ChatHistoryResponse(
        messages=[
            ChatMessageOut(
                id=m.id,
                role=m.role,
                text=m.text,
                created_at=m.created_at,
                retrieval_trace=[
                    RetrievalTraceOut(kind=t.kind, ref=t.ref, snippet=t.snippet)
                    for t in traces.get(m.id, [])
                ],
            )
            for m in messages
        ]
    )


@router.post("/{project_id}/chat", response_model=ChatAnswerResponse)
def post_chat(
    project_id: int,
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    client: LLMClient = Depends(get_chat_llm_client),
    engine: Engine = Depends(get_chat_engine),
) -> ChatAnswerResponse:
    try:
        result = pipeline.answer_question(
            db, project_id, current_user, payload.question, client, engine=engine
        )
    except HourlyTokenCapExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc

    # Raw SQL/internals are NOT exposed to normal users (no admin role yet). The debug
    # block is populated only when explicitly enabled for dev/test; otherwise it's null.
    debug = None
    if get_settings().chat_debug_enabled:
        debug = ChatDebug(
            sql=result.sql,
            no_query=result.no_query,
            row_count=result.row_count,
            columns=result.columns,
            attempts=result.attempts,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
        )

    return ChatAnswerResponse(
        answer=result.answer,
        ok=result.ok,
        refused=result.refused,
        retrieval_trace=[
            RetrievalTraceOut(kind=t.kind, ref=t.ref, snippet=t.snippet)
            for t in result.traces
        ],
        debug=debug,
    )
