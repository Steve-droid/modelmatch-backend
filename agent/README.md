# ModelMatch CI agent — two tasks, two images, one codebase

The product's **proof**: a standalone image (built from this repo) that runs in the
**user's** Jenkins on the **user's** key (BYOK), does real LLM work on their code, and
sets the **pass/fail gate in CI**. It **never edits the repo**. Since `1.1.0` there are two
tasks, selected per project, each shipped as its own image from the same `agent/` code:

| Task | Image (Dockerfile) | What runs | Gate | Runtime |
|---|---|---|---|---|
| **`review`** | `modelmatch-agent` (`agent/Dockerfile`), **172 MB** | one LLM call over the PR **diff**, security + style findings | any **high/critical** finding fails | our provider-agnostic `LLMClient` (Anthropic · Gemini · Bedrock) |
| **`security`** | `modelmatch-agent-security` (`agent/Dockerfile.security`), **427 MB** | an **OpenCode agentic loop** over a **read-only checkout**, RealVuln's auditor prompt (bundled verbatim), Semgrep-shaped findings **with a CWE** | any **critical** finding fails | the OpenCode 1.18.20 **binary** (DeepSeek · OpenAI · Anthropic · Gemini · Bedrock) |

Each image bakes `AGENT_IMAGE_TASK`; running a project of the other task against it is a
config error (exit `4`) naming the right image — never an `ImportError` (the review image
carries no OpenCode, the security image carries no provider SDKs).

**Why two images, and how they stay small** (amd64, measured 2026-09-08; the single
combined image was 997 MB, the v1 review image 291 MB):

- the venv holds **only the agent's dependency closure**, resolved against the project's
  frozen `uv.lock` as *constraints* (same versions, but sqlalchemy / psycopg / alembic /
  fastapi / uvicorn / sqlglot… never enter an image): review 44 MB, security 9 MB;
- botocore's ~430 service models are pruned to `bedrock-runtime` / `bedrock` / `sts`;
- OpenCode is one bun-compiled binary fetched from the pinned GitHub release and
  **sha256-verified** in a throw-away stage — no Node, no npm, no NodeSource repo, no
  curl/gnupg in the runtime. The binary is 184 MB on disk, the floor of that image;
- no bytecode (the agent runs once per CI job), no pip/ensurepip (nothing installs at
  run time), no shell entrypoint (the credential remap is Python), `dist-info` kept so
  Trivy can read it. Both images share the same `python:3.12-slim` base layers.

> How the agent fits the whole product — the metadata-only Jenkins connection, the two
> Jenkins credentials it reads, and the savings/quality loop it feeds — is in the
> [HLD §3b](../../docs/planning/hld.md) and the [Runbook](../docs/runbook.md) (§6). The
> run-time config contract with the backend is **HLD §3b.1**.

## Run

```bash
# build (context = repo root; Jenkins agents are amd64, the M4 laptop is arm64)
docker buildx build --platform linux/amd64 -f agent/Dockerfile          -t modelmatch-agent .
docker buildx build --platform linux/amd64 -f agent/Dockerfile.security -t modelmatch-agent-security .

# review a diff (stdin); offline fake client
git diff origin/main...HEAD | docker run -i --rm -e LLM_CLIENT=fake modelmatch-agent

# security scan of a checkout, read-only, sandboxed (BYOK key passed BY NAME)
docker run --rm -v "$PWD:/workspace:ro" \
  --cap-drop ALL --security-opt no-new-privileges --memory 2g --cpus 2 --tmpfs /tmp:size=256m \
  -e AGENT_MODEL=deepseek/deepseek-v4-flash -e DEEPSEEK_API_KEY \
  modelmatch-agent-security

# or locally (review)
git diff origin/main...HEAD | python -m agent
```

## Output (stdout)

One JSON object — the `/ci-runs` contract plus `cwe` per finding (`null` for review findings):

```json
{"findings":[{"severity":"critical","category":"security","file":"app/main.py","line":38,
  "message":"…","cwe":"CWE-1336: Server-Side Template Injection"}],
 "tokensIn":11501,"tokensOut":2618,"model":"deepseek/deepseek-v4-flash",
 "gate":"fail","gateReason":"1 finding(s) at blocking severity (critical)"}
```

`tokensIn` / `tokensOut` are what the user's key paid for: for the security loop, the sum
of OpenCode's `step_finish` **`input` + `output`** over every attempt — **never `tokens.total`**,
which includes cache reads and overstated one measured run threefold. Cache reads, steps,
attempts, refusals, coverage and wall-clock go to **stderr** as one
`{"agent_security_summary": …}` line. Provider-reported cost is never posted: both sides of
the savings figure are `tokens × catalog price`.

Everything else on stderr is structured too (the per-request LLM log line, `agent_note`
lines, and on failure the **last line** `{"error": …, "detail": …}`) — never a traceback,
never the diff, prompt or a key.

## Exit codes — one table for both tasks (the gate, in CI)

| code | meaning | pass? |
|---|---|---|
| `0` | gate **pass** / clean scan | **yes — the only pass** |
| `1` | gate **fail** — a blocking finding (review: high/critical · security: critical) | no — **the stage fails** |
| `2` | the model's output did not parse (security: after `AGENT_MAX_ATTEMPTS`) | no |
| `3` | the model **refused** the task — **never a clean result** | no |
| `4` | config / input / credential / API-fetch / provider failure | no |
| `124` | a ceiling aborted the run (tokens, steps or wall-clock) | no |

**Changed in 1.1.0:** v1 used `3` for the token ceiling and had no refusal code. The ceiling
moved to `124` so `3` can carry the one outcome a security gate must never mistake for
clean: a refusal returns no findings, and a stage that treated it as "no vulnerabilities
found" would go green over code the model never examined. A stage that branches on the code
should print `3` in its own words. Only `0` is a pass.

## Config (env)

### Run-time config from the API (HLD §3b.1) — the snippet stays task-agnostic

| var | meaning |
|---|---|
| `MODELMATCH_API_URL` | e.g. `https://api.<ip>.sslip.io` |
| `MODELMATCH_PROJECT_ID` | the project |
| `MODELMATCH_CI_TOKEN` | the per-project CI token (Jenkins `Secret text` `modelmatch-ci-token`, pass `-e` **by name**) |
| `MODELMATCH_POST_RESULT` | `true` → the agent POSTs `/ci-runs` itself (uses `BUILD_TAG`, pass it with `-e BUILD_TAG`); the result JSON is still printed |

With all three set, the agent `GET`s `/projects/{id}/agent-config` and the API is
**authoritative** for the task, the model (`provider` + bare `providerModelId` → composed
per runtime) and the review preferences; it also **pre-flights the credential** the config
names (`credentialEnvVar`) and exits `4` *before spending a token* if it is missing. Any
fetch failure is exit `4` — the agent never guesses a task. A snippet must either let the
agent post **or** post with curl, never both (the second POST is a `409`).

### Env fallback (local / offline / fixtures — ignored when the API is configured)

| var | default | meaning |
|---|---|---|
| `MODELMATCH_TASK` | `review` (the security image bakes `security`) | `review` \| `security` |
| `AGENT_MODEL` | `fake-model` | review: the SDK's model id · security: the full OpenCode string `provider/model` |
| `LLM_CLIENT` | `fake` | review provider seam: `fake` \| `anthropic` \| `gemini` \| `bedrock` |
| `MODELMATCH_REVIEW_PREFERENCES` | unset | review: free text appended to the system prompt (≤ 2000 chars, control chars stripped) |
| `AGENT_AWS_REGION` | unset | optional Bedrock region override; otherwise `AWS_DEFAULT_REGION` / `AWS_REGION` |

### Ceilings (both tasks; every one ABORTS — none is only an alert)

| var | default | meaning |
|---|---|---|
| `AGENT_TOKEN_CEILING` | review `100000` · security `1000000` | cumulative `input + output` tokens; the security loop is killed mid-flight when it trips |
| `AGENT_MAX_STEPS` | `40` | security: agent turns (`step_finish` events) |
| `AGENT_MAX_SECONDS` | `600` | security: wall-clock; the child runs in its own process group so the whole tree dies |
| `AGENT_MAX_ATTEMPTS` | `3` | security: retries **both** refusal and unparseable output. A default of the **image**, not the Jenkinsfile — do not regress to 1 |
| `AGENT_MAX_TOKENS` | `1024` | review: per-call output cap |
| `AGENT_FAIL_SEVERITIES` | review `high,critical` · security `critical` | severities that fail the build |

### Security task knobs

| var | default | meaning |
|---|---|---|
| `AGENT_WORKSPACE` | `/workspace` | the read-only checkout. An **empty** workspace is exit `4`, not a clean scan (see the trap below) |
| `AGENT_OPENCODE_BIN` | `opencode` | the runtime binary (`/usr/local/bin/opencode` in the security image); tests point it at `tests/agent/fixtures/fake-opencode.py` |
| `AGENT_PROMPT_FILE` | bundled `agent/prompts/security-auditor.txt` | RealVuln's auditor prompt (`sha256:3481f1432c23`), Apache-2.0 © kolega-ai. **Unreworded** — that is what keeps our scores comparable |

### Providers (creds read by each runtime — never stored by us)

| provider | review (`LLM_CLIENT`) | security (OpenCode `-m`) | credential |
|---|---|---|---|
| Anthropic | `anthropic` | `anthropic/<id>` | `ANTHROPIC_API_KEY` |
| Gemini | `gemini` | `google/<id>` | `GOOGLE_API_KEY` (the agent exports it as `GOOGLE_GENERATIVE_AI_API_KEY` for OpenCode) — **non-confidential code only** on the free tier |
| Bedrock | `bedrock` | `amazon-bedrock/<id>` | AWS creds via IAM / profile / IRSA; region from `AWS_DEFAULT_REGION` |
| DeepSeek | — (not review-runnable) | `deepseek/<id>` | `DEEPSEEK_API_KEY` |
| OpenAI | — (not review-runnable) | `openai/<id>` | `OPENAI_API_KEY` |

Real review providers need the `llm` extra (in the image already; locally `uv sync --extra llm`).
Live smoke: `RUN_LLM_LIVE=1 ANTHROPIC_API_KEY=... uv run pytest tests/test_llm_live.py`.

## The security stage (reference for the generated snippet)

Until `/ci-setup` emits it per task (P38e), this is the shape:

```groovy
stage('ModelMatch Security Analysis') {
  agent any
  environment {
    MODELMATCH_CI_TOKEN = credentials('modelmatch-ci-token')
    DEEPSEEK_API_KEY    = credentials('modelmatch-model-api-key')   // = the runtime config's credentialEnvVar
  }
  steps {
    sh '''
        set +e
        docker run --rm -v "$PWD:/workspace:ro" \\
          --cap-drop ALL --security-opt no-new-privileges --memory 2g --cpus 2 --tmpfs /tmp:size=256m \\
          -e MODELMATCH_API_URL=https://api.<ip>.sslip.io -e MODELMATCH_PROJECT_ID=7 \\
          -e MODELMATCH_CI_TOKEN -e MODELMATCH_POST_RESULT=true -e BUILD_TAG \\
          -e DEEPSEEK_API_KEY \\
          <registry>/modelmatch-agent-security:1.1.0 > result.json
        AGENT_RC=$?
        case "$AGENT_RC" in
          0)   echo "ModelMatch: scan completed, no blocking findings." ;;
          1)   echo "ModelMatch: CRITICAL finding - failing the stage." ;;
          2)   echo "ModelMatch: unparseable model output - NOT a clean scan." ;;
          3)   echo "ModelMatch: the model REFUSED to audit this code. Nothing was scanned. NOT a pass." ;;
          124) echo "ModelMatch: a run ceiling aborted the scan - NOT a clean scan." ;;
          *)   echo "ModelMatch: agent failed ($AGENT_RC) - treating as failure." ;;
        esac
        exit $AGENT_RC
    '''
  }
}
```

Sandbox posture: read-only `/workspace`, non-root (`appuser`, uid 10001), all capabilities
dropped, `no-new-privileges`, memory + CPU capped, no Docker socket. Network stays **on** —
the agent runs *inside* the container and needs egress to the provider API; that is the one
deliberate loosening, and it is why the workspace is read-only. `HOME` is writable because
OpenCode keeps its session store under `~/.local/share/opencode`.

## Traps this image fixes or guards (measured, not theoretical)

- **A top-level `app.py` shadowed the agent** (`ModuleNotFoundError: app.llm`) because the
  review stage runs `-v "$PWD:/work" -w /work` and the checkout became `sys.path[0]`. Five
  measured collisions at `1.0.12`: `agent`, `app`, `json`, `logging`, `types`. Fixed with
  **`PYTHONSAFEPATH=1`** in the image (verified with a checkout containing all five).
- **A user repo's `.env` could reconfigure the agent** for the same reason (cwd = their
  checkout). The agent no longer reads any `.env` file.
- **The workspace bind is resolved by the HOST daemon.** A containerised Jenkins whose
  workspace path differs on the host gets an *empty* directory, silently — and an agent
  that audits nothing reports a clean scan. The agent now refuses to run on an empty
  workspace (exit `4`). Align the paths (see `jenkins-local/README.md`).
- **`tokens.total` is not `input + output`** (it includes cache reads). Never posted.
- **A refusal is not a clean result.** Exit `3`, distinct, retried up to `AGENT_MAX_ATTEMPTS`.
- **Severity labels are not stable across runs** (same code, model and prompt: MEDIUM then
  HIGH). The security gate is on `critical` only (RealVuln `ERROR` + `HIGH` confidence);
  recorded in HLD §8, not "fixed" by prompt edits.

## Tests ($0)

`uv run pytest tests/agent tests/test_agent_review.py` — the security loop runs against
`tests/agent/fixtures/fake-opencode.py` (stub event stream: success / empty / refusal /
malformed / prose-fenced / braces / provider failure / sequences / **replay of a real
recorded DeepSeek run**), the config fetch + `/ci-runs` POST against an in-process stub of
the P38e API, the golden Semgrep fixtures (`sample-run-deepseek.json`, `sample-run-gemini.json`),
and the exit-code table. No provider is ever called; the one live run per release is a
manual, counted step.
