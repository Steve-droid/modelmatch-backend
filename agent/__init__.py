"""ModelMatch CI agent (the product's proof) — two tasks, one image.

A standalone image (built from this repo) that runs in the *user's* Jenkins on the
user's key (BYOK): `review` (one LLM call over the PR diff, security + style
findings) or `security` (an OpenCode agentic loop over a read-only checkout with
RealVuln's auditor prompt, Semgrep-shaped findings with a CWE). Both emit findings +
token usage as JSON and set the pass/fail gate in CI, under one exit-code table
(agent/errors.py). It never edits the repo. Shares app.llm + the findings contract
with the backend (direct import, no cross-repo drift); fetches its per-project
config from the API at run time (HLD §3b.1).
"""
