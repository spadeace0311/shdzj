from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.events.domain import EventKind, NormalizedEvent
from app.events.lifecycle import message_family, semantic_fingerprint
from app.events.repository import (
    EventDetailRecord,
    EventIngestResult,
    EventRepository,
    EventSummaryRecord,
)
from app.events.response_rules import (
    ResponseInput,
    ResponseRuleEngine,
)
from app.regions.domain import RegionContext
from app.regions.repository import RegionRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LifecycleIngestOutcome:
    event_id: str
    revision_id: str
    revision_no: int
    event_kind: EventKind
    lifecycle_state: str | None
    is_current: bool
    is_new: bool
    triggered_assessment: bool
    institutional_level: str | None
    service_level: int | None
    t1_at: datetime | None


_ALLOWED_PROVIDERS = {"fan", "wolfx", "api"}
_ALLOWED_LANES = {"websocket", "http"}
_ALLOWED_TRIGGER_REASONS = {"live", "recovery"}
_PROVIDER_LANE_RULES = {
    "fan": {"websocket"},
    "wolfx": {"http"},
    "api": {"http"},
}


class EventService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository: EventRepository | None = None,
        region_repository: RegionRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository = repository or EventRepository(session_factory)
        self._region_repository = region_repository or RegionRepository(session_factory)

    async def ingest(
        self,
        raw_payload: dict[str, object],
        event: NormalizedEvent,
        received_at: datetime | None = None,
    ) -> EventIngestResult:
        if not isinstance(event, NormalizedEvent):
            raise TypeError("event must be a NormalizedEvent")
        EventRepository.validate_payload(raw_payload)
        normalized_received_at = _normalize_received_at(received_at)
        fingerprint = _semantic_fingerprint(event)

        async with self._session_factory() as session:
            async with session.begin():
                await self._repository.acquire_ingest_lock(session, event.source)
                raw = await self._repository.get_or_create_raw_message(
                    session,
                    raw_payload,
                    event,
                    normalized_received_at,
                    provider="api",
                    ingest_lane="http",
                )
                return await self._repository.append_revision(
                    session,
                    raw,
                    event,
                    semantic_fingerprint=fingerprint,
                    provider="api",
                    ingest_lane="http",
                    ingested_at=normalized_received_at,
                    region_context=None,
                )

    async def ingest_with_response_suggestion(
        self,
        raw_payload: dict[str, object],
        event: NormalizedEvent,
        response_input: ResponseInput | None = None,
        received_at: datetime | None = None,
        region_context: RegionContext | None = None,
    ) -> LifecycleIngestOutcome:
        return await self.ingest_collected(
            raw_payload,
            event,
            provider="api",
            lane="http",
            received_at=received_at,
            response_input=response_input,
            region_context=region_context,
            trigger_reason="recovery",
        )

    async def ingest_collected(
        self,
        raw_payload: dict[str, object],
        event: NormalizedEvent,
        provider: str,
        lane: str,
        received_at: datetime,
        response_input: ResponseInput | None,
        region_context: RegionContext | None,
        trigger_reason: str = "live",
    ) -> LifecycleIngestOutcome:
        if not isinstance(event, NormalizedEvent):
            raise TypeError("event must be a NormalizedEvent")
        EventRepository.validate_payload(raw_payload)
        normalized_received_at = _normalize_received_at(received_at)
        _validate_provider_lane(provider, lane)
        if trigger_reason not in _ALLOWED_TRIGGER_REASONS:
            raise ValueError("trigger_reason must be either 'live' or 'recovery'")
        if response_input is not None and not isinstance(response_input, ResponseInput):
            raise TypeError("response_input must be a ResponseInput")
        if region_context is not None and not isinstance(region_context, RegionContext):
            raise TypeError("region_context must be a RegionContext")
        fingerprint = _semantic_fingerprint(event)

        async with self._session_factory() as session:
            async with session.begin():
                await self._repository.acquire_ingest_lock(session, event.source)
                region_context = await self._resolve_assessment_boundary(
                    session,
                    event,
                    region_context,
                )
                raw = await self._repository.get_or_create_raw_message(
                    session,
                    raw_payload,
                    event,
                    normalized_received_at,
                    provider=provider,
                    ingest_lane=lane,
                )
                result = await self._repository.append_revision(
                    session,
                    raw,
                    event,
                    semantic_fingerprint=fingerprint,
                    provider=provider,
                    ingest_lane=lane,
                    ingested_at=normalized_received_at,
                    region_context=region_context,
                )

                triggered_assessment = False
                if (
                    result.is_new
                    and result.is_current
                    and result.event_kind in {
                        EventKind.FORMAL,
                        EventKind.CORRECTION,
                        EventKind.MANUAL,
                        EventKind.TEST,
                        EventKind.DRILL,
                    }
                ):
                    if response_input is not None:
                        engine = ResponseRuleEngine.from_yaml(
                            settings.response_rules_path
                        )
                        suggestion = engine.suggest(response_input)
                        suggestion_payload = asdict(suggestion)
                        suggestion_payload["causes"] = list(suggestion.causes)
                        await self._repository.set_revision_suggestion(
                            session,
                            result.revision_id,
                            suggestion,
                            suggestion_payload,
                        )
                    if _assessment_applicable(
                        event,
                        region_context,
                        response_input,
                    ):
                        triggered_assessment = await self._repository.enqueue_assessment(
                            session,
                            event_id=result.event_id,
                            revision_id=result.revision_id,
                            revision_no=result.revision_no,
                            trigger_reason=trigger_reason,
                            created_at=normalized_received_at,
                        )
                    else:
                        logger.warning(
                            "assessment applicability rejected event_id=%s kind=%s",
                            result.event_id,
                            result.event_kind,
                        )

                (
                    institutional_level,
                    service_level,
                ) = await self._repository.get_current_response_levels(
                    session,
                    result.event_id,
                )
                lifecycle_state, t1_at = await self._repository.get_event_lifecycle_snapshot(
                    session,
                    result.event_id,
                )

        return LifecycleIngestOutcome(
            event_id=result.event_id,
            revision_id=result.revision_id,
            revision_no=result.revision_no,
            event_kind=result.event_kind,
            lifecycle_state=lifecycle_state,
            is_current=result.is_current,
            is_new=result.is_new,
            triggered_assessment=triggered_assessment,
            institutional_level=institutional_level,
            service_level=service_level,
            t1_at=t1_at,
        )

    async def _resolve_assessment_boundary(
        self,
        session: AsyncSession,
        event: NormalizedEvent,
        region_context: RegionContext | None,
    ) -> RegionContext | None:
        if event.kind not in {
            EventKind.FORMAL,
            EventKind.CORRECTION,
            EventKind.MANUAL,
            EventKind.TEST,
            EventKind.DRILL,
        }:
            return region_context
        if region_context is not None and region_context.boundary_version:
            return region_context
        active = await self._region_repository.get_active(session)
        if active is None:
            raise ValueError(
                "active region boundary is required for formal and correction "
                "assessment ingestion"
            )
        if region_context is None:
            return RegionContext(
                inside_shanghai=None,
                distance_to_boundary_km=None,
                boundary_version=active.version,
                computed_at=datetime.now(UTC),
            )
        return replace(region_context, boundary_version=active.version)

    async def list_events(self) -> list[EventSummaryRecord]:
        async with self._session_factory() as session:
            async with session.begin():
                return await self._repository.list_current_events(session)

    async def get_event(
        self,
        event_id: str,
    ) -> EventDetailRecord:
        async with self._session_factory() as session:
            async with session.begin():
                return await self._repository.get_current_event(session, event_id)


def _normalize_received_at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, datetime):
        raise TypeError("received_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("received_at must include timezone information")
    return value.astimezone(UTC)


def _semantic_fingerprint(event: NormalizedEvent) -> str | None:
    if event.kind not in {EventKind.AUTO, EventKind.FORMAL, EventKind.CORRECTION}:
        return None
    return semantic_fingerprint(event, message_family(event.kind))


def _validate_provider_lane(provider: str, lane: str) -> None:
    if provider not in _ALLOWED_PROVIDERS:
        raise ValueError("provider must be one of fan, wolfx, api")
    if lane not in _ALLOWED_LANES:
        raise ValueError("lane must be one of websocket, http")
    if lane not in _PROVIDER_LANE_RULES[provider]:
        raise ValueError(f"provider '{provider}' is not allowed with lane '{lane}'")


def _assessment_applicable(
    event: NormalizedEvent,
    region_context: RegionContext | None,
    response_input: ResponseInput | None,
) -> bool:
    if event.kind is EventKind.AUTO:
        return False

    inside_shanghai = (
        region_context.inside_shanghai
        if region_context is not None
        else None
    )
    if inside_shanghai is None and response_input is not None:
        inside_shanghai = response_input.inside_shanghai
    distance = (
        region_context.distance_to_boundary_km
        if region_context is not None
        else None
    )
    if distance is None and response_input is not None:
        distance = response_input.distance_to_boundary_km

    geographic_applicable = inside_shanghai is True or (
        inside_shanghai is False
        and distance is not None
        and Decimal(str(distance)) <= Decimal("20")
    )
    magnitude_applicable = Decimal(str(event.magnitude)) >= Decimal("3.0")
    intensity_applicable = (
        response_input is None
        or response_input.max_intensity is None
        or Decimal(str(response_input.max_intensity)) >= Decimal("2.0")
    )
    return geographic_applicable and magnitude_applicable and intensity_applicable
