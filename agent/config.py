"""Agent configuration — the agent's OWN env, decoupled from the backend Settings.

The agent ships as a separate image and must not require the backend's JWT/DB
config. It reads only what a CI run needs: which task, which provider/model (BYOK),
the per-run ceilings, the gate policy, and — since 1.1.0 — how to reach the
ModelMatch API for its run-time config (HLD §3b.1).

Deliberately NO `.env` file: the review stage runs the image with the user's
checkout as the working directory, and a user repo's own `.env` must never be able
to reconfigure the agent (or reach its provider).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Task = Literal["review", "security"]

# Per-task defaults. Security's ceiling is high because an agentic loop reads a lot
# (a measured DeepSeek run: ~4k input + ~2.6k output + ~88k cache-read per audit).
DEFAULT_TOKEN_CEILING = {"review": 100_000, "security": 1_000_000}
DEFAULT_FAIL_SEVERITIES = {"review": ["high", "critical"], "security": ["critical"]}


class AgentConfig(BaseSettings):
    # populate_by_name so the fields are still constructable by name in code/tests
    # even though each carries an env alias.
    model_config = SettingsConfigDict(
        extra="ignore", protected_namespaces=(), populate_by_name=True
    )

    # ---- task + run-time config from the API (HLD §3b.1) ----
    # review | security. Env fallback ONLY: when the three MODELMATCH_* below are
    # set, the API's answer wins (the snippet stays task-agnostic).
    task: Task = Field(
        default="review", validation_alias=AliasChoices("MODELMATCH_TASK", "AGENT_TASK")
    )
    api_url: str | None = Field(default=None, validation_alias="MODELMATCH_API_URL")
    project_id: int | None = Field(default=None, validation_alias="MODELMATCH_PROJECT_ID")
    # The per-project CI ingest token. SecretStr so it can never be repr'd/logged.
    ci_token: SecretStr | None = Field(default=None, validation_alias="MODELMATCH_CI_TOKEN")
    # Let the agent POST /ci-runs itself (needs the trio above + a build id).
    post_result: bool = Field(default=False, validation_alias="MODELMATCH_POST_RESULT")
    # Jenkins sets BUILD_TAG (jenkins-<job>-<n>); pass it through with `-e BUILD_TAG`.
    build_id: str | None = Field(
        default=None, validation_alias=AliasChoices("MODELMATCH_BUILD_ID", "BUILD_TAG")
    )
    http_timeout: int = Field(
        default=15, ge=1, validation_alias="MODELMATCH_HTTP_TIMEOUT"
    )
    # Local/offline fallback for the per-project review preferences.
    review_preferences: str | None = Field(
        default=None, validation_alias="MODELMATCH_REVIEW_PREFERENCES"
    )

    # ---- review mode: provider seam (BYOK) fake | anthropic | gemini | bedrock ----
    # Creds are read by each SDK directly (ANTHROPIC_API_KEY / GOOGLE_API_KEY /
    # AWS-IRSA) — never stored.
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
    # Model id to call + report. Review: the SDK's id (claude-haiku-4-5). Security
    # (env fallback only): the full OpenCode string (deepseek/deepseek-v4-flash).
    model_id: str = Field(
        default="fake-model",
        validation_alias=AliasChoices("AGENT_MODEL", "LLM_MODEL_ID", "MODEL_ID"),
    )
    max_tokens: int = Field(
        default=1024, ge=1, validation_alias=AliasChoices("AGENT_MAX_TOKENS", "MAX_TOKENS")
    )
    max_iterations: int = Field(
        default=1, ge=1,
        validation_alias=AliasChoices("AGENT_MAX_ITERATIONS", "MAX_ITERATIONS"),
    )

    # ---- ceilings (both modes; all ABORT, none is an alert) ----
    # None → the per-task default (see effective_token_ceiling).
    token_ceiling: int | None = Field(
        default=None, ge=1,
        validation_alias=AliasChoices("AGENT_TOKEN_CEILING", "TOKEN_CEILING"),
    )
    max_steps: int = Field(default=40, ge=1, validation_alias="AGENT_MAX_STEPS")
    max_seconds: int = Field(default=600, ge=1, validation_alias="AGENT_MAX_SECONDS")
    # Retries refusal AND unparseable output. A default of the IMAGE, not the
    # Jenkinsfile — the generated snippet is pasted verbatim. Do not regress to 1.
    max_attempts: int = Field(default=3, ge=1, validation_alias="AGENT_MAX_ATTEMPTS")

    # ---- security mode: the OpenCode loop ----
    workspace: str = Field(default="/workspace", validation_alias="AGENT_WORKSPACE")
    opencode_bin: str = Field(default="opencode", validation_alias="AGENT_OPENCODE_BIN")
    # None → the bundled RealVuln auditor prompt (agent/prompts/security-auditor.txt).
    prompt_file: str | None = Field(default=None, validation_alias="AGENT_PROMPT_FILE")

    # ---- gate policy: None → per-task default (see effective_fail_severities) ----
    # NoDecode so a CSV env value reaches the validator as a raw string (not JSON).
    fail_severities: Annotated[list[str] | None, NoDecode] = Field(
        default=None,
        validation_alias=AliasChoices("AGENT_FAIL_SEVERITIES", "FAIL_SEVERITIES"),
    )

    @field_validator("fail_severities", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [s.strip().lower() for s in v.split(",") if s.strip()]
        return v

    @field_validator("api_url", mode="before")
    @classmethod
    def _strip_url(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip().rstrip("/")
            return v or None
        return v

    # ---- derived ----
    @property
    def remote_configured(self) -> bool:
        """All three of URL / project / token → fetch the config from the API."""
        return bool(self.api_url and self.project_id is not None and self.ci_token)

    def effective_token_ceiling(self, task: str) -> int:
        return self.token_ceiling if self.token_ceiling is not None else DEFAULT_TOKEN_CEILING[task]

    def effective_fail_severities(self, task: str) -> list[str]:
        if self.fail_severities is not None:
            return self.fail_severities
        return list(DEFAULT_FAIL_SEVERITIES[task])
