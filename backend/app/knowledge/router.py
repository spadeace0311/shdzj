from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.router import require_role
from app.auth.service import AuthUser
from app.db import get_session
from app.knowledge.schemas import (
    KnowledgeJobResponse,
    KnowledgeLifecycleRequest,
    KnowledgeSourceCreate,
    KnowledgeSourceResponse,
    KnowledgeVersionCreate,
    KnowledgeVersionResponse,
)
from app.knowledge.service import KnowledgeService

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

_READ_ROLES = (
    "superadmin",
    "data_publisher",
    "data_maintainer",
    "group_leader",
    "group_deputy",
    "group_member",
    "viewer",
)
_WRITE_ROLES = ("superadmin", "data_publisher", "data_maintainer")
_PUBLISH_ROLES = ("superadmin", "data_publisher")


def get_knowledge_service() -> KnowledgeService:
    return KnowledgeService()


@router.get(
    "/sources",
    response_model=list[KnowledgeSourceResponse],
)
async def list_knowledge_sources(
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    _current_user: object = Depends(require_role(*_READ_ROLES)),
) -> list[KnowledgeSourceResponse]:
    try:
        return await service.list_sources(session)
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc


@router.post(
    "/sources",
    response_model=KnowledgeSourceResponse,
    status_code=201,
)
async def create_knowledge_source(
    request: KnowledgeSourceCreate,
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    current_user: AuthUser = Depends(require_role(*_WRITE_ROLES)),
) -> KnowledgeSourceResponse:
    try:
        async with session.begin():
            source = await service.create_source(
                session,
                current_user.username,
                request,
            )
        return _source_response(source)
    except (LookupError, ValueError, IntegrityError, SQLAlchemyError) as exc:
        raise _map_error(exc) from exc


@router.post(
    "/sources/{source_id}/versions",
    response_model=KnowledgeVersionResponse,
    status_code=202,
)
async def create_knowledge_file_version(
    source_id: UUID,
    version: str = Form(...),
    source_uri: str | None = Form(default=None),
    metadata: str = Form(default="{}"),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    current_user: AuthUser = Depends(require_role(*_WRITE_ROLES)),
) -> KnowledgeVersionResponse:
    request = _file_version_request(version, source_uri, metadata)
    try:
        async with session.begin():
            return await service.create_file_version(
                session,
                current_user.username,
                source_id,
                request,
                file,
            )
    except (LookupError, ValueError, IntegrityError, SQLAlchemyError) as exc:
        raise _map_error(exc) from exc


@router.post(
    "/sources/{source_id}/url-versions",
    response_model=KnowledgeVersionResponse,
    status_code=202,
)
async def create_knowledge_url_version(
    source_id: UUID,
    request: KnowledgeVersionCreate,
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    current_user: AuthUser = Depends(require_role(*_WRITE_ROLES)),
) -> KnowledgeVersionResponse:
    try:
        async with session.begin():
            return await service.create_url_version(
                session,
                current_user.username,
                source_id,
                request,
            )
    except (LookupError, ValueError, IntegrityError, SQLAlchemyError) as exc:
        raise _map_error(exc) from exc


@router.get(
    "/versions/{version_id}",
    response_model=KnowledgeVersionResponse,
)
async def get_knowledge_version(
    version_id: UUID,
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    _current_user: object = Depends(require_role(*_READ_ROLES)),
) -> KnowledgeVersionResponse:
    try:
        return await service.get_version(session, version_id)
    except (LookupError, ValueError, SQLAlchemyError) as exc:
        raise _map_error(exc) from exc


@router.post(
    "/versions/{version_id}/rebuild",
    response_model=KnowledgeJobResponse,
    status_code=202,
)
async def rebuild_knowledge_version(
    version_id: UUID,
    request: KnowledgeLifecycleRequest,
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    current_user: AuthUser = Depends(require_role(*_PUBLISH_ROLES)),
) -> KnowledgeJobResponse:
    try:
        async with session.begin():
            return await service.rebuild_index(
                session,
                current_user.username,
                version_id,
                request.reason,
            )
    except (LookupError, ValueError, IntegrityError, SQLAlchemyError) as exc:
        raise _map_error(exc) from exc


@router.post(
    "/versions/{version_id}/publish",
    response_model=KnowledgeVersionResponse,
)
async def publish_knowledge_version(
    version_id: UUID,
    request: KnowledgeLifecycleRequest,
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    current_user: AuthUser = Depends(require_role(*_PUBLISH_ROLES)),
) -> KnowledgeVersionResponse:
    try:
        async with session.begin():
            return await service.publish_version(
                session,
                current_user.username,
                version_id,
                request.reason,
            )
    except (LookupError, ValueError, IntegrityError, SQLAlchemyError) as exc:
        raise _map_error(exc) from exc


@router.post(
    "/versions/{version_id}/rollback",
    response_model=KnowledgeVersionResponse,
)
async def rollback_knowledge_version(
    version_id: UUID,
    request: KnowledgeLifecycleRequest,
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    current_user: AuthUser = Depends(require_role(*_PUBLISH_ROLES)),
) -> KnowledgeVersionResponse:
    try:
        async with session.begin():
            return await service.rollback_version(
                session,
                current_user.username,
                version_id,
                request.reason,
            )
    except (LookupError, ValueError, IntegrityError, SQLAlchemyError) as exc:
        raise _map_error(exc) from exc


@router.get(
    "/jobs",
    response_model=list[KnowledgeJobResponse],
)
async def list_knowledge_jobs(
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    _current_user: object = Depends(require_role(*_READ_ROLES)),
) -> list[KnowledgeJobResponse]:
    try:
        return await service.list_jobs(session)
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc


@router.post(
    "/jobs/{job_id}/retry",
    response_model=KnowledgeJobResponse,
)
async def retry_knowledge_job(
    job_id: UUID,
    session: AsyncSession = Depends(get_session),
    service: KnowledgeService = Depends(get_knowledge_service),
    current_user: AuthUser = Depends(require_role(*_WRITE_ROLES)),
) -> KnowledgeJobResponse:
    try:
        async with session.begin():
            return await service.retry_job(
                session,
                current_user.username,
                job_id,
            )
    except (LookupError, ValueError, IntegrityError, SQLAlchemyError) as exc:
        raise _map_error(exc) from exc


def _file_version_request(
    version: str,
    source_uri: str | None,
    metadata: str,
) -> KnowledgeVersionCreate:
    try:
        parsed_metadata = json.loads(metadata or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail="metadata must be a JSON object",
        ) from exc
    if not isinstance(parsed_metadata, dict):
        raise HTTPException(
            status_code=422,
            detail="metadata must be a JSON object",
        )
    return KnowledgeVersionCreate(
        version=version,
        source_uri=source_uri,
        metadata=parsed_metadata,
    )


def _source_response(source) -> KnowledgeSourceResponse:
    return KnowledgeSourceResponse(
        id=source.id,
        source_key=source.source_key,
        title=source.title,
        layer=source.layer,
        source_type=source.source_type,
        access_level=source.access_level,
        origin=source.origin,
        allow_online_refresh=source.allow_online_refresh,
        is_active=source.is_active,
        created_by=source.created_by,
        created_at=source.created_at,
    )


def _map_error(error: Exception) -> HTTPException:
    if isinstance(error, LookupError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, ValidationError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, IntegrityError):
        return HTTPException(status_code=409, detail="knowledge resource conflict")
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, SQLAlchemyError):
        return _storage_unavailable()
    raise error


def _storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="knowledge storage is unavailable",
    )
