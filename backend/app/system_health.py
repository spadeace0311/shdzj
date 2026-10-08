from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel
from qdrant_client import AsyncQdrantClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings

DependencyStatus = Literal["ok", "starting", "unavailable"]
OverallStatus = Literal["ok", "degraded", "unavailable"]


class DependencyHealth(BaseModel):
    status: DependencyStatus


class SystemHealthResponse(BaseModel):
    status: OverallStatus
    checks: dict[str, DependencyHealth]
    checked_at: datetime


async def probe_postgresql() -> DependencyHealth:
    engine = create_async_engine(
        settings.database_url,
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return DependencyHealth(status="unavailable")
    finally:
        try:
            await engine.dispose()
        except Exception:
            pass
    return DependencyHealth(status="ok")


async def probe_qdrant() -> DependencyHealth:
    try:
        client = AsyncQdrantClient(url=settings.qdrant_url, timeout=2.0)
    except Exception:
        return DependencyHealth(status="unavailable")
    try:
        try:
            await client.get_collections()
        except Exception:
            return DependencyHealth(status="unavailable")
        finally:
            try:
                await client.close()
            except Exception:
                pass
    except Exception:
        return DependencyHealth(status="unavailable")
    return DependencyHealth(status="ok")


async def probe_embedding() -> DependencyHealth:
    return await _probe_http_service(
        settings.embedding_service_url,
        include_starting=True,
    )


async def probe_knowledge_worker() -> DependencyHealth:
    return await _probe_http_service(
        settings.knowledge_worker_health_url,
        include_starting=False,
    )


async def collect_system_health() -> SystemHealthResponse:
    postgresql = await probe_postgresql()
    qdrant = await probe_qdrant()
    embedding = await probe_embedding()
    knowledge_worker = await probe_knowledge_worker()
    checks = {
        "postgresql": postgresql,
        "qdrant": qdrant,
        "embedding": embedding,
        "knowledge_worker": knowledge_worker,
    }
    if postgresql.status == "unavailable":
        status: OverallStatus = "unavailable"
    elif any(
        check.status in {"starting", "unavailable"}
        for check in (qdrant, embedding, knowledge_worker)
    ):
        status = "degraded"
    else:
        status = "ok"
    return SystemHealthResponse(
        status=status,
        checks=checks,
        checked_at=datetime.now(UTC),
    )


async def _probe_http_service(
    base_url: str,
    *,
    include_starting: bool,
) -> DependencyHealth:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{base_url.rstrip('/')}/health"
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return DependencyHealth(status="unavailable")
    status = payload.get("status") if isinstance(payload, dict) else None
    if status == "ok":
        return DependencyHealth(status="ok")
    if include_starting and status == "starting":
        return DependencyHealth(status="starting")
    return DependencyHealth(status="unavailable")
