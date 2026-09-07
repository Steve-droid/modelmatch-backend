"""Deterministic RealVuln ingest — the `security_analysis` half of the catalog (P38c).

**There is no LLM on this path.** The S5b ingestion (#3) exists because model/benchmark
write-ups are unstructured prose; RealVuln is not. It publishes machine-readable
results, so extracting them with a model would be strictly worse: slower, costly, and
capable of inventing a score. This module is plain arithmetic over two files the
benchmark itself publishes, and every number it writes can be diffed against them.

Two upstream files, joined on RealVuln's own `scanner_slug`:

  * `reports/dashboard.json`      → scores (`aggregates[slug].micro.f3_score`) and the
                                    run metadata (tokens, cost, repos scored)
  * `llm-bench/config/models.yaml` → prices (`pricing.input_per_1m` / `output_per_1m`),
                                    provider, and the provider's own model id

Why F3: RealVuln ranks on a recall-weighted F-measure. For a security scanner a missed
vulnerability costs far more than a false positive, so F3 weights recall 3× precision.
It is the metric the benchmark leads with, and mixing it with F2 or raw precision in
one catalog would break the comparability rule (one metric per task type — P38c).

What is imported: general-purpose, model-driven **agentic** scanners only, via the
explicit `_SCANNERS` table below. Deliberately excluded, with reasons:

  * `kolega-*` — the benchmark author's own commercial products, not models a user
    could select. Including them would rank a vendor's product against raw models.
  * `semgrep` / `sonarqube` / `rowan` — static analysers, not LLMs. No token cost, no
    provider, nothing for a model recommender to say about them.

Idempotency mirrors S5b: the dashboard bytes are SHA-256'd into a `source_document`
row, so a re-run over unchanged input rewrites the same values and reports 0 changes.

Usage (offline by default — the checked-in copies under `data/catalog/` are the
reproducible source of the shipped seed):

    python -m app.catalog.ingest_realvuln                     # → the database
    python -m app.catalog.ingest_realvuln --write-seed        # → app/catalog/seed_data.json
    python -m app.catalog.ingest_realvuln --dashboard <url|path> --models <url|path>

A scheduled refresh that fetches upstream on a timer is a later slice; this module is
already the fetch-and-map half of it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from urllib.request import urlopen

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.service import upsert_catalog_row
from app.models import SourceDocument
from app.schemas.catalog import CatalogRowIn

# ---------------------------------------------------------------------------
# Constants describing the benchmark itself
# ---------------------------------------------------------------------------

BENCHMARK = "RealVuln"
METRIC = "f3_score"
TASK_TYPE = "security_analysis"

REPO_URL = "https://github.com/kolega-ai/Real-Vuln-Benchmark"
# The commit the checked-in copies under data/catalog/ were taken from. Pinned so the
# shipped figures are reproducible even after upstream moves on.
SOURCE_COMMIT = "248ffed0b940"
DASHBOARD_URL = f"{REPO_URL}/blob/main/reports/dashboard.json"
MODELS_URL = f"{REPO_URL}/blob/main/llm-bench/config/models.yaml"

_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "catalog"
DEFAULT_DASHBOARD = _DATA_DIR / "realvuln-v2.1-dashboard.json"
DEFAULT_MODELS = _DATA_DIR / "realvuln-v2.1-models.yaml"

BENCHMARK_NOTES = (
    "Recall-weighted F3 over 66 real Python repositories with 1,903 curated "
    "vulnerabilities (Apache-2.0). Agentic scanners run a multi-turn loop over a "
    "read-only checkout and emit Semgrep-shaped findings. F3 weights recall 3x "
    "precision: for a security scanner a missed vulnerability costs more than a "
    "false positive. Harness matters and is recorded per row — the same model "
    "scores differently under different agent loops."
)


@dataclass(frozen=True)
class ScannerMapping:
    """How one RealVuln scanner slug maps onto our catalog's identity columns.

    Explicit rather than derived from the slug: a slug like `gpt-5.6-sol-codex-cli`
    encodes vendor, model and harness in a naming convention that upstream is free to
    change, and a silent mis-parse would attribute a score to the wrong model.
    """

    model: str  # our display name
    vendor: str
    harness: str  # the agent loop the score was produced under
    harness_vendor: str | None = None


# Every general-purpose agentic scanner on RealVuln v2.1. `*-agentic-v1` slugs were run
# under OpenCode (the loop P38d builds on), `*-codex-cli` under OpenAI's Codex CLI, and
# Opus 5 under Claude Code — recorded because the harness materially changes the score.
_OPENCODE = "OpenCode"
_CODEX = "Codex CLI"
_CLAUDE_CODE = "Claude Code"

_SCANNERS: dict[str, ScannerMapping] = {
    "claude-opus-5-cc-agentic-v1": ScannerMapping("Claude Opus 5", "Anthropic", _CLAUDE_CODE, "Anthropic"),
    "kimi-k3-agentic-v1": ScannerMapping("Kimi K3", "Moonshot AI", _OPENCODE, "SST"),
    "gpt-5.5-agentic-v1": ScannerMapping("GPT-5.5", "OpenAI", _OPENCODE, "SST"),
    "gpt-5.6-sol-codex-cli": ScannerMapping("GPT-5.6 Sol", "OpenAI", _CODEX, "OpenAI"),
    "kimi-k2.7-agentic-v1": ScannerMapping("Kimi K2.7", "Moonshot AI", _OPENCODE, "SST"),
    "deepseek-v4-flash-agentic-v1": ScannerMapping("DeepSeek V4 Flash", "DeepSeek", _OPENCODE, "SST"),
    "deepseek-v4-pro-agentic-v1": ScannerMapping("DeepSeek V4 Pro", "DeepSeek", _OPENCODE, "SST"),
    "kimi-k2.6-agentic-v1": ScannerMapping("Kimi K2.6", "Moonshot AI", _OPENCODE, "SST"),
    "gpt-5.6-terra-codex-cli": ScannerMapping("GPT-5.6 Terra", "OpenAI", _CODEX, "OpenAI"),
    "glm-5.1-agentic-v1": ScannerMapping("GLM-5.1", "Z.ai", _OPENCODE, "SST"),
    "kimi-k2.5-agentic-v1": ScannerMapping("Kimi K2.5", "Moonshot AI", _OPENCODE, "SST"),
    "glm-5.2-agentic-v1": ScannerMapping("GLM-5.2", "Z.ai", _OPENCODE, "SST"),
    "gpt-5.6-luna-codex-cli": ScannerMapping("GPT-5.6 Luna", "OpenAI", _CODEX, "OpenAI"),
    "glm-5-agentic-v1": ScannerMapping("GLM-5", "Z.ai", _OPENCODE, "SST"),
    "gemini-3.5-flash-agentic-v1": ScannerMapping("Gemini 3.5 Flash", "Google", _OPENCODE, "SST"),
    "minimax-m2.7-agentic-v1": ScannerMapping("MiniMax M2.7", "MiniMax", _OPENCODE, "SST"),
}

# Locally-hosted open-weight models (Ollama) are scored by RealVuln but priced at zero
# because they run on the operator's own hardware. A $0 row is not comparable with
# metered API pricing on our cost axis — it would win any cost-leaning ranking by
# construction, and "savings vs baseline" is meaningless when the alternative is
# someone's electricity bill. Skipped with the reason stated, not silently dropped.
_LOCAL_ZERO_PRICE = {
    "qwen3.6-35b-agentic-v1",
    "gemma4-31b-agentic-v1",
    "ornith-q3-agentic-v1",
}

# Models RealVuln runs but does not price in models.yaml (its Claude Code runs are
# configured outside that file). Priced from the provider's own public pricing page,
# cited per row like every other figure. NEVER a guess: if a model is neither in
# models.yaml nor here, the ingest refuses to invent a price and skips it.
_EXTERNAL_PRICES: dict[str, tuple[Decimal, Decimal, str]] = {
    "claude-opus-5-cc-agentic-v1": (
        Decimal("5.00"),
        Decimal("25.00"),
        "Anthropic pricing https://platform.claude.com/docs/en/about-claude/pricing "
        "($5 in / $25 out per MTok)",
    ),
}


# ---------------------------------------------------------------------------
# Reading the two upstream files
# ---------------------------------------------------------------------------


def _read_bytes(location: str | Path) -> bytes:
    """Read a local path or an http(s) URL. Returns raw bytes so the caller can hash
    exactly what was read — the idempotency key must cover the real input."""
    text = str(location)
    scheme = urlparse(text).scheme
    if scheme in ("http", "https"):
        # The scheme is allowlisted immediately above, so file:// and the other
        # schemes urlopen supports cannot be reached here; the URL is an
        # operator-supplied benchmark location, never user input.
        with urlopen(text) as response:  # nosec B310  # noqa: S310
            return response.read()
    if scheme:
        raise ValueError(
            f"unsupported source scheme {scheme!r} — pass an https URL or a local path"
        )
    return Path(text).read_bytes()


def parse_prices(models_yaml: bytes) -> dict[str, dict[str, Any]]:
    """RealVuln's price table, keyed by the `scanner_slug` that joins it to the scores.

    Imported lazily: PyYAML is a dev-only dependency because only this operator tool
    needs it, and the runtime API image must not carry it (see pyproject.toml).
    """
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "PyYAML is required to parse RealVuln's price table. It is a dev-only "
            "dependency of this repo — run this tool with `uv run`, not from the "
            "runtime image."
        ) from exc

    parsed = yaml.safe_load(models_yaml) or {}
    by_slug: dict[str, dict[str, Any]] = {}
    for entry in (parsed.get("models") or {}).values():
        slug = entry.get("scanner_slug")
        if slug:
            by_slug[slug] = entry
    return by_slug


# ---------------------------------------------------------------------------
# Mapping upstream records → catalog rows (pure; no DB, no network)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestSummary:
    rows: list[CatalogRowIn]
    skipped: dict[str, str]  # slug → why
    generated_at: date
    benchmark_version: str
    content_hash: str


def _blended_context_window(entry: dict[str, Any] | None) -> int | None:
    value = (entry or {}).get("max_context")
    return int(value) if value else None


def build_rows(
    dashboard_bytes: bytes,
    models_yaml_bytes: bytes,
    *,
    dashboard_uri: str = DASHBOARD_URL,
) -> IngestSummary:
    """Turn the two upstream files into catalog rows. Pure: same bytes → same rows.

    Every returned row carries a `source` citing the benchmark version, the date the
    results were generated, and where the price came from — the `source` field is the
    only provenance that reaches the API and the grounded chat.
    """
    dashboard = json.loads(dashboard_bytes)
    prices = parse_prices(models_yaml_bytes)

    version = dashboard.get("benchmark_version", "unknown")
    generated_raw = dashboard.get("generated_at", "")
    generated_at = (
        datetime.fromisoformat(generated_raw).date()
        if generated_raw
        else datetime.now(timezone.utc).date()
    )
    aggregates: dict[str, Any] = dashboard.get("aggregates", {})
    metadata: dict[str, Any] = dashboard.get("scanner_metadata", {})

    rows: list[CatalogRowIn] = []
    skipped: dict[str, str] = {}

    for slug in sorted(aggregates):
        if slug in _LOCAL_ZERO_PRICE:
            skipped[slug] = "locally-hosted open-weight model, priced at zero upstream"
            continue
        mapping = _SCANNERS.get(slug)
        if mapping is None:
            skipped[slug] = "not a general-purpose agentic model scanner"
            continue

        score = (aggregates[slug].get("micro") or {}).get(METRIC)
        if score is None:
            skipped[slug] = f"no {METRIC} in the dashboard"
            continue

        price_entry = prices.get(slug)
        if price_entry:
            pricing = price_entry.get("pricing") or {}
            input_price = pricing.get("input_per_1m")
            output_price = pricing.get("output_per_1m")
            price_note = (
                f"prices from the benchmark's own {MODELS_URL} "
                f"(${input_price} in / ${output_price} out per MTok)"
            )
        elif slug in _EXTERNAL_PRICES:
            in_dec, out_dec, price_note = _EXTERNAL_PRICES[slug]
            input_price, output_price = in_dec, out_dec
        else:
            # Refuse to invent a price. An unpriced row cannot be ranked on the cost
            # axis anyway, so importing it would only add a row that can never win.
            skipped[slug] = "no published price (would require inventing one)"
            continue

        if input_price is None or output_price is None:
            skipped[slug] = "incomplete price entry upstream"
            continue

        meta = metadata.get(slug) or {}
        source = (
            f"RealVuln v{version} {REPO_URL} "
            f"(F3 {score}, micro-averaged over 66 repos, generated {generated_at}, "
            f"run under {mapping.harness}); {price_note}"
        )

        rows.append(
            CatalogRowIn(
                model=mapping.model,
                vendor=mapping.vendor,
                harness=mapping.harness,
                harness_vendor=mapping.harness_vendor,
                benchmark=BENCHMARK,
                task_type=TASK_TYPE,
                metric=METRIC,
                score=Decimal(str(score)),
                # cost_per_mtok is REQUIRED by the schema but is DERIVED from the split
                # prices by the upsert (the 3:1 blend), so this value is never the one
                # stored — the service recomputes it. Passing the input price keeps the
                # payload valid without asserting a blend the service owns.
                cost_per_mtok=Decimal(str(input_price)),
                input_price_per_mtok=Decimal(str(input_price)),
                output_price_per_mtok=Decimal(str(output_price)),
                context_window=_blended_context_window(price_entry),
                source=source,
                measured_at=generated_at,
                benchmark_as_of=generated_at,
                benchmark_notes=BENCHMARK_NOTES,
            )
        )

    return IngestSummary(
        rows=rows,
        skipped=skipped,
        generated_at=generated_at,
        benchmark_version=version,
        content_hash=hashlib.sha256(dashboard_bytes).hexdigest(),
    )


# ---------------------------------------------------------------------------
# Persisting
# ---------------------------------------------------------------------------


def ingest_to_db(
    db: Session,
    summary: IngestSummary,
    *,
    dashboard_uri: str = DASHBOARD_URL,
) -> dict[str, Any]:
    """Upsert the rows and record the source document. Idempotent twice over: the
    content hash short-circuits an unchanged re-run, and every row upserts on its
    natural key anyway, so even a forced re-run cannot duplicate."""
    existing = db.scalar(
        select(SourceDocument).where(SourceDocument.content_hash == summary.content_hash)
    )
    if existing is None:
        db.add(
            SourceDocument(
                kind="realvuln_dashboard",
                uri=dashboard_uri,
                content_hash=summary.content_hash,
                fetched_at=datetime.now(timezone.utc),
                status="ingested",
            )
        )
        db.flush()

    for row in summary.rows:
        upsert_catalog_row(db, row)
    db.commit()

    return {
        "rows": len(summary.rows),
        "skipped": len(summary.skipped),
        "benchmark_version": summary.benchmark_version,
        "content_hash": summary.content_hash,
        "already_ingested": existing is not None,
    }


def _row_to_seed_entry(row: CatalogRowIn) -> dict[str, Any]:
    """Serialize a row into the seed file's flat shape (snake_case, JSON scalars)."""
    return {
        "model": row.model,
        "vendor": row.vendor,
        "harness": row.harness,
        "harness_vendor": row.harness_vendor,
        "benchmark": row.benchmark,
        "task_type": row.task_type,
        "score": float(row.score),
        "metric": row.metric,
        "cost_per_mtok": float(row.cost_per_mtok),
        "input_price_per_mtok": float(row.input_price_per_mtok),
        "output_price_per_mtok": float(row.output_price_per_mtok),
        "context_window": row.context_window,
        "source": row.source,
        "measured_at": row.measured_at.isoformat() if row.measured_at else None,
        "benchmark_as_of": row.benchmark_as_of.isoformat() if row.benchmark_as_of else None,
        "benchmark_notes": row.benchmark_notes,
    }


def _dump_seed(data: dict[str, Any]) -> str:
    """Serialize the seed file with ONE catalog row per line.

    `json.dumps(indent=2)` would explode every row into ~16 lines and turn any future
    refresh into an unreadable diff. The seed is a table; keeping a row on a line
    keeps it reviewable and makes a price or score change a one-line diff.
    """
    lines: list[str] = ["{"]
    keys = list(data)
    for key_index, key in enumerate(keys):
        tail = "" if key_index == len(keys) - 1 else ","
        value = data[key]
        if isinstance(value, list):
            lines.append(f"  {json.dumps(key)}: [")
            for i, entry in enumerate(value):
                sep = "" if i == len(value) - 1 else ","
                lines.append(f"    {json.dumps(entry, ensure_ascii=False)}{sep}")
            lines.append(f"  ]{tail}")
        else:
            lines.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}{tail}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_seed_rows(rows: Iterable[CatalogRowIn], seed_path: Path) -> int:
    """Replace the seed file's RealVuln rows with `rows`, leaving everything else
    untouched. Deterministic: rows are written in descending score order, so a
    re-generation produces a byte-identical file and shows up as an empty diff."""
    data = json.loads(seed_path.read_text())
    kept = [
        entry
        for entry in data["benchmark_results"]
        if entry.get("benchmark") != BENCHMARK
    ]
    incoming = sorted(
        (_row_to_seed_entry(r) for r in rows),
        key=lambda e: (-e["score"], e["model"]),
    )
    data["benchmark_results"] = kept + incoming
    seed_path.write_text(_dump_seed(data))
    return len(incoming)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.catalog.ingest_realvuln",
        description=(
            "Ingest RealVuln security-analysis scores into the catalog. "
            "Deterministic, no LLM, idempotent."
        ),
    )
    parser.add_argument("--dashboard", default=str(DEFAULT_DASHBOARD))
    parser.add_argument("--models", default=str(DEFAULT_MODELS))
    parser.add_argument(
        "--write-seed",
        action="store_true",
        help="write the rows into app/catalog/seed_data.json instead of the database",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be imported and change nothing",
    )
    args = parser.parse_args(argv)

    summary = build_rows(
        _read_bytes(args.dashboard),
        _read_bytes(args.models),
        dashboard_uri=str(args.dashboard),
    )

    for slug, reason in sorted(summary.skipped.items()):
        print(f"  skip {slug}: {reason}", file=sys.stderr)
    print(
        f"RealVuln v{summary.benchmark_version} ({summary.generated_at}): "
        f"{len(summary.rows)} rows, {len(summary.skipped)} skipped"
    )

    if args.dry_run:
        for row in sorted(summary.rows, key=lambda r: -float(r.score)):
            print(f"  {row.score:>6} F3  {row.model:22} {row.harness}")
        return

    if args.write_seed:
        from app.catalog.seed import SEED_PATH

        count = write_seed_rows(summary.rows, SEED_PATH)
        print(f"wrote {count} RealVuln rows into {SEED_PATH}")
        return

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        result = ingest_to_db(db, summary, dashboard_uri=str(args.dashboard))
        print(
            f"catalog rows upserted: {result['rows']} "
            f"(source already ingested: {result['already_ingested']})"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
