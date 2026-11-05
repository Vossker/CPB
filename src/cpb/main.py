import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cpb.api import router
from cpb.config import get_settings
from cpb.credits import ensure_platform_user
from cpb.database import SessionLocal
from cpb.errors import DomainError
from cpb.services import recover_expired_leases

logger = logging.getLogger(__name__)


def _recover_once() -> None:
    with SessionLocal.begin() as session:
        recover_expired_leases(session)


async def _recovery_loop() -> None:
    interval = max(10, get_settings().lease_seconds // 2)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_recover_once)
        except Exception:
            logger.exception("lease recovery iteration failed")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.is_production and settings.api_secret == "development-only-change-me":
        raise RuntimeError("CPB_API_SECRET must be changed in production")
    with SessionLocal.begin() as session:
        ensure_platform_user(session)
    recovery_task = asyncio.create_task(_recovery_loop())
    try:
        yield
    finally:
        recovery_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recovery_task


app = FastAPI(
    title="CPB Compute Marketplace",
    version="0.1.0",
    description="Centralized task scheduling, verification, and internal credit settlement.",
    lifespan=lifespan,
)
app.include_router(router)


@app.exception_handler(DomainError)
async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
    headers = {"WWW-Authenticate": "ApiKey"} if exc.status_code == 401 else None
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)}, headers=headers)
