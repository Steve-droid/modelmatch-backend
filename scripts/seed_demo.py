#!/usr/bin/env python
"""Seed a spread of CI runs into an EXISTING demo project (local dev tooling).

The reusable seeding logic now lives in `app.demo.seed` (shared with the P30
auto-seed PostSync hook). This wrapper keeps the original local workflow — seed runs
into a project that already exists — handy for quick dashboard iteration without a
full bring-up. To create the demo user + project too, use the gated entrypoint
`python -m app.demo.seed` (with the DEMO_SEED* settings set).

Usage (from modelmatch-backend/, with the app env set — DATABASE_URL etc.):
    uv run python scripts/seed_demo.py [project_name] [count]   # default: demo-api 30
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.demo.seed import seed_runs
from app.models import Project


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "demo-api"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    db = SessionLocal()
    try:
        project = db.scalar(select(Project).where(Project.name == name))
        if project is None:
            raise SystemExit(
                f"No project named {name!r} — create it first, "
                "or use `python -m app.demo.seed` to create the user + project too."
            )
        summary = seed_runs(db, project, count)
        print(
            f"Seeded {summary['runs']} runs for {name!r} (id {project.id}): "
            f"{summary['banked']} banked · {summary['quality_risk']} quality-risk · "
            f"{summary['unrated']} unrated · "
            f"selected={summary['selected']} baseline={summary['baseline']}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
