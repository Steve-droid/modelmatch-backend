"""LLM seam package: the provider-agnostic client interface + fake + real adapters."""

from app.llm.anthropic_client import AnthropicClient
from app.llm.base import LLMClient, LLMResponse, approx_tokens
from app.llm.bedrock_client import BedrockClient
from app.llm.factory import build_llm_client
from app.llm.fake import EMPTY_FINDINGS, FakeLLMClient
from app.llm.gemini_client import GeminiClient

__all__ = [
    "LLMClient",
    "LLMResponse",
    "approx_tokens",
    "FakeLLMClient",
    "EMPTY_FINDINGS",
    "AnthropicClient",
    "GeminiClient",
    "BedrockClient",
    "build_llm_client",
]
