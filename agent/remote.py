"""The agent's two calls to the Driftplain API (HLD §3b.1), both under the CI token.

- `fetch_agent_config`  GET  /projects/{id}/agent-config  → task, model, preferences
- `post_ci_run`         POST /projects/{id}/ci-runs       → the run + findings + tokens

Plain urllib: no new dependency in the image, and nothing here ever prints, logs or
raises with the token in it. Failures surface as RemoteError with the status code
and (for 4xx) the API's `detail`, never the request headers.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from agent.errors import AgentConfigError

TASKS = ("review", "security")


class RemoteError(AgentConfigError):
    """The API could not be reached or answered with an error."""


@dataclass(frozen=True)
class RemoteModel:
    name: str
    provider: str
    provider_model_id: str
    auth_mode: str
    credential_env_var: str | None


@dataclass(frozen=True)
class RemoteConfig:
    project_id: int
    task: str
    task_type: str | None
    model: RemoteModel
    review_preferences: str | None


def _request(
    method: str, url: str, token: str, timeout: int, body: dict | None = None
) -> Any:
    data = None
    headers = {"X-CI-Token": token, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https/http API URL from config)
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = _safe_detail(exc)
        raise RemoteError(f"{method} {_path(url)} → HTTP {exc.code}{detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RemoteError(f"{method} {_path(url)} failed: {type(exc).__name__}: {reason}") from None
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise RemoteError(f"{method} {_path(url)} → non-JSON response") from None


def _path(url: str) -> str:
    # Only the path in error messages — the host is fine too, but keep it short.
    return url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url


def _safe_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
        detail = body.get("detail") if isinstance(body, dict) else None
    except Exception:
        return ""
    if not detail:
        return ""
    return f" ({str(detail)[:200]})"


def fetch_agent_config(api_url: str, project_id: int, token: str, timeout: int = 15) -> RemoteConfig:
    url = f"{api_url.rstrip('/')}/projects/{project_id}/agent-config"
    body = _request("GET", url, token, timeout)
    try:
        task = body["task"]
        m = body["model"]
        model = RemoteModel(
            name=str(m["name"]),
            provider=str(m["provider"]),
            provider_model_id=str(m["providerModelId"]),
            auth_mode=str(m.get("authMode") or "api_key"),
            credential_env_var=m.get("credentialEnvVar") or None,
        )
    except (KeyError, TypeError) as exc:
        raise RemoteError(f"agent-config response missing field: {exc}") from None
    if task not in TASKS:
        raise RemoteError(f"agent-config task {task!r} is not one of {TASKS}")
    prefs = body.get("reviewPreferences")
    return RemoteConfig(
        project_id=int(body.get("projectId", project_id)),
        task=task,
        task_type=body.get("taskType"),
        model=model,
        review_preferences=str(prefs) if prefs else None,
    )


def post_ci_run(
    api_url: str, project_id: int, token: str, payload: dict, timeout: int = 15
) -> Any:
    url = f"{api_url.rstrip('/')}/projects/{project_id}/ci-runs"
    return _request("POST", url, token, timeout, body=payload)


def build_ci_run_payload(result_json: dict, build_id: str) -> dict:
    """What we POST: the backend's `CiRunIngest` contract (extra=forbid).

    tokensIn/tokensOut are OpenCode's `input` + `output` sums — never `total`
    (it includes cache reads). cacheReadTokens is stored separately, never priced;
    review runs have no captured cache count and send null.
    """
    return {
        "findings": [
            {
                "severity": f["severity"],
                "category": f["category"],
                "file": f["file"],
                "line": f.get("line"),
                "message": f["message"],
                "cwe": f.get("cwe"),
            }
            for f in result_json["findings"]
        ],
        "tokensIn": result_json["tokensIn"],
        "tokensOut": result_json["tokensOut"],
        "cacheReadTokens": result_json.get("cacheReadTokens"),
        "model": result_json["model"],
        "gate": result_json["gate"],
        "gateReason": result_json.get("gateReason"),
        "jenkinsBuildId": build_id,
    }
