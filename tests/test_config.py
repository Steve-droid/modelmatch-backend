"""Config security contract: JWT_SECRET is required + non-placeholder, and the
token-TTL env var is accepted under either spelling.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_missing_jwt_secret_raises(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("placeholder", ["", "  ", "change-me-in-env", "CHANGEME", "secret"])
def test_placeholder_jwt_secret_rejected(monkeypatch, placeholder):
    monkeypatch.setenv("JWT_SECRET", placeholder)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_real_jwt_secret_accepted(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "a-real-strong-secret-value")
    assert Settings(_env_file=None).jwt_secret == "a-real-strong-secret-value"


@pytest.mark.parametrize("env_name", ["JWT_EXPIRES_MINUTES", "JWT_EXPIRE_MINUTES"])
def test_ttl_accepts_either_env_spelling(monkeypatch, env_name):
    monkeypatch.setenv("JWT_SECRET", "a-real-strong-secret-value")
    monkeypatch.delenv("JWT_EXPIRES_MINUTES", raising=False)
    monkeypatch.delenv("JWT_EXPIRE_MINUTES", raising=False)
    monkeypatch.setenv(env_name, "15")
    assert Settings(_env_file=None).jwt_expires_minutes == 15
