from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class OperationsHealthState:
    def __init__(
        self,
        *,
        stale_seconds: float,
        health_file: str | Path | None = None,
    ) -> None:
        if stale_seconds <= 0:
            raise ValueError("stale_seconds must be positive")
        self._stale_seconds = stale_seconds
        self._last_heartbeat_at: datetime | None = None
        self._health_file = (
            Path(health_file).resolve() if health_file is not None else None
        )

    def mark_alive(self, now: datetime | None = None) -> None:
        heartbeat = _utc(now)
        self._last_heartbeat_at = heartbeat
        if self._health_file is not None:
            _write_heartbeat(self._health_file, heartbeat)

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


def read_health_payload(
    path: str | Path,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    health_file = Path(path)
    try:
        payload = json.loads(health_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return "unavailable", ""
    raw_heartbeat = payload.get("last_heartbeat_at")
    if not isinstance(raw_heartbeat, str):
        return "unavailable", ""
    try:
        heartbeat = datetime.fromisoformat(raw_heartbeat)
    except ValueError:
        return "unavailable", ""
    observed = _utc(now)
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    if observed - heartbeat.astimezone(UTC) > timedelta(seconds=30):
        return "unavailable", ""
    return "ok", heartbeat.isoformat()


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


def _write_heartbeat(path: Path, heartbeat: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "status": "ok",
                    "last_heartbeat_at": heartbeat.isoformat(),
                },
                stream,
                ensure_ascii=True,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
