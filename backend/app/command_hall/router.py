from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.command_hall.schemas import (
    ActiveEventResponse,
    EventOverview,
    GroupDetail,
    TaskDetail,
)
from app.command_hall.service import CommandHallService
from app.db import SessionFactory

router = APIRouter(prefix="/api/v1/command-hall", tags=["command-hall"])


async def get_command_hall_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        async with session.begin():
            yield session


def get_command_hall_service() -> CommandHallService:
    return CommandHallService()


def get_command_hall_session_factory() -> async_sessionmaker[AsyncSession]:
    return SessionFactory


@router.get("/active-event", response_model=ActiveEventResponse)
async def active_event(
    session: AsyncSession = Depends(get_command_hall_session),
    service: CommandHallService = Depends(get_command_hall_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> ActiveEventResponse:
    try:
        return await service.active_event_response(session)
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc


@router.get(
    "/events/{event_id}/overview",
    response_model=EventOverview,
)
async def event_overview(
    event_id: UUID,
    session: AsyncSession = Depends(get_command_hall_session),
    service: CommandHallService = Depends(get_command_hall_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> EventOverview:
    try:
        return await service.overview(session, event_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc


@router.get(
    "/events/{event_id}/groups/{group_code}",
    response_model=GroupDetail,
)
async def event_group_detail(
    event_id: UUID,
    group_code: str,
    session: AsyncSession = Depends(get_command_hall_session),
    service: CommandHallService = Depends(get_command_hall_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> GroupDetail:
    try:
        return await service.group_detail(session, event_id, group_code)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetail,
)
async def task_detail(
    task_id: UUID,
    session: AsyncSession = Depends(get_command_hall_session),
    service: CommandHallService = Depends(get_command_hall_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> TaskDetail:
    try:
        return await service.task_detail(session, task_id)
    except LookupError as exc:
        raise _not_found(exc) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc


@router.get("/events/{event_id}/stream")
async def event_stream(
    event_id: UUID,
    request: Request,
    session_factory: async_sessionmaker[AsyncSession] = Depends(
        get_command_hall_session_factory
    ),
    service: CommandHallService = Depends(get_command_hall_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> StreamingResponse:
    async def read_projection():
        try:
            async with session_factory() as session:
                return await service.get_event_projection(session, event_id)
        except SQLAlchemyError as exc:
            raise _storage_unavailable() from exc

    projection = await read_projection()
    if projection is None:
        raise _not_found(LookupError("command_hall_event_not_found"))

    async def is_disconnected() -> bool:
        return await request.is_disconnected()

    async def stream():
        async for chunk in service.stream_projection(
            event_id,
            read_projection=read_projection,
            is_disconnected=is_disconnected,
        ):
            yield chunk

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _not_found(exc: LookupError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="command hall storage is unavailable",
    )
