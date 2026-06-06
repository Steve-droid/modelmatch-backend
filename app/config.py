"""Application settings, loaded from environment (pydantic-settings).

Only the S1 (platform-shell) settings live here. Later stories add JWT, baseline/
quality knobs, AWS/Bedrock/S3, and the LLM seam — see .env.example for the full set.
"""

from functools import lru_cache
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Values that must never be accepted as a real JWT signing key.
_PLACEHOLDER_SECRETS = {
    "change-me-in-env",
    "changeme",
    "secret",
    "dev-only-insecure-secret-change-me",
}


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
    baseline_model_id: str = "Claude Sonnet 4.x"
    # ≥ 1 so suggested = shortlist[0] can never index an empty list.
    recommendation_shortlist_size: int = Field(default=3, ge=1)

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
