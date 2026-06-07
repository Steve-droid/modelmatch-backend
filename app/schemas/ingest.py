"""Ingestion contract schemas (S5b): the request body + the result envelope.

`POST /benchmarks/ingest` takes an unstructured source (raw text + optional
metadata), the LLM extracts catalog rows, and we return a count-only result:
how many rows were created vs rejected, plus why each rejection happened and the
tokens the extraction spent. The per-row catalog shape stays `CatalogRowIn`
(app/schemas/catalog.py) — validated as UNTRUSTED in app/ingest/validation.py.
"""

from typing import Literal, Optional

from pydantic import Field

from app.schemas.base import CamelModel

# A source doc is text we hash + (in real runs) push to S3. Cap the body so an
# absurd payload is a clean 422, not an unbounded hash/store. Generous for real
# model cards / leaderboard dumps.
MAX_SOURCE_LEN = 200_000
MAX_KIND_LEN = 64
MAX_URI_LEN = 1024

IngestStatus = Literal["skipped", "ingested", "invalid"]


class IngestRequest(CamelModel):
    """The unstructured source to ingest. `kind`/`uri` are provenance metadata only —
    the backend does NOT fetch the uri in this slice (raw text is authoritative)."""

    source_text: str = Field(min_length=1, max_length=MAX_SOURCE_LEN)
    kind: Optional[str] = Field(default=None, max_length=MAX_KIND_LEN)
    uri: Optional[str] = Field(default=None, max_length=MAX_URI_LEN)


class IngestRejection(CamelModel):
    """One row the LLM produced that failed deterministic validation (dropped, not
    stored). `index` is its position in the model's output; `reason` is human-readable."""

    index: int
    reason: str


class IngestResult(CamelModel):
    """Count-only outcome of one ingest call (no row payloads echoed back).

    `status`: skipped (hash unchanged → zero LLM/tokens), ingested (≥1 valid row),
    or invalid (the source produced no valid rows). `tokensIn/Out` are 0 on a skip.
    """

    source_document_id: int
    status: IngestStatus
    rows_created: int = 0
    rows_rejected: int = 0
    rejections: list[IngestRejection] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
