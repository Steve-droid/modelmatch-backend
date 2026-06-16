"""Gunicorn config — serves the FastAPI app and wires prometheus_client multiprocess.

Why this file exists: we run 2 worker processes, and prometheus_client keeps a PRIVATE
in-process registry per worker. Without multiprocess mode, Prometheus scrapes whichever
worker the kernel hands the connection to, so counters appear to jump between workers
and `rate()` breaks. Multiprocess mode points every worker at one shared mmap dir
(``PROMETHEUS_MULTIPROC_DIR``); the ``/metrics`` route aggregates across them (see
``app.observability.metrics.render_metrics``).

CRITICAL — the env var is set HERE, not as an image-wide Dockerfile ENV. Multiprocess
mode must apply ONLY to the gunicorn serve path. The SAME image also runs
``alembic upgrade head`` (the migrate Job / compose migrate service); alembic executes
SQL, which fires the DB-timing listener → a metric write. In multiprocess mode that
write targets a per-pid mmap file under the dir — and only gunicorn (`on_starting`)
ever creates that dir, so under a blanket ENV alembic crashed with FileNotFoundError.
Setting the var here scopes it to the gunicorn arbiter (inherited by forked workers);
alembic never loads this file, so it uses the ordinary in-process registry. Locally /
in tests the var is unset → ordinary single-registry behaviour.
"""

import os
import shutil

# Scope multiprocess metrics to the gunicorn process tree (see the module docstring).
# setdefault so an explicit override from the environment still wins.
os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/prometheus-multiproc")

bind = "0.0.0.0:8000"
workers = 2
worker_class = "uvicorn.workers.UvicornWorker"


def on_starting(server):
    """Master, before any worker forks: create a clean multiprocess dir so a previous
    run's stale per-pid metric files can't bleed into this process's exposition."""
    path = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if path:
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path, exist_ok=True)


def child_exit(server, worker):
    """When a worker dies, drop its metric files so its frozen counters stop being
    summed into the aggregate (the documented prometheus_client cleanup hook)."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
