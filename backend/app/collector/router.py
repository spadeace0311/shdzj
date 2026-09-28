from fastapi import APIRouter, Depends

from app.auth.router import require_role
from app.collector.schemas import CollectorStatusResponse
from app.collector.status_service import CollectorStatusService
from app.db import SessionFactory
from app.regions.repository import RegionRepository


router = APIRouter(prefix="/api/v1/collector", tags=["collector"])


def get_collector_status_service() -> CollectorStatusService:
    return CollectorStatusService(SessionFactory, RegionRepository(SessionFactory))


@router.get("/status", response_model=CollectorStatusResponse)
async def get_status(
    service: CollectorStatusService = Depends(get_collector_status_service),
    _current_user: object = Depends(require_role("superadmin", "group_leader", "group_deputy")),
) -> CollectorStatusResponse:
    return await service.get_status()
