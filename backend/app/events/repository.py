import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from geoalchemy2.elements import WKTElement
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.domain import EventKind, NormalizedEvent, canonical_source_id
from app.events.models import EarthquakeEvent, EarthquakeRevision, RawMessage

_MERGE_TIME_TOLERANCE_SECONDS = 120
_MERGE_DISTANCE_TOLERANCE_DEGREES = 0.2
_REVIEWED_EVENT_KINDS = {EventKind.FORMAL, EventKind.CORRECTION}


@dataclass(frozen=True, slots=True)
class EventIngestResult:
    event_id: str
    revision_id: str
    revision_no: int
    event_kind: EventKind


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
        else:
            latest_revision_no = await session.scalar(
                select(func.max(EarthquakeRevision.revision_no)).where(
                    EarthquakeRevision.event_id == canonical.id
                )
            )
            revision_no = int(latest_revision_no or 0) + 1
            self._promote_reviewed_source_id(canonical, event, source_id)

        becomes_current = _becomes_current(canonical, event)
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

    @staticmethod
    def _promote_reviewed_source_id(
        canonical: EarthquakeEvent,
        event: NormalizedEvent,
        source_id: str,
    ) -> None:
        if event.kind in _REVIEWED_EVENT_KINDS and event.source_event_id:
            canonical.canonical_source_id = source_id


def _becomes_current(canonical: EarthquakeEvent, event: NormalizedEvent) -> bool:
    if event.kind in _REVIEWED_EVENT_KINDS:
        return True
    if canonical.current_revision_id is None:
        return True
    if canonical.event_type in {kind.value for kind in _REVIEWED_EVENT_KINDS}:
        return False
    return event.kind is not EventKind.AUTO


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
