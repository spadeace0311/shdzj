import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.domain import EventKind, NormalizedEvent, canonical_source_id
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage

_MERGE_TIME_TOLERANCE_SECONDS = 120
_MERGE_DISTANCE_TOLERANCE_DEGREES = 0.2
_REVIEWED_EVENT_KINDS = {EventKind.FORMAL, EventKind.CORRECTION}
_LOGICAL_KIND_RANK = {
    EventKind.AUTO: 0,
    EventKind.FORMAL: 1,
    EventKind.CORRECTION: 2,
}


@dataclass(frozen=True, slots=True)
class EventIngestResult:
    event_id: str
    revision_id: str
    revision_no: int
    event_kind: EventKind
    is_current: bool


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
            )

        source_id = canonical_source_id(event)
        canonical = await self._find_canonical_event(session, event, source_id)
        geom = _point_geometry(event)

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
        else:
            latest_revision_no = await session.scalar(
                select(func.max(EarthquakeRevision.revision_no)).where(
                    EarthquakeRevision.event_id == canonical.id
                )
            )
            revision_no = int(latest_revision_no or 0) + 1
            current_revision = await self._get_current_revision(session, canonical)

        becomes_current = _becomes_current(current_revision, event, raw.received_at)
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
            revision_kind=event.kind.value,
            source_event_id=event.source_event_id,
            source_report_time=event.report_time,
            source_report_number=event.report_number,
            origin_time=event.origin_time,
            longitude=event.longitude,
            latitude=event.latitude,
            depth_km=event.depth_km,
            magnitude=event.magnitude,
            place=event.place,
            is_current=becomes_current,
            created_at=raw.received_at,
        )
        session.add(revision)
        await session.flush()

        if becomes_current:
            _apply_current_event_fields(canonical, event, revision.id, geom)
        canonical.updated_at = raw.received_at

        return EventIngestResult(
            event_id=str(canonical.id),
            revision_id=str(revision.id),
            revision_no=revision_no,
            event_kind=event.kind,
            is_current=becomes_current,
        )

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
) -> bool:
    if current_revision is None:
        return True

    current_kind = EventKind(current_revision.revision_kind)
    if event.kind is EventKind.AUTO:
        if current_kind in _REVIEWED_EVENT_KINDS:
            return False
        return _event_order(event, received_at) > _revision_order(current_revision)

    if event.kind in _REVIEWED_EVENT_KINDS:
        return _event_order(event, received_at) > _revision_order(current_revision)

    if current_kind in _REVIEWED_EVENT_KINDS:
        return False
    return _normalize_utc(received_at, "received_at") > _normalize_utc(
        current_revision.created_at,
        "revision.created_at",
    )


def _event_order(
    event: NormalizedEvent,
    received_at: datetime,
) -> tuple[datetime, int, int, datetime]:
    received_at = _normalize_utc(received_at, "received_at")
    return (
        event.report_time or received_at,
        event.report_number if event.report_number is not None else -1,
        _LOGICAL_KIND_RANK.get(event.kind, -1),
        received_at,
    )


def _revision_order(
    revision: EarthquakeRevision,
) -> tuple[datetime, int, int, datetime]:
    created_at = _normalize_utc(revision.created_at, "revision.created_at")
    return (
        revision.source_report_time or created_at,
        (revision.source_report_number if revision.source_report_number is not None else -1),
        _LOGICAL_KIND_RANK.get(EventKind(revision.revision_kind), -1),
        created_at,
    )


def _apply_current_event_fields(
    canonical: EarthquakeEvent,
    event: NormalizedEvent,
    revision_id: object,
    geom: WKTElement,
) -> None:
    canonical.event_type = event.kind.value
    canonical.origin_time = event.origin_time
    canonical.longitude = event.longitude
    canonical.latitude = event.latitude
    canonical.depth_km = event.depth_km
    canonical.magnitude = event.magnitude
    canonical.place = event.place
    canonical.geom = geom
    canonical.current_revision_id = revision_id


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
