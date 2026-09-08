#!/usr/bin/env python3
"""Stub `opencode` that emits a realistic `--format json` event stream. $0.

Shape matches OpenCode 1.18.20 (and what RealVuln's run_agentic.py parses):
`step_finish` parts carry cost + tokens {input, output, total, cache{read,write}},
`tool_use` parts carry the tool + its input, `text` parts carry the answer. Grown
from the P38b spike's stub (docs/showcase/p38b-runtime-spike/fake-opencode.py).

Env knobs (all optional):
  FAKE_MODE           success (default) | empty | refusal | malformed | prose_fence |
                      braces | fail | sequence
  FAKE_SEQUENCE       comma list of modes, one per invocation (needs FAKE_COUNTER_FILE)
  FAKE_COUNTER_FILE   file holding the invocation count (also lets tests count calls)
  FAKE_ARGV_FILE      where to record argv (tests assert -m and the prompt)
  FAKE_RESULTS_FILE   Semgrep JSON to emit as the answer (golden fixtures)
  FAKE_STEPS          steps before finishing (default 3)
  FAKE_STEP_TOKENS    input tokens per step (output = 1/10)   (default 1000)
  FAKE_CACHE_READ     cache-read tokens per step               (default 4000)
  FAKE_DELAY          seconds to sleep between steps           (default 0)
"""
from __future__ import annotations

import json
import os
import sys
import time

SID = "ses_fake0000000000000000000000"


def _emit(obj: dict) -> None:
    print(json.dumps({"sessionID": SID, **obj}), flush=True)


def _bump_counter() -> int:
    path = os.environ.get("FAKE_COUNTER_FILE")
    if not path:
        return 1
    n = 0
    try:
        with open(path) as fh:
            n = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        n = 0
    n += 1
    with open(path, "w") as fh:
        fh.write(str(n))
    return n


def main() -> int:
    call_no = _bump_counter()
    if os.environ.get("FAKE_ARGV_FILE"):
        with open(os.environ["FAKE_ARGV_FILE"], "a") as fh:
            fh.write(json.dumps(sys.argv[1:]) + "\n")

    mode = os.environ.get("FAKE_MODE", "success")
    if mode == "replay":
        # Byte-for-byte replay of a REAL recorded `opencode run --format json` stream
        # (FAKE_REPLAY_FILE) — proves the parser against the genuine event shape.
        with open(os.environ["FAKE_REPLAY_FILE"]) as fh:
            sys.stdout.write(fh.read())
        sys.stdout.flush()
        return 0
    if mode == "sequence":
        seq = [s.strip() for s in os.environ.get("FAKE_SEQUENCE", "success").split(",")]
        mode = seq[min(call_no - 1, len(seq) - 1)]

    if mode == "fail":
        # OpenCode dying before the model answers (invalid key, bad model id…): no
        # text, non-zero exit, an error on stderr.
        print('{"name":"ProviderAuthError","data":{"message":"401 Unauthorized"}}', file=sys.stderr)
        return 1

    steps = int(os.environ.get("FAKE_STEPS", "3"))
    per = int(os.environ.get("FAKE_STEP_TOKENS", "1000"))
    cache_read = int(os.environ.get("FAKE_CACHE_READ", "4000"))
    delay = float(os.environ.get("FAKE_DELAY", "0"))

    files = ["/workspace/app/main.py", "/workspace/app/config.py"]
    for i in range(steps):
        time.sleep(delay)
        _emit({"type": "step_start", "part": {"type": "step-start"}})
        _emit({"type": "tool_use", "part": {
            "type": "tool", "tool": "read",
            "state": {"status": "completed", "input": {"filePath": files[i % len(files)]}},
        }})
        _emit({"type": "step_finish", "part": {
            "type": "step-finish", "reason": "tool-calls",
            "cost": 0.0001 * (i + 1),
            "tokens": {"input": per, "output": per // 10,
                       "total": per + per // 10 + cache_read,   # total INCLUDES cache reads
                       "reasoning": 0,
                       "cache": {"read": cache_read, "write": 0}},
        }})

    if mode == "refusal":
        text = ("I cannot fulfill this request. Performing a security audit to identify "
                "exploitable vulnerabilities could facilitate harmful activity.")
    elif mode == "malformed":
        # The DeepSeek failure mode: the audit is done, then an unescaped quote in a
        # message breaks the JSON. Not repairable deterministically → retry.
        text = ('{"version":"1.0.0","results":[{"check_id":"x","path":"app/main.py",'
                '"start":{"line":1},"extra":{"message":"uses __globals__["os"]","severity":"ERROR"}}]}')
    else:
        if os.environ.get("FAKE_RESULTS_FILE"):
            with open(os.environ["FAKE_RESULTS_FILE"]) as fh:
                payload = json.load(fh)
        elif mode == "empty":
            payload = {"version": "1.0.0", "results": []}
        else:
            payload = {"version": "1.0.0", "results": [{
                "check_id": "python.security.injection.sql-injection",
                "path": "app/main.py",
                "start": {"line": 42, "col": 1}, "end": {"line": 42, "col": 40},
                "extra": {"message": "User input concatenated into a SQL query.",
                          "severity": "ERROR",
                          "metadata": {"cwe": ["CWE-89: SQL Injection"], "confidence": "HIGH"}},
            }]}
        body = json.dumps(payload)
        if mode == "prose_fence":
            text = f"Here are my findings after auditing every file:\n\n```json\n{body}\n```\n\nLet me know if you need more detail."
        elif mode == "braces":
            payload["results"][0]["extra"]["message"] = (
                "SSTI: /?name={{7*7}} renders 49; {{lipsum.__globals__['os'].popen('id')}} gives RCE"
            )
            text = "Audit complete.\n\n" + json.dumps(payload)
        else:
            text = body

    _emit({"type": "text", "part": {"type": "text", "text": text}})
    _emit({"type": "step_finish", "part": {
        "type": "step-finish", "reason": "stop", "cost": 0.0,
        "tokens": {"input": 0, "output": 0, "total": 0, "cache": {"read": 0, "write": 0}},
    }})
    return 0


if __name__ == "__main__":
    sys.exit(main())
