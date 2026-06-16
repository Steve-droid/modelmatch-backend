"""Gunicorn config — serves the FastAPI app and wires prometheus_client multiprocess.

Why this file exists: we run 2 worker processes, and prometheus_client keeps a PRIVATE
in-process registry per worker. Without multiprocess mode, Prometheus scrapes whichever
worker the kernel hands the connection to, so counters appear to jump between workers
and `rate()` breaks. Multiprocess mode points every worker at one shared mmap dir
(``PROMETHEUS_MULTIPROC_DIR``); the ``/metrics`` route aggregates across them (see
``app.observability.metrics.render_metrics``).

``PROMETHEUS_MULTIPROC_DIR`` is set in the Dockerfile (the only place 2 workers run).
Locally / in tests it's unset → ordinary single-registry behaviour.
"""

import os
import shutil

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
