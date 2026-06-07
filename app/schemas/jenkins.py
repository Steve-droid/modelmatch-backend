"""Jenkins connection schemas (S9). camelCase out.

The request carries the *plaintext* Jenkins API token + BYOK model key as
`SecretStr` — so they're masked in any repr/log/traceback — and the service hands
them to the SecretStore, persisting only the returned refs. The response never
echoes the plaintext: it exposes the refs + status only.
"""

from pydantic import AnyHttpUrl, Field, SecretStr, TypeAdapter, ValidationError, field_validator

from app.schemas.base import CamelModel

# Validate base_url is a real http(s) URL, but keep the user's exact string (no
# trailing-slash normalization) so it concatenates cleanly with paths later (S11).
_HTTP_URL = TypeAdapter(AnyHttpUrl)


class JenkinsConnectionUpdate(CamelModel):
    base_url: str = Field(max_length=1024)
    job_name: str = Field(min_length=1, max_length=255)
    jenkins_token: SecretStr = Field(min_length=1)  # Jenkins API token (plaintext in)
    model_api_key: SecretStr = Field(min_length=1)  # BYOK model key (plaintext in)

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
    jenkins_token_ref: str  # secret-store reference, NOT the token
    model_api_key_ref: str  # secret-store reference, NOT the key
