# ModelMatch CI code-review agent

The product's **proof**: a standalone image (built from this repo) that runs in the
**user's** Jenkins, reviews the PR diff for **security + style** via the
provider-agnostic `LLMClient` (BYOK), and emits findings + token usage as JSON with a
**pass/fail gate** — all in CI. It **never edits the repo**.

> How the agent fits the whole product — the metadata-only Jenkins connection, the two Jenkins
> credentials it reads, and the savings/quality loop it feeds — is in the
> [Runbook & Demo Walkthrough](../docs/runbook.md) (§6).

## Run

```bash
# build (context = repo root)
docker build -f agent/Dockerfile -t modelmatch-agent .

# review a diff (stdin); offline fake client
git diff origin/main...HEAD | docker run -i --rm -e LLM_CLIENT=fake modelmatch-agent

# or locally
git diff origin/main...HEAD | python -m agent
```

## Output (stdout)

```json
{"findings":[{"severity":"high","category":"security","file":"app/x.py","line":10,
  "message":"..."}],"tokensIn":1234,"tokensOut":56,"model":"...","gate":"fail",
  "gateReason":"1 finding(s) at blocking severity (high)"}
```

## Exit codes (the gate, in CI)

| code | meaning |
|---|---|
| 0 | gate **pass** |
| 1 | gate **fail** (blocking findings) |
| 2 | malformed model output |
| 3 | token ceiling exceeded |
| 4 | config / input / LLM client / provider failure (structured JSON on stderr) |

## Config (env)

| var | default | meaning |
|---|---|---|
| `LLM_CLIENT` | `fake` | provider seam: `fake` \| `anthropic` \| `gemini` \| `bedrock` |
| `AGENT_AWS_REGION` | unset | optional Bedrock region override; otherwise the agent reads `AWS_DEFAULT_REGION` / `AWS_REGION` |
| `AGENT_MODEL` | `fake-model` | model id to call + report (BYOK) |
| `AGENT_MAX_TOKENS` | `1024` | per-call output cap |
| `AGENT_TOKEN_CEILING` | `100000` | abort if estimated/actual tokens exceed (preflight + post-call) |
| `AGENT_FAIL_SEVERITIES` | `high,critical` | severities that fail the build |

### Providers (creds read by each SDK — never stored by us)

| `LLM_CLIENT` | creds | notes |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | BYOK; demo Haiku live / Sonnet computed |
| `gemini` | `GEMINI_API_KEY` / `GOOGLE_API_KEY` | BYOK free-tier (`google-genai` SDK) — **non-confidential code only** (trains on inputs) |
| `bedrock` | AWS creds via IAM / profile | region from `AWS_DEFAULT_REGION` / `AWS_REGION`; no static keys |

Real providers need the `llm` extra (in the image already; locally `uv sync --extra llm`).
Live smoke: `RUN_LLM_LIVE=1 ANTHROPIC_API_KEY=... uv run pytest tests/test_llm_live.py`.
