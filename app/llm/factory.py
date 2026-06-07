"""LLMClient factory — pick a client by name, config-light by design.

Takes the provider name explicitly (the backend passes its setting; the agent
passes its own AgentConfig), so this never imports the backend Settings — keeping
the agent a standalone image. Adapter SDKs are imported lazily inside each client,
so constructing one here never requires the SDK to be installed (only a real
`complete()` call does).
"""

from __future__ import annotations

from typing import Any, Optional

from app.llm.anthropic_client import AnthropicClient
from app.llm.base import LLMClient
from app.llm.bedrock_client import BedrockClient
from app.llm.fake import FakeLLMClient
from app.llm.gemini_client import GeminiClient

_SUPPORTED = {"fake", "anthropic", "gemini", "bedrock"}


def build_llm_client(
    name: str,
    *,
    model: str = "fake-model",
    api_key: Optional[str] = None,
    region: Optional[str] = None,
    client: Any = None,
) -> LLMClient:
    """Build a client by provider name. `api_key`/`region`/`client` are optional —
    creds default to the SDK's own env/IRSA resolution; `client` injects a fake."""
    if name == "fake":
        return FakeLLMClient(model=model)
    if name == "anthropic":
        return AnthropicClient(model, api_key=api_key, client=client)
    if name == "gemini":
        return GeminiClient(model, api_key=api_key, client=client)
    if name == "bedrock":
        return BedrockClient(model, region=region, client=client)
    raise ValueError(
        f"Unsupported LLM_CLIENT={name!r} (supported: {sorted(_SUPPORTED)})."
    )
