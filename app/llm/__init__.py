"""LLM seam package: the provider-agnostic client interface + fake + factory."""

from app.llm.base import LLMClient, LLMResponse, approx_tokens
from app.llm.factory import build_llm_client
from app.llm.fake import EMPTY_FINDINGS, FakeLLMClient

__all__ = [
    "LLMClient",
    "LLMResponse",
    "approx_tokens",
    "FakeLLMClient",
    "EMPTY_FINDINGS",
    "build_llm_client",
]
