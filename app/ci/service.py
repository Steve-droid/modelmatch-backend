"""CI integration service (S11): ci-setup snippet + run ingest.

Two operations close the CI savings loop:
- `ci_setup` (owner JWT): returns the Jenkins stage snippet the user pastes into
  their pipeline. Mints the per-project ingest token *once* (stores only its hash)
  and returns the plaintext on that first fetch; later fetches return token=None.
- `ingest_run` (per-project token): persists a `ci_run` + its `ci_finding` rows
  from the agent's `AgentResult`. DETERMINISTIC — no LLM, zero tokens; it only
  stores what the agent already computed.

Scope (S11): persist tokens, model, build id, findings, AND the agent's gate +
gate_reason as an audit trail (the pass/fail gate acts in the user's CI; the
backend keeps the record). Savings (S12) + quality_ok (S13) stay null on insert.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_owner
from app.ci.tokens import hash_token, mint_token
from app.config import get_settings
from app.models import (
    CiFinding,
    CiRun,
    JenkinsConnection,
    Project,
    RecommendationOption,
    User,
)
from app.schemas.ci import CiRunIngest, CiRunOut, CiSetupOut


def _require_owned_project(db: Session, project_id: int, current_user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    require_owner(project.user_id, current_user)  # 403 if not the caller's
    return project


# The agent image's ENTRYPOINT is ["python","-m","agent"] (see agent/Dockerfile),
# so the snippet runs the IMAGE and appends `--diff <file>` as args. We deliberately
# do NOT use a Jenkins `agent { docker { image … } }` block: that runs the build's
# shell steps *inside* the image, which an executable-entrypoint image can't host.
# Instead: a normal node + an explicit `docker run`. Pinned + drift-tested.
AGENT_DIFF_FILE = "pr.diff"
AGENT_DIFF_ARG = f"--diff {AGENT_DIFF_FILE}"
# Credential ids the user creates in Jenkins (Secret text).
CI_TOKEN_CRED_ID = "modelmatch-ci-token"
MODEL_KEY_CRED_ID = "modelmatch-model-api-key"


def _provider_wiring(llm_client: str, aws_region: str) -> tuple[str, str, str]:
    """Map a provider to its Jenkins credential binding + `docker run` env flags.

    BYOK (anthropic/gemini): the model API key is a Jenkins 'Secret text' credential
    bound DIRECTLY to the provider's SDK env var, then passed to docker BY NAME
    (`-e VAR`, no value) so the secret never appears in the docker argv. Bedrock: no
    static key — the agent uses the node's AWS credentials (EC2 instance role or a
    mounted ~/.aws profile). Returns (environment-block lines, docker-run flag lines,
    a leading shell comment).
    """
    client = llm_client.lower()
    if client == "bedrock":
        env_block = ""  # no model-key credential for Bedrock
        run_flags = (
            f"        -e AWS_REGION={aws_region} \\\n"  # region is not a secret
            '        -v "$HOME/.aws:/home/appuser/.aws:ro" \\\n'
        )
        note = (
            "# Bedrock: the agent uses this node's AWS credentials (an EC2 instance\n"
            "        # role, or the mounted ~/.aws profile) — no API key. The role/profile\n"
            "        # must allow bedrock:InvokeModel / Converse in $AWS_REGION.\n        "
        )
        return env_block, run_flags, note

    key_env = "GOOGLE_API_KEY" if client == "gemini" else "ANTHROPIC_API_KEY"
    # Bind the credential straight to the SDK's env var name — then pass it to docker
    # by name (no value in argv). The secret stays in the Jenkins-managed env only.
    env_block = (
        f"    // BYOK model key — add a 'Secret text' credential id '{MODEL_KEY_CRED_ID}'.\n"
        f"    {key_env} = credentials('{MODEL_KEY_CRED_ID}')\n"
    )
    run_flags = f"        -e {key_env} \\\n"  # by NAME, not value
    return env_block, run_flags, ""


def build_review_snippet(
    *,
    ci_runs_url: str,
    image_ref: str,
    llm_client: str,
    model: str,
    max_tokens: int,
    token_ceiling: int,
    aws_region: str,
) -> str:
    """A Jenkins declarative stage that runs the agent IMAGE on the PR diff and POSTs
    results back. Runs on a normal node (needs docker, git, jq, curl) and invokes the
    image via `docker run … <image> --diff pr.diff` — the image entrypoint is
    `python -m agent`. Provider config (LLM_CLIENT / AGENT_MODEL / cost caps) is set
    explicitly so the agent never silently runs the fake client.

    Secret hygiene: neither the BYOK key nor the CI token is expanded into a command's
    argv. The key is passed to docker BY NAME (`-e VAR`); the CI token is written to a
    0600 curl config file (via a heredoc, so it isn't even a printf argument) and
    consumed with `--config`. The diff is PR-safe: `CHANGE_TARGET` (set on multibranch
    PR builds) with a documented fall back to `main`.

    The agent exits non-zero when the gate FAILS, so we capture the exit code, POST
    the result regardless (the backend keeps the audit record), then propagate the
    gate as the build status — review + gate stay in CI, ingest still happens.
    """
    env_block, cred_flags, note = _provider_wiring(llm_client, aws_region)
    return f"""// Runs on a normal Jenkins node (needs: docker, git, jq, curl).
stage('ModelMatch AI Review') {{
  agent any
  environment {{
    // The per-project ingest token (shown once by /ci-setup) — add as 'Secret text' '{CI_TOKEN_CRED_ID}'.
    MODELMATCH_CI_TOKEN = credentials('{CI_TOKEN_CRED_ID}')
{env_block}  }}
  steps {{
    sh '''
        set -e
        # PR-safe diff: CHANGE_TARGET is set on multibranch PR builds; fall back to main.
        TARGET="${{CHANGE_TARGET:-main}}"
        git fetch --no-tags origin "$TARGET"
        git diff "origin/${{TARGET}}...HEAD" > {AGENT_DIFF_FILE}
        {note}set +e
        docker run --rm -v "$PWD:/work" -w /work \\
        -e LLM_CLIENT={llm_client} \\
        -e AGENT_MODEL={model} \\
        -e AGENT_MAX_TOKENS={max_tokens} \\
        -e AGENT_TOKEN_CEILING={token_ceiling} \\
{cred_flags}        {image_ref} {AGENT_DIFF_ARG} > result.json
        AGENT_RC=$?
        set -e
        jq --arg b "$BUILD_TAG" '. + {{jenkinsBuildId: $b}}' result.json > payload.json
        # Keep the CI token out of any command's argv: write it to a 0600 curl config.
        CURL_CFG="$(mktemp)"
        trap 'rm -f "$CURL_CFG"' EXIT
        cat > "$CURL_CFG" <<CFGEOF
header = "X-CI-Token: $MODELMATCH_CI_TOKEN"
CFGEOF
        curl -fsS --config "$CURL_CFG" -X POST "{ci_runs_url}" \\
          -H "Content-Type: application/json" \\
          --data @payload.json
        exit $AGENT_RC
    '''
  }}
}}"""


def ci_setup(db: Session, project_id: int, current_user: User) -> CiSetupOut:
    _require_owned_project(db, project_id, current_user)

    conn = db.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == project_id)
    )
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jenkins connection not found — configure Jenkins first",
        )

    # Mint-once: issue a token only if none exists yet (we keep only the hash, so a
    # previously-minted token can never be re-shown — rotation is a later endpoint).
    token_plain: str | None = None
    if conn.ci_token_hash is None:
        token_plain = mint_token()
        conn.ci_token_hash = hash_token(token_plain)  # store the hash, never plaintext
        db.commit()

    settings = get_settings()
    ci_runs_url = f"{settings.public_base_url}/projects/{project_id}/ci-runs"
    snippet = build_review_snippet(
        ci_runs_url=ci_runs_url,
        image_ref=settings.agent_image,
        llm_client=settings.ci_agent_llm_client,
        model=settings.ci_agent_model,
        max_tokens=settings.ci_agent_max_tokens,
        token_ceiling=settings.ci_agent_token_ceiling,
        aws_region=settings.aws_region,
    )
    return CiSetupOut(
        snippet=snippet,
        image_ref=settings.agent_image,
        ci_runs_url=ci_runs_url,
        token=token_plain,
    )


def _resolve_model_id(db: Session, project: Project) -> int | None:
    """The model this run is configured (BYOK) to use = the project's selected
    option's model — a priced catalog row (S12 savings join). The agent reports a
    provider model-id string too, but it won't match catalog display names and we
    have no column for it, so the selected model is authoritative here."""
    if project.selected_option_id is None:
        return None
    option = db.get(RecommendationOption, project.selected_option_id)
    return option.model_id if option else None


def ingest_run(db: Session, project: Project, payload: CiRunIngest) -> CiRunOut:
    # Reject a re-POSTed build for this project (409) — runs are not idempotent yet.
    existing = db.scalar(
        select(CiRun.id).where(
            CiRun.project_id == project.id,
            CiRun.jenkins_build_id == payload.jenkins_build_id,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A run for this Jenkins build id already exists",
        )

    run = CiRun(
        project_id=project.id,
        jenkins_build_id=payload.jenkins_build_id,
        model_id=_resolve_model_id(db, project),
        task="code_review",
        tokens_in=payload.tokens_in,
        tokens_out=payload.tokens_out,
        gate=payload.gate,  # audit trail of the agent's pass/fail decision
        gate_reason=payload.gate_reason,
        # savings (S12) + quality_ok (S13) intentionally left null.
    )
    db.add(run)
    db.flush()  # assign run.id for the finding FKs

    for f in payload.findings:
        db.add(
            CiFinding(
                ci_run_id=run.id,
                severity=f.severity,
                category=f.category,
                file=f.file,
                line=f.line,
                message=f.message,
            )
        )
    db.commit()
    db.refresh(run)

    return CiRunOut(
        id=run.id,
        project_id=run.project_id,
        jenkins_build_id=run.jenkins_build_id,
        model_id=run.model_id,
        task=run.task,
        tokens_in=run.tokens_in,
        tokens_out=run.tokens_out,
        gate=run.gate,
        gate_reason=run.gate_reason,
        findings_count=len(payload.findings),
    )
