from collections.abc import AsyncIterator
import json
import logging
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.storage import ArtifactStore
from app.auth.router import get_current_user, require_role
from app.auth.service import AuthUser
from app.collaboration.domain import DutyRole
from app.collaboration.purge import (
    EventNotFoundError,
    SuperadminPurgeService,
    cleanup_purged_event,
)
from app.collaboration.repository import CollaborationRepository
from app.collaboration.roster import (
    EventRosterGroup,
    MemberInput,
    RosterService,
    snapshot_role_for_user,
)
from app.collaboration.schemas import (
    AttendanceResponse,
    AttendanceUpdateRequest,
    ConfirmingAuthorityResponse,
    DeliverableOverrideRequest,
    DeliverablePublicationResponse,
    DeliverablePublishRequest,
    DeliverableResponse,
    DeliverableTextVersionRequest,
    DeliverableVersionResponse,
    EventWorkgroupResponse,
    EventWorkgroupsResponse,
    MembershipReplaceRequest,
    MembershipResponse,
    RosterMemberResponse,
    TaskCancelRequest,
    TaskContributorResponse,
    TaskReturnRequest,
    TaskSubmitRequest,
    TemporaryTaskCreateRequest,
    TemporaryTaskUpdateRequest,
    WorkgroupResponse,
    WorkgroupTaskResponse,
)
from app.collaboration.service import (
    CollaborationTaskService,
    DeliverableService,
    MissingRequiredDeliverableError,
    StaleTaskVersion,
    TemporaryTaskService,
)
from app.config import settings
from app.db import SessionFactory

router = APIRouter(prefix="/api/v1", tags=["workgroups"])
logger = logging.getLogger(__name__)


async def get_roster_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        async with session.begin():
            yield session


def get_roster_service() -> RosterService:
    return RosterService()


def get_task_service() -> CollaborationTaskService:
    return CollaborationTaskService(
        repository=CollaborationRepository(),
        roster_service=RosterService(),
    )


def get_temporary_task_service() -> TemporaryTaskService:
    return TemporaryTaskService(
        repository=CollaborationRepository(),
        roster_service=RosterService(),
    )


def get_deliverable_service() -> DeliverableService:
    return DeliverableService(
        repository=CollaborationRepository(),
        roster_service=RosterService(),
    )


async def get_purge_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


def get_purge_service() -> SuperadminPurgeService:
    return SuperadminPurgeService()


def get_artifact_store() -> ArtifactStore:
    return ArtifactStore(settings.artifact_storage_root)


@router.get("/workgroups", response_model=list[WorkgroupResponse])
async def list_workgroups(
    session: AsyncSession = Depends(get_roster_session),
    service: RosterService = Depends(get_roster_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> list[WorkgroupResponse]:
    try:
        definitions = await service.list_workgroups(session)
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    return [
        WorkgroupResponse(
            code=definition.code,
            name=definition.name,
            display_order=definition.display_order,
            is_active=definition.is_active,
        )
        for definition in definitions
    ]


@router.get(
    "/workgroups/{code}/memberships",
    response_model=list[MembershipResponse],
)
async def list_group_memberships(
    code: str,
    session: AsyncSession = Depends(get_roster_session),
    service: RosterService = Depends(get_roster_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> list[MembershipResponse]:
    try:
        rows = await service.list_group_memberships(session, code)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    return [_membership_response(membership, user) for membership, user in rows]


@router.put(
    "/workgroups/{code}/memberships",
    response_model=list[MembershipResponse],
)
async def replace_group_memberships(
    code: str,
    request: MembershipReplaceRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: RosterService = Depends(get_roster_service),
    current_user: AuthUser = Depends(require_role("superadmin")),
) -> list[MembershipResponse]:
    try:
        await service.replace_group_members(
            session,
            code,
            [
                MemberInput(
                    user_id=member.user_id,
                    duty_role=member.duty_role,
                    deputy_order=member.deputy_order,
                )
                for member in request.members
            ],
            actor=current_user.username,
        )
        rows = await service.list_group_memberships(session, code)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="roster_conflict") from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    return [_membership_response(membership, user) for membership, user in rows]


@router.get(
    "/events/{event_id}/workgroups",
    response_model=EventWorkgroupsResponse,
)
async def list_event_workgroups(
    event_id: UUID,
    session: AsyncSession = Depends(get_roster_session),
    service: RosterService = Depends(get_roster_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> EventWorkgroupsResponse:
    try:
        groups = await service.event_rosters(session, event_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    return EventWorkgroupsResponse(
        event_id=event_id,
        groups=[_event_group_response(group) for group in groups],
    )


@router.put(
    "/events/{event_id}/workgroups/{code}/attendance",
    response_model=AttendanceResponse,
)
async def set_group_attendance(
    event_id: UUID,
    code: str,
    request: AttendanceUpdateRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: RosterService = Depends(get_roster_service),
    current_user: AuthUser = Depends(get_current_user),
) -> AttendanceResponse:
    try:
        if current_user.role != "superadmin" and not await service.can_manage_attendance(
            session,
            event_id,
            code,
            request.user_id,
            current_user.username,
        ):
            raise PermissionError("Insufficient permissions")
        attendance = await service.set_attendance(
            session,
            event_id,
            code,
            request.user_id,
            request.state,
            current_user.username,
        )
        memberships = await service.list_group_memberships(session, code)
        usernames = {membership.user_id: user.username for membership, user in memberships}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    return _attendance_response(
        attendance,
        username=usernames.get(attendance.user_id),
    )


@router.get(
    "/events/{event_id}/collaboration/tasks",
    response_model=list[WorkgroupTaskResponse],
)
async def list_collaboration_tasks(
    event_id: UUID,
    session: AsyncSession = Depends(get_roster_session),
    service: CollaborationTaskService = Depends(get_task_service),
    current_user: AuthUser = Depends(get_current_user),
) -> list[WorkgroupTaskResponse]:
    try:
        tasks = await service.repository.list_tasks(session, event_id)
        actor = await service.repository.get_user_by_username(
            session,
            current_user.username,
        )
        if actor is None:
            return []
        if actor.role != "superadmin":
            readable_tasks = []
            for task in tasks:
                readable, _, _ = await _task_access(
                    session,
                    service,
                    task,
                    current_user,
                )
                if readable:
                    readable_tasks.append(task)
            tasks = tuple(readable_tasks)
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc

    responses: list[WorkgroupTaskResponse] = []
    for task in tasks:
        responses.append(await _task_response(session, service, task, current_user))
    return responses


@router.post(
    "/events/{event_id}/collaboration/tasks",
    response_model=WorkgroupTaskResponse,
    status_code=201,
)
async def create_temporary_collaboration_task(
    event_id: UUID,
    request: TemporaryTaskCreateRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: TemporaryTaskService = Depends(get_temporary_task_service),
    current_user: AuthUser = Depends(get_current_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> WorkgroupTaskResponse:
    try:
        task = await service.create(
            session,
            event_id=event_id,
            workgroup_code=request.workgroup_code,
            title=request.title,
            instruction=request.instruction,
            priority=request.priority,
            due_at=request.due_at,
            continues_until_cancelled=request.continues_until_cancelled,
            source_ref=request.source_ref,
            actor=current_user,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return await _task_response(session, service, task, current_user)


@router.get(
    "/collaboration/tasks/{task_id}",
    response_model=WorkgroupTaskResponse,
)
async def get_collaboration_task(
    task_id: UUID,
    session: AsyncSession = Depends(get_roster_session),
    service: CollaborationTaskService = Depends(get_task_service),
    current_user: AuthUser = Depends(get_current_user),
) -> WorkgroupTaskResponse:
    try:
        task = await service.repository.get_task(session, task_id)
        if task is None:
            raise LookupError("task_not_found")
        readable, _, _ = await _task_access(
            session,
            service,
            task,
            current_user,
        )
        if not readable:
            raise PermissionError("Insufficient permissions")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    return await _task_response(session, service, task, current_user)


@router.post(
    "/collaboration/tasks/{task_id}/start",
    response_model=WorkgroupTaskResponse,
)
async def start_collaboration_task(
    task_id: UUID,
    session: AsyncSession = Depends(get_roster_session),
    service: CollaborationTaskService = Depends(get_task_service),
    current_user: AuthUser = Depends(get_current_user),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> WorkgroupTaskResponse:
    version = _parse_if_match(if_match)
    try:
        task = await service.start(
            session,
            task_id,
            current_user,
            version,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return await _task_response(session, service, task, current_user)


@router.post(
    "/collaboration/tasks/{task_id}/submit",
    response_model=WorkgroupTaskResponse,
)
async def submit_collaboration_task(
    task_id: UUID,
    request: TaskSubmitRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: CollaborationTaskService = Depends(get_task_service),
    current_user: AuthUser = Depends(get_current_user),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> WorkgroupTaskResponse:
    version = _parse_if_match(if_match)
    try:
        task = await service.submit(
            session,
            task_id,
            current_user,
            version,
            result_text=request.result_text,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return await _task_response(session, service, task, current_user)


@router.post(
    "/collaboration/tasks/{task_id}/return",
    response_model=WorkgroupTaskResponse,
)
async def return_collaboration_task(
    task_id: UUID,
    request: TaskReturnRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: CollaborationTaskService = Depends(get_task_service),
    current_user: AuthUser = Depends(get_current_user),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> WorkgroupTaskResponse:
    version = _parse_if_match(if_match)
    try:
        task = await service.return_to_work(
            session,
            task_id,
            current_user,
            version,
            reason=request.reason,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return await _task_response(session, service, task, current_user)


@router.post(
    "/collaboration/tasks/{task_id}/complete",
    response_model=WorkgroupTaskResponse,
)
async def complete_collaboration_task(
    task_id: UUID,
    session: AsyncSession = Depends(get_roster_session),
    service: CollaborationTaskService = Depends(get_task_service),
    current_user: AuthUser = Depends(get_current_user),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> WorkgroupTaskResponse:
    version = _parse_if_match(if_match)
    try:
        task = await service.complete(
            session,
            task_id,
            current_user,
            version,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return await _task_response(session, service, task, current_user)


@router.post(
    "/collaboration/tasks/{task_id}/cancel",
    response_model=WorkgroupTaskResponse,
)
async def cancel_collaboration_task(
    task_id: UUID,
    request: TaskCancelRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: CollaborationTaskService = Depends(get_task_service),
    temporary_service: TemporaryTaskService = Depends(get_temporary_task_service),
    current_user: AuthUser = Depends(get_current_user),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> WorkgroupTaskResponse:
    version = _parse_if_match(if_match)
    try:
        task = await service.repository.get_task(session, task_id)
        if task is None:
            raise LookupError("task_not_found")
        if task.source_type == "ad_hoc":
            task = await temporary_service.cancel(
                session,
                task_id,
                current_user,
                version,
                reason=request.reason,
                idempotency_key=idempotency_key,
            )
        else:
            task = await service.cancel(
                session,
                task_id,
                current_user,
                version,
                reason=request.reason,
                idempotency_key=idempotency_key,
            )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return await _task_response(session, service, task, current_user)


@router.patch(
    "/collaboration/tasks/{task_id}",
    response_model=WorkgroupTaskResponse,
)
async def update_collaboration_task(
    task_id: UUID,
    request: TemporaryTaskUpdateRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: CollaborationTaskService = Depends(get_task_service),
    temporary_service: TemporaryTaskService = Depends(get_temporary_task_service),
    current_user: AuthUser = Depends(get_current_user),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> WorkgroupTaskResponse:
    version = _parse_if_match(if_match)
    try:
        task = await service.repository.get_task(session, task_id)
        if task is None:
            raise LookupError("task_not_found")
        if task.source_type == "ad_hoc":
            task = await temporary_service.update(
                session,
                task_id,
                current_user,
                version,
                title=request.title,
                instruction=request.instruction,
                priority=request.priority,
                due_at=request.due_at,
                continues_until_cancelled=(request.continues_until_cancelled),
                idempotency_key=idempotency_key,
            )
        else:
            task = await service.update(
                session,
                task_id,
                current_user,
                version,
                title=request.title,
                instruction=request.instruction,
                priority=request.priority,
                due_at=request.due_at,
                idempotency_key=idempotency_key,
            )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return await _task_response(session, service, task, current_user)


@router.get(
    "/collaboration/tasks/{task_id}/deliverables",
    response_model=list[DeliverableResponse],
)
async def list_task_deliverables(
    task_id: UUID,
    session: AsyncSession = Depends(get_roster_session),
    service: DeliverableService = Depends(get_deliverable_service),
    current_user: AuthUser = Depends(get_current_user),
) -> list[DeliverableResponse]:
    try:
        task = await service.repository.get_task(session, task_id)
        if task is None:
            raise LookupError("task_not_found")
        readable, _, _ = await _task_access(
            session,
            service,
            task,
            current_user,
        )
        if not readable:
            raise PermissionError("Insufficient permissions")
        deliverables = await service.repository.list_task_deliverables(session, task_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    return [
        await _deliverable_response(session, service, deliverable) for deliverable in deliverables
    ]


@router.post(
    "/collaboration/deliverables/{deliverable_id}/versions",
    response_model=DeliverableVersionResponse,
)
async def add_text_deliverable_version(
    deliverable_id: UUID,
    request: DeliverableTextVersionRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: DeliverableService = Depends(get_deliverable_service),
    current_user: AuthUser = Depends(get_current_user),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> DeliverableVersionResponse:
    try:
        version = await service.add_text_version(
            session,
            deliverable_id,
            current_user,
            text_result=request.text_result,
            basis_text=request.basis_text,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return _deliverable_version_response(version)


@router.post(
    "/collaboration/deliverables/{deliverable_id}/versions/file",
    response_model=DeliverableVersionResponse,
)
async def add_file_deliverable_version(
    deliverable_id: UUID,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_roster_session),
    service: DeliverableService = Depends(get_deliverable_service),
    current_user: AuthUser = Depends(get_current_user),
    mime_type: str | None = Form(default=None),
    text_result: str | None = Form(default=None),
    basis_text: str | None = Form(default=None),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> DeliverableVersionResponse:
    parsed_text_result = _parse_optional_json_object(text_result)
    try:
        version = await service.add_manual_version(
            session,
            deliverable_id,
            current_user,
            source=file.file,
            file_name=file.filename or "",
            mime_type=mime_type or file.content_type,
            text_result=parsed_text_result,
            basis_text=basis_text,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return _deliverable_version_response(version)


@router.delete(
    "/collaboration/deliverable-versions/{version_id}",
    status_code=204,
)
async def delete_deliverable_candidate(
    version_id: UUID,
    session: AsyncSession = Depends(get_roster_session),
    service: DeliverableService = Depends(get_deliverable_service),
    current_user: AuthUser = Depends(get_current_user),
    if_match: str | None = Header(default=None, alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> None:
    expected_version = _parse_optional_if_match(if_match)
    try:
        await service.delete_candidate(
            session,
            version_id,
            current_user,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc


@router.post(
    "/collaboration/deliverables/{deliverable_id}/publish",
    response_model=DeliverablePublicationResponse,
)
async def publish_deliverable_version(
    deliverable_id: UUID,
    request: DeliverablePublishRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: DeliverableService = Depends(get_deliverable_service),
    current_user: AuthUser = Depends(get_current_user),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> DeliverablePublicationResponse:
    expected_version = _parse_if_match(if_match)
    try:
        publication = await service.publish(
            session,
            deliverable_id,
            current_user,
            version_id=request.version_id,
            publication_note=request.publication_note,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return _deliverable_publication_response(publication)


@router.post(
    "/collaboration/deliverables/{deliverable_id}/restore",
    response_model=DeliverablePublicationResponse,
)
async def restore_deliverable_version(
    deliverable_id: UUID,
    request: DeliverablePublishRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: DeliverableService = Depends(get_deliverable_service),
    current_user: AuthUser = Depends(get_current_user),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> DeliverablePublicationResponse:
    expected_version = _parse_if_match(if_match)
    try:
        publication = await service.restore(
            session,
            deliverable_id,
            current_user,
            version_id=request.version_id,
            publication_note=request.publication_note,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return _deliverable_publication_response(publication)


@router.post(
    "/collaboration/deliverables/{deliverable_id}/override",
    response_model=DeliverablePublicationResponse,
)
async def override_deliverable_version(
    deliverable_id: UUID,
    request: DeliverableOverrideRequest,
    session: AsyncSession = Depends(get_roster_session),
    service: DeliverableService = Depends(get_deliverable_service),
    current_user: AuthUser = Depends(require_role("superadmin")),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> DeliverablePublicationResponse:
    expected_version = _parse_if_match(if_match)
    try:
        publication = await service.override(
            session,
            deliverable_id,
            current_user,
            text_result=request.text_result,
            basis_text=request.basis_text,
            publication_note=request.publication_note,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return _deliverable_publication_response(publication)


@router.post(
    "/collaboration/deliverables/{deliverable_id}/override/file",
    response_model=DeliverablePublicationResponse,
)
async def override_deliverable_file(
    deliverable_id: UUID,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_roster_session),
    service: DeliverableService = Depends(get_deliverable_service),
    current_user: AuthUser = Depends(require_role("superadmin")),
    mime_type: str | None = Form(default=None),
    text_result: str | None = Form(default=None),
    basis_text: str | None = Form(default=None),
    publication_note: str | None = Form(default=None),
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> DeliverablePublicationResponse:
    expected_version = _parse_if_match(if_match)
    parsed_text_result = _parse_optional_json_object(text_result)
    try:
        publication = await service.override(
            session,
            deliverable_id,
            current_user,
            source=file.file,
            file_name=file.filename or "",
            mime_type=mime_type or file.content_type,
            text_result=parsed_text_result,
            basis_text=basis_text,
            publication_note=publication_note,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except _task_mutation_error_types() as exc:
        raise _task_mutation_exception(exc) from exc
    return _deliverable_publication_response(publication)


@router.delete(
    "/admin/events/{event_id}/purge",
    status_code=204,
)
async def purge_event(
    event_id: UUID,
    session: AsyncSession = Depends(get_purge_session),
    service: SuperadminPurgeService = Depends(get_purge_service),
    artifact_store: ArtifactStore = Depends(get_artifact_store),
    current_user: AuthUser = Depends(require_role("superadmin")),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
    ),
) -> Response:
    try:
        async with session.begin():
            result = await service.purge_event(
                session,
                event_id,
                actor=current_user,
                idempotency_key=idempotency_key,
            )
    except EventNotFoundError as exc:
        raise HTTPException(status_code=404, detail="event_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc

    try:
        async with session.begin():
            cleanup_succeeded = await cleanup_purged_event(
                session,
                event_id,
                result.storage_paths,
                artifact_store,
            )
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc

    if not cleanup_succeeded:
        logger.warning(
            "event purge object cleanup incomplete event_id=%s paths=%s",
            event_id,
            result.storage_paths,
        )
        raise _storage_unavailable()
    return Response(status_code=204)


def _membership_response(membership, user) -> MembershipResponse:
    return MembershipResponse(
        membership_id=membership.id,
        user_id=membership.user_id,
        username=user.username,
        duty_role=DutyRole(membership.duty_role),
        deputy_order=membership.deputy_order,
        effective_from=membership.effective_from,
        effective_to=membership.effective_to,
        is_active=membership.is_active,
    )


def _event_group_response(group: EventRosterGroup) -> EventWorkgroupResponse:
    usernames = {
        UUID(str(entry["user_id"])): str(entry["username"]) for entry in _group_entries(group)
    }
    return EventWorkgroupResponse(
        code=group.code,
        name=group.name,
        display_order=group.display_order,
        roster_version=group.roster_version,
        roster_fingerprint=group.roster_fingerprint,
        leader=_roster_member_response(group.leader) if group.leader else None,
        deputies=[_roster_member_response(entry) for entry in group.deputies],
        members=[_roster_member_response(entry) for entry in group.members],
        attendance=[
            _attendance_response(item, username=usernames.get(item.user_id))
            for item in group.attendance
        ],
        confirming_authority=(
            ConfirmingAuthorityResponse(
                user_id=group.authority.user_id,
                role=group.authority.role,
                deputy_order=group.authority.deputy_order,
            )
            if group.authority is not None
            else None
        ),
    )


def _roster_member_response(entry: dict[str, object]) -> RosterMemberResponse:
    return RosterMemberResponse(
        user_id=UUID(str(entry["user_id"])),
        username=str(entry["username"]),
        duty_role=DutyRole(str(entry["duty_role"])),
        deputy_order=(
            int(entry["deputy_order"]) if entry.get("deputy_order") is not None else None
        ),
    )


def _attendance_response(
    attendance,
    *,
    username: str | None,
) -> AttendanceResponse:
    return AttendanceResponse(
        user_id=attendance.user_id,
        username=username,
        state=attendance.state,
        duty_role_in_snapshot=DutyRole(attendance.duty_role_in_snapshot),
        deputy_order_in_snapshot=attendance.deputy_order_in_snapshot,
        checked_in_at=attendance.checked_in_at,
        checked_out_at=attendance.checked_out_at,
        updated_by=attendance.updated_by,
    )


def _group_entries(
    group: EventRosterGroup,
) -> tuple[dict[str, object], ...]:
    return (
        *((group.leader,) if group.leader is not None else ()),
        *group.deputies,
        *group.members,
    )


async def _task_response(
    session: AsyncSession,
    service: CollaborationTaskService,
    task,
    current_user: AuthUser,
) -> WorkgroupTaskResponse:
    contributors = await service.repository.list_contributors(session, task.id)
    _, can_work, can_confirm = await _task_access(
        session,
        service,
        task,
        current_user,
    )
    return WorkgroupTaskResponse(
        id=task.id,
        event_id=task.event_id,
        workgroup_code=task.workgroup_code,
        task_code=task.task_code,
        title=task.title,
        status=task.status,
        timeliness_state=task.timeliness_state,
        due_at=task.due_at,
        row_version=task.row_version,
        instruction=task.instruction,
        priority=task.priority,
        phase_code=task.phase_code,
        source_type=task.source_type,
        source_ref=task.source_ref,
        activated_at=task.activated_at,
        completed_at=task.completed_at,
        closed_at=task.closed_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
        contributors=[
            TaskContributorResponse(
                user_id=contributor.user_id,
                username=user.username,
                contribution_count=contributor.contribution_count,
                first_contributed_at=contributor.first_contributed_at,
                last_contributed_at=contributor.last_contributed_at,
            )
            for contributor, user in contributors
        ],
        can_work=can_work,
        can_confirm=can_confirm,
    )


async def _task_access(
    session: AsyncSession,
    service: CollaborationTaskService,
    task,
    current_user: AuthUser,
) -> tuple[bool, bool, bool]:
    actor = await service.repository.get_user_by_username(
        session,
        current_user.username,
    )
    if actor is None:
        return False, False, False
    if actor.role == "superadmin":
        return True, True, True

    membership = await service.repository.get_active_membership(
        session,
        task.workgroup_code,
        actor.id,
    )
    if membership is None or membership.duty_role not in {"leader", "deputy", "member"}:
        return False, False, False

    snapshot = await service.roster_service.repository.get_roster_snapshot(
        session,
        task.event_id,
        task.workgroup_code,
    )
    snapshot_role = snapshot_role_for_user(snapshot, actor.id)
    if snapshot_role != membership.duty_role:
        return False, False, False
    can_work = True
    authority = await service.roster_service.resolve_confirming_authority(
        session,
        task.event_id,
        task.workgroup_code,
    )
    can_confirm = (
        can_work
        and authority is not None
        and authority.user_id == actor.id
        and membership.duty_role == authority.role.value
    )
    return True, can_work, can_confirm


async def _deliverable_response(
    session: AsyncSession,
    service: DeliverableService,
    deliverable,
) -> DeliverableResponse:
    versions = await service.repository.list_deliverable_versions(session, deliverable.id)
    current_publication = await service.repository.get_current_publication(session, deliverable.id)
    current_version_id = (
        current_publication.version_id
        if current_publication is not None
        else await service.current_version_id(session, deliverable.id)
    )
    return DeliverableResponse(
        id=deliverable.id,
        task_id=deliverable.task_id,
        deliverable_code=deliverable.deliverable_code,
        title=deliverable.title,
        is_required=deliverable.is_required,
        requirement_kind=deliverable.requirement_kind,
        artifact_binding=deliverable.artifact_binding,
        display_order=deliverable.display_order,
        current_version_id=current_version_id,
        current_publication=(
            _deliverable_publication_response(current_publication)
            if current_publication is not None
            else None
        ),
        versions=[_deliverable_version_response(version) for version in versions],
    )


def _deliverable_version_response(version) -> DeliverableVersionResponse:
    return DeliverableVersionResponse(
        id=version.id,
        deliverable_id=version.deliverable_id,
        version_no=version.version_no,
        source_kind=version.source_kind,
        artifact_id=version.artifact_id,
        artifact_publication_id=version.artifact_publication_id,
        storage_key=version.storage_key,
        file_name=version.file_name,
        checksum=version.checksum,
        mime_type=version.mime_type,
        size_bytes=version.size_bytes,
        text_result=version.text_result,
        created_by=version.created_by,
        basis_text=version.basis_text,
        supersedes_version_id=version.supersedes_version_id,
        created_at=version.created_at,
    )


def _deliverable_publication_response(
    publication,
) -> DeliverablePublicationResponse:
    return DeliverablePublicationResponse(
        id=publication.id,
        deliverable_id=publication.deliverable_id,
        version_id=publication.version_id,
        published_by=publication.published_by,
        published_role=publication.published_role,
        published_at=publication.published_at,
        superseded_at=publication.superseded_at,
        publication_note=publication.publication_note,
        created_at=publication.created_at,
    )


def _parse_if_match(if_match: str) -> int:
    try:
        return int(if_match.strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="If-Match must be an integer",
        ) from exc


def _parse_optional_if_match(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    return _parse_if_match(if_match)


def _parse_optional_json_object(value: str | None) -> dict | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail="text_result must be a JSON object",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=422,
            detail="text_result must be a JSON object",
        )
    return parsed


def _task_mutation_error_types() -> tuple[type[Exception], ...]:
    return (
        PermissionError,
        LookupError,
        StaleTaskVersion,
        MissingRequiredDeliverableError,
        TypeError,
        ValueError,
        SQLAlchemyError,
    )


def _task_mutation_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, StaleTaskVersion):
        return HTTPException(status_code=409, detail="task_version_conflict")
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, MissingRequiredDeliverableError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (TypeError, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    return _storage_unavailable()


def _storage_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="collaboration storage is unavailable",
    )
