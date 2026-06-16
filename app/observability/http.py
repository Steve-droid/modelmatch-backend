"""HTTP request metrics middleware (P20) — request rate / latency / error rate.

One Starlette middleware that times every request and records it under the matched
ROUTE TEMPLATE (e.g. ``/projects/{project_id}/ci-runs``), not the raw URL — so a path
id can never explode Prometheus label cardinality. The ``/metrics`` scrape itself is
excluded (it would otherwise count its own scrapes).

Pairs with `app.observability.metrics.observe_http`. The error rate isn't its own
metric: Grafana derives it as the 5xx share of `modelmatch_http_requests_total`.
"""

from __future__ import annotations

from time import perf_counter

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.metrics import observe_http

_EXCLUDED_PATHS = frozenset({"/metrics"})


class MetricsMiddleware:
    """Pure-ASGI middleware: time the request, then record method + route template +
    status. ASGI (not BaseHTTPMiddleware) so it never buffers response bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _EXCLUDED_PATHS:
            await self.app(scope, receive, send)
            return

        start = perf_counter()
        status_code = 500  # default if the app errors before sending a response start

        async def _send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            duration = perf_counter() - start
            method = scope.get("method", "GET")
            observe_http(method, _route_template(scope), status_code, duration)


def _route_template(scope: Scope) -> str:
    """The matched route's path template, or "__unmatched__" for 404s (so unknown URLs
    collapse to a single low-cardinality bucket instead of one series per bad path)."""
    route = scope.get("route")
    path_format = getattr(route, "path_format", None) or getattr(route, "path", None)
    return path_format or "__unmatched__"
