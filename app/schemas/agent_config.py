"""`GET /projects/{id}/agent-config` (E20, HLD §3b.1) — what the CI agent fetches.

Served under the per-project CI token (the same dependency as `/ci-runs`), so the
agent running in the user's Jenkins learns its task, model and review preferences
from the app at run time instead of from the pasted Jenkinsfile. The body NEVER
carries a credential: `credentialEnvVar` is the NAME of the variable the agent must
find in its own environment (it pre-flights that before spending a token).
"""

from typing import Literal, Optional

from app.schemas.base import CamelModel

AgentTask = Literal["review", "security"]


class AgentConfigModel(CamelModel):
    """The selected option's trusted `agent_runtime_config` row, verbatim: a bare
    `providerModelId` (the agent composes `<provider>/<id>` for OpenCode) and the
    env-var NAME the user's key arrives in (`null` for `aws_iam`)."""

    name: str
    provider: str
    provider_model_id: str
    auth_mode: str
    credential_env_var: Optional[str] = None


class AgentConfigOut(CamelModel):
    project_id: int
    task: AgentTask  # what the agent dispatches on
    task_type: str  # the catalog vocabulary, for display/logging
    model: AgentConfigModel
    # review task only (≤ 2000 chars); null for security projects.
    review_preferences: Optional[str] = None
