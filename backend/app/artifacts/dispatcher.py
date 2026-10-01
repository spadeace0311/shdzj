from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from app.artifacts.models import ArtifactProductionCancelRequest
from app.artifacts.workflow import (
    ArtifactProductionWorkflow,
    artifact_production_workflow_id,
)


class ArtifactCancellationStarter(Protocol):
    async def signal_cancel(
        self,
        *,
        workflow_id: str,
        reason: str,
    ) -> None: ...


class TemporalArtifactCancellationStarter:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def signal_cancel(
        self,
        *,
        workflow_id: str,
        reason: str,
    ) -> None:
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.signal(
            ArtifactProductionWorkflow.cancel_requested,
            reason,
        )


@dataclass(frozen=True, slots=True)
class _ClaimedCancellation:
    id: object
    production_run_id: object
    workflow_id: str
    reason: str
    attempt_count: int


class ArtifactProductionDispatcher:
    """Claims durable cancellation rows and signals the matching workflow."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        starter: ArtifactCancellationStarter,
        batch_size: int,
        max_attempts: int,
        lease_seconds: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._session_factory = session_factory
        self._starter = starter
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._lease_seconds = lease_seconds
        self._now = now

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    async def dispatch_once(self) -> int:
        claimed = await self._claim_pending()
        published = 0
        for item in claimed:
            try:
                await self._starter.signal_cancel(
                    workflow_id=item.workflow_id,
                    reason=item.reason,
                )
            except Exception as exc:
                await self._record_failure(
                    item.id,
                    item.attempt_count,
                    exc,
                )
            else:
                await self._record_success(item.id, item.attempt_count)
                published += 1
        return published

    async def _claim_pending(self) -> list[_ClaimedCancellation]:
        now = _normalize_utc(self._now())
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(ArtifactProductionCancelRequest)
                        .where(
                            ArtifactProductionCancelRequest.status.in_(
                                ("pending", "processing")
                            ),
                            ArtifactProductionCancelRequest.available_at
                            <= now,
                        )
                        .order_by(
                            ArtifactProductionCancelRequest.available_at,
                            ArtifactProductionCancelRequest.created_at,
                            ArtifactProductionCancelRequest.id,
                        )
                        .limit(self._batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                claimed: list[_ClaimedCancellation] = []
                for row in rows:
                    row.status = "processing"
                    row.attempt_count += 1
                    row.available_at = lease_until
                    row.lease_expires_at = lease_until
                    claimed.append(
                        _ClaimedCancellation(
                            id=row.id,
                            production_run_id=row.production_run_id,
                            workflow_id=(
                                row.workflow_id
                                or artifact_production_workflow_id(
                                    row.production_run_id
                                )
                            ),
                            reason=row.reason,
                            attempt_count=row.attempt_count,
                        )
                    )
                return claimed

    async def _record_success(
        self,
        outbox_id: object,
        expected_attempt_count: int,
    ) -> None:
        now = _normalize_utc(self._now())
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(
                    ArtifactProductionCancelRequest,
                    outbox_id,
                    with_for_update=True,
                )
                if not _is_current_attempt(
                    row,
                    expected_attempt_count=expected_attempt_count,
                ):
                    return
                row.status = "published"
                row.dispatched_at = now
                row.available_at = now
                row.lease_expires_at = None
                row.last_error = None

    async def _record_failure(
        self,
        outbox_id: object,
        expected_attempt_count: int,
        exc: Exception,
    ) -> None:
        now = _normalize_utc(self._now())
        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(
                    ArtifactProductionCancelRequest,
                    outbox_id,
                    with_for_update=True,
                )
                if not _is_current_attempt(
                    row,
                    expected_attempt_count=expected_attempt_count,
                ):
                    return
                row.last_error = _safe_error_text(exc)
                if row.attempt_count >= self._max_attempts:
                    row.status = "dead_letter"
                    row.available_at = now
                    row.lease_expires_at = None
                    return
                row.status = "pending"
                row.available_at = now + timedelta(
                    seconds=min(
                        2 ** max(row.attempt_count - 1, 0),
                        300,
                    )
                )
                row.lease_expires_at = None


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value.astimezone(UTC)


def _is_current_attempt(
    row: ArtifactProductionCancelRequest | None,
    *,
    expected_attempt_count: int,
) -> bool:
    return (
        row is not None
        and row.status == "processing"
        and row.attempt_count == expected_attempt_count
    )


def _safe_error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2_000]
