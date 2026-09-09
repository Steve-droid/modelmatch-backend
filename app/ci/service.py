"""CI integration service (S11, E20): ci-setup snippet + agent-config + run ingest.

Three operations close the CI loop:
- `ci_setup` (owner JWT): returns the Jenkins stage snippet the user pastes into
  their pipeline — ONE PER TASK since E20 (review image vs security image). Mints
  the per-project ingest token *once* (stores only its hash) and returns the
  plaintext on that first fetch; later fetches return token=None.
- `agent_config` (per-project token, HLD §3b.1): what the agent fetches at run time —
  its task, the selected model's runtime config and the review preferences. The
  snippet stays task-agnostic; the app is the source of truth.
- `ingest_run` (per-project token): persists a `ci_run` + its `ci_finding` rows
  from the agent's `AgentResult`. DETERMINISTIC — no LLM, zero tokens; it only
  stores what the agent already computed.

Scope: persist tokens, model, build id, findings (+ `cwe`), cache-read tokens, AND
the agent's gate + gate_reason as an audit trail (the pass/fail gate acts in the
user's CI; the backend keeps the record). Savings (S12) + quality_ok (S13) stay
null on insert.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_runtime import require_enabled_runtime_config, runtime_config_error
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
from app.observability import LLMObservation
from app.observability.metrics import record_llm_metrics
from app.savings.service import compute_savings, price_for
from app.schemas.agent_config import AgentConfigModel, AgentConfigOut
from app.schemas.ci import CiRunIngest, CiRunOut, CiSetupOut
from app.tasks import CI_REVIEW, SECURITY_ANALYSIS, agent_task_for


def _require_owned_project(db: Session, project_id: int, current_user: User) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    require_owner(project.user_id, current_user)  # 403 if not the caller's
    return project


# The agent images' ENTRYPOINT is ["python","-m","agent"] (see agent/Dockerfile*),
# so the snippets run the IMAGE and (review only) append `--diff <file>` as args. We
# deliberately do NOT use a Jenkins `agent { docker { image … } }` block: that runs
# the build's shell steps *inside* the image, which an executable-entrypoint image
# can't host. Instead: a normal node + an explicit `docker run`. Pinned + drift-tested.
AGENT_DIFF_FILE = "pr.diff"
AGENT_DIFF_ARG = f"--diff {AGENT_DIFF_FILE}"
# Credential ids the user creates in Jenkins (Secret text).
CI_TOKEN_CRED_ID = "modelmatch-ci-token"
MODEL_KEY_CRED_ID = "modelmatch-model-api-key"
# The security image's read-only workspace + the sandbox flags P38b/P38d proved
# (no capabilities, no privilege escalation, bounded memory/CPU, a tmpfs /tmp).
SECURITY_WORKSPACE = "/workspace"
SECURITY_SANDBOX_FLAGS = (
    "        --cap-drop ALL --security-opt no-new-privileges \\\n"
    "        --memory 2g --cpus 2 --tmpfs /tmp:size=256m \\\n"
)


def _provider_wiring(
    *, auth_mode: str, credential_env_var: str | None, aws_region: str
) -> tuple[str, str, str]:
    """Map a provider to its Jenkins credential binding + `docker run` env flags.

    API-key providers bind the Jenkins Secret text credential directly to the
    provider SDK env var, then pass it to docker BY NAME (`-e VAR`, no value) so the
    secret never appears in argv. AWS IAM uses the node's AWS credentials/profile and
    carries no model-key binding. Returns (environment-block lines, docker-run flag
    lines, a leading shell comment).
    """
    if auth_mode == "aws_iam":
        env_block = ""  # no model-key credential for Bedrock
        run_flags = (
            f"        -e AWS_DEFAULT_REGION={aws_region} \\\n"
            f"        -e AWS_REGION={aws_region} \\\n"  # region is not a secret
            '        -v "$HOME/.aws:/home/appuser/.aws:ro" \\\n'
        )
        note = (
            "# Bedrock: the agent uses this node's AWS credentials (an EC2 instance\n"
            "        # role, or the mounted ~/.aws profile) — no API key. The role/profile\n"
            "        # must allow bedrock:InvokeModel / Converse in $AWS_DEFAULT_REGION.\n        "
        )
        return env_block, run_flags, note

    if auth_mode != "api_key" or not credential_env_var:
        raise ValueError("api_key runtime configs must declare credential_env_var")
    # Bind the credential straight to the SDK's env var name — then pass it to docker
    # by name (no value in argv). The secret stays in the Jenkins-managed env only.
    env_block = (
        f"    // BYOK model key — add a 'Secret text' credential id '{MODEL_KEY_CRED_ID}'.\n"
        f"    {credential_env_var} = credentials('{MODEL_KEY_CRED_ID}')\n"
    )
    run_flags = f"        -e {credential_env_var} \\\n"  # by NAME, not value
    return env_block, run_flags, ""


def _modelmatch_flags(*, api_url: str, project_id: int) -> str:
    """The `MODELMATCH_*` trio (+ POST_RESULT + BUILD_TAG) the agent needs to fetch its
    config and post its own run (HLD §3b.1). The token is passed BY NAME — it is bound
    in `environment {}` from the Jenkins credential and never appears in argv."""
    return (
        f"        -e MODELMATCH_API_URL={api_url} \\\n"
        f"        -e MODELMATCH_PROJECT_ID={project_id} \\\n"
        "        -e MODELMATCH_CI_TOKEN \\\n"
        "        -e MODELMATCH_POST_RESULT=true \\\n"
        "        -e BUILD_TAG \\\n"
    )


def _image_note(image_ref: str) -> str:
    # Honest about the registry: in this phase the image lives in a private ECR (no
    # public mirror), so the node must have it (a pull with registry credentials, or
    # a pre-pulled/retagged copy) before the first build.
    return (
        f"# Image: {image_ref}\n"
        "        # (private registry in this phase — no public mirror. Pull it onto this node\n"
        "        #  with your registry credentials, or pre-load it, before the first build.)\n        "
    )


# One exit table for both images (HLD §3b.1); only 0 is a pass. A refusal (3) returns
# no findings, so it must never read as clean — the stage says so in its own words.
def _exit_case(*, pass_msg: str, fail_msg: str, nothing_verb: str) -> str:
    return f"""        case "$AGENT_RC" in
          0)   echo "ModelMatch: {pass_msg}" ;;
          1)   echo "ModelMatch: {fail_msg} — failing the stage." ;;
          2)   echo "ModelMatch: unparseable model output — NOT a pass." ;;
          3)   echo "ModelMatch: the model REFUSED. Nothing was {nothing_verb}. NOT a pass." ;;
          4)   echo "ModelMatch: config / credential / API error (see above) — NOT a pass." ;;
          124) echo "ModelMatch: a run ceiling aborted the agent — NOT a pass." ;;
          *)   echo "ModelMatch: unexpected agent exit $AGENT_RC — treating as failure." ;;
        esac
        exit $AGENT_RC"""


def build_review_snippet(
    *,
    api_url: str,
    project_id: int,
    image_ref: str,
    max_tokens: int,
    token_ceiling: int,
    aws_region: str,
    auth_mode: str,
    credential_env_var: str | None = None,
) -> str:
    """A Jenkins declarative stage that runs the REVIEW image on the PR diff. Runs on
    a normal node (needs docker + git) and invokes the image via `docker run …
    <image> --diff pr.diff` — the image entrypoint is `python -m agent`.

    Fetch-config shape (E20): the agent gets its task / model / review preferences
    from `GET /projects/{id}/agent-config` with the CI token and POSTs `/ci-runs`
    itself (`MODELMATCH_POST_RESULT=true`, `BUILD_TAG` as the build id) — so the
    stage carries no provider/model lines and no jq+curl block, and preferences
    change in the app without editing the pipeline. Exactly ONE poster per stage:
    the agent posts, the stage never curls (a second POST would be a 409).

    Secret hygiene: neither the BYOK key nor the CI token is expanded into a
    command's argv — both are bound in `environment {}` and passed to docker BY
    NAME (`-e VAR`). The diff is PR-safe: `CHANGE_TARGET` (set on multibranch PR
    builds) with a documented fall back to `main`. The agent exits non-zero when the
    gate FAILS; the stage propagates that as the build status.
    """
    env_block, cred_flags, note = _provider_wiring(
        auth_mode=auth_mode, credential_env_var=credential_env_var, aws_region=aws_region
    )
    mm_flags = _modelmatch_flags(api_url=api_url, project_id=project_id)
    exit_case = _exit_case(
        pass_msg="review passed — no blocking findings.",
        fail_msg="the review found a blocking (high/critical) issue",
        nothing_verb="reviewed",
    )
    return f"""// Runs on a normal Jenkins node (needs: docker, git). The agent fetches this
// project's task, model and review preferences from ModelMatch at run time and posts
// the run itself — change them in the app, not here.
stage('ModelMatch AI Review') {{
  agent any
  environment {{
    // The per-project CI token (shown once by /ci-setup) — add as 'Secret text' '{CI_TOKEN_CRED_ID}'.
    MODELMATCH_CI_TOKEN = credentials('{CI_TOKEN_CRED_ID}')
{env_block}  }}
  steps {{
    sh '''
        set -e
        # PR-safe diff: CHANGE_TARGET is set on multibranch PR builds; fall back to main.
        TARGET="${{CHANGE_TARGET:-main}}"
        git fetch --no-tags origin "$TARGET"
        git diff "origin/${{TARGET}}...HEAD" > {AGENT_DIFF_FILE}
        {_image_note(image_ref)}{note}set +e
        docker run --rm -v "$PWD:/work" -w /work \\
{mm_flags}        -e AGENT_MAX_TOKENS={max_tokens} \\
        -e AGENT_TOKEN_CEILING={token_ceiling} \\
{cred_flags}        {image_ref} {AGENT_DIFF_ARG}
        AGENT_RC=$?
        set -e
{exit_case}
    '''
  }}
}}"""


def build_security_snippet(
    *,
    api_url: str,
    project_id: int,
    image_ref: str,
    aws_region: str,
    auth_mode: str,
    credential_env_var: str | None = None,
) -> str:
    """A Jenkins declarative stage that runs the SECURITY image over the whole
    checkout (E20). The workspace is mounted READ-ONLY at `/workspace` (the agent
    never writes to the checkout), under the sandbox flags the P38b spike proved, and
    the image takes NO args: it fetches its config from the API, runs the OpenCode
    loop, and POSTs the run (with per-finding CWEs + cache-read tokens) itself.

    Ceilings are the image's own security-task defaults (a scan reads a whole repo;
    the review caps would abort it) — env overrides stay possible in Jenkins. Exit
    `1` (a critical finding) fails the stage: that is the demo beat, and only `0` is
    a pass.
    """
    env_block, cred_flags, note = _provider_wiring(
        auth_mode=auth_mode, credential_env_var=credential_env_var, aws_region=aws_region
    )
    mm_flags = _modelmatch_flags(api_url=api_url, project_id=project_id)
    exit_case = _exit_case(
        pass_msg="scan completed — no critical findings.",
        fail_msg="CRITICAL vulnerability found",
        nothing_verb="scanned",
    )
    return f"""// Runs on a normal Jenkins node (needs: docker). The agent audits the whole checkout
// READ-ONLY with the model ModelMatch configured for this project, posts the run itself
// and fails the stage on a critical finding.
stage('ModelMatch Security Analysis') {{
  agent any
  environment {{
    // The per-project CI token (shown once by /ci-setup) — add as 'Secret text' '{CI_TOKEN_CRED_ID}'.
    MODELMATCH_CI_TOKEN = credentials('{CI_TOKEN_CRED_ID}')
{env_block}  }}
  steps {{
    sh '''
        set -e
        {_image_note(image_ref)}{note}set +e
        docker run --rm -v "$PWD:{SECURITY_WORKSPACE}:ro" \\
{SECURITY_SANDBOX_FLAGS}{mm_flags}{cred_flags}        {image_ref}
        AGENT_RC=$?
        set -e
{exit_case}
    '''
  }}
}}"""


def _require_connected(db: Session, project_id: int, current_user: User) -> JenkinsConnection:
    """Owner-scoped + must already have a Jenkins connection (the token lives on it)."""
    _require_owned_project(db, project_id, current_user)
    conn = db.scalar(
        select(JenkinsConnection).where(JenkinsConnection.project_id == project_id)
    )
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Jenkins connection not found — configure Jenkins first",
        )
    return conn


def _selected_runtime_config(db: Session, project: Project):
    if project.selected_option_id is None:
        raise runtime_config_error()
    option = db.get(RecommendationOption, project.selected_option_id)
    if option is None:
        raise runtime_config_error()
    return require_enabled_runtime_config(db, option.model_id, option.model.name)


def agent_image_for(task_type: str) -> str:
    """The image the snippet runs for a task (P38d split: review vs security)."""
    settings = get_settings()
    if task_type == SECURITY_ANALYSIS:
        return settings.agent_security_image
    return settings.agent_image


def build_snippet(project: Project, runtime, *, api_url: str) -> str:
    """Per-task dispatcher: the review stage (diff → one call) or the security
    stage (read-only checkout → agentic scan). Both fetch-config, both agent-posts."""
    settings = get_settings()
    image_ref = agent_image_for(project.task_type)
    if project.task_type == SECURITY_ANALYSIS:
        return build_security_snippet(
            api_url=api_url,
            project_id=project.id,
            image_ref=image_ref,
            aws_region=settings.aws_region,
            auth_mode=runtime.auth_mode,
            credential_env_var=runtime.credential_env_var,
        )
    return build_review_snippet(
        api_url=api_url,
        project_id=project.id,
        image_ref=image_ref,
        max_tokens=settings.ci_agent_max_tokens,
        token_ceiling=settings.ci_agent_token_ceiling,
        aws_region=settings.aws_region,
        auth_mode=runtime.auth_mode,
        credential_env_var=runtime.credential_env_var,
    )


def _setup_out(project: Project, token_plain: str | None, db: Session) -> CiSetupOut:
    """Build the snippet + ingest URL (the stable parts); `token_plain` is non-None
    only when a token was just minted/rotated (never re-shown otherwise)."""
    settings = get_settings()
    runtime = _selected_runtime_config(db, project)
    api_url = settings.public_base_url.rstrip("/")
    ci_runs_url = f"{api_url}/projects/{project.id}/ci-runs"
    return CiSetupOut.for_task(
        project.task_type,
        snippet=build_snippet(project, runtime, api_url=api_url),
        image_ref=agent_image_for(project.task_type),
        ci_runs_url=ci_runs_url,
        token=token_plain,
    )


def ci_setup(db: Session, project_id: int, current_user: User) -> CiSetupOut:
    conn = _require_connected(db, project_id, current_user)
    project = db.get(Project, project_id)

    # Mint-once: issue a token only if none exists yet (we keep only the hash, so a
    # previously-minted token is never re-shown here — use rotate_ci_token to recover
    # a lost token, which the FE exposes as "Regenerate token").
    token_plain: str | None = None
    if conn.ci_token_hash is None:
        token_plain = mint_token()
        conn.ci_token_hash = hash_token(token_plain)  # store the hash, never plaintext
        db.commit()

    return _setup_out(project, token_plain, db)


def rotate_ci_token(db: Session, project_id: int, current_user: User) -> CiSetupOut:
    """Issue a FRESH per-project ingest token, replacing any existing one. The
    recovery path for a token that was minted but never copied (mint-once means it
    can't be re-shown) — the old token stops working immediately. Owner-scoped; needs
    an existing Jenkins connection."""
    conn = _require_connected(db, project_id, current_user)
    project = db.get(Project, project_id)
    token_plain = mint_token()
    conn.ci_token_hash = hash_token(token_plain)
    db.commit()
    return _setup_out(project, token_plain, db)


def agent_config(db: Session, project: Project) -> AgentConfigOut:
    """HLD §3b.1: the agent's run-time configuration for a project, under the CI
    token (the caller resolved `project` through `require_project_token`, so an
    unknown project is already a 404 and a bad token a 401). The selected option's
    runtime-config row is served verbatim — a bare provider model id and the NAME of
    the credential variable (never a value). Review preferences travel for the review
    task only; a security project gets `null` whatever is stored."""
    runtime = _selected_runtime_config(db, project)
    task_type = project.task_type
    return AgentConfigOut(
        project_id=project.id,
        task=agent_task_for(task_type),
        task_type=task_type,
        model=AgentConfigModel(
            name=runtime.model.name,
            provider=runtime.provider,
            provider_model_id=runtime.provider_model_id,
            auth_mode=runtime.auth_mode,
            credential_env_var=runtime.credential_env_var,
        ),
        review_preferences=project.review_preferences if task_type == CI_REVIEW else None,
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

    # Savings engine (S12): cost this run on the SELECTED model (the project's pick)
    # vs the project's BASELINE model, both already concrete ids — using each model's
    # split input/output prices. Deterministic, no LLM, zero tokens. An unpriced model
    # leaves the trio NULL (compute_savings returns None) without failing the ingest.
    # cache_read_tokens is deliberately NOT in this call (HLD §8).
    selected_model_id = _resolve_model_id(db, project)
    actual_cost, baseline_cost, savings = compute_savings(
        payload.tokens_in,
        payload.tokens_out,
        price_for(db, selected_model_id),
        price_for(db, project.baseline_model_id),
    )

    run = CiRun(
        project_id=project.id,
        jenkins_build_id=payload.jenkins_build_id,
        model_id=selected_model_id,
        task=project.task_type,  # the run records the project's task (one vocabulary)
        tokens_in=payload.tokens_in,
        tokens_out=payload.tokens_out,
        cache_read_tokens=payload.cache_read_tokens,
        actual_cost=actual_cost,
        baseline_cost=baseline_cost,
        savings=savings,
        gate=payload.gate,  # audit trail of the agent's pass/fail decision
        gate_reason=payload.gate_reason,
        # quality_ok (S13, acceptance-rate gate) intentionally left null.
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
                cwe=f.cwe,
            )
        )
    db.commit()
    db.refresh(run)

    # Observability (S16): fold the AGENT's token usage into the backend's /metrics
    # under purpose="agent". The agent runs in the user's CI (no scrape there), so its
    # tokens reach our Prometheus only via this ingest. Metrics ONLY — /ci-runs stays
    # deterministic: no LLM call, no diff, no token-cap, and no misleading log line.
    # `model` is the agent-reported provider model string; provider is BYOK (unknown).
    record_llm_metrics(
        LLMObservation(
            purpose="agent",
            model=payload.model,
            tokens_in=payload.tokens_in,
            tokens_out=payload.tokens_out,
            status="ok",
        )
    )

    return CiRunOut(
        id=run.id,
        project_id=run.project_id,
        jenkins_build_id=run.jenkins_build_id,
        model_id=run.model_id,
        task=run.task,
        tokens_in=run.tokens_in,
        tokens_out=run.tokens_out,
        cache_read_tokens=run.cache_read_tokens,
        actual_cost=run.actual_cost,
        baseline_cost=run.baseline_cost,
        savings=run.savings,
        gate=run.gate,
        gate_reason=run.gate_reason,
        findings_count=len(payload.findings),
    )
