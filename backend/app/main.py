import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.config import settings
from app.database import init_db
from app.routers import clubs, events, recommend, review, upload
from app.security import RateLimitMiddleware, SecurityHeadersMiddleware
from app.services import dropbox_watcher

# Uvicorn only sets up its own loggers. Without this, every logger.info() under app.* is
# dropped (Python's fallback prints WARNING and up), including the review audit lines in
# routers/review.py and the Dropbox watcher's progress.
_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.INFO)
if not _app_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _app_logger.addHandler(_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # The watcher runs for the life of the app. Opt out via
    # DROPBOX_WATCHER_ENABLED=0 so tests and frontend-only work don't burn Claude
    # calls on every restart -- uvicorn --reload restarts on every file save.
    watcher = None
    if settings.dropbox_watcher_enabled:
        watcher = asyncio.create_task(dropbox_watcher.watch())

    yield

    if watcher is not None:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


app = FastAPI(title="Campus Compass API", lifespan=lifespan)

# Middleware runs in reverse registration order, so the last one added is the
# outermost. Registered cheapest-first: a blocked host or an over-quota client
# should be turned away before anything touches the database or an LLM.
app.add_middleware(
    RateLimitMiddleware,
    enabled=settings.rate_limit_enabled,
    # Only believe X-Forwarded-For where a proxy actually sets it (see security.py).
    trust_forwarded=settings.is_production,
)
app.add_middleware(SecurityHeadersMiddleware, production=settings.is_production)

# Origins come from settings so a deploy can widen them without a code change.
# The defaults are the same three localhost origins this has always allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Transport rules are inert in development on purpose: an HTTPS redirect on
# http://localhost:8000 would bounce every request from Vite and break local dev.
# Set ENVIRONMENT=production (and TRUSTED_HOSTS) once this is reachable publicly.
if settings.is_production:
    if settings.trusted_host_list != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
    app.add_middleware(HTTPSRedirectMiddleware)

# Everything the frontend calls lives under /api.
#
# frontend/src/lib/api.js uses baseURL "/api", and vite.config.js proxies /api to
# this server *without* rewriting the path -- so a request to /recommend arrives
# here as /api/recommend. Serving the prefix here rather than stripping it in the
# proxy means the same URLs work in production, where the frontend is on Vercel,
# the backend is elsewhere, and there is no Vite in between to do the rewriting.
API_PREFIX = "/api"

app.include_router(recommend.router, prefix=API_PREFIX)
app.include_router(clubs.router, prefix=API_PREFIX)
app.include_router(events.router, prefix=API_PREFIX)
app.include_router(upload.router, prefix=API_PREFIX)
app.include_router(review.router, prefix=API_PREFIX)


@app.get("/health")
def health():
    """Unprefixed: this is for uptime checks and deploy probes, not the frontend."""
    return {"status": "ok"}
