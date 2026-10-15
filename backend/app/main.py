"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import routes_market, routes_signals, routes_trading
from .config import get_settings
from .db import run_migrations
from .jobs import scheduler
from .logging_setup import setup_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    s = get_settings()
    setup_logging(s.log_level, s.secret_values())
    if s.live_trading_enabled:
        log.warning("LIVE_TRADING_ENABLED is set but live execution is not implemented; ignoring.")
    run_migrations()
    tasks: list[asyncio.Task] = []
    if s.background_jobs:
        await asyncio.to_thread(scheduler.startup_sync)
        scheduler.start_background(tasks)
    log.info("startup complete (provider=%s)", s.data_provider)
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="AI Trading Dashboard API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type"])
app.include_router(routes_market.router)
app.include_router(routes_signals.router)
app.include_router(routes_trading.router)
