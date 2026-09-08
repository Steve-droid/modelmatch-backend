"""Provider name maps: the catalog's `agent_provider` enum → each runtime's vocabulary.

The runtime-config row stores `provider` + a BARE `provider_model_id`; the agent
composes what each runtime wants. Review mode goes through our own `LLMClient`
adapters (anthropic / gemini / bedrock only). Security mode goes through OpenCode,
which has its own provider ids and speaks DeepSeek/OpenAI natively.
"""

from __future__ import annotations

from agent.errors import AgentConfigError

# agent_provider enum value → OpenCode provider id (`opencode run -m <provider>/<id>`).
OPENCODE_PROVIDER = {
    "anthropic": "anthropic",
    "gemini": "google",
    "bedrock": "amazon-bedrock",
    "deepseek": "deepseek",
    "openai": "openai",
}

# agent_provider enum value → LLM_CLIENT name (review mode). deepseek/openai have no
# adapter and no CodeReviewBench row, so a review project on them is a config error.
REVIEW_LLM_CLIENT = {
    "anthropic": "anthropic",
    "gemini": "gemini",
    "bedrock": "bedrock",
}


def opencode_model(provider: str, provider_model_id: str) -> str:
    """`deepseek` + `deepseek-v4-flash` → `deepseek/deepseek-v4-flash`."""
    try:
        oc = OPENCODE_PROVIDER[provider]
    except KeyError:
        raise AgentConfigError(
            f"provider {provider!r} is not runnable for the security task "
            f"(known: {sorted(OPENCODE_PROVIDER)})"
        ) from None
    if not provider_model_id or "/" in provider_model_id:
        raise AgentConfigError(
            "providerModelId must be the BARE model id (no provider prefix)"
        )
    return f"{oc}/{provider_model_id}"


def review_llm_client(provider: str) -> str:
    try:
        return REVIEW_LLM_CLIENT[provider]
    except KeyError:
        raise AgentConfigError(
            f"provider {provider!r} is not runnable for the review task "
            f"(runnable: {sorted(REVIEW_LLM_CLIENT)})"
        ) from None
