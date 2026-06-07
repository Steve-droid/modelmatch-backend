"""FakeLLMClient — offline, deterministic, zero-cost (the module-required mock seam).

Returns scripted responses (no network), so every LLM use — agent, ingestion,
chat — is testable offline. Token counts are derived from text length, so they're
stable across runs. Real models only run in the gated `test-llm-live` job.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.llm.base import LLMResponse, approx_tokens

# A valid, empty findings payload — the default fake "review" finds nothing, so a
# bare `LLM_CLIENT=fake` agent run is a clean pass.
EMPTY_FINDINGS = '{"findings": []}'


class FakeLLMClient:
    """Replays canned text. Pass a single string (returned every call) or a list
    (consumed in order; the last item repeats once exhausted)."""

    def __init__(
        self, responses: str | Sequence[str] = EMPTY_FINDINGS, model: str = "fake-model"
    ) -> None:
        self._responses: list[str] = [responses] if isinstance(responses, str) else list(responses)
        if not self._responses:
            self._responses = [EMPTY_FINDINGS]
        self._model = model
        self._i = 0

    def complete(self, system: str, user: str, max_tokens: int) -> LLMResponse:
        text = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return LLMResponse(
            text=text,
            tokens_in=approx_tokens(system) + approx_tokens(user),
            tokens_out=approx_tokens(text),
            model=self._model,
        )
