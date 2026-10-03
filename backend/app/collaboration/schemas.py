from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.collaboration.domain import DutyRole


class WorkgroupResponse(BaseModel):
    code: str
    name: str
    display_order: int
    is_active: bool


class MembershipInput(BaseModel):
    user_id: UUID
    duty_role: DutyRole
    deputy_order: int | None = None


class MembershipReplaceRequest(BaseModel):
    members: list[MembershipInput]


class MembershipResponse(BaseModel):
    membership_id: UUID
    user_id: UUID
    username: str
    duty_role: DutyRole
    deputy_order: int | None
    effective_from: datetime
    effective_to: datetime | None
    is_active: bool


class AttendanceUpdateRequest(BaseModel):
    user_id: UUID
    state: Literal["unknown", "present", "absent", "departed"]


class AttendanceResponse(BaseModel):
    user_id: UUID
    username: str | None
    state: str
    duty_role_in_snapshot: DutyRole
    deputy_order_in_snapshot: int | None
    checked_in_at: datetime | None
    checked_out_at: datetime | None
    updated_by: str


class ConfirmingAuthorityResponse(BaseModel):
    user_id: UUID
    role: DutyRole
    deputy_order: int | None


class RosterMemberResponse(BaseModel):
    user_id: UUID
    username: str
    duty_role: DutyRole
    deputy_order: int | None


class EventWorkgroupResponse(BaseModel):
    code: str
    name: str
    display_order: int
    roster_version: int
    roster_fingerprint: str
    leader: RosterMemberResponse | None
    deputies: list[RosterMemberResponse]
    members: list[RosterMemberResponse]
    attendance: list[AttendanceResponse]
    confirming_authority: ConfirmingAuthorityResponse | None


class EventWorkgroupsResponse(BaseModel):
    event_id: UUID
    groups: list[EventWorkgroupResponse] = Field(default_factory=list)


class TaskContributorResponse(BaseModel):
    user_id: UUID
    username: str
    contribution_count: int
    first_contributed_at: datetime
    last_contributed_at: datetime


class WorkgroupTaskResponse(BaseModel):
    id: UUID
    event_id: UUID
    workgroup_code: str
    task_code: str
    title: str
    status: str
    timeliness_state: str
    due_at: datetime | None
    row_version: int
    instruction: str
    priority: int
    phase_code: str | None
    source_type: str
    source_ref: str | None
    activated_at: datetime | None
    completed_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    contributors: list[TaskContributorResponse] = Field(default_factory=list)


class TaskSubmitRequest(BaseModel):
    result_text: str | None = None


class TaskReturnRequest(BaseModel):
    reason: str = Field(min_length=1)


class TaskCancelRequest(BaseModel):
    reason: str | None = None


class TaskUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    instruction: str | None = None
    priority: int | None = Field(default=None, ge=0)
    due_at: datetime | None = None


class DeliverableVersionResponse(BaseModel):
    id: UUID
    deliverable_id: UUID
    version_no: int
    source_kind: str
    artifact_id: UUID | None
    artifact_publication_id: UUID | None
    storage_key: str | None
    file_name: str | None
    checksum: str | None
    mime_type: str | None
    size_bytes: int | None
    text_result: dict[str, Any] | None
    created_by: str | None
    basis_text: str | None
    supersedes_version_id: UUID | None
    created_at: datetime


class DeliverablePublicationResponse(BaseModel):
    id: UUID
    deliverable_id: UUID
    version_id: UUID
    published_by: str
    published_role: str
    published_at: datetime
    superseded_at: datetime | None
    publication_note: str | None
    created_at: datetime


class DeliverableResponse(BaseModel):
    id: UUID
    task_id: UUID
    deliverable_code: str
    title: str
    is_required: bool
    requirement_kind: str
    artifact_binding: dict[str, Any] | None
    display_order: int
    current_version_id: UUID | None
    current_publication: DeliverablePublicationResponse | None
    versions: list[DeliverableVersionResponse] = Field(default_factory=list)


class DeliverableTextVersionRequest(BaseModel):
    text_result: dict[str, Any]
    basis_text: str | None = None


class DeliverablePublishRequest(BaseModel):
    version_id: UUID
    publication_note: str | None = None


class DeliverableOverrideRequest(BaseModel):
    text_result: dict[str, Any] | None = None
    basis_text: str | None = None
    publication_note: str | None = None
