"""Application settings, loaded from environment (pydantic-settings).

Only the S1 (platform-shell) settings live here. Later stories add JWT, baseline/
quality knobs, AWS/Bedrock/S3, and the LLM seam — see .env.example for the full set.
"""

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "ModelMatch backend"
    database_url: str = (
        "postgresql+psycopg://modelmatch:modelmatch@localhost:5432/modelmatch"
    )
    cors_allow_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

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
