import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.assessment.router import router as assessment_router
from app.auth.router import router as auth_router
from app.auth.service import AuthService
from app.collector.router import router as collector_router
from app.data_assets.router import router as data_assets_router
from app.db import SessionFactory
from app.events.router import router as events_router
from app.loss.router import router as loss_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        await AuthService(SessionFactory).bootstrap_superadmin()
    except Exception as exc:
        logger.exception("Superadmin bootstrap failed")
        raise RuntimeError("Superadmin bootstrap failed; the database may be unavailable") from exc
    yield


app = FastAPI(title="Shanghai Earthquake Emergency API", lifespan=lifespan)
app.include_router(assessment_router)
app.include_router(loss_router)
app.include_router(auth_router)
app.include_router(events_router)
app.include_router(collector_router)
app.include_router(data_assets_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
