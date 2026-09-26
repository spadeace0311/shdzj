from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collector.domain import CollectorProvider
from app.collector.models import CollectorDeadLetter, CollectorRuntimeState
from app.collector.schemas import (
    CollectorProviderStatusResponse,
    CollectorStatusResponse,
)
from app.events.models import EarthquakeRevision
from app.regions.repository import RegionRepository


class CollectorStatusService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        region_repository: RegionRepository,
    ) -> None:
        self._session_factory = session_factory
        self._region_repository = region_repository

    async def get_status(self) -> CollectorStatusResponse:
        async with self._session_factory() as session:
            runtime_rows = (
                await session.scalars(
                    select(CollectorRuntimeState).where(
                        CollectorRuntimeState.provider.in_(
                            (
                                CollectorProvider.FAN.value,
                                CollectorProvider.WOLFX.value,
                            )
                        )
                    )
                )
            ).all()
            rows_by_provider = {row.provider: row for row in runtime_rows}

            providers = [
                _provider_response(rows_by_provider[provider.value])
                for provider in (CollectorProvider.FAN, CollectorProvider.WOLFX)
                if provider.value in rows_by_provider
            ]
            open_dead_letter_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(CollectorDeadLetter)
                    .where(CollectorDeadLetter.status == "open")
                )
                or 0
            )
            boundary = await self._region_repository.get_active(session)
            last_ingested_event_id = await session.scalar(
                select(EarthquakeRevision.event_id)
                .where(EarthquakeRevision.ingested_at.is_not(None))
                .order_by(
                    EarthquakeRevision.ingested_at.desc(),
                    EarthquakeRevision.revision_no.desc(),
                )
                .limit(1)
            )

        return CollectorStatusResponse(
            overall_state=_overall_state(rows_by_provider),
            providers=providers,
            open_dead_letter_count=open_dead_letter_count,
            boundary_version=boundary.version if boundary is not None else None,
            last_ingested_event_id=(
                str(last_ingested_event_id) if last_ingested_event_id is not None else None
            ),
        )


def _provider_response(row: CollectorRuntimeState) -> CollectorProviderStatusResponse:
    return CollectorProviderStatusResponse(
        provider=row.provider,
        state=row.state,
        connected=row.connected,
        last_http_status=row.last_http_status,
        last_connected_at=row.last_connected_at,
        last_message_at=row.last_message_at,
        last_success_at=row.last_success_at,
        consecutive_failures=row.consecutive_failures,
        reconnect_count=row.reconnect_count,
        last_error=row.last_error,
        updated_at=row.updated_at,
    )


def _overall_state(rows_by_provider: dict[str, CollectorRuntimeState]) -> str:
    fan = rows_by_provider.get(CollectorProvider.FAN.value)
    wolfx = rows_by_provider.get(CollectorProvider.WOLFX.value)
    if fan is not None and fan.state == "healthy":
        return "healthy"
    if wolfx is not None and wolfx.state == "healthy":
        return "degraded"
    return "critical"
