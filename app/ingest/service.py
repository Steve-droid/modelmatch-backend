"""Ingestion orchestrator (S5b): source → Nova extract → validate → upsert.

The one place the backend's in-cluster LLM runs against the catalog. The flow is
idempotent and cost-guarded:

    hash(source) seen before?  ── yes ──▶ skip (no LLM, zero tokens)
            │ no
    reserve worst-case tokens (hourly cap)  ── over budget ──▶ 429 (no call)
            │ ok
    Nova extract  →  reconcile actual tokens  →  llm_call row
            │
    validate UNTRUSTED output  →  upsert each valid row (link source_document)
            │
    source_document.status = ingested | invalid

The LLM only *fills* the catalog; ranking over it stays deterministic (S6). All
provider work is behind the injected LLMClient, so tests run on the fake offline.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm_budget
from app.blob_store import BlobStore, get_blob_store, source_key
from app.catalog.service import upsert_catalog_row
from app.config import get_settings
from app.ingest.prompts import INGEST_SYSTEM_PROMPT, build_user_prompt
from app.ingest.validation import parse_catalog_rows
from app.llm import LLMClient, approx_tokens, build_llm_client
from app.models import LlmCall, SourceDocument
from app.observability import LLMObservation
from app.observability.metrics import record_llm_call
from app.schemas.ingest import IngestRequest, IngestResult


def build_ingest_client() -> LLMClient:
    """The in-cluster LLM client for ingestion, per config (fake offline; Bedrock Nova
    via IRSA in-cluster). A FastAPI dependency so tests can override it with a fake."""
    s = get_settings()
    return build_llm_client(s.llm_client, model=s.bedrock_model_id, region=s.aws_region)


def _normalize(source_text: str) -> bytes:
    """Canonical bytes for hashing/storage: normalize line endings + trim edges so
    trivially-different copies of the same source still hash equal (true idempotency)."""
    return source_text.replace("\r\n", "\n").replace("\r", "\n").strip().encode("utf-8")


def ingest_source(
    db: Session,
    request: IngestRequest,
    client: LLMClient,
    *,
    hourly_cap: int | None = None,
    max_tokens: int | None = None,
    blob: BlobStore | None = None,
) -> IngestResult:
    """Ingest one source. Raises llm_budget.HourlyTokenCapExceeded if the hourly cap
    would be busted (caller maps it to 429). Re-ingesting unchanged bytes is a no-op."""
    settings = get_settings()
    cap = hourly_cap if hourly_cap is not None else settings.llm_hourly_token_cap
    out_cap = max_tokens if max_tokens is not None else settings.ingest_max_tokens
    blob = blob if blob is not None else get_blob_store()

    source_bytes = _normalize(request.source_text)
    content_hash = hashlib.sha256(source_bytes).hexdigest()

    # Idempotency: an unchanged source short-circuits BEFORE any reservation/LLM call.
    existing = db.scalar(
        select(SourceDocument).where(SourceDocument.content_hash == content_hash)
    )
    if existing is not None:
        return IngestResult(source_document_id=existing.id, status="skipped")

    system = INGEST_SYSTEM_PROMPT
    user = build_user_prompt(request.source_text)
    # The in-cluster provider + intended model (used as the metric/log label on the
    # failure paths, where there is no provider response to read the model from).
    provider = settings.llm_client
    intended_model = settings.bedrock_model_id

    # Worst case = prompt in + full output budget. Reserve it before spending a token.
    estimate = approx_tokens(system) + approx_tokens(user) + out_cap
    try:
        reservation = llm_budget.reserve_tokens(db, estimate, cap)
    except llm_budget.HourlyTokenCapExceeded:
        # Over the hourly cap → aborted before any provider call. Record it as throttled
        # (status only — the exception message carries token numbers we never log).
        record_llm_call(
            LLMObservation(
                purpose="ingestion",
                provider=provider,
                model=intended_model,
                tokens_in=0,
                tokens_out=0,
                status="throttled",
                error_kind="HourlyTokenCapExceeded",
                prompt=request.source_text,
            )
        )
        raise

    try:
        started = time.perf_counter()
        resp = client.complete(system, user, out_cap)
        latency_ms = int((time.perf_counter() - started) * 1000)
    except Exception as exc:
        # A failed call must not burn the hour's budget — release the reservation.
        llm_budget.release(db, reservation)
        # Record the failure: the exception CLASS only, never its message (it may carry
        # provider error text / prompt fragments).
        record_llm_call(
            LLMObservation(
                purpose="ingestion",
                provider=provider,
                model=intended_model,
                tokens_in=0,
                tokens_out=0,
                status="error",
                error_kind=type(exc).__name__,
                prompt=request.source_text,
            )
        )
        raise

    used = resp.tokens_in + resp.tokens_out
    llm_budget.reconcile(db, reservation, used)

    # Per-request LLM log line + token metrics (S16). The unstructured source appears in
    # the log only as derived metadata (size / hash / redaction count), never raw;
    # ingestion retrieves nothing, so retrieved_context_size is None.
    record_llm_call(
        LLMObservation(
            purpose="ingestion",
            provider=provider,
            model=resp.model,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            latency_ms=latency_ms,
            retrieved_context_size=None,
            status="ok",
            prompt=request.source_text,
        )
    )

    # One llm_call row per real extraction (purpose=ingestion, ci_run_id null). model_id
    # stays null: Nova's model string isn't a catalog model row (S16 logs the string).
    db.add(
        LlmCall(
            ci_run_id=None,
            purpose="ingestion",
            model_id=None,
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            latency_ms=latency_ms,
            status="ok",
        )
    )
    db.commit()

    valid_rows, rejections = parse_catalog_rows(resp.text)
    status = "ingested" if valid_rows else "invalid"

    # Source bytes land in blob storage (S3 in-cluster); we keep only the key.
    s3_key = blob.put(source_key(content_hash), source_bytes)
    source_doc = SourceDocument(
        kind=request.kind,
        uri=request.uri,
        s3_key=s3_key,
        content_hash=content_hash,
        fetched_at=datetime.now(timezone.utc),
        status=status,
    )
    db.add(source_doc)
    db.flush()  # assign id for the row FKs

    for row in valid_rows:
        upsert_catalog_row(db, row, source_document_id=source_doc.id)
    db.commit()

    return IngestResult(
        source_document_id=source_doc.id,
        status=status,
        rows_created=len(valid_rows),
        rows_rejected=len(rejections),
        rejections=rejections,
        tokens_in=resp.tokens_in,
        tokens_out=resp.tokens_out,
    )
