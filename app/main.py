"""FastAPI app entrypoint."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.db import close_pool, init_pool
from app.gpt.proxy import router as gpt_router
from app.health import router as health_router
from app.logging_setup import configure_logging, get_logger
from app.oauth.authorize import router as authorize_router
from app.oauth.token import router as token_router
from app.settings import get_settings
from app.upgrade.validate import router as upgrade_router


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger(__name__)

    if settings.sentry_dsn:
        import sentry_sdk
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            traces_sample_rate=0.1,
        )

    await init_pool()
    log.info("gateway.boot", version=__version__, environment=settings.environment)
    try:
        yield
    finally:
        await close_pool()
        log.info("gateway.shutdown")


app = FastAPI(
    title="AskAstroBot Gateway",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,       # disable OpenAPI docs in prod
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://askastrobot.com", "https://www.askastrobot.com"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
    max_age=600,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        path=request.url.path,
        method=request.method,
    )
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    response.headers["x-request-id"] = request_id
    return response


app.include_router(health_router)
app.include_router(authorize_router)
app.include_router(token_router)
app.include_router(gpt_router)
app.include_router(upgrade_router)
