"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from basivo_orch import __version__
from basivo_orch.auth.router import auth_router, install_auth
from basivo_orch.auth.settings import get_settings as get_auth_settings
from basivo_orch.config import get_settings
from basivo_orch.db import dispose_engine
from basivo_orch.flows.events import RedisClient
from basivo_orch.flows.router import external_router, management_router
from basivo_orch.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    configure_logging(json_logs=settings.is_production)

    # One Redis client for the whole process, carrying the live tail of run
    # events. The run log itself is in Postgres, so a Redis outage degrades
    # streaming to polling rather than losing anything.
    client: RedisClient | None = None
    try:
        client = redis.from_url(get_auth_settings().redis_url, decode_responses=True)
        await client.ping()
    except Exception as exc:  # noqa: BLE001 - startup must not hinge on Redis
        log.warning(
            "redis.unavailable", error=str(exc), impact="run streaming falls back to polling"
        )
        client = None
    app.state.redis = client

    log.info("service.start", environment=settings.ENVIRONMENT, version=__version__)
    yield

    if client is not None:
        # types-redis lags the runtime (stubs 4.6 vs redis 8.1), where
        # close() is deprecated in favour of aclose().
        await client.aclose()  # type: ignore[attr-defined]
    await dispose_engine()
    log.info("service.stop")


def create_app() -> FastAPI:
    settings = get_settings()
    auth_settings = get_auth_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version=__version__,
        # An unauthenticated schema dump is a free map of the attack surface.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    # Auth middleware first, browser policy second.
    #
    # Starlette inserts each new middleware at the *outside* of the stack, so
    # the last one added is the first to see a request and the last to touch a
    # response. CORS must therefore be added after install_auth: it has to wrap
    # everything, including responses that never reach a route handler — a 403
    # from the CSRF check or a 429 from the rate limiter. Without those headers
    # the browser reports an opaque CORS failure and the real reason is lost.
    install_auth(
        app,
        # The external execution API is authenticated by an API key and nothing
        # else — `require_api_key` never consults a cookie — so CSRF does not
        # apply to it. Without this exemption a caller sending `X-API-Key` is
        # rejected with "CSRF token missing" before reaching authentication at
        # all, because the middleware only recognises `Authorization`.
        #
        # /api/v1/* is deliberately NOT exempt: those routes are the web app's,
        # authenticated by the session cookie, and exempting them would hand an
        # attacker cross-site writes.
        csrf_exempt_prefixes=("/flows",),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=auth_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
        expose_headers=["X-CSRF-Token", "X-Step-Up-Token", "Retry-After"],
        max_age=600,
    )

    # Mounted at the root, deliberately not under API_V1_PREFIX.
    #
    # The refresh cookie is scoped to path=/auth so the long-lived credential is
    # not attached to ordinary API calls. That scoping is set inside the auth
    # package; putting the router behind /api/v1 would move the endpoint to
    # /api/v1/auth/refresh, the cookie path would no longer match, and the
    # browser would silently stop sending it — sessions would die at the first
    # access-token expiry with nothing in the logs to explain it.
    #
    # So: /auth/* and /users/* are auth's, /api/v1/* is the orchestrator's.
    app.include_router(auth_router)

    # The orchestrator's own API. Management is versioned; execution sits at
    # the paths the SOW specifies, because those go into other people's code.
    app.include_router(management_router, prefix=settings.API_V1_PREFIX)
    app.include_router(external_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
