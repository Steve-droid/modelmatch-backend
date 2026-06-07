"""Bedrock adapter — the in-cluster surface (Nova) via IRSA, no static keys.

Uses the bedrock-runtime Converse API (uniform across Bedrock models, returns token
usage). boto3 resolves creds via its default chain — in-cluster that's IRSA, so we
hold no keys. SDK imported lazily; tests inject a fake boto3 client.
"""

from __future__ import annotations

from typing import Any, Optional

from app.llm.base import LLMResponse


class BedrockClient:
    def __init__(
        self, model: str, *, region: Optional[str] = None, client: Any = None
    ) -> None:
        self._model = model
        self._region = region
        self._client = client

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - no-SDK path
                raise RuntimeError(
                    "boto3 not installed — install the 'llm' extra (uv sync --extra llm)"
                ) from exc
            # creds via the default chain (IRSA in-cluster) — no static keys here.
            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def complete(self, system: str, user: str, max_tokens: int) -> LLMResponse:
        resp = self._ensure_client().converse(
            modelId=self._model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": max_tokens},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        usage = resp["usage"]
        return LLMResponse(
            text=text,
            tokens_in=usage["inputTokens"],
            tokens_out=usage["outputTokens"],
            model=self._model,
        )
