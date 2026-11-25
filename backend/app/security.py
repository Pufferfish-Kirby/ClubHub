"""Transport hardening and rate limiting for the public API.

Three things live here, in the order a request meets them:

    TrustedHost / HTTPSRedirect   (main.py, production only)
    SecurityHeadersMiddleware     every response
    RateLimitMiddleware           per client IP, per route budget

There is no authentication anywhere in this app and none is planned for the
hackathon scope, so none of this is access control -- it is abuse control. The
thing actually worth protecting is spend: an unauthenticated POST /recommend
costs a Voyage embedding plus a Sonnet rerank, and POST /upload costs a Sonnet
vision call plus a Dropbox write. Left open, one loop empties the API budget
before the demo.

The limiter is deliberately in-process:

  - The app runs as a single uvicorn worker, so one dict is the whole picture.
  - Redis would be the right answer for multiple workers, and the wrong answer
    for a 24h project -- another service to run, and another thing to fail on
    stage.

If this ever runs multi-worker, the budgets become per-worker and this module
needs a shared store. That is the one assumption to re-check before scaling.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# (requests, window_seconds) pairs. A route must pass EVERY rule that matches it,
# so a short burst rule and a long sustain rule can be applied together: 10/min
# stops hammering, 100/hour stops a slow drip from draining the budget overnight.
Budget = tuple[int, int]

# Matched longest-prefix-first against "<METHOD> <path>", so a specific rule wins
# over a general one. Costs are what set these numbers, not symmetry.
ROUTE_BUDGETS: dict[str, tuple[Budget, ...]] = {
    # Voyage embedding + Sonnet rerank on every call: the most expensive route.
    "POST /api/recommend": ((10, 60), (100, 3600)),
    # Sonnet vision + a Dropbox upload + database writes.
    "POST /api/upload": ((5, 60), (30, 3600)),
    # The same pipeline behind a club key: the key proves who, not that they won't loop it.
    "POST /api/club/upload": ((5, 60), (30, 3600)),
    # Cheap, but it publishes an event, so it mutates what the demo shows.
    "POST /api/events": ((20, 60), (200, 3600)),
}

# Everything unmatched: plain SQLite reads. Loose enough that a student clicking
# around the graph never notices, tight enough to blunt a scraper.
DEFAULT_BUDGETS: tuple[Budget, ...] = ((120, 60), (2000, 3600))

# Uptime probes must never be rate limited or they cause the outage they check for.
EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})

# Stops the hit map growing without bound when many distinct IPs appear. Well
# above any plausible concurrent audience for this demo.
MAX_TRACKED_CLIENTS = 10_000


def _client_key(request: Request, *, trust_forwarded: bool) -> str:
    """Who to bill this request to.

    X-Forwarded-For is only honoured in production, where a proxy sets it. Trusting
    it in development would let anyone mint a fresh identity per request with a
    header, which is worse than no limiter at all because it looks like one.
    """
    if trust_forwarded:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _route_key(request: Request) -> str:
    """The budget bucket for this request: the longest configured prefix it matches."""
    target = f"{request.method} {request.url.path}"
    best = ""
    for configured in ROUTE_BUDGETS:
        if target.startswith(configured) and len(configured) > len(best):
            best = configured
    return best or "default"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window limiter, keyed on (client, route bucket).

    A sliding window rather than a fixed one: a fixed window lets someone spend
    the whole budget at 11:59:59 and the whole next budget at 12:00:00, which for
    an LLM-backed route is double the spend in two seconds.
    """

    def __init__(self, app, *, enabled: bool = True, trust_forwarded: bool = False):
        super().__init__(app)
        self.enabled = enabled
        self.trust_forwarded = trust_forwarded
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if not self.enabled or request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        # CORS preflight carries no credentials and costs nothing; limiting it
        # would break the browser's own handshake before the real call is seen.
        if request.method == "OPTIONS":
            return await call_next(request)

        route = _route_key(request)
        budgets = ROUTE_BUDGETS.get(route, DEFAULT_BUDGETS)
        client = _client_key(request, trust_forwarded=self.trust_forwarded)
        now = time.monotonic()

        retry_after = self._exceeded(client, route, budgets, now)
        if retry_after is not None:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Give it a moment and try again."},
                headers={"Retry-After": str(retry_after)},
            )

        self._hits[(client, route)].append(now)
        self._evict_if_large()
        return await call_next(request)

    def _exceeded(
        self, client: str, route: str, budgets: tuple[Budget, ...], now: float
    ) -> int | None:
        """Seconds to wait if any budget is spent, else None.

        Prunes against the longest window first so one pass keeps the deque honest
        for every rule -- entries older than the widest window can never matter.
        """
        hits = self._hits[(client, route)]
        longest = max(window for _, window in budgets)
        while hits and now - hits[0] > longest:
            hits.popleft()

        for limit, window in budgets:
            in_window = sum(1 for t in hits if now - t <= window)
            if in_window >= limit:
                oldest = next(t for t in hits if now - t <= window)
                return max(1, int(window - (now - oldest)) + 1)
        return None

    def _evict_if_large(self) -> None:
        """Drop the emptiest buckets when the map grows past its cap.

        Dropping a bucket forgives whatever it held, so this trades a little
        accuracy under a wide IP spread for a hard memory ceiling. Buckets with
        the fewest hits are dropped first: they are the cheapest to forgive.
        """
        if len(self._hits) <= MAX_TRACKED_CLIENTS:
            return
        for key in sorted(self._hits, key=lambda k: len(self._hits[k]))[: len(self._hits) // 4]:
            del self._hits[key]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Response headers that cost nothing and close whole bug classes.

    HSTS is only sent in production: on http://localhost it is either ignored or,
    worse, remembered by the browser and applied to every other localhost port
    you develop against afterwards.
    """

    def __init__(self, app, *, production: bool = False):
        super().__init__(app)
        self.production = production

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Stops a browser second-guessing a declared content type -- the route by
        # which an uploaded "image" gets sniffed as HTML and run.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        # Keeps query strings (a student's blurb) out of the Referer on any
        # outbound click from an API-rendered page.
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if self.production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
