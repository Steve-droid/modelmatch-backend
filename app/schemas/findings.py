"""CI-agent findings contract (S10) — shared by the agent (produces) and the
backend's run-ingest endpoint (S11, consumes), so there's no cross-repo drift.

A finding is one security/style issue the reviewer raised on the PR diff. AgentResult
is the whole run: the findings, token usage (for the savings engine), the model used,
and the gate decision (review + pass/fail stay in CI — the agent runs in CI).
"""

from typing import Literal, Optional

from pydantic import Field

from app.schemas.base import CamelModel

Severity = Literal["low", "medium", "high", "critical"]
FindingCategory = Literal["security", "style"]
Gate = Literal["pass", "fail"]


class Finding(CamelModel):
    severity: Severity
    category: FindingCategory
    file: str
    line: Optional[int] = None
    message: str


class AgentResult(CamelModel):
    findings: list[Finding] = Field(default_factory=list)
    tokens_in: int
    tokens_out: int
    model: str
    gate: Gate
    gate_reason: Optional[str] = None
