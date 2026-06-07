"""Per-project CI ingest token: mint, hash, verify (S11).

The agent runs in the *user's* Jenkins and POSTs results back authed by this token
— NOT a user JWT (it carries no user session). We store only the token's SHA-256
hash (a random 256-bit token needs no slow KDF; argon2 is for low-entropy
passwords), never the plaintext, and compare in constant time.

Standalone (stdlib only) so both the auth dependency and the CI service can import
it without an import cycle through app.auth / app.ci.service.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def mint_token() -> str:
    """A fresh, URL-safe, high-entropy token (~256 bits). Shown to the user once."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex digest (64 chars) — what we persist instead of the plaintext."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    """Constant-time check of a presented token against a stored hash."""
    return hmac.compare_digest(hash_token(token), token_hash)
