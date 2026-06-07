"""LLMClient factory — pick a client by name, config-light by design.

Takes the provider name explicitly (the backend passes its setting; the agent
passes its own AgentConfig), so this never imports the backend Settings — keeping
the agent a standalone image. Only `fake` exists in v0.10.0; the real
Bedrock/Anthropic/Gemini adapters register here in v0.10.1.
"""

from __future__ import annotations

from app.llm.base import LLMClient
from app.llm.fake import FakeLLMClient

_SUPPORTED = {"fake"}


def build_llm_client(name: str, *, model: str = "fake-model", **kwargs) -> LLMClient:
    if name == "fake":
        return FakeLLMClient(model=model)
    raise ValueError(
        f"Unsupported LLM_CLIENT={name!r} (supported: {sorted(_SUPPORTED)}). "
        "Real provider adapters (anthropic/gemini/bedrock) arrive in v0.10.1."
    )
