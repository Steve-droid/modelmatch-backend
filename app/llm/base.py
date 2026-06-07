"""LLMClient seam — one interface for all three LLM uses (agent, ingestion, chat).

A provider-agnostic `complete()` plus a token-counted `LLMResponse`. Concrete
clients: FakeLLMClient (offline, deterministic — this slice) and the real
Bedrock/Anthropic/Gemini adapters (v0.10.1). Nothing here imports the backend's
Settings/DB, so the CI agent can use this seam as a standalone image.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    model: str


@runtime_checkable
class LLMClient(Protocol):
    """Provider-agnostic completion. Adapters map this to their SDK."""

    def complete(self, system: str, user: str, max_tokens: int) -> LLMResponse:
        ...


def approx_tokens(text: str) -> int:
    """Cheap, deterministic token estimate (~4 chars/token) for the fake client and
    for ceilings when a provider doesn't return usage. Never zero for non-empty text."""
    if not text:
        return 0
    return max(1, len(text) // 4)
