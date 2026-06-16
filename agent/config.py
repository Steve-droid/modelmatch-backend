"""Agent configuration — the agent's OWN env, decoupled from the backend Settings.

The agent ships as a separate image and must not require the backend's JWT/DB
config. It reads only what a CI review needs: which provider/model (BYOK), the
per-run cost ceilings, and the gate policy.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AgentConfig(BaseSettings):
    # populate_by_name so the fields are still constructable by name in code/tests
    # even though each carries an env alias.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", protected_namespaces=(), populate_by_name=True
    )

    # Provider seam (BYOK): fake | anthropic | gemini | bedrock. Creds are read by
    # each SDK directly (ANTHROPIC_API_KEY / GOOGLE_API_KEY / AWS-IRSA) — never stored.
    llm_client: str = Field(
        default="fake", validation_alias=AliasChoices("LLM_CLIENT", "AGENT_LLM_CLIENT")
    )
    # Bedrock region. The generated Jenkins snippet exports the standard AWS env names
    # (`AWS_DEFAULT_REGION` + `AWS_REGION`), but allow an explicit AGENT_AWS_REGION
    # override for local/manual runs too.
    aws_region: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AGENT_AWS_REGION", "AWS_DEFAULT_REGION", "AWS_REGION"
        ),
    )
    # Model id to call + report (e.g. claude-haiku-4 for the demo live run).
    model_id: str = Field(
        default="fake-model",
        validation_alias=AliasChoices("AGENT_MODEL", "LLM_MODEL_ID", "MODEL_ID"),
    )

    # Per-run cost guards (the agentic ceiling on the user's key). Env names match
    # the README (AGENT_*), with the bare names accepted as fallbacks.
    max_tokens: int = Field(
        default=1024, ge=1, validation_alias=AliasChoices("AGENT_MAX_TOKENS", "MAX_TOKENS")
    )
    token_ceiling: int = Field(
        default=100_000, ge=1,
        validation_alias=AliasChoices("AGENT_TOKEN_CEILING", "TOKEN_CEILING"),
    )
    max_iterations: int = Field(
        default=1, ge=1,
        validation_alias=AliasChoices("AGENT_MAX_ITERATIONS", "MAX_ITERATIONS"),
    )

    # Gate policy: fail the build if any finding's severity is in this set.
    # NoDecode so a CSV env value reaches the validator as a raw string (not JSON).
    fail_severities: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["high", "critical"],
        validation_alias=AliasChoices("AGENT_FAIL_SEVERITIES", "FAIL_SEVERITIES"),
    )

    @field_validator("fail_severities", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip().lower() for s in v.split(",") if s.strip()]
        return v
