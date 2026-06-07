"""LLM seam tests: the fake client + factory (offline, deterministic)."""

import pytest

from app.llm import EMPTY_FINDINGS, FakeLLMClient, build_llm_client
from app.llm.base import approx_tokens


def test_fake_returns_text_and_token_counts():
    client = FakeLLMClient("hello world", model="m1")
    r = client.complete("system prompt", "user prompt", max_tokens=100)
    assert r.text == "hello world"
    assert r.model == "m1"
    assert r.tokens_out == approx_tokens("hello world")
    assert r.tokens_in == approx_tokens("system prompt") + approx_tokens("user prompt")


def test_fake_replays_scripted_sequence_then_repeats_last():
    client = FakeLLMClient(["a", "b"])
    assert client.complete("s", "u", 10).text == "a"
    assert client.complete("s", "u", 10).text == "b"
    assert client.complete("s", "u", 10).text == "b"  # last repeats


def test_default_fake_returns_empty_findings():
    assert FakeLLMClient().complete("s", "u", 10).text == EMPTY_FINDINGS


def test_factory_builds_fake():
    client = build_llm_client("fake", model="x")
    assert isinstance(client, FakeLLMClient)
    assert client.complete("s", "u", 10).model == "x"


@pytest.mark.parametrize("name", ["anthropic", "gemini", "bedrock", "nope"])
def test_factory_rejects_unimplemented_providers(name):
    with pytest.raises(ValueError):
        build_llm_client(name)
