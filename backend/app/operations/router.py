from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from app.auth.router import get_current_user, require_role
from app.auth.service import AuthUser
from app.db import SessionFactory
from app.operations.priority import OperationsWorkBlocked
from app.operations.repository import StaleOperationsSettings
from app.operations.scenario import OperationsConfigurationError
from app.operations.schemas import (
    DrillTriggerRequest,
    ManualTestTriggerRequest,
    OperationsOverviewRecord,
    OperationsRunRecord,
    OperationsScheduleRecord,
    OperationsSettingsRecord,
    OperationsSettingsUpdateRequest,
    OperationsTriggerResult,
)
from app.operations.service import OperationsService, RealEventPriorityActive


router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


def get_operations_service() -> OperationsService:
    return OperationsService(SessionFactory)


@router.get("/overview", response_model=OperationsOverviewRecord)
async def get_overview(
    service: OperationsService = Depends(get_operations_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> OperationsOverviewRecord:
    try:
        return await service.get_overview(datetime.now(UTC))
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    except OperationsConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc) or "operations_configuration_unavailable",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise _validation_error(str(exc)) from exc


@router.get("/settings", response_model=OperationsSettingsRecord)
async def get_settings(
    service: OperationsService = Depends(get_operations_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> OperationsSettingsRecord:
    try:
        return await service.get_settings()
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    except OperationsConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc) or "operations_configuration_unavailable",
        ) from exc


@router.put("/settings", response_model=OperationsSettingsRecord)
async def update_settings(
    request: OperationsSettingsUpdateRequest,
    service: OperationsService = Depends(get_operations_service),
    current_user: AuthUser = Depends(require_role("superadmin")),
) -> OperationsSettingsRecord:
    if request.expected_version is None:
        raise _validation_error("expected_version is required")
    try:
        return await service.update_settings(
            request,
            current_user,
            request.expected_version,
        )
    except StaleOperationsSettings as exc:
        raise HTTPException(
            status_code=409,
            detail="settings_version_conflict",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise _validation_error(str(exc)) from exc
    except OperationsConfigurationError as exc:
        detail = (
            "invalid_operations_timezone"
            if str(exc) == "invalid_operations_timezone"
            else "operations_configuration_unavailable"
        )
        raise HTTPException(status_code=503, detail=detail) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc


@router.post(
    "/test-trigger",
    response_model=OperationsTriggerResult,
    status_code=201,
)
async def trigger_test(
    request: ManualTestTriggerRequest | None = Body(default=None),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
    service: OperationsService = Depends(get_operations_service),
    current_user: AuthUser = Depends(require_role("superadmin", "group_leader", "group_deputy")),
) -> OperationsTriggerResult:
    effective = _with_manual_request(request, idempotency_key)
    try:
        return await service.trigger_manual_test(effective, current_user)
    except Exception as exc:
        raise _map_trigger_error(exc) from exc


@router.post(
    "/drill-trigger",
    response_model=OperationsTriggerResult,
    status_code=201,
)
async def trigger_drill(
    request: DrillTriggerRequest | None = Body(default=None),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
    service: OperationsService = Depends(get_operations_service),
    current_user: AuthUser = Depends(require_role("superadmin", "group_leader", "group_deputy")),
) -> OperationsTriggerResult:
    effective = _with_drill_request(request, idempotency_key)
    try:
        return await service.trigger_drill(effective, current_user)
    except Exception as exc:
        raise _map_trigger_error(exc) from exc


@router.get("/schedules", response_model=list[OperationsScheduleRecord])
async def list_schedules(
    limit: int = Query(default=20, ge=1, le=100),
    service: OperationsService = Depends(get_operations_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> list[OperationsScheduleRecord]:
    try:
        return await service.list_schedules(limit)
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    except (TypeError, ValueError) as exc:
        raise _validation_error(str(exc)) from exc


@router.get("/runs", response_model=list[OperationsRunRecord])
async def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    service: OperationsService = Depends(get_operations_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> list[OperationsRunRecord]:
    try:
        return await service.list_runs(limit)
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    except (TypeError, ValueError) as exc:
        raise _validation_error(str(exc)) from exc


@router.post(
    "/runs/{run_id}/resume",
    response_model=OperationsTriggerResult,
    status_code=201,
)
async def resume_drill_run(
    run_id: str,
    service: OperationsService = Depends(get_operations_service),
    current_user: AuthUser = Depends(require_role("superadmin")),
) -> OperationsTriggerResult:
    try:
        return await service.resume_drill(run_id, current_user)
    except Exception as exc:
        raise _map_trigger_error(exc) from exc


def _with_manual_request(
    request: ManualTestTriggerRequest | None,
    idempotency_key: str | None,
) -> ManualTestTriggerRequest:
    if request is None:
        return ManualTestTriggerRequest(idempotency_key=idempotency_key)
    key = idempotency_key if idempotency_key is not None else request.idempotency_key
    return request.model_copy(update={"idempotency_key": key})


def _with_drill_request(
    request: DrillTriggerRequest | None,
    idempotency_key: str | None,
) -> DrillTriggerRequest:
    if request is None:
        return DrillTriggerRequest(idempotency_key=idempotency_key)
    key = idempotency_key if idempotency_key is not None else request.idempotency_key
    return request.model_copy(update={"idempotency_key": key})


def _map_trigger_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RealEventPriorityActive):
        return HTTPException(
            status_code=409,
            detail="real_event_priority_active",
        )
    if isinstance(exc, OperationsWorkBlocked):
        return HTTPException(
            status_code=409,
            detail="operations_work_blocked",
        )
    if isinstance(exc, OperationsConfigurationError):
        detail = (
            "invalid_operations_timezone"
            if str(exc) == "invalid_operations_timezone"
            else "operations_configuration_unavailable"
        )
        return HTTPException(
            status_code=503,
            detail=detail,
        )
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (TypeError, ValueError)):
        return _validation_error(str(exc))
    if isinstance(exc, SQLAlchemyError):
        return _storage_unavailable()
    return _storage_unavailable()


def _validation_error(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="operations_storage_unavailable",
    )
