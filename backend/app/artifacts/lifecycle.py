from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.repository import ArtifactProductionRepository
from app.events.domain import EventKind


class ArtifactProductionLifecycleController:
    """Applies production cancellation rules inside the event transaction."""

    def __init__(
        self,
        repository: ArtifactProductionRepository | None = None,
    ) -> None:
        self._repository = repository or ArtifactProductionRepository()

    async def apply_current_event_transition(
        self,
        session: AsyncSession,
        *,
        event_id: object,
        revision_id: object,
        revision_no: int,
        event_kind: EventKind,
    ) -> tuple[uuid.UUID, ...]:
        event_uuid = _coerce_uuid(event_id, "event_id")
        revision_uuid = _coerce_uuid(revision_id, "revision_id")
        if event_kind is EventKind.CORRECTION:
            await self._repository.supersede_runs_for_revision(
                session,
                event_uuid,
                revision_uuid,
                revision_no,
            )
        elif event_kind in {
            EventKind.MANUAL,
            EventKind.FORMAL,
        }:
            await self._repository.cancel_non_live_runs_for_real_event(
                session,
                event_uuid,
            )
        return ()


def _coerce_uuid(value: object, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error
