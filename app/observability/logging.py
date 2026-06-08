"""Per-request LLM log line (S16) — one structured JSON line per LLM call.

Emitted for all three LLM uses (ingestion #3 · chat #4 · the CI agent) on success
AND on failure. It's what you grep when an answer is wrong: provider, model, status,
latency, tokens in/out, retrieved-context size, and **safe derived metadata** about
the input (char count, a short content hash, and how many secret/PII patterns were
scrubbed) — never the raw text.

Hard rules baked into `build_log_record`:
- **Never log raw prompts, source documents, questions, generated queries, diffs,
  secrets, secret refs, or token strings.** Inputs are reduced to derived metadata
  only (`*_chars`, `*_sha256`, `*_redactions`). The CI agent additionally passes NO
  input at all — the diff never even reaches this module.
- **Never log raw provider/DB error text** — only the exception's class name
  (`error_kind`), which carries no message, prompt, or secret.
- This module is **stdlib-only** — no Settings/DB import — so the standalone CI agent
  image can emit the same log line without dragging in the backend's config or database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("modelmatch.llm")

# Secret/PII patterns. We never log the matched text — these are used only to COUNT how
# many sensitive spans an input contained (a signal that something risky was passed),
# surfaced as `*_redactions`. Order is irrelevant for counting.
_REDACTIONS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),                       # Anthropic keys
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                       # generic provider keys
    re.compile(r"AKIA[0-9A-Z]{16}"),                             # AWS access key id
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+", re.I),            # bearer tokens
    re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"]?[^\s'\",}]+"),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),       # email (PII)
)


@dataclass(frozen=True)
class LLMObservation:
    """One LLM call's observable facts. `prompt`/`query` are raw inputs that are NEVER
    logged verbatim — only their derived metadata is. Pass None to omit them (the agent
    always omits both: the diff must never be logged)."""

    purpose: str                                  # 'ingestion' | 'chat' | 'agent'
    model: str
    tokens_in: int
    tokens_out: int
    provider: str | None = None                   # client/provider: fake|bedrock|anthropic|gemini
    latency_ms: int | None = None
    retrieved_context_size: int | None = None     # rows/chunks grounding the call
    status: str = "ok"                            # ok | error | throttled
    error_kind: str | None = None                 # exception CLASS name only (no message)
    prompt: str | None = None                     # reduced to derived metadata only
    query: str | None = None                      # generated query (e.g. SQL); idem


def count_redactions(text: str) -> int:
    """How many secret/PII spans the text contains (we never log the spans themselves)."""
    return sum(len(p.findall(text)) for p in _REDACTIONS)


def _digest(text: str) -> str:
    """A short, stable content fingerprint for correlating identical inputs without
    revealing them (first 12 hex of sha256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _input_metadata(prefix: str, text: str) -> dict[str, object]:
    """Safe, content-free facts about an input: size, fingerprint, sensitive-span count."""
    return {
        f"{prefix}_chars": len(text),
        f"{prefix}_sha256": _digest(text),
        f"{prefix}_redactions": count_redactions(text),
    }


def build_log_record(obs: LLMObservation) -> dict[str, object]:
    """The structured log line as a dict (pure — used directly in tests). Inputs appear
    ONLY as derived metadata; provider/error_kind are included when known."""
    record: dict[str, object] = {
        "event": "llm_call",
        "purpose": obs.purpose,
        "provider": obs.provider,
        "model": obs.model,
        "status": obs.status,
        "tokens_in": obs.tokens_in,
        "tokens_out": obs.tokens_out,
        "latency_ms": obs.latency_ms,
        "retrieved_context_size": obs.retrieved_context_size,
    }
    if obs.error_kind is not None:
        record["error_kind"] = obs.error_kind
    if obs.prompt is not None:
        record.update(_input_metadata("prompt", obs.prompt))
    if obs.query is not None:
        record.update(_input_metadata("query", obs.query))
    return record


def log_llm_call(obs: LLMObservation) -> dict[str, object]:
    """Emit the per-request LLM log line as one JSON object at INFO. Returns the record
    (handy for callers/tests). Logging only — no metrics, no Settings/DB (agent-safe)."""
    record = build_log_record(obs)
    logger.info(json.dumps(record, sort_keys=True))
    return record
