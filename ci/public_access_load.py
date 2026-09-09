"""Bounded local-only P38o capacity probe, with synthetic data and fake LLMs.

Run --seed after migrations against a disposable `modicum_p38o_capacity` database.
Run without --seed against the locally exposed production backend container.
Never prints passwords, tokens, emails or application response bodies.
"""
import argparse
import asyncio
from collections import Counter
import json
import statistics
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.auth.security import create_access_token, hash_password
from app.config import get_settings
from app.db import SessionLocal
from app.models import User, Project


def fixtures(seed=False):
    url = make_url(get_settings().database_url)
    if url.database != "modicum_p38o_capacity" or url.host not in {"localhost", "127.0.0.1"}:
        raise SystemExit("This probe is restricted to the disposable local capacity database")
    if get_settings().llm_client != "fake":
        raise SystemExit("Capacity checks require fake LLM")
    with SessionLocal() as db:
        if seed:
            from app.catalog.seed import load_seed
            from app.demo.seed import seed_demo_data
            if db.scalar(select(func.count()).select_from(User)):
                raise SystemExit("Seed requires an empty disposable database; refusing to overwrite")
            password_hash = hash_password("capacity-fixture-password")
            db.add_all([User(email=f"capacity{n}@example.com", password_hash=password_hash,
                             is_operator=(n == 0)) for n in range(700)])
            db.commit()
            load_seed(db)
            for n in range(50):
                seed_demo_data(db, email=f"capacity{n}@example.com", password="unused",
                               project_name="capacity-demo", run_count=30)
        rows = db.execute(select(User.id, Project.id).join(Project, Project.user_id == User.id)
                          .order_by(User.id)).all()
        return [(create_access_token(str(uid)), pid) for uid, pid in rows]


async def probe(url, users, active, seconds):
    latencies = []
    statuses = Counter()
    transport_errors = Counter()
    deadline = time.monotonic() + seconds
    async with httpx.AsyncClient(base_url=url, timeout=10,
                                  limits=httpx.Limits(max_connections=100)) as client:
        async def browse(index):
            token, project = users[index]
            headers = {"Authorization": f"Bearer {token}"}
            step = 0
            while time.monotonic() < deadline:
                start = time.monotonic()
                path = "/projects" if step % 3 == 0 else f"/projects/{project}/savings?range=all"
                try:
                    response = await client.get(path, headers=headers)
                    statuses[str(response.status_code)] += 1
                except httpx.HTTPError as exc:
                    statuses["transport_error"] += 1
                    transport_errors[type(exc).__name__] += 1
                latencies.append(time.monotonic() - start)
                step += 1
                await asyncio.sleep(1)  # each user thinks for one second between reads
        await asyncio.gather(*(browse(i) for i in range(active)))
    ordered = sorted(latencies)
    total = len(ordered)
    unexpected = sum(v for k, v in statuses.items() if k != "200")
    return {"active_clients": active, "duration_seconds": seconds, "requests": total,
            "requests_per_second": round(total / seconds, 2), "statuses": dict(statuses),
            "transport_error_types": dict(transport_errors),
            "p95_seconds": round(ordered[int((total - 1) * .95)], 4),
            "mean_seconds": round(statistics.mean(ordered), 4),
            "unexpected_error_pct": round(100 * unexpected / total, 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:58038")
    parser.add_argument("--clients", type=int, default=25)
    parser.add_argument("--seconds", type=int, default=300)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.scheme != "http" or target.hostname not in {"localhost", "127.0.0.1"}:
        raise SystemExit("Target must be the local test backend")
    if not 1 <= args.clients <= 50 or not 1 <= args.seconds <= 300:
        raise SystemExit("Probe limited to 50 clients and 300 seconds")
    users = fixtures(args.seed)
    if args.seed:
        print(json.dumps({"accounts": 700, "populated_dashboards": len(users), "runs_per_project": 30}))
    else:
        print(json.dumps(asyncio.run(probe(args.url, users, args.clients, args.seconds)), indent=2))


if __name__ == "__main__":
    main()
