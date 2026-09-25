import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from geoalchemy2.elements import WKTElement
from sqlalchemy import case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.domain import EventKind, NormalizedEvent, canonical_source_id
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from app.events.lifecycle import classify_reviewed_kind
from app.events.response_rules import ResponseSuggestion
from app.regions.domain import RegionContext

_MERGE_TIME_TOLERANCE_SECONDS = 120
_MERGE_DISTANCE_TOLERANCE_DEGREES = 0.2
_REVIEWED_EVENT_KINDS = {EventKind.FORMAL, EventKind.CORRECTION}
_LOGICAL_EVENT_KINDS = {EventKind.AUTO, EventKind.FORMAL, EventKind.CORRECTION}
_REAL_EVENT_KINDS = {EventKind.AUTO, EventKind.MANUAL, EventKind.FORMAL, EventKind.CORRECTION}


@dataclass(frozen=True, slots=True)
class EventIngestResult:
    event_id: str
    revision_id: str
    revision_no: int
    event_kind: EventKind
    is_current: bool
    is_new: bool = True


@dataclass(frozen=True, slots=True)
class EventIngestOutcome:
    event_id: str
    revision_id: str
    revision_no: int
    event_kind: EventKind
    is_current: bool
    institutional_level: str | None
    service_level: int | None


@dataclass(frozen=True, slots=True)
class EventSummaryRecord:
    event_id: str
    source: str
    event_kind: str
    place: str
    magnitude: Decimal
    depth_km: Decimal
    origin_time: datetime
    longitude: Decimal
    latitude: Decimal
    institutional_level: str | None
    service_level: int | None
    revision_no: int
    lifecycle_state: str
    t1_at: datetime | None


@dataclass(frozen=True, slots=True)
class EventDetailRecord:
    event_id: str
    event_kind: str
    source: str
    place: str
    magnitude: Decimal
    depth_km: Decimal
    origin_time: datetime
    longitude: Decimal
    latitude: Decimal
    institutional_level: str | None
    service_level: int | None
    response_suggestion: dict | None
    response_rule_version: str | None
    revision_no: int
    lifecycle_state: str
    t1_at: datetime | None


class EventRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory

    @classmethod
    def validate_payload(cls, payload: object) -> None:
        if payload is None or payload == {}:
            raise ValueError("raw_payload must not be empty")
        if not isinstance(payload, dict):
            raise TypeError("raw_payload must be a JSON object")
        _validate_json_value(payload, "$")

    @classmethod
    def checksum(cls, payload: dict[str, object], source: str, message_kind: str) -> str:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must not be empty")
        if not isinstance(message_kind, str) or not message_kind.strip():
            raise ValueError("message_kind must not be empty")

        cls.validate_payload(payload)
        material = {
            "source": source,
            "message_kind": message_kind,
            "payload": payload,
        }
        _validate_json_value(material, "$")
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def get_or_create_raw_message(
        self,
        session: AsyncSession,
        payload: dict[str, object],
        event: NormalizedEvent,
        received_at: datetime,
        *,
        provider: str,
        ingest_lane: str,
    ) -> RawMessage:
        received_at = _normalize_utc(received_at, "received_at")
        checksum = self.checksum(payload, event.source, event.kind.value)
        existing = await session.scalar(select(RawMessage).where(RawMessage.checksum == checksum))
        if existing is not None:
            return existing

        raw = RawMessage(
            source=event.source,
            source_message_id=event.source_event_id,
            message_kind=event.kind.value,
            received_at=received_at,
            checksum=checksum,
            payload=payload,
            provider=provider,
            ingest_lane=ingest_lane,
        )
        session.add(raw)
        await session.flush()
        return raw

    async def acquire_ingest_lock(
        self,
        session: AsyncSession,
        source: str,
    ) -> None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(" "hashtextextended(:lock_key, 0)" ")"),
            {"lock_key": f"earthquake-ingest:{source}"},
        )

    async def append_revision(
        self,
        session: AsyncSession,
        raw: RawMessage,
        event: NormalizedEvent,
        *,
        semantic_fingerprint: str | None,
        provider: str,
        ingest_lane: str,
        ingested_at: datetime,
        region_context: RegionContext | None,
    ) -> EventIngestResult:
        existing_revision = await session.scalar(
            select(EarthquakeRevision).where(EarthquakeRevision.raw_message_id == raw.id)
        )
        if existing_revision is not None:
            return EventIngestResult(
                event_id=str(existing_revision.event_id),
                revision_id=str(existing_revision.id),
                revision_no=existing_revision.revision_no,
                event_kind=EventKind(existing_revision.revision_kind),
                is_current=existing_revision.is_current,
                is_new=False,
            )

        if event.kind in _LOGICAL_EVENT_KINDS and not semantic_fingerprint:
            raise ValueError("semantic_fingerprint is required for CENC lifecycle events")

        source_id = canonical_source_id(event)
        canonical = await self._find_canonical_event(session, event, source_id)
        geom = _point_geometry(event)
        canonical_existed = canonical is not None

        if canonical is None:
            canonical = EarthquakeEvent(
                source=event.source,
                canonical_source_id=source_id,
                event_type=event.kind.value,
                origin_time=event.origin_time,
                longitude=event.longitude,
                latitude=event.latitude,
                depth_km=event.depth_km,
                magnitude=event.magnitude,
                place=event.place,
                geom=geom,
                created_at=raw.received_at,
                updated_at=raw.received_at,
            )
            session.add(canonical)
            await session.flush()
            revision_no = 1
            current_revision = None
            existing_reviewed_kinds: tuple[EventKind, ...] = ()
        else:
            latest_revision_no = await session.scalar(
                select(func.max(EarthquakeRevision.revision_no)).where(
                    EarthquakeRevision.event_id == canonical.id
                )
            )
            revision_no = int(latest_revision_no or 0) + 1
            current_revision = await self._get_current_revision(session, canonical)
            reviewed_kind_values = await session.scalar(
                select(func.array_agg(EarthquakeRevision.revision_kind)).where(
                    EarthquakeRevision.event_id == canonical.id,
                    EarthquakeRevision.revision_kind.in_(
                        tuple(kind.value for kind in _REVIEWED_EVENT_KINDS)
                    ),
                )
            )
            existing_reviewed_kinds = tuple(
                EventKind(value) for value in (reviewed_kind_values or ())
            )

        if semantic_fingerprint is not None and canonical_existed:
            duplicate_revision = await session.scalar(
                select(EarthquakeRevision).where(
                    EarthquakeRevision.event_id == canonical.id,
                    EarthquakeRevision.semantic_fingerprint == semantic_fingerprint,
                )
            )
            if duplicate_revision is not None:
                return EventIngestResult(
                    event_id=str(duplicate_revision.event_id),
                    revision_id=str(duplicate_revision.id),
                    revision_no=duplicate_revision.revision_no,
                    event_kind=EventKind(duplicate_revision.revision_kind),
                    is_current=duplicate_revision.is_current,
                    is_new=False,
                )

        revision_kind = _resolve_revision_kind(event.kind, existing_reviewed_kinds)
        becomes_current = _becomes_current(
            current_revision,
            event,
            raw.received_at,
            event_kind=revision_kind,
        )
        if becomes_current:
            await session.execute(
                update(EarthquakeRevision)
                .where(EarthquakeRevision.event_id == canonical.id)
                .values(is_current=False)
            )

        revision = EarthquakeRevision(
            event_id=canonical.id,
            raw_message_id=raw.id,
            revision_no=revision_no,
            revision_kind=revision_kind.value,
            source_event_id=event.source_event_id,
            source_report_time=event.report_time,
            source_report_number=event.report_number,
            origin_time=event.origin_time,
            longitude=event.longitude,
            latitude=event.latitude,
            depth_km=event.depth_km,
            magnitude=event.magnitude,
            place=event.place,
            semantic_fingerprint=semantic_fingerprint,
            provider=provider,
            ingest_lane=ingest_lane,
            ingested_at=_normalize_utc(ingested_at, "ingested_at"),
            inside_shanghai=region_context.inside_shanghai if region_context else None,
            distance_to_boundary_km=(
                region_context.distance_to_boundary_km if region_context else None
            ),
            region_boundary_version=region_context.boundary_version if region_context else None,
            region_computed_at=region_context.computed_at if region_context else None,
            is_current=becomes_current,
            created_at=raw.received_at,
        )
        session.add(revision)
        await session.flush()

        if becomes_current:
            _apply_current_event_fields(canonical, event, revision.id, geom, revision_kind)
            if revision_kind in _LOGICAL_EVENT_KINDS:
                canonical.lifecycle_state = _lifecycle_state_for_kind(revision_kind)
        if revision_kind is EventKind.FORMAL and becomes_current and canonical.t1_at is None:
            canonical.t1_at = _normalize_utc(ingested_at, "ingested_at")
        canonical.updated_at = raw.received_at

        return EventIngestResult(
            event_id=str(canonical.id),
            revision_id=str(revision.id),
            revision_no=revision_no,
            event_kind=revision_kind,
            is_current=becomes_current,
            is_new=True,
        )

    async def enqueue_assessment(
        self,
        session: AsyncSession,
        *,
        event_id: object,
        revision_id: object,
        revision_no: int,
        trigger_reason: str,
        created_at: datetime,
    ) -> bool:
        event_uuid = _coerce_uuid(event_id)
        revision_uuid = _coerce_uuid(revision_id)
        existing = await session.scalar(
            select(EventLifecycleOutbox.id).where(
                EventLifecycleOutbox.event_id == event_uuid,
                EventLifecycleOutbox.revision_id == revision_uuid,
                EventLifecycleOutbox.trigger_type == "assessment.requested",
            )
        )
        if existing is not None:
            return False

        event = await session.get(EarthquakeEvent, event_uuid, with_for_update=True)
        if event is None:
            raise LookupError(f"event not found: {event_uuid}")

        session.add(
            EventLifecycleOutbox(
                event_id=event_uuid,
                revision_id=revision_uuid,
                trigger_type="assessment.requested",
                trigger_reason=trigger_reason,
                payload={
                    "event_id": str(event_uuid),
                    "revision_id": str(revision_uuid),
                    "revision_no": revision_no,
                },
                status="pending",
                attempt_count=0,
                created_at=created_at,
                available_at=created_at,
            )
        )
        event.latest_trigger_revision_id = revision_uuid
        await session.flush()
        return True

    async def get_event_lifecycle_snapshot(
        self,
        session: AsyncSession,
        event_id: str,
    ) -> tuple[str | None, datetime | None]:
        event_uuid = _coerce_uuid(event_id)
        event = await session.get(EarthquakeEvent, event_uuid)
        if event is None:
            raise LookupError(f"event not found: {event_id}")
        return event.lifecycle_state, event.t1_at

    async def set_revision_suggestion(
        self,
        session: AsyncSession,
        revision_id: str,
        suggestion: ResponseSuggestion,
        suggestion_payload: dict,
    ) -> None:
        try:
            revision_uuid = uuid.UUID(revision_id)
        except (TypeError, ValueError) as exc:
            raise LookupError(f"revision not found: {revision_id}") from exc

        revision = await session.get(
            EarthquakeRevision,
            revision_uuid,
            with_for_update=True,
        )
        if revision is None:
            raise LookupError(f"revision not found: {revision_id}")
        _apply_suggestion_to_revision(revision, suggestion, suggestion_payload)

        if revision.is_current:
            event = await session.get(
                EarthquakeEvent,
                revision.event_id,
                with_for_update=True,
            )
            if event is None:
                raise LookupError(f"event not found: {revision.event_id}")
            _apply_suggestion_to_event(event, suggestion, suggestion_payload)

    async def get_current_response_levels(
        self,
        session: AsyncSession,
        event_id: str,
    ) -> tuple[str | None, int | None]:
        try:
            event_uuid = uuid.UUID(event_id)
        except (TypeError, ValueError) as exc:
            raise LookupError(f"event not found: {event_id}") from exc

        event = await session.get(EarthquakeEvent, event_uuid)
        if event is None:
            raise LookupError(f"event not found: {event_id}")
        return event.institutional_level, event.service_level

    async def list_current_events(
        self,
        session: AsyncSession,
    ) -> list[EventSummaryRecord]:
        real_event_rank = case(
            (
                EarthquakeEvent.event_type.in_(tuple(kind.value for kind in _REAL_EVENT_KINDS)),
                0,
            ),
            else_=1,
        )
        rows = await session.execute(
            select(EarthquakeEvent, EarthquakeRevision)
            .join(
                EarthquakeRevision,
                EarthquakeRevision.id == EarthquakeEvent.current_revision_id,
            )
            .where(EarthquakeEvent.current_revision_id.is_not(None))
            .order_by(
                real_event_rank.asc(),
                EarthquakeEvent.origin_time.desc(),
                EarthquakeEvent.id.desc(),
            )
        )
        return [_event_summary_record(event, revision) for event, revision in rows.all()]

    async def get_current_event(
        self,
        session: AsyncSession,
        event_id: str,
    ) -> EventDetailRecord:
        try:
            event_uuid = uuid.UUID(event_id)
        except (TypeError, ValueError) as exc:
            raise LookupError(f"event not found: {event_id}") from exc

        event = await session.get(EarthquakeEvent, event_uuid)
        if event is None or event.current_revision_id is None:
            raise LookupError(f"event not found: {event_id}")
        revision = await session.get(EarthquakeRevision, event.current_revision_id)
        if revision is None:
            raise LookupError(f"event not found: {event_id}")
        return _event_detail_record(event, revision)

    async def _find_canonical_event(
        self,
        session: AsyncSession,
        event: NormalizedEvent,
        source_id: str,
    ) -> EarthquakeEvent | None:
        canonical = await session.scalar(
            select(EarthquakeEvent)
            .where(EarthquakeEvent.canonical_source_id == source_id)
            .with_for_update()
        )
        if canonical is not None:
            return canonical

        if event.source_event_id:
            historical_alias = await session.scalar(
                select(EarthquakeEvent)
                .join(
                    EarthquakeRevision,
                    EarthquakeRevision.event_id == EarthquakeEvent.id,
                )
                .where(
                    EarthquakeRevision.source_event_id == event.source_event_id,
                    EarthquakeEvent.source == event.source,
                )
                .order_by(
                    EarthquakeRevision.created_at.desc(),
                    EarthquakeRevision.revision_no.desc(),
                )
                .limit(1)
                .with_for_update(of=EarthquakeEvent)
            )
            if historical_alias is not None:
                return historical_alias

        point = _point_geometry(event)
        return await session.scalar(
            select(EarthquakeEvent)
            .where(
                EarthquakeEvent.source == event.source,
                func.abs(
                    func.extract(
                        "epoch",
                        EarthquakeEvent.origin_time - event.origin_time,
                    )
                )
                <= _MERGE_TIME_TOLERANCE_SECONDS,
                func.ST_DWithin(
                    EarthquakeEvent.geom,
                    point,
                    _MERGE_DISTANCE_TOLERANCE_DEGREES,
                ),
            )
            .order_by(
                EarthquakeEvent.origin_time.desc(),
                EarthquakeEvent.id.asc(),
            )
            .with_for_update()
        )

    async def _get_current_revision(
        self,
        session: AsyncSession,
        canonical: EarthquakeEvent,
    ) -> EarthquakeRevision | None:
        if canonical.current_revision_id is None:
            return None
        return await session.scalar(
            select(EarthquakeRevision).where(EarthquakeRevision.id == canonical.current_revision_id)
        )


def _becomes_current(
    current_revision: EarthquakeRevision | None,
    event: NormalizedEvent,
    received_at: datetime,
    *,
    event_kind: EventKind | None = None,
) -> bool:
    if current_revision is None:
        return True

    current_kind = EventKind(current_revision.revision_kind)
    incoming_kind = event_kind if event_kind is not None else event.kind
    incoming_is_real = incoming_kind in _REAL_EVENT_KINDS
    current_is_real = current_kind in _REAL_EVENT_KINDS
    if incoming_is_real != current_is_real:
        return incoming_is_real

    if incoming_kind not in _LOGICAL_EVENT_KINDS:
        return _normalize_utc(received_at, "received_at") > _normalize_utc(
            current_revision.created_at,
            "revision.created_at",
        )

    if current_kind not in _LOGICAL_EVENT_KINDS:
        return incoming_kind in _REVIEWED_EVENT_KINDS

    if current_kind is EventKind.AUTO:
        if incoming_kind in _REVIEWED_EVENT_KINDS:
            return True
        return _compare_explicit_order(current_revision, event) == 1

    if incoming_kind is EventKind.AUTO:
        return False

    comparison = _compare_explicit_order(current_revision, event)
    if current_kind is EventKind.FORMAL:
        if incoming_kind is EventKind.CORRECTION:
            return comparison != -1
        return comparison == 1

    if current_kind is EventKind.CORRECTION:
        return comparison == 1
    return False


def _compare_explicit_order(
    current_revision: EarthquakeRevision,
    incoming_event: NormalizedEvent,
) -> int | None:
    current_number = current_revision.source_report_number
    incoming_number = incoming_event.report_number
    number_comparable = current_number is not None and incoming_number is not None
    if number_comparable:
        if incoming_number > current_number:
            return 1
        if incoming_number < current_number:
            return -1

    current_time = current_revision.source_report_time
    incoming_time = incoming_event.report_time
    time_comparable = current_time is not None and incoming_time is not None
    if time_comparable:
        if incoming_time > current_time:
            return 1
        if incoming_time < current_time:
            return -1
        return 0

    if number_comparable:
        return 0
    return None


def _apply_current_event_fields(
    canonical: EarthquakeEvent,
    event: NormalizedEvent,
    revision_id: object,
    geom: WKTElement,
    event_kind: EventKind,
) -> None:
    canonical.event_type = event_kind.value
    canonical.origin_time = event.origin_time
    canonical.longitude = event.longitude
    canonical.latitude = event.latitude
    canonical.depth_km = event.depth_km
    canonical.magnitude = event.magnitude
    canonical.place = event.place
    canonical.geom = geom
    canonical.current_revision_id = revision_id
    canonical.institutional_level = None
    canonical.service_level = None
    canonical.response_suggestion = None
    canonical.response_rule_version = None


def _resolve_revision_kind(
    incoming_kind: EventKind,
    existing_reviewed_kinds: tuple[EventKind, ...],
) -> EventKind:
    if incoming_kind is EventKind.AUTO:
        return EventKind.AUTO
    if incoming_kind in _REVIEWED_EVENT_KINDS:
        return classify_reviewed_kind(existing_reviewed_kinds)
    return incoming_kind


def _lifecycle_state_for_kind(event_kind: EventKind) -> str:
    if event_kind is EventKind.AUTO:
        return "auto_pending"
    if event_kind is EventKind.FORMAL:
        return "formal_triggered"
    if event_kind is EventKind.CORRECTION:
        return "correction_triggered"
    raise ValueError(f"unsupported lifecycle event kind: {event_kind.value}")


def _coerce_uuid(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        return uuid.UUID(value)
    return value


def _apply_suggestion_to_revision(
    revision: EarthquakeRevision,
    suggestion: ResponseSuggestion,
    suggestion_payload: dict,
) -> None:
    revision.institutional_level = suggestion.institutional_level
    revision.service_level = suggestion.service_level
    revision.response_suggestion = suggestion_payload
    revision.response_rule_version = suggestion.rule_version


def _apply_suggestion_to_event(
    event: EarthquakeEvent,
    suggestion: ResponseSuggestion,
    suggestion_payload: dict,
) -> None:
    event.institutional_level = suggestion.institutional_level
    event.service_level = suggestion.service_level
    event.response_suggestion = suggestion_payload
    event.response_rule_version = suggestion.rule_version


def _event_summary_record(
    event: EarthquakeEvent,
    revision: EarthquakeRevision,
) -> EventSummaryRecord:
    return EventSummaryRecord(
        event_id=str(event.id),
        source=event.source,
        event_kind=revision.revision_kind,
        place=revision.place,
        magnitude=revision.magnitude,
        depth_km=revision.depth_km,
        origin_time=revision.origin_time,
        longitude=revision.longitude,
        latitude=revision.latitude,
        institutional_level=event.institutional_level,
        service_level=event.service_level,
        revision_no=revision.revision_no,
        lifecycle_state=event.lifecycle_state,
        t1_at=event.t1_at,
    )


def _event_detail_record(
    event: EarthquakeEvent,
    revision: EarthquakeRevision,
) -> EventDetailRecord:
    return EventDetailRecord(
        event_id=str(event.id),
        event_kind=revision.revision_kind,
        source=event.source,
        place=revision.place,
        magnitude=revision.magnitude,
        depth_km=revision.depth_km,
        origin_time=revision.origin_time,
        longitude=revision.longitude,
        latitude=revision.latitude,
        institutional_level=event.institutional_level,
        service_level=event.service_level,
        response_suggestion=event.response_suggestion,
        response_rule_version=event.response_rule_version,
        revision_no=revision.revision_no,
        lifecycle_state=event.lifecycle_state,
        t1_at=event.t1_at,
    )


def _point_geometry(event: NormalizedEvent) -> WKTElement:
    return WKTElement(
        f"POINT({event.longitude} {event.latitude})",
        srid=4326,
    )


def _normalize_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information")
    return value.astimezone(UTC)


def _validate_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} keys must be strings to be JSON compatible")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise TypeError(f"{path} must contain only JSON-compatible values, got {type(value).__name__}")
