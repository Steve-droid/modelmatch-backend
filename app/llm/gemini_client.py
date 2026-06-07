"""Gemini adapter (BYOK) — the 3rd demo vendor (free-tier; non-confidential code only).

Uses the current `google-genai` SDK (Client-based). Creds via GEMINI_API_KEY /
GOOGLE_API_KEY (SDK default) unless an explicit api_key is passed. The SDK is
imported lazily — and `config` is passed as a plain dict — so tests inject a fake
client without the SDK installed. NOTE (HLD): the free tier trains on inputs +
allows human review — feed it only throwaway/demo code.
"""

from __future__ import annotations

from typing import Any, Optional

from app.llm.base import LLMResponse


class GeminiClient:
    def __init__(
        self, model: str, *, api_key: Optional[str] = None, client: Any = None
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._client = client  # google.genai.Client (real or fake)

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - no-SDK path
                raise RuntimeError(
                    "google-genai SDK not installed — install the 'llm' extra "
                    "(uv sync --extra llm)"
                ) from exc
            self._client = genai.Client(api_key=self._api_key) if self._api_key else genai.Client()
        return self._client

    def complete(self, system: str, user: str, max_tokens: int) -> LLMResponse:
        resp = self._ensure_client().models.generate_content(
            model=self._model,
            contents=user,
            # dict form (GenerateContentConfigOrDict) — avoids importing genai.types
            config={"system_instruction": system, "max_output_tokens": max_tokens},
        )
        usage = resp.usage_metadata
        return LLMResponse(
            text=resp.text,
            tokens_in=usage.prompt_token_count,
            tokens_out=usage.candidates_token_count,
            model=self._model,
        )
