# ModelMatch CI code-review agent

The product's **proof**: a standalone image (built from this repo) that runs in the
**user's** Jenkins, reviews the PR diff for **security + style** via the
provider-agnostic `LLMClient` (BYOK), and emits findings + token usage as JSON with a
**pass/fail gate** — all in CI. It **never edits the repo**.

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

## Config (env)

| var | default | meaning |
|---|---|---|
| `LLM_CLIENT` | `fake` | provider seam (`fake` now; `anthropic`/`gemini`/`bedrock` in v0.10.1) |
| `AGENT_MODEL` | `fake-model` | model id to call + report (BYOK) |
| `AGENT_MAX_TOKENS` | `1024` | per-call output cap |
| `AGENT_TOKEN_CEILING` | `100000` | abort if cumulative tokens exceed |
| `AGENT_FAIL_SEVERITIES` | `high,critical` | severities that fail the build |
