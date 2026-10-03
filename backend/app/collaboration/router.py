from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.router import get_current_user, require_role
from app.auth.service import AuthUser
from app.collaboration.domain import DutyRole
from app.collaboration.repository import CollaborationRepository
from app.collaboration.roster import (
    EventRosterGroup,
    MemberInput,
    RosterService,
)
from app.collaboration.schemas import (
    AttendanceResponse,
    AttendanceUpdateRequest,
    ConfirmingAuthorityResponse,
    EventWorkgroupResponse,
    EventWorkgroupsResponse,
    MembershipReplaceRequest,
    MembershipResponse,
    RosterMemberResponse,
    TaskCancelRequest,
    TaskContributorResponse,
    TaskReturnRequest,
    TaskSubmitRequest,
    TaskUpdateRequest,
    WorkgroupResponse,
    WorkgroupTaskResponse,
)
from app.collaboration.service import (
    CollaborationTaskService,
    MissingRequiredDeliverableError,
    StaleTaskVersion,
)
from app.db import SessionFactory

router = APIRouter(prefix="/api/v1", tags=["workgroups"])


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
        if (
            current_user.role != "superadmin"
            and not await service.can_manage_attendance(
                session,
                event_id,
                code,
                request.user_id,
                current_user.username,
            )
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
        usernames = {
            membership.user_id: user.username
            for membership, user in memberships
        }
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
    _current_user: AuthUser = Depends(get_current_user),
) -> list[WorkgroupTaskResponse]:
    try:
        tasks = await service.repository.list_tasks(session, event_id)
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc

    responses: list[WorkgroupTaskResponse] = []
    for task in tasks:
        responses.append(await _task_response(session, service, task))
    return responses


@router.get(
    "/collaboration/tasks/{task_id}",
    response_model=WorkgroupTaskResponse,
)
async def get_collaboration_task(
    task_id: UUID,
    session: AsyncSession = Depends(get_roster_session),
    service: CollaborationTaskService = Depends(get_task_service),
    _current_user: AuthUser = Depends(get_current_user),
) -> WorkgroupTaskResponse:
    try:
        task = await service.repository.get_task(session, task_id)
        if task is None:
            raise LookupError("task_not_found")
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise _storage_unavailable() from exc
    return await _task_response(session, service, task)


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
    return await _task_response(session, service, task)


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
    return await _task_response(session, service, task)


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
    return await _task_response(session, service, task)


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
    return await _task_response(session, service, task)


@router.post(
    "/collaboration/tasks/{task_id}/cancel",
    response_model=WorkgroupTaskResponse,
)
async def cancel_collaboration_task(
    task_id: UUID,
    request: TaskCancelRequest,
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
    return await _task_response(session, service, task)


@router.patch(
    "/collaboration/tasks/{task_id}",
    response_model=WorkgroupTaskResponse,
)
async def update_collaboration_task(
    task_id: UUID,
    request: TaskUpdateRequest,
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
    return await _task_response(session, service, task)


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
        UUID(str(entry["user_id"])): str(entry["username"])
        for entry in _group_entries(group)
    }
    return EventWorkgroupResponse(
        code=group.code,
        name=group.name,
        display_order=group.display_order,
        roster_version=group.roster_version,
        roster_fingerprint=group.roster_fingerprint,
        leader=_roster_member_response(group.leader) if group.leader else None,
        deputies=[
            _roster_member_response(entry) for entry in group.deputies
        ],
        members=[
            _roster_member_response(entry) for entry in group.members
        ],
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
            int(entry["deputy_order"])
            if entry.get("deputy_order") is not None
            else None
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
) -> WorkgroupTaskResponse:
    contributors = await service.repository.list_contributors(
        session, task.id
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
    )


def _parse_if_match(if_match: str) -> int:
    try:
        return int(if_match.strip())
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="If-Match must be an integer",
        ) from exc


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
