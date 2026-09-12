"""The security task: a bounded OpenCode agentic loop over a READ-ONLY checkout.

Drives `opencode run --format json` with RealVuln's auditor prompt (bundled
verbatim, so our results stay comparable to the published v2.1 scores), streams
its JSON event log, enforces three independent ceilings, extracts the
Semgrep-shaped findings, maps them to the Driftplain finding shape (with a CWE)
and applies the gate — any CRITICAL finding fails the stage.

Folded in from the P38b spike (`docs/showcase/p38b-runtime-spike/run_security_scan.py`)
minus the experiments (sweep passes, all-files scaffold). Everything the spike
measured still holds:

- usage comes from `step_finish` events: `tokens.input` / `.output` / `.cache.read`.
  `tokens.total` INCLUDES cache reads and is never what we post.
- the child runs in its own process group so a ceiling kills the whole tree
  (OpenCode spawns node workers + tool subprocesses).
- stdout is consumed as it streams, so token/step ceilings abort mid-flight.
- braces inside finding messages (`{{7*7}}`) are normal; the extractor is
  string-aware. An unescaped quote is a retry, not a repair.
- a refusal is prose, never JSON; it is exit 3, never "clean".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent.config import AgentConfig
from agent.errors import (
    AgentConfigError,
    CeilingExceeded,
    MalformedFindings,
    ModelRefused,
    ProviderError,
    looks_like_refusal,
)
from agent.review import apply_gate, _log_agent_call
from agent.schemas import AgentFinding, AgentRunResult

DEFAULT_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "security-auditor.txt"

# Bounds that mirror the ingest's (app/schemas/ci.py) so a POST never 422s on a
# model-authored string.
MAX_MESSAGE_LEN = 4000
MAX_FILE_LEN = 1024


# ---------------------------------------------------------------- prompt


def build_task(system_prompt: str) -> str:
    """RealVuln's auditor prompt, kept verbatim, plus their step scaffold.

    The SYSTEM PROMPT is never reworded: keeping it identical is what makes our run
    comparable to the published v2.1 scores. The four numbered steps are the
    scaffold RealVuln's runner appends in run_agentic.py (kept verbatim too).
    """
    return (
        f"{system_prompt}\n\n"
        "The repository to audit is in the current directory.\n\n"
        "You MUST follow these steps IN ORDER:\n"
        "1. List all Python files in this repo\n"
        "2. Read each Python file to understand the code\n"
        "3. Look for SQL injection, XSS, command injection, path traversal, etc.\n"
        "4. ONLY after reading ALL files, output your findings\n\n"
        "CRITICAL: The example JSON in the prompt above is just a FORMAT TEMPLATE.\n"
        "Your findings must reference actual files and line numbers from THIS repo.\n"
        "Output ONLY the JSON findings object at the end - no markdown fences."
    )


def load_system_prompt(path: str | None) -> str:
    p = Path(path) if path else DEFAULT_PROMPT_FILE
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentConfigError(f"cannot read the auditor prompt: {type(exc).__name__}") from None


# ---------------------------------------------------------------- workspace

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache"}


def list_repo_files(workspace: str) -> list[str]:
    """Every text file in the checkout, relative to it. Used for the empty-mount
    guard and the coverage number on stderr."""
    out: list[str] = []
    for root, dirs, names in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for n in names:
            path = os.path.join(root, n)
            try:
                with open(path, "rb") as fh:
                    if b"\0" in fh.read(2048):
                        continue  # binary
            except OSError:
                continue
            out.append(os.path.relpath(path, workspace))
    return sorted(out)


def require_workspace(workspace: str) -> list[str]:
    """The bind is resolved by the HOST daemon: a containerised Jenkins whose
    workspace path differs on the host gets an EMPTY directory, not an error — and
    an agent that audits nothing reports a clean scan. Refuse to run on nothing."""
    if not os.path.isdir(workspace):
        raise AgentConfigError(f"workspace {workspace} does not exist (is it mounted?)")
    files = list_repo_files(workspace)
    if not files:
        raise AgentConfigError(
            f"workspace {workspace} holds no files to audit — check the bind mount "
            "(a containerised Jenkins must mount the same HOST path)"
        )
    return files


def files_read(events: list[dict], workspace: str) -> set[str]:
    """Which files the agent actually opened, from its `read` tool calls."""
    seen: set[str] = set()
    for e in events:
        part = e.get("part", {}) or {}
        if part.get("tool") != "read":
            continue
        fp = ((part.get("state", {}) or {}).get("input", {}) or {}).get("filePath")
        if fp:
            seen.add(os.path.relpath(fp, workspace) if fp.startswith("/") else fp)
    return seen


# ---------------------------------------------------------------- the loop


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    total_reported: int = 0   # OpenCode's `total` — INCLUDES cache reads; never posted
    cache_read: int = 0
    cache_write: int = 0
    cost_reported: float = 0.0
    steps: int = 0

    def add(self, other: "Usage") -> None:
        for k in ("input", "output", "total_reported", "cache_read", "cache_write", "cost_reported", "steps"):
            setattr(self, k, getattr(self, k) + getattr(other, k))

    @property
    def billable(self) -> int:
        """What the ceiling and `/ci-runs` count: input + output. Cache reads are
        billed by providers at a fraction of the input rate and are reported
        separately (stderr) until the ingest carries `cacheReadTokens`."""
        return self.input + self.output


@dataclass
class StreamResult:
    returncode: int
    usage: Usage
    text: str
    elapsed: float
    ceiling: CeilingExceeded | None
    events: list[dict]
    session_id: str
    stderr_tail: str
    tool_calls: int = 0


def _kill_tree(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def stream_opencode(
    cmd: list[str], cwd: str, *, max_seconds: int, max_tokens: int, max_steps: int
) -> StreamResult:
    """Run opencode, consuming its JSON event stream live so ceilings can abort it."""
    proc = subprocess.Popen(  # noqa: S603 — argv list, no shell
        cmd, cwd=cwd, text=True, bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,  # own process group → a ceiling kills the whole tree
        env={**os.environ, "NO_COLOR": "1"},
    )
    started = time.time()
    usage = Usage()
    text_out: list[str] = []
    events: list[dict] = []
    tripped: CeilingExceeded | None = None
    session_id = ""
    tool_calls = 0

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(event)
            if not session_id and event.get("sessionID"):
                session_id = str(event["sessionID"])

            etype = event.get("type")
            part = event.get("part", {}) or {}
            if etype == "text":
                text_out.append(part.get("text", "") or "")
            elif etype == "tool_use":
                tool_calls += 1
            elif etype == "step_finish":
                usage.steps += 1
                usage.cost_reported += float(part.get("cost", 0) or 0)
                tok = part.get("tokens", {}) or {}
                usage.input += int(tok.get("input", 0) or 0)
                usage.output += int(tok.get("output", 0) or 0)
                usage.total_reported += int(tok.get("total", 0) or 0)
                cache = tok.get("cache", {}) or {}
                usage.cache_read += int(cache.get("read", 0) or 0)
                usage.cache_write += int(cache.get("write", 0) or 0)

            if max_tokens and usage.billable > max_tokens:
                tripped = CeilingExceeded("token", f"{usage.billable} > {max_tokens}")
                break
            if max_steps and usage.steps > max_steps:
                tripped = CeilingExceeded("step", f"{usage.steps} > {max_steps}")
                break
            if max_seconds and (time.time() - started) > max_seconds:
                tripped = CeilingExceeded("wall-clock", f"{time.time() - started:.0f}s > {max_seconds}s")
                break

        if tripped is not None:
            _kill_tree(proc)
            rc = 124
        else:
            try:
                proc.wait(timeout=max(1, max_seconds - int(time.time() - started)))
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                tripped = CeilingExceeded("wall-clock", f"exceeded {max_seconds}s waiting for exit")
                rc = 124
        stderr_tail = ""
        try:
            if proc.stderr is not None:
                stderr_tail = proc.stderr.read()[-2000:]
        except (OSError, ValueError):
            pass
    finally:
        if proc.poll() is None:
            _kill_tree(proc)

    return StreamResult(
        returncode=rc, usage=usage, text="".join(text_out),
        elapsed=time.time() - started, ceiling=tripped, events=events,
        session_id=session_id, stderr_tail=stderr_tail, tool_calls=tool_calls,
    )


# ---------------------------------------------------------------- extraction

_FENCE_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)\n?```", re.DOTALL)


def _balanced_json_blocks(text: str) -> list[str]:
    """Top-level {...} blocks, ignoring braces inside JSON string literals.

    A naive brace counter fails on exactly the findings we care most about: a
    report describing server-side template injection quotes payloads like
    '{{7*7}}' inside the message string. Observed live on a DeepSeek run.
    """
    blocks: list[str] = []
    depth = 0
    start = None
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    blocks.append(text[start : i + 1])
    return blocks


# JSON permits only \" \\ \/ \b \f \n \r \t and \uXXXX. Models emit others — \' is
# the common one. An invalid escape means the backslash was never meant as one, so
# dropping it restores the character the model meant. Applied ONLY after a normal
# parse has failed, so well-formed output is never touched.
_BAD_ESCAPE = re.compile(r'\\([^"\\/bfnrtu])')


def _repair_json(text: str) -> str:
    return _BAD_ESCAPE.sub(r"\1", text)


def extract_results(text: str) -> list[dict] | None:
    """Pull the Semgrep-shaped `results` array out of the agent's final message.

    Models routinely ignore "output ONLY the JSON": a prose preamble, a markdown
    fence, or both. Try, in order of increasing tolerance: the whole string, every
    fenced block, then every balanced {...} block — last candidate first, since the
    findings object is the model's final word. Anything unparseable is rejected,
    never guessed at.
    """
    candidates: list[str] = [text.strip()]
    candidates += [m.group(1) for m in _FENCE_BLOCK.finditer(text)]
    candidates += _balanced_json_blocks(text)
    for cand in reversed(candidates):
        for attempt in (cand, _repair_json(cand)):
            try:
                data = json.loads(attempt)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                return data["results"]
    return None


# RealVuln severity + confidence → Driftplain severity. ERROR+HIGH is the only
# combination that fails the build (the gate is on `critical`).
def map_severity(sev: str, confidence: str) -> str:
    sev = (sev or "").upper()
    confidence = (confidence or "").upper()
    if sev == "ERROR":
        return "critical" if confidence == "HIGH" else "high"
    return {"WARNING": "medium", "INFO": "low"}.get(sev, "low")


def _relative_path(path: str, workspace: str) -> str:
    p = (path or "").strip().replace("\\", "/")
    if p.startswith("/"):
        p = os.path.relpath(p, workspace) if p.startswith(workspace.rstrip("/") + "/") else p.lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    return p[:MAX_FILE_LEN]


def to_findings(results: list[dict], workspace: str = "/workspace") -> tuple[list[AgentFinding], int]:
    """Semgrep rows → validated findings. Rows the ingest would reject (no path, no
    message, traversal) are DROPPED and counted, so one bad row never 422s the run."""
    findings: list[AgentFinding] = []
    dropped = 0
    for r in results:
        if not isinstance(r, dict):
            dropped += 1
            continue
        extra = r.get("extra", {}) or {}
        meta = extra.get("metadata", {}) or {}
        cwes = meta.get("cwe") or []
        cwe = str(cwes[0])[:200] if isinstance(cwes, list) and cwes else (str(cwes)[:200] if cwes else None)
        path = _relative_path(str(r.get("path", "")), workspace)
        message = str(extra.get("message", "") or "").strip()[:MAX_MESSAGE_LEN]
        line = (r.get("start", {}) or {}).get("line")
        if not path or not message or ".." in path.split("/"):
            dropped += 1
            continue
        findings.append(
            AgentFinding(
                severity=map_severity(str(extra.get("severity", "")), str(meta.get("confidence", ""))),
                category="security",
                file=path,
                line=int(line) if isinstance(line, int) and line >= 0 else None,
                message=message,
                cwe=cwe,
            )
        )
    return findings, dropped


# ---------------------------------------------------------------- the run


@dataclass
class SecurityDiagnostics:
    """Everything worth knowing about the run that is NOT part of the /ci-runs
    contract. Printed as one JSON line on stderr (no prompt, no code, no keys)."""

    model: str
    attempts: int = 0
    refusals: int = 0
    malformed: int = 0
    steps: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_reported_tokens: int = 0
    cost_usd_reported: float = 0.0
    wall_clock_seconds: float = 0.0
    files_read: int = 0
    files_total: int = 0
    findings_dropped: int = 0
    ceiling: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d.pop("extra")
        d["cost_usd_reported"] = round(self.cost_usd_reported, 6)
        d["wall_clock_seconds"] = round(self.wall_clock_seconds, 1)
        d.update(self.extra)
        return d


def _provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else "opencode"


def run_security(config: AgentConfig, model: str) -> tuple[AgentRunResult, SecurityDiagnostics]:
    """One security scan: up to `max_attempts` OpenCode runs sharing the ceilings.

    Retries BOTH refusal and unparseable output — both are transient and cost a few
    thousand tokens, far less than a wasted CI run. Every attempt's tokens count:
    the user's key paid for all of them, so that is what we report.
    """
    if "/" not in model:
        raise AgentConfigError("security model must be '<opencode-provider>/<model-id>'")
    if shutil.which(config.opencode_bin) is None:
        raise AgentConfigError(
            f"OpenCode runtime {config.opencode_bin!r} not found — the security task needs "
            "the modelmatch-agent-security image"
        )
    system_prompt = load_system_prompt(config.prompt_file)
    repo_files = require_workspace(config.workspace)
    task = build_task(system_prompt)
    diag = SecurityDiagnostics(model=model, files_total=len(repo_files))
    ceiling_tokens = config.effective_token_ceiling("security")

    totals = Usage()
    elapsed = 0.0
    all_events: list[dict] = []
    last: StreamResult | None = None
    results: list[dict] | None = None
    refused = False
    provider = _provider_of(model)

    for _ in range(max(1, config.max_attempts)):
        diag.attempts += 1
        cmd = [config.opencode_bin, "run", "--format", "json", "-m", model, task]
        run = stream_opencode(
            cmd, config.workspace,
            max_seconds=max(1, config.max_seconds - int(elapsed)),
            max_tokens=max(1, ceiling_tokens - totals.billable),
            max_steps=max(1, config.max_steps - totals.steps),
        )
        last = run
        totals.add(run.usage)
        elapsed += run.elapsed
        all_events.extend(run.events)
        diag.tool_calls += run.tool_calls

        if run.ceiling is not None:
            diag.ceiling = run.ceiling.which
            break

        text = run.text.strip()
        if run.returncode != 0 and not text:
            # OpenCode died before the model answered (auth, network, bad model id):
            # not a flake to retry on the user's key.
            _log_agent_call(model=model, tokens_in=totals.input, tokens_out=totals.output,
                            provider=provider, status="error", error_kind="ProviderError")
            diag.tokens_in, diag.tokens_out = totals.input, totals.output
            diag.wall_clock_seconds = elapsed
            raise _with_diag(ProviderError(
                f"opencode exited {run.returncode} with no output: {_scrub(run.stderr_tail)}"
            ), diag)

        refused = looks_like_refusal(text)
        if refused:
            diag.refusals += 1
            continue
        results = extract_results(text)
        if results is not None:
            break
        diag.malformed += 1

    assert last is not None
    diag.steps = totals.steps
    diag.tokens_in = totals.input
    diag.tokens_out = totals.output
    diag.cache_read_tokens = totals.cache_read
    diag.cache_write_tokens = totals.cache_write
    diag.total_reported_tokens = totals.total_reported
    diag.cost_usd_reported = totals.cost_reported
    diag.wall_clock_seconds = elapsed
    diag.files_read = len(files_read(all_events, config.workspace))

    if diag.ceiling is not None:
        assert last.ceiling is not None
        _log_agent_call(model=model, tokens_in=totals.input, tokens_out=totals.output,
                        provider=provider, status="error", error_kind=last.ceiling.log_kind)
        raise _with_diag(last.ceiling, diag)
    if refused:
        _log_agent_call(model=model, tokens_in=totals.input, tokens_out=totals.output,
                        provider=provider, status="error", error_kind="ModelRefused")
        raise _with_diag(ModelRefused(
            f"the model declined the audit on {diag.refusals} of {diag.attempts} attempt(s) — "
            "nothing was scanned; this is NOT a clean result"
        ), diag)
    if results is None:
        _log_agent_call(model=model, tokens_in=totals.input, tokens_out=totals.output,
                        provider=provider, status="error", error_kind="MalformedFindings")
        raise _with_diag(MalformedFindings(
            f"no parseable Semgrep JSON after {diag.attempts} attempt(s) "
            f"({diag.malformed} malformed, {diag.refusals} refused)"
        ), diag)

    findings, dropped = to_findings(results, config.workspace)
    diag.findings_dropped = dropped
    _log_agent_call(model=model, tokens_in=totals.input, tokens_out=totals.output,
                    provider=provider, latency_ms=int(elapsed * 1000), status="ok")
    gate, reason = apply_gate(findings, config.effective_fail_severities("security"))
    return (
        AgentRunResult(
            findings=findings,
            tokens_in=totals.input,
            tokens_out=totals.output,
            model=model,
            gate=gate,
            gate_reason=reason,
        ),
        diag,
    )


def _with_diag(exc: Exception, diag: "SecurityDiagnostics") -> Exception:
    """Non-pass outcomes still carry the run's numbers to stderr (attempts,
    refusals, tokens, ceiling) — the CLI prints them before the structured error."""
    exc.diagnostics = diag  # type: ignore[attr-defined]
    return exc


_CRED_NAMES = (
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
    "GEMINI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "MODELMATCH_CI_TOKEN",
)


def _scrub(text: str) -> str:
    """First line of OpenCode's stderr, with any credential VALUE present in the
    environment replaced — belt and braces; OpenCode does not echo keys, but a
    stderr tail is exactly the kind of text that ends up in a Jenkins log."""
    t = (text or "").strip().splitlines()
    first = t[0] if t else ""
    for name in _CRED_NAMES:
        v = os.environ.get(name)
        if v and len(v) >= 8 and v in first:
            first = first.replace(v, "***")
    return first[:300]


def print_diagnostics(diag: SecurityDiagnostics) -> None:
    print(json.dumps({"agent_security_summary": diag.as_dict()}), file=sys.stderr)
