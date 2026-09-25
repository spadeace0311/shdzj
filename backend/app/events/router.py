from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionFactory
from app.events.domain import EventKind, NormalizedEvent
from app.events.repository import (
    EventDetailRecord,
    EventIngestOutcome,
    EventIngestResult,
    EventSummaryRecord,
)
from app.events.response_rules import ResponseInput
from app.events.schemas import (
    EventDetailResponse,
    EventIngestResponse,
    EventSummaryResponse,
    ManualEventRequest,
    RegionContext,
)
from app.events.service import EventService
from app.events.sources.cenc import CencAdapter

router = APIRouter(prefix="/api/v1")


def get_event_service() -> EventService:
    return EventService(SessionFactory)


@router.post(
    "/ingest/auto",
    response_model=EventIngestResponse,
    status_code=201,
)
async def ingest_auto(
    payload: dict[str, object] = Body(...),
    service: EventService = Depends(get_event_service),
) -> EventIngestResponse:
    event = _parse_cenc(payload, EventKind.AUTO)
    result = await _ingest(service, payload, event)
    return _ingest_response(result, None)


@router.post(
    "/ingest/formal",
    response_model=EventIngestResponse,
    status_code=201,
)
async def ingest_formal(
    payload: dict[str, object] = Body(...),
    service: EventService = Depends(get_event_service),
) -> EventIngestResponse:
    event = _parse_cenc(payload, EventKind.FORMAL)
    region_context = _parse_region_context(payload)
    outcome = await _ingest_with_suggestion(
        service,
        payload,
        event,
        _response_input(event, region_context),
    )
    return _ingest_outcome_response(outcome)


@router.post(
    "/ingest/correction",
    response_model=EventIngestResponse,
    status_code=201,
)
async def ingest_correction(
    payload: dict[str, object] = Body(...),
    service: EventService = Depends(get_event_service),
) -> EventIngestResponse:
    event = _parse_cenc(payload, EventKind.CORRECTION)
    region_context = _parse_region_context(payload)
    outcome = await _ingest_with_suggestion(
        service,
        payload,
        event,
        _response_input(event, region_context),
    )
    return _ingest_outcome_response(outcome)


@router.post(
    "/events/manual",
    response_model=EventIngestResponse,
    status_code=201,
)
async def create_manual_event(
    request: ManualEventRequest,
    service: EventService = Depends(get_event_service),
) -> EventIngestResponse:
    try:
        event = NormalizedEvent(
            kind=EventKind(request.event_kind),
            source=request.source,
            source_event_id=request.source_event_id,
            origin_time=request.origin_time,
            longitude=request.longitude,
            latitude=request.latitude,
            depth_km=request.depth_km,
            magnitude=request.magnitude,
            place=request.place.strip() or "Unknown",
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = request.model_dump(mode="json")
    result = await _ingest(service, payload, event)
    return _ingest_response(result, None)


@router.get("/events", response_model=list[EventSummaryResponse])
async def list_events(
    service: EventService = Depends(get_event_service),
) -> list[EventSummaryResponse]:
    try:
        records = await service.list_events()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="event storage is unavailable") from exc
    return [_summary_response(record) for record in records]


@router.get("/events/{event_id}", response_model=EventDetailResponse)
async def get_event(
    event_id: str,
    service: EventService = Depends(get_event_service),
) -> EventDetailResponse:
    try:
        record = await service.get_event(event_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="event storage is unavailable") from exc
    return _detail_response(record)


def _parse_cenc(payload: dict[str, object], expected_kind: EventKind) -> NormalizedEvent:
    try:
        event = CencAdapter().parse(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if event.kind is not expected_kind:
        raise HTTPException(
            status_code=400,
            detail=f"expected {expected_kind.value} CENC report, got {event.kind.value}",
        )
    return event


async def _ingest(
    service: EventService,
    payload: dict[str, object],
    event: NormalizedEvent,
) -> EventIngestResult:
    try:
        return await service.ingest(payload, event)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="event storage is unavailable") from exc


async def _ingest_with_suggestion(
    service: EventService,
    payload: dict[str, object],
    event: NormalizedEvent,
    response_input: ResponseInput | None,
) -> EventIngestOutcome:
    try:
        return await service.ingest_with_response_suggestion(
            payload,
            event,
            response_input,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="event storage is unavailable") from exc


def _response_input(
    event: NormalizedEvent,
    region_context: RegionContext | None,
) -> ResponseInput | None:
    if region_context is None:
        return None
    return ResponseInput(
        magnitude=event.magnitude,
        depth_km=event.depth_km,
        inside_shanghai=region_context.inside_shanghai,
        distance_to_boundary_km=region_context.distance_to_boundary_km,
        deaths=region_context.deaths,
        max_intensity=region_context.max_intensity,
    )


def _parse_region_context(payload: dict[str, object]) -> RegionContext | None:
    if "regionContext" not in payload or payload["regionContext"] is None:
        return None
    try:
        return RegionContext.model_validate(payload["regionContext"])
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ingest_response(
    result: EventIngestResult,
    suggestion: object | None,
) -> EventIngestResponse:
    del suggestion
    return EventIngestResponse(
        event_id=result.event_id,
        revision_id=result.revision_id,
        revision_no=result.revision_no,
        event_kind=result.event_kind.value,
        institutional_level=None,
        service_level=None,
    )


def _ingest_outcome_response(outcome: EventIngestOutcome) -> EventIngestResponse:
    return EventIngestResponse(
        event_id=outcome.event_id,
        revision_id=outcome.revision_id,
        revision_no=outcome.revision_no,
        event_kind=outcome.event_kind.value,
        institutional_level=outcome.institutional_level,
        service_level=outcome.service_level,
    )


def _summary_response(record: EventSummaryRecord) -> EventSummaryResponse:
    return EventSummaryResponse(
        id=record.event_id,
        source=record.source,
        event_kind=record.event_kind,
        place=record.place,
        magnitude=record.magnitude,
        depth_km=record.depth_km,
        origin_time=record.origin_time,
        longitude=record.longitude,
        latitude=record.latitude,
        institutional_level=record.institutional_level,
        service_level=record.service_level,
        revision_no=record.revision_no,
    )


def _detail_response(record: EventDetailRecord) -> EventDetailResponse:
    return EventDetailResponse(
        id=record.event_id,
        source=record.source,
        place=record.place,
        magnitude=record.magnitude,
        depth_km=record.depth_km,
        origin_time=record.origin_time,
        longitude=record.longitude,
        latitude=record.latitude,
        institutional_level=record.institutional_level,
        service_level=record.service_level,
        response_suggestion=record.response_suggestion,
        response_rule_version=record.response_rule_version,
        revision_no=record.revision_no,
    )
