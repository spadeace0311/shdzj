from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class OperationsHealthState:
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


class OperationsHealthResponse(BaseModel):
    status: str
    last_heartbeat_at: str = Field(default="")


health_app = FastAPI(title="Operations worker health")
_state = OperationsHealthState(stale_seconds=30)


def set_operations_health_state(state: OperationsHealthState) -> None:
    global _state
    _state = state


def get_operations_health_state() -> OperationsHealthState:
    return _state


@health_app.get("/health", response_model=OperationsHealthResponse)
async def health() -> OperationsHealthResponse:
    return OperationsHealthResponse(**get_operations_health_state().payload())


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
