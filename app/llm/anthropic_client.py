"""Anthropic adapter (BYOK) — the agent's demo provider (Haiku live, Sonnet computed).

Creds come from the SDK's default (ANTHROPIC_API_KEY) unless an explicit api_key is
passed; we never store or log the key. The SDK is imported lazily so the core stays
SDK-free, and tests inject a fake `client` instead of installing the SDK.
"""

from __future__ import annotations

from typing import Any, Optional

from app.llm.base import LLMResponse


class AnthropicClient:
    def __init__(
        self, model: str, *, api_key: Optional[str] = None, client: Any = None
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - exercised in the no-SDK path
                raise RuntimeError(
                    "anthropic SDK not installed — install the 'llm' extra "
                    "(uv sync --extra llm)"
                ) from exc
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def complete(self, system: str, user: str, max_tokens: int) -> LLMResponse:
        resp = self._ensure_client().messages.create(
            model=self._model,
            system=system,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(getattr(block, "text", "") for block in resp.content)
        return LLMResponse(
            text=text,
            tokens_in=resp.usage.input_tokens,
            tokens_out=resp.usage.output_tokens,
            model=self._model,
        )
