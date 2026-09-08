"""The agent's output shape: the shared findings contract + what the security task adds.

`Finding` / `AgentResult` (app/schemas/findings.py) are the contract the backend's
`/ci-runs` ingest consumes. The security task adds a CWE per finding. Until P38e
adds `cwe` to the shared schema (HLD §3b.1) it lives here as a subclass: the agent
emits it today, and today's API silently ignores it (Finding is extra=ignore) —
nothing breaks on either side of the deploy order.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from app.schemas.findings import AgentResult, Finding


class AgentFinding(Finding):
    # "CWE-89: SQL Injection" for security findings; None for review findings.
    cwe: Optional[str] = None


class AgentRunResult(AgentResult):
    findings: list[AgentFinding] = Field(default_factory=list)
