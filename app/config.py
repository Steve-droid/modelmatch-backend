"""Application settings, loaded from environment (pydantic-settings).

Only the S1 (platform-shell) settings live here. Later stories add JWT, baseline/
quality knobs, AWS/Bedrock/S3, and the LLM seam — see .env.example for the full set.
"""

import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Values that must never be accepted as a real JWT signing key.
_PLACEHOLDER_SECRETS = {
    "change-me-in-env",
    "changeme",
    "secret",
    "dev-only-insecure-secret-change-me",
}

# Chat read-only role password values that are fine for dev/test (fake LLM) but must
# NOT be used once the in-cluster LLM is real (Bedrock) — the role gates DB access.
_PLACEHOLDER_CHAT_PASSWORDS = {
    "modelmatch_chat_ro",  # the dev/test default
    "change-me-in-env",
    "changeme",
    "secret",
    "password",
}

# A safe SQL identifier (the role name is interpolated into role-management DDL in the
# chat-role migration, so it must not be attacker-controllable text).
_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "ModelMatch backend"
    database_url: str = (
        "postgresql+psycopg://modelmatch:modelmatch@localhost:5432/modelmatch"
    )
    cors_allow_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    # JWT auth (S4). JWT_SECRET is REQUIRED and has NO default — a known signing
    # key is a security footgun (the app could boot in a real env with a guessable
    # secret), so we fail fast instead. argon2 hashes passwords; HS256 signs
    # short-lived access tokens. Accept either env spelling for the TTL.
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = Field(
        default=60,
        validation_alias=AliasChoices("JWT_EXPIRES_MINUTES", "JWT_EXPIRE_MINUTES"),
    )

    # Deterministic recommender (S6). The quality↔cost slider's w_q is preset by
    # budget_sensitivity; w_c = 1 − w_q. low → quality-leaning, high → cost-leaning.
    # All env-tunable, no hardcoding in the formula.
    # w_q must be a valid weight (0..1) so w_c = 1 − w_q is also valid.
    rank_weight_low: float = Field(default=0.85, ge=0.0, le=1.0)
    rank_weight_medium: float = Field(default=0.60, ge=0.0, le=1.0)
    rank_weight_high: float = Field(default=0.40, ge=0.0, le=1.0)
    # The "expensive default" savings are measured against. A model NAME (matched
    # within the ranked comparability group), not a DB id — ids differ per fresh
    # DB. Falls back to the highest-cost model in the group if absent. Demo = Sonnet.
    baseline_model_id: str = "Claude Sonnet 4.5"
    # ≥ 1 so suggested = shortlist[0] can never index an empty list.
    recommendation_shortlist_size: int = Field(default=3, ge=1)

    # Quality gate (S13, OQ1). A CI run's findings get accept/reject feedback; the
    # per-run acceptance rate (accepted / rated) must be ≥ this threshold for the run
    # to count toward the honest cumulative savings. Env-driven, never hardcoded in
    # the gate logic. 0..1; default 0.8.
    quality_threshold: float = Field(default=0.8, ge=0.0, le=1.0)

    # Secret-store backend for Jenkins/BYOK refs (S9). Only `fake` (in-memory,
    # dev/tests) is implemented today; a Literal so an unsupported value (e.g. aws)
    # fails at config-load, not mid-request. The aws (Secrets Manager via IRSA)
    # adapter is added with its implementation.
    secret_store: Literal["fake"] = "fake"

    # CI integration (S11). The ci-setup snippet embeds the backend's public base
    # URL (where the user's Jenkins POSTs results) and the agent image ref to pull.
    # Env-driven, no hardcoding; real values land with the infra/GitOps stories.
    public_base_url: str = "http://localhost:8000"
    agent_image: str = "modelmatch-agent:latest"
    aws_region: str = "ap-south-1"  # used by the Bedrock variant of the snippet

    # CI runtime caps baked into the generated Jenkins snippet. The provider/model
    # come from the selected model's agent_runtime_config row, not from env.
    ci_agent_max_tokens: int = Field(default=1024, ge=1)
    ci_agent_token_ceiling: int = Field(default=20_000, ge=1)

    # In-cluster LLM (S5b ingestion #3 + later chat #4) — the "two-surface" rule:
    # this is OUR account's Bedrock Nova via IRSA (no static keys), distinct from
    # the user-side BYOK CI agent. `fake` is the offline default (tests + dev); a
    # real run sets LLM_CLIENT=bedrock. The model id is deliberate (see S11 smoke:
    # Nova-1 Lite is fine on-demand; Nova-2 Lite needs its inference-profile id).
    llm_client: str = "fake"  # fake | bedrock (anthropic/gemini are BYOK-only)
    # Nova Lite via its APAC cross-region inference profile. In ap-south-1 the BARE id
    # (amazon.nova-lite-v1:0) is rejected on-demand ("Invocation … with on-demand
    # throughput isn't supported"); the inference profile is required (verified live,
    # S5b 2026-06-07). Other regions use their own prefix (us./eu.).
    bedrock_model_id: str = "apac.amazon.nova-lite-v1:0"  # in-cluster ingestion + chat
    s3_bucket: str = "modelmatch-ingestion-sources"  # ingestion source docs land here

    # Blob store for ingestion source bytes (S5b). Only `fake` (in-process, dev/tests)
    # is implemented today; the S3/IRSA adapter lands with the infra story. A Literal
    # so an unsupported value fails at config-load, not mid-request — mirrors SECRET_STORE.
    blob_store: Literal["fake"] = "fake"

    # Hard hourly token cap for OUR in-cluster Nova (Roey 2026-06-04): a real ceiling
    # that ABORTS (429), not just an alert. Counted via the Postgres `llm_usage` tally
    # (shared across replicas). Ingestion reserves its worst-case estimate before the
    # call and reconciles to actual after. Per-call output cap for one extraction.
    llm_hourly_token_cap: int = Field(default=200_000, ge=1)
    ingest_max_tokens: int = Field(default=2048, ge=1)

    # Grounded Q&A chat (S14b, #4). The chat's LLM-generated catalog SQL runs on a
    # SEPARATE connection authenticating as a restricted read-only role that can
    # SELECT only the curated `chat_catalog` view — the DB-level half of the safety
    # story behind the app-level SELECT-only gate (it cannot write, and cannot read
    # `user`/`jenkins_connection` secret refs even if the gate is bypassed). The role
    # + view are provisioned by a migration; the password is env-driven (dev/test
    # default mirrors the inline creds in `database_url`, never a real secret).
    chat_readonly_db_user: str = "modelmatch_chat_ro"
    chat_readonly_db_password: str = "modelmatch_chat_ro"
    chat_max_tokens: int = Field(default=1024, ge=1)  # per LLM call (SQL-gen / answer)
    chat_max_rows: int = Field(default=200, ge=1)      # LIMIT injected into catalog SQL
    chat_retry_max: int = Field(default=1, ge=0)       # DB-error retries for SQL-gen
    # Expose the answer's debug block (generated SQL, attempts, token counts) on the
    # chat response. OFF by default — raw SQL/internals are NOT shown to normal users
    # (there is no admin role yet, S4). Turn on only in dev/test (CHAT_DEBUG_ENABLED=
    # true). Even when on, debug carries no secrets/refs/raw DB errors (see api/chat).
    chat_debug_enabled: bool = False

    # Demo data auto-seed (P30). The catalog always seeds (idempotent); the demo
    # DATASET — a demo user + project + a spread of CI runs — seeds only when
    # DEMO_SEED is true, via a gated ArgoCD PostSync hook in demo/portfolio envs.
    # Deterministic, zero tokens. The password is the demo user's LOGIN credential,
    # so in-cluster it arrives via ESO (Secrets Manager), never Git. Backend pods
    # leave DEMO_SEED off, so these are inert there.
    demo_seed: bool = False
    demo_seed_email: str | None = None
    demo_seed_password: str | None = None
    demo_seed_project: str = "demo-api"
    demo_seed_run_count: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _require_demo_creds_when_seeding(self) -> "Settings":
        # If the demo seed is enabled, the user creds it creates must be present —
        # fail fast in the seed Job rather than half-create a login-less user.
        if self.demo_seed and not (self.demo_seed_email and self.demo_seed_password):
            raise ValueError(
                "DEMO_SEED is on but DEMO_SEED_EMAIL / DEMO_SEED_PASSWORD are unset."
            )
        return self

    @property
    def chat_database_url(self) -> str:
        """The read-only chat DSN: the app database, but authenticating as the
        restricted `chat_readonly_db_user` role (same host/port/db as `database_url`,
        creds swapped). Derived so it can never drift to a different database."""
        from sqlalchemy.engine import make_url

        return (
            make_url(self.database_url)
            .set(
                username=self.chat_readonly_db_user,
                password=self.chat_readonly_db_password,
            )
            .render_as_string(hide_password=False)
        )

    @field_validator("llm_client")
    @classmethod
    def _validate_incluster_provider(cls, v: str) -> str:
        # The in-cluster surface is OUR account: only `fake` (offline) or `bedrock`
        # (Nova via IRSA). anthropic/gemini are BYOK — they belong to the agent, not
        # here — so reject them at config-load, enforcing the two-surface rule.
        allowed = {"fake", "bedrock"}
        norm = v.strip().lower()
        if norm not in allowed:
            raise ValueError(
                f"LLM_CLIENT must be one of {sorted(allowed)} for the in-cluster "
                f"surface (got {v!r}); anthropic/gemini are BYOK (agent-only)."
            )
        return norm

    @field_validator("chat_readonly_db_user")
    @classmethod
    def _validate_chat_role_identifier(cls, v: str) -> str:
        # The chat-role migration interpolates this into CREATE/ALTER/GRANT ROLE DDL,
        # so it must be a plain SQL identifier (no quotes, spaces, or injection text).
        if not _SQL_IDENTIFIER.fullmatch(v) or len(v) > 63:
            raise ValueError(
                "CHAT_READONLY_DB_USER must be a safe SQL identifier "
                "(letter/underscore start, then letters/digits/underscore, ≤ 63 chars)."
            )
        return v

    @model_validator(mode="after")
    def _require_real_chat_password_when_not_fake(self) -> "Settings":
        # Dev/test runs on the fake in-cluster client and may keep the default role
        # password. Once the in-cluster LLM is real (Bedrock), the read-only role is a
        # live DB credential — a default/placeholder password is rejected, like JWT.
        if (
            self.llm_client != "fake"
            and self.chat_readonly_db_password in _PLACEHOLDER_CHAT_PASSWORDS
        ):
            raise ValueError(
                "CHAT_READONLY_DB_PASSWORD must be a real, non-default value when "
                "LLM_CLIENT is not 'fake' (the read-only role is a live DB credential)."
            )
        return self

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder_secret(cls, v: str) -> str:
        if not v or not v.strip() or v.strip().lower() in _PLACEHOLDER_SECRETS:
            raise ValueError(
                "JWT_SECRET must be set to a real, non-placeholder value "
                "(see .env.example) — there is no built-in default."
            )
        return v

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # Accept a comma-separated string from the environment, not just JSON.
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
