"""Secret-store seam (S9): write a secret, get back a reference.

The backend never persists Jenkins/BYOK plaintext — it hands the value to a
SecretStore and stores only the returned *ref* (the same pattern as the LLMClient
fake seam). `fake` (in-memory) is the dev/test default; an AWS Secrets Manager
adapter (via IRSA) replaces it in-cluster (SECRET_STORE=aws), with no call-site
changes. Module named secret_store (not `secrets`) to avoid shadowing the stdlib.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional, Protocol, runtime_checkable

from app.config import get_settings


@runtime_checkable
class SecretStore(Protocol):
    def put(self, name: str, value: str) -> str:
        """Store `value` under a logical `name`; return an opaque reference."""

    def get(self, ref: str) -> Optional[str]:
        """Resolve a reference back to its secret (None if unknown)."""


class InMemorySecretStore:
    """Dev/test store: keeps secrets in-process, returns a stable ref per name.

    NOT for production (no persistence, no encryption) — the AWS adapter replaces
    it. The ref encodes only the logical name, never the secret value.
    """

    _PREFIX = "local://secret/"

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def put(self, name: str, value: str) -> str:
        ref = f"{self._PREFIX}{name}"
        self._data[ref] = value  # re-putting the same name overwrites → idempotent
        return ref

    def get(self, ref: str) -> Optional[str]:
        return self._data.get(ref)


@lru_cache
def get_secret_store() -> SecretStore:
    """Process-wide singleton, chosen by SECRET_STORE (default: fake)."""
    kind = get_settings().secret_store
    if kind == "fake":
        return InMemorySecretStore()
    raise ValueError(f"Unsupported SECRET_STORE={kind!r} (supported: fake)")
