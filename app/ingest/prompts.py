"""Versioned extraction prompt for catalog ingestion (#3).

The model's ONLY job is to turn an unstructured source (model card, leaderboard
page, release post) into strict JSON catalog rows — never to rank or judge (that
stays deterministic in S6). The output is treated as UNTRUSTED and re-validated in
app/ingest/validation.py; the prompt just maximises the chance of clean JSON.

`PROMPT_VERSION` is bumped whenever the prompt/target shape changes, so a logged
ingest can be traced to the exact instructions that produced it.
"""

from __future__ import annotations

PROMPT_VERSION = "ingest-v1"

# Target shape maps 1:1 to CatalogRowIn (camelCase on the wire; snake_case also
# accepted by the validator). Required: model, vendor, benchmark, metric, score,
# costPerMtok. The rest are optional and omitted when the source doesn't state them.
INGEST_SYSTEM_PROMPT = (
    "You extract LLM benchmark facts from an unstructured source into STRICT JSON. "
    "Return ONLY JSON in exactly this shape and nothing else:\n"
    '{"rows": [{"model": "string", "vendor": "string", "benchmark": "string", '
    '"metric": "string", "score": 0.0, "costPerMtok": 0.0, "harness": "string|null", '
    '"harnessVendor": "string|null", "taskType": "string|null", '
    '"contextWindow": null, "source": "string|null", "measuredAt": "YYYY-MM-DD|null"}]}\n'
    "Rules: one row per (model, benchmark, metric) figure actually stated in the "
    "source. `score` is the numeric benchmark result; `costPerMtok` is USD per "
    "million tokens. Omit a row entirely if model, vendor, benchmark, metric, "
    "score, or costPerMtok is not stated — never invent or estimate values. Use an "
    "empty rows array if the source states no benchmark figures. Never include prose."
)


def build_user_prompt(source_text: str) -> str:
    return f"Extract benchmark catalog rows from this source:\n\n{source_text}"
