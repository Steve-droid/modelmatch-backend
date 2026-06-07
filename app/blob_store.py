"""Blob-store seam (S5b): persist ingestion source bytes, keep only a key.

Ingestion sources land in blob storage (S3 in-cluster), NOT container disk — the
backend stores only the returned *key* on `source_document.s3_key` (the same
ref-not-payload pattern as app/secret_store.py). `fake` (in-process) is the
dev/test default; an S3 adapter (via IRSA) replaces it in-cluster
(BLOB_STORE=s3) with no call-site changes.

The key is derived from the content hash, so re-putting the same source is
idempotent and the key is stable/inspectable — it never encodes the bytes.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional, Protocol, runtime_checkable

from app.config import get_settings

# Stable, hash-addressed key layout (e.g. "sources/<sha256>"). Deterministic from
# the content hash → the same source always maps to the same key (idempotent put).
_KEY_PREFIX = "sources/"


def source_key(content_hash: str) -> str:
    """The blob key for a source document, addressed by its content hash."""
    return f"{_KEY_PREFIX}{content_hash}"


@runtime_checkable
class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> str:
        """Store `data` under `key`; return the key actually written (the s3_key)."""

    def get(self, key: str) -> Optional[bytes]:
        """Fetch bytes by key (None if absent)."""


class InMemoryBlobStore:
    """Dev/test store: keeps blobs in-process. NOT for production (no persistence) —
    the S3 adapter replaces it. Re-putting the same key overwrites → idempotent."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        self._data[key] = data
        return key

    def get(self, key: str) -> Optional[bytes]:
        return self._data.get(key)


@lru_cache
def get_blob_store() -> BlobStore:
    """Process-wide singleton, chosen by BLOB_STORE (default: fake)."""
    kind = get_settings().blob_store
    if kind == "fake":
        return InMemoryBlobStore()
    raise ValueError(f"Unsupported BLOB_STORE={kind!r} (supported: fake)")
