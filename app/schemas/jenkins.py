"""Jenkins connection schemas (S9, metadata-only since S15c). camelCase out.

The connection is now **metadata only**: just the base URL + job name. The provider
key and the per-project CI token live in the *user's own Jenkins credentials*
(`modelmatch-model-api-key`, `modelmatch-ci-token`) — the backend never reads them,
so it no longer collects or stores them. The request therefore rejects any secret
(`extra="forbid"`), and the response carries no secret refs at all.
"""

from pydantic import (
    AnyHttpUrl,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from app.schemas.base import CamelModel

# Validate base_url is a real http(s) URL, but keep the user's exact string (no
# trailing-slash normalization) so it concatenates cleanly with paths later (S11).
_HTTP_URL = TypeAdapter(AnyHttpUrl)


class JenkinsConnectionUpdate(CamelModel):
    # Strictly metadata-only: a stray `jenkinsToken`/`modelApiKey` (or any other
    # field) is a 422 — the backend must never be handed a provider secret again.
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(max_length=1024)
    job_name: str = Field(min_length=1, max_length=255)

    @field_validator("base_url")
    @classmethod
    def _must_be_http_url(cls, v: str) -> str:
        try:
            _HTTP_URL.validate_python(v)
        except ValidationError:
            raise ValueError("baseUrl must be a valid http(s) URL")
        return v


class JenkinsConnectionOut(CamelModel):
    project_id: int
    base_url: str
    job_name: str
    status: str
