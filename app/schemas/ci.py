"""CI integration schemas (S11): the ci-setup snippet + the run-ingest body.

The ingest body builds on the agent's shared `AgentResult` (app/schemas/findings.py)
plus a Jenkins build id — reusing the contract is the no-drift win: the agent
produces what the backend consumes. `/ci-runs` is deterministic (no LLM); it only
stores what the agent already computed.

Guardrail (ABC's untrusted-LLM-output lesson): the findings come from a model, so
the ingest body is treated as UNTRUSTED and validated deterministically — bounded
token counts, length-capped + relative-only file paths, capped finding count, a
constrained build id, and `extra="forbid"` so a raw diff (or any unexpected field)
is REJECTED, never accepted or stored. We never accept or persist diff content.

Scope (S11): persist tokens, model, build id, findings, AND the agent's gate as an
audit trail (architecture §4 — the gate acts in CI, but the backend keeps the
record, like ABC's `ingestion_run.status`). Savings (S12) + quality_ok (S13) stay
null on insert.
"""

import re
from decimal import Decimal
from typing import Optional

from pydantic import ConfigDict, Field, field_validator

from app.schemas.base import CamelModel
from app.schemas.findings import AgentResult, Finding
from app.tasks import agent_task_for

# Untrusted-input bounds. Generous enough for real runs, tight enough to reject
# absurd/oversized payloads with a clean 422 (not a DB error or unbounded store).
MAX_TOKENS = 10_000_000          # ~10M tokens/run is already far beyond a code review
MAX_FINDINGS = 500               # a single diff review yielding >500 findings is bogus
MAX_MESSAGE_LEN = 4000           # one finding message; ci_finding.message is Text
MAX_FILE_LEN = 1024              # matches ci_finding.file (String(1024))
MAX_MODEL_LEN = 200              # matches model.name (String(200))
MAX_CWE_LEN = 200                # matches ci_finding.cwe (String(200))
# Jenkins BUILD_TAG-ish, no spaces/control. `%` because a multibranch job encodes a
# branch slash as `%2F` in BUILD_TAG (jenkins-<job>-feature%2Fx-12) — June bug 6.
_BUILD_ID_RE = r"^[A-Za-z0-9._#/%\-]{1,255}$"


class IngestFinding(Finding):
    """A `Finding` (shared contract) with the strict bounds the ingest enforces on
    untrusted model output: capped lengths, non-negative line, relative path only."""

    file: str = Field(min_length=1, max_length=MAX_FILE_LEN)
    line: Optional[int] = Field(default=None, ge=0)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LEN)
    # E20 (HLD §3b.1): the security task's CWE ("CWE-89: SQL Injection"); null on
    # review findings. Additive + optional so a v1 agent's payload still validates.
    cwe: Optional[str] = Field(default=None, max_length=MAX_CWE_LEN)

    @field_validator("cwe")
    @classmethod
    def _blank_cwe_is_none(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("file")
    @classmethod
    def _must_be_relative(cls, v: str) -> str:
        # No absolute POSIX/UNC/Windows-drive paths, no '..' traversal — a finding
        # references a file inside the user's repo, never an absolute host path.
        if v.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", v):
            raise ValueError("file must be a relative path, not absolute")
        if ".." in re.split(r"[\\/]", v):
            raise ValueError("file must not contain '..' path segments")
        return v


class CiRunIngest(AgentResult):
    """What the user's Jenkins POSTs back: the AgentResult + which build it was.

    `extra="forbid"` (overriding the lenient base) rejects any unexpected field —
    notably a raw `diff` — with a 422; we never accept or store diff content.
    """

    model_config = ConfigDict(extra="forbid")

    jenkins_build_id: str = Field(min_length=1, max_length=255, pattern=_BUILD_ID_RE)
    findings: list[IngestFinding] = Field(default_factory=list, max_length=MAX_FINDINGS)
    tokens_in: int = Field(ge=0, le=MAX_TOKENS)
    tokens_out: int = Field(ge=0, le=MAX_TOKENS)
    model: str = Field(min_length=1, max_length=MAX_MODEL_LEN)
    # E20 (HLD §3b.1): the agentic loop's cache-read tokens. Optional so a v1 agent
    # (which never sends it) still validates under extra="forbid"; STORED, never
    # priced — savings stay `tokens_in/out × catalog price` on both sides (HLD §8).
    cache_read_tokens: Optional[int] = Field(default=None, ge=0, le=MAX_TOKENS)


class CiRunOut(CamelModel):
    """The persisted run, echoed back to the caller (camelCase out)."""

    id: int
    project_id: int
    jenkins_build_id: str
    model_id: Optional[int] = None
    task: str
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    # Savings trio (S12): cost of this run on the selected model vs the project's
    # baseline model, and the gap. NULL when a model is unpriced (see compute_savings).
    actual_cost: Optional[Decimal] = None
    baseline_cost: Optional[Decimal] = None
    savings: Optional[Decimal] = None
    gate: Optional[str] = None
    gate_reason: Optional[str] = None
    findings_count: int


class CiSetupOut(CamelModel):
    """The Jenkins stage snippet + the bits the FE renders/copies.

    `token` carries the plaintext per-project ingest token ONLY on the first fetch
    (mint-once): the backend stores only its hash, so it can never be re-shown.
    Subsequent fetches return `token=None` — rotate to issue a new one.
    """

    snippet: str
    image_ref: str
    ci_runs_url: str
    token: Optional[str] = None
    # E20: which task the snippet runs (catalog vocabulary + the agent's short name),
    # so the FE can word the CI-setup step per task.
    task_type: str
    task: str

    @classmethod
    def for_task(cls, task_type: str, **fields) -> "CiSetupOut":
        return cls(task_type=task_type, task=agent_task_for(task_type), **fields)
