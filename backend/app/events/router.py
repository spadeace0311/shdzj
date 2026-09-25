from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionFactory
from app.events.domain import EventKind, NormalizedEvent
from app.events.repository import EventIngestResult
from app.events.response_rules import ResponseInput, ResponseSuggestion
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
    result = await _ingest(service, payload, event)
    return _ingest_response(result, await _suggest_if_requested(service, result, event, payload))


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
    result = await _ingest(service, payload, event)
    return _ingest_response(result, await _suggest_if_requested(service, result, event, payload))


@router.post(
    "/events/manual",
    response_model=EventIngestResponse,
    status_code=201,
)
async def create_manual_event(
    request: ManualEventRequest,
    service: EventService = Depends(get_event_service),
) -> EventIngestResponse:
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
    payload = request.model_dump(mode="json")
    result = await _ingest(service, payload, event)
    return _ingest_response(result, None)


@router.get("/events", response_model=list[EventSummaryResponse])
async def list_events(
    service: EventService = Depends(get_event_service),
) -> list[EventSummaryResponse]:
    try:
        return await service.list_events()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="event storage is unavailable") from exc


@router.get("/events/{event_id}", response_model=EventDetailResponse)
async def get_event(
    event_id: str,
    service: EventService = Depends(get_event_service),
) -> EventDetailResponse:
    try:
        return await service.get_event(event_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="event storage is unavailable") from exc


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


async def _suggest_if_requested(
    service: EventService,
    result: EventIngestResult,
    event: NormalizedEvent,
    payload: dict[str, object],
) -> ResponseSuggestion | None:
    region_context = _parse_region_context(payload)
    if region_context is None:
        return None
    value = ResponseInput(
        magnitude=event.magnitude,
        depth_km=event.depth_km,
        inside_shanghai=region_context.inside_shanghai,
        distance_to_boundary_km=region_context.distance_to_boundary_km,
        deaths=region_context.deaths,
        max_intensity=region_context.max_intensity,
    )
    try:
        return await service.apply_response_suggestion(result.event_id, value)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="event storage is unavailable") from exc


def _parse_region_context(payload: dict[str, object]) -> RegionContext | None:
    if "regionContext" not in payload or payload["regionContext"] is None:
        return None
    try:
        return RegionContext.model_validate(payload["regionContext"])
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _ingest_response(
    result: EventIngestResult,
    suggestion: ResponseSuggestion | None,
) -> EventIngestResponse:
    return EventIngestResponse(
        event_id=result.event_id,
        revision_id=result.revision_id,
        revision_no=result.revision_no,
        event_kind=result.event_kind.value,
        institutional_level=suggestion.institutional_level if suggestion else None,
        service_level=suggestion.service_level if suggestion else None,
    )
