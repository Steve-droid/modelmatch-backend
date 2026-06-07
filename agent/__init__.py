"""ModelMatch CI code-review agent (the product's proof).

A standalone image (built from this repo) that runs in the *user's* Jenkins: it
reviews the PR diff for security + style issues via the provider-agnostic LLMClient
(BYOK), emits findings + token usage as JSON, and applies a pass/fail gate — all in
CI. It never edits the repo. Shares app.llm + the findings contract with the backend
(direct import, no cross-repo drift).
"""
