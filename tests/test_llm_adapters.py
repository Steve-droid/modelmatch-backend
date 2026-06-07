"""S10b adapter tests — offline, no SDK required.

Each provider adapter is exercised with an INJECTED fake that mimics its SDK's
response shape, proving the request build + response mapping (text, token usage,
model). The factory dispatches by name without importing any SDK, and a missing SDK
raises a clear error. Real models are only hit by the gated test_llm_live.py.
"""

import importlib.util
from types import SimpleNamespace

import pytest


def _installed(modname: str) -> bool:
    """True if an importable module/spec exists. Safe when a parent package (e.g.
    `google`) is absent — find_spec raises ModuleNotFoundError in that case."""
    try:
        return importlib.util.find_spec(modname) is not None
    except ModuleNotFoundError:
        return False

from app.llm import (
    AnthropicClient,
    BedrockClient,
    FakeLLMClient,
    GeminiClient,
    build_llm_client,
)


# --- fake SDK doubles (match each SDK's surface) -------------------------------

class FakeAnthropic:
    def __init__(self, text="review", tin=11, tout=7):
        self.calls = []
        resp = SimpleNamespace(
            content=[SimpleNamespace(text=text)],
            usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
        )
        self.messages = SimpleNamespace(create=self._make(resp))

    def _make(self, resp):
        def create(**kwargs):
            self.calls.append(kwargs)
            return resp
        return create


class FakeGenAIClient:
    """Mimics google-genai's Client: client.models.generate_content(model, contents, config)."""

    def __init__(self, text="g", tin=5, tout=3):
        self.calls = []
        resp = SimpleNamespace(
            text=text,
            usage_metadata=SimpleNamespace(prompt_token_count=tin, candidates_token_count=tout),
        )
        outer = self

        class _Models:
            def generate_content(self, model, contents, config=None):
                outer.calls.append({"model": model, "contents": contents, "config": config})
                return resp

        self.models = _Models()


class FakeBedrock:
    def __init__(self, text="b", tin=9, tout=2):
        self.calls = []
        self._resp = {
            "output": {"message": {"content": [{"text": text}]}},
            "usage": {"inputTokens": tin, "outputTokens": tout},
        }

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return self._resp


# --- response mapping ----------------------------------------------------------

def test_anthropic_maps_request_and_response():
    fake = FakeAnthropic(text="ok", tin=11, tout=7)
    r = AnthropicClient("claude-haiku-4", client=fake).complete("sys", "user", 128)
    assert (r.text, r.tokens_in, r.tokens_out, r.model) == ("ok", 11, 7, "claude-haiku-4")
    sent = fake.calls[0]
    assert sent["model"] == "claude-haiku-4" and sent["system"] == "sys" and sent["max_tokens"] == 128
    assert sent["messages"] == [{"role": "user", "content": "user"}]


def test_gemini_maps_request_and_response():
    fake = FakeGenAIClient(text="g", tin=5, tout=3)
    r = GeminiClient("gemini-2.5-flash", client=fake).complete("sys", "user", 64)
    assert (r.text, r.tokens_in, r.tokens_out, r.model) == ("g", 5, 3, "gemini-2.5-flash")
    sent = fake.calls[0]
    assert sent["model"] == "gemini-2.5-flash" and sent["contents"] == "user"
    assert sent["config"] == {"system_instruction": "sys", "max_output_tokens": 64}


def test_bedrock_maps_request_and_response():
    fake = FakeBedrock(text="b", tin=9, tout=2)
    r = BedrockClient("amazon.nova-lite-v1:0", client=fake).complete("sys", "user", 32)
    assert (r.text, r.tokens_in, r.tokens_out, r.model) == ("b", 9, 2, "amazon.nova-lite-v1:0")
    sent = fake.calls[0]
    assert sent["modelId"] == "amazon.nova-lite-v1:0"
    assert sent["system"] == [{"text": "sys"}]
    assert sent["messages"] == [{"role": "user", "content": [{"text": "user"}]}]
    assert sent["inferenceConfig"] == {"maxTokens": 32}


# --- factory dispatch (no SDK needed to construct) -----------------------------

@pytest.mark.parametrize("name,cls", [
    ("fake", FakeLLMClient),
    ("anthropic", AnthropicClient),
    ("gemini", GeminiClient),
    ("bedrock", BedrockClient),
])
def test_factory_dispatches_by_name(name, cls):
    assert isinstance(build_llm_client(name, model="m"), cls)


# --- missing-SDK paths (only meaningful when the SDK is absent) -----------------

@pytest.mark.skipif(_installed("anthropic"), reason="anthropic installed")
def test_anthropic_missing_sdk_raises_runtimeerror():
    with pytest.raises(RuntimeError):
        AnthropicClient("m").complete("s", "u", 8)


@pytest.mark.skipif(_installed("google.genai"), reason="genai installed")
def test_gemini_missing_sdk_raises_runtimeerror():
    with pytest.raises(RuntimeError):
        GeminiClient("m").complete("s", "u", 8)


@pytest.mark.skipif(_installed("boto3"), reason="boto3 installed")
def test_bedrock_missing_sdk_raises_runtimeerror():
    with pytest.raises(RuntimeError):
        BedrockClient("m").complete("s", "u", 8)


# --- the agent runs on a real-adapter-shaped client ----------------------------

def test_agent_review_runs_through_anthropic_adapter():
    from agent.config import AgentConfig
    from agent.review import review

    findings = '{"findings":[{"severity":"high","category":"security","file":"x.py","line":1,"message":"bad"}]}'
    client = AnthropicClient("claude-haiku-4", client=FakeAnthropic(text=findings, tin=20, tout=12))
    res = review("diff", client, AgentConfig(llm_client="anthropic", _env_file=None))
    assert res.model == "claude-haiku-4"
    assert res.gate == "fail" and len(res.findings) == 1
    assert res.tokens_in == 20 and res.tokens_out == 12
