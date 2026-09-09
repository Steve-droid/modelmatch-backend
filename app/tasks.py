"""The two tasks a project can run (E20, HLD §3b.1) — ONE vocabulary end to end.

The catalog scores rows per `task_type`; a project runs exactly one of those tasks;
every `ci_run` records the task it ran; the agent image dispatches on the short
`task` name it fetches from `GET /projects/{id}/agent-config`. Keeping the mapping in
one place is what lets the chat grounding, the runs table and the agent all say the
same word for the same thing.

`agentic_coding` catalog rows exist for breadth only — the product never runs an
autonomous coding agent in anyone's CI, so it is not a project task.
"""

from __future__ import annotations

from typing import Literal

CI_REVIEW = "ci_review"
SECURITY_ANALYSIS = "security_analysis"

TaskType = Literal["ci_review", "security_analysis"]
PROJECT_TASK_TYPES: tuple[str, ...] = (CI_REVIEW, SECURITY_ANALYSIS)

# catalog vocabulary → the agent's short task name (the contract's `task` field)
AGENT_TASK_BY_TASK_TYPE: dict[str, str] = {
    CI_REVIEW: "review",
    SECURITY_ANALYSIS: "security",
}

# Per-project review preferences (review task only): bounded text appended to the
# agent's system prompt. The bound is the contract's (§3b.1), mirrored by the agent.
REVIEW_PREFERENCES_MAX_LEN = 2000

# Human labels the API/UI/chat can share.
TASK_LABELS: dict[str, str] = {
    CI_REVIEW: "PR code review",
    SECURITY_ANALYSIS: "security analysis",
}


def is_project_task(task_type: str | None) -> bool:
    return task_type in PROJECT_TASK_TYPES


def agent_task_for(task_type: str) -> str:
    """`ci_review → "review"`, `security_analysis → "security"`; anything else is a
    programming error (the column is validated at create/update)."""
    try:
        return AGENT_TASK_BY_TASK_TYPE[task_type]
    except KeyError:  # pragma: no cover — guarded by the project validators
        raise ValueError(f"{task_type!r} is not a project task type") from None
