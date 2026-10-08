from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class WorkerHealthState:
    def __init__(self, *, stale_seconds: float) -> None:
        if stale_seconds <= 0:
            raise ValueError("stale_seconds must be positive")
        self._stale_seconds = stale_seconds
        self._last_heartbeat_at: datetime | None = None

    def mark_alive(self, now: datetime | None = None) -> None:
        self._last_heartbeat_at = _utc(now)

    def payload(self, now: datetime | None = None) -> dict[str, str]:
        observed = _utc(now)
        heartbeat = self._last_heartbeat_at
        if heartbeat is None or observed - heartbeat > timedelta(
            seconds=self._stale_seconds
        ):
            return {
                "status": "unavailable",
                "last_heartbeat_at": "",
            }
        return {
            "status": "ok",
            "last_heartbeat_at": heartbeat.isoformat(),
        }


class WorkerHealthResponse(BaseModel):
    status: str
    last_heartbeat_at: str = Field(default="")


app = FastAPI(title="Knowledge worker health")
_state = WorkerHealthState(stale_seconds=30)


def set_worker_health_state(state: WorkerHealthState) -> None:
    global _state
    _state = state


def get_worker_health_state() -> WorkerHealthState:
    return _state


@app.get("/health", response_model=WorkerHealthResponse)
async def health() -> WorkerHealthResponse:
    return WorkerHealthResponse(**get_worker_health_state().payload())


async def run_health_server(port: int = 8100) -> None:
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
    )
    try:
        await uvicorn.Server(config).serve()
    except Exception:
        logger.exception("knowledge worker health server stopped")


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
