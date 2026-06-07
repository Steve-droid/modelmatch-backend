"""Gated live LLM smoke — deliberately hits real models, so it's OFF by default.

Run with real keys to validate the adapters end-to-end:
    RUN_LLM_LIVE=1 ANTHROPIC_API_KEY=... uv run pytest tests/test_llm_live.py
Each test also skips if its SDK or key is missing. Costs a few tokens — mock-first
everywhere else; this is the only path that spends.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LLM_LIVE") != "1",
    reason="set RUN_LLM_LIVE=1 to hit real models (spends tokens)",
)


def test_anthropic_live():
    pytest.importorskip("anthropic")
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    from app.llm import build_llm_client

    client = build_llm_client(
        "anthropic", model=os.getenv("LIVE_ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
    )
    resp = client.complete("You are terse.", "Reply with the single word: ok", 16)
    assert resp.text.strip()
    assert resp.tokens_out > 0


def test_gemini_live():
    pytest.importorskip("google.genai")
    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        pytest.skip("GEMINI_API_KEY / GOOGLE_API_KEY not set")
    from app.llm import build_llm_client

    client = build_llm_client(
        "gemini", model=os.getenv("LIVE_GEMINI_MODEL", "gemini-2.0-flash")
    )
    resp = client.complete("You are terse.", "Reply with the single word: ok", 16)
    assert resp.text.strip()
