from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.collaboration.domain import DutyRole, WorkgroupCode
from app.collaboration.models import (
    WorkgroupAttendance,
    WorkgroupDefinition,
    WorkgroupMembership,
    WorkgroupRosterSnapshot,
)
from app.events.models import EarthquakeEvent

_ATTENDANCE_STATES = {"unknown", "present", "absent", "departed"}


@dataclass(frozen=True, slots=True)
class MemberInput:
    user_id: uuid.UUID
    duty_role: DutyRole
    deputy_order: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, uuid.UUID):
            raise TypeError("user_id must be a UUID")
        object.__setattr__(self, "duty_role", DutyRole(self.duty_role))


@dataclass(frozen=True, slots=True)
class ConfirmingAuthority:
    user_id: uuid.UUID
    role: DutyRole
    deputy_order: int | None


@dataclass(frozen=True, slots=True)
class EventRosterGroup:
    code: str
    name: str
    display_order: int
    roster_version: int
    roster_fingerprint: str
    leader: dict[str, object] | None
    deputies: tuple[dict[str, object], ...]
    members: tuple[dict[str, object], ...]
    attendance: tuple[WorkgroupAttendance, ...]
    authority: ConfirmingAuthority | None


class RosterRepository:
    async def get_event(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> EarthquakeEvent | None:
        statement = select(EarthquakeEvent).where(
            EarthquakeEvent.id == event_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_workgroup(
        self,
        session: AsyncSession,
        group_code: str,
    ) -> WorkgroupDefinition | None:
        return await session.scalar(
            select(WorkgroupDefinition).where(
                WorkgroupDefinition.code == group_code,
                WorkgroupDefinition.is_active.is_(True),
            )
        )

    async def list_workgroups(
        self,
        session: AsyncSession,
    ) -> tuple[WorkgroupDefinition, ...]:
        rows = await session.scalars(
            select(WorkgroupDefinition)
            .where(WorkgroupDefinition.is_active.is_(True))
            .order_by(WorkgroupDefinition.display_order)
        )
        return tuple(rows)

    async def list_active_memberships(
        self,
        session: AsyncSession,
        group_code: str | None = None,
        *,
        for_update: bool = False,
    ) -> tuple[tuple[WorkgroupMembership, User], ...]:
        statement = (
            select(WorkgroupMembership, User)
            .join(User, User.id == WorkgroupMembership.user_id)
            .where(
                WorkgroupMembership.is_active.is_(True),
                WorkgroupMembership.effective_to.is_(None),
            )
            .order_by(
                WorkgroupMembership.workgroup_code,
                WorkgroupMembership.duty_role,
                WorkgroupMembership.deputy_order,
                User.username,
            )
        )
        if group_code is not None:
            statement = statement.where(
                WorkgroupMembership.workgroup_code == group_code
            )
        if for_update:
            statement = statement.with_for_update()
        rows = await session.execute(statement)
        return tuple((membership, user) for membership, user in rows.all())

    async def list_users(
        self,
        session: AsyncSession,
        user_ids: Sequence[uuid.UUID],
    ) -> tuple[User, ...]:
        if not user_ids:
            return ()
        rows = await session.scalars(
            select(User).where(
                User.id.in_(tuple(user_ids)),
                User.is_active.is_(True),
            )
        )
        return tuple(rows)

    async def get_user_by_username(
        self,
        session: AsyncSession,
        username: str,
    ) -> User | None:
        return await session.scalar(
            select(User).where(
                User.username == username,
                User.is_active.is_(True),
            )
        )

    async def get_roster_snapshot(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        group_code: str,
        *,
        for_update: bool = False,
    ) -> WorkgroupRosterSnapshot | None:
        statement = (
            select(WorkgroupRosterSnapshot)
            .where(
                WorkgroupRosterSnapshot.event_id == event_id,
                WorkgroupRosterSnapshot.workgroup_code == group_code,
            )
            .order_by(WorkgroupRosterSnapshot.roster_version.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def list_roster_snapshots(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> tuple[WorkgroupRosterSnapshot, ...]:
        rows = await session.scalars(
            select(WorkgroupRosterSnapshot)
            .where(WorkgroupRosterSnapshot.event_id == event_id)
            .order_by(
                WorkgroupRosterSnapshot.workgroup_code,
                WorkgroupRosterSnapshot.roster_version.desc(),
            )
        )
        latest: dict[str, WorkgroupRosterSnapshot] = {}
        for row in rows:
            latest.setdefault(row.workgroup_code, row)
        return tuple(latest.values())

    async def list_attendance(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        group_code: str,
    ) -> tuple[WorkgroupAttendance, ...]:
        rows = await session.scalars(
            select(WorkgroupAttendance)
            .where(
                WorkgroupAttendance.event_id == event_id,
                WorkgroupAttendance.workgroup_code == group_code,
            )
            .order_by(WorkgroupAttendance.created_at, WorkgroupAttendance.user_id)
        )
        return tuple(rows)

    async def get_attendance(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        group_code: str,
        user_id: uuid.UUID,
    ) -> WorkgroupAttendance | None:
        return await session.scalar(
            select(WorkgroupAttendance)
            .where(
                WorkgroupAttendance.event_id == event_id,
                WorkgroupAttendance.workgroup_code == group_code,
                WorkgroupAttendance.user_id == user_id,
            )
            .with_for_update()
        )


class RosterService:
    def __init__(self, repository: RosterRepository | None = None) -> None:
        self.repository = repository or RosterRepository()

    async def list_workgroups(
        self,
        session: AsyncSession,
    ) -> tuple[WorkgroupDefinition, ...]:
        return await self.repository.list_workgroups(session)

    async def list_group_memberships(
        self,
        session: AsyncSession,
        group_code: str,
    ) -> tuple[tuple[WorkgroupMembership, User], ...]:
        normalized_code = _workgroup_code(group_code)
        if await self.repository.get_workgroup(session, normalized_code) is None:
            raise LookupError("workgroup_not_found")
        return await self.repository.list_active_memberships(
            session, normalized_code
        )

    async def replace_group_members(
        self,
        session: AsyncSession,
        group_code: str,
        members: Sequence[MemberInput],
        actor: str,
    ) -> None:
        normalized_code = _workgroup_code(group_code)
        if await self.repository.get_workgroup(session, normalized_code) is None:
            raise LookupError("workgroup_not_found")

        normalized_members = tuple(members)
        _validate_members(normalized_members)
        users = await self.repository.list_users(
            session, [member.user_id for member in normalized_members]
        )
        if {user.id for user in users} != {
            member.user_id for member in normalized_members
        }:
            raise LookupError("workgroup_member_not_found")

        now = datetime.now(UTC)
        active = await self.repository.list_active_memberships(
            session,
            normalized_code,
            for_update=True,
        )
        for membership, _user in active:
            membership.is_active = False
            membership.effective_to = now
            membership.updated_at = now
        await session.flush()

        for member in normalized_members:
            session.add(
                WorkgroupMembership(
                    user_id=member.user_id,
                    workgroup_code=normalized_code,
                    duty_role=member.duty_role.value,
                    deputy_order=member.deputy_order,
                    effective_from=now,
                    is_active=True,
                    created_by=actor,
                )
            )
        await session.flush()

    async def snapshot_for_event(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> tuple[WorkgroupRosterSnapshot, ...]:
        event = await self.repository.get_event(
            session, event_id, for_update=True
        )
        if event is None:
            raise LookupError("event_not_found")

        definitions = await self.repository.list_workgroups(session)
        existing = {
            snapshot.workgroup_code: snapshot
            for snapshot in await self.repository.list_roster_snapshots(
                session, event_id
            )
        }
        memberships = await self.repository.list_active_memberships(session)
        memberships_by_group: dict[
            str, list[tuple[WorkgroupMembership, User]]
        ] = {}
        for membership, user in memberships:
            memberships_by_group.setdefault(membership.workgroup_code, []).append(
                (membership, user)
            )

        for definition in definitions:
            snapshot = existing.get(definition.code)
            if snapshot is None:
                snapshot = self._create_snapshot(
                    event_id,
                    definition,
                    memberships_by_group.get(definition.code, []),
                )
                session.add(snapshot)
                await session.flush()
                existing[definition.code] = snapshot
            await self._ensure_attendance_rows(session, snapshot)

        return tuple(
            existing[definition.code]
            for definition in definitions
            if definition.code in existing
        )

    def _create_snapshot(
        self,
        event_id: uuid.UUID,
        definition: WorkgroupDefinition,
        memberships: Sequence[tuple[WorkgroupMembership, User]],
    ) -> WorkgroupRosterSnapshot:
        leader: dict[str, object] | None = None
        deputies: list[dict[str, object]] = []
        members: list[dict[str, object]] = []
        for membership, user in memberships:
            entry = _membership_entry(membership, user)
            if membership.duty_role == DutyRole.LEADER.value:
                leader = entry
            elif membership.duty_role == DutyRole.DEPUTY.value:
                deputies.append(entry)
            else:
                members.append(entry)
        deputies.sort(key=_entry_order)
        members.sort(key=lambda entry: str(entry["username"]))
        payload = {
            "workgroup_code": definition.code,
            "leader": leader,
            "deputies": deputies,
            "members": members,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return WorkgroupRosterSnapshot(
            event_id=event_id,
            workgroup_code=definition.code,
            roster_version=1,
            roster_fingerprint=fingerprint,
            leader_user_id=(
                uuid.UUID(str(leader["user_id"])) if leader is not None else None
            ),
            deputies=deputies,
            members=members,
            snapshot={
                **payload,
                "roster_version": 1,
                "roster_fingerprint": fingerprint,
            },
        )

    async def _ensure_attendance_rows(
        self,
        session: AsyncSession,
        snapshot: WorkgroupRosterSnapshot,
    ) -> None:
        attendance = await self.repository.list_attendance(
            session, snapshot.event_id, snapshot.workgroup_code
        )
        existing_user_ids = {row.user_id for row in attendance}
        for entry in _snapshot_entries(snapshot):
            user_id = uuid.UUID(str(entry["user_id"]))
            if user_id in existing_user_ids:
                continue
            session.add(
                WorkgroupAttendance(
                    event_id=snapshot.event_id,
                    workgroup_code=snapshot.workgroup_code,
                    user_id=user_id,
                    duty_role_in_snapshot=str(entry["duty_role"]),
                    deputy_order_in_snapshot=_optional_order(entry),
                    state="unknown",
                    updated_by="system",
                )
            )
            existing_user_ids.add(user_id)
        await session.flush()

    async def set_attendance(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        group_code: str,
        user_id: uuid.UUID,
        state: str,
        actor: str,
    ) -> WorkgroupAttendance:
        normalized_code = _workgroup_code(group_code)
        normalized_state = str(state)
        if normalized_state not in _ATTENDANCE_STATES:
            raise ValueError(
                "state must be one of unknown, present, absent, departed"
            )
        snapshot = await self.repository.get_roster_snapshot(
            session,
            event_id,
            normalized_code,
            for_update=True,
        )
        if snapshot is None:
            raise LookupError("roster_snapshot_not_found")
        entry = next(
            (
                item
                for item in _snapshot_entries(snapshot)
                if uuid.UUID(str(item["user_id"])) == user_id
            ),
            None,
        )
        if entry is None:
            raise LookupError("roster_member_not_found")

        now = datetime.now(UTC)
        attendance = await self.repository.get_attendance(
            session, event_id, normalized_code, user_id
        )
        if attendance is None:
            attendance = WorkgroupAttendance(
                event_id=event_id,
                workgroup_code=normalized_code,
                user_id=user_id,
                duty_role_in_snapshot=str(entry["duty_role"]),
                deputy_order_in_snapshot=_optional_order(entry),
                updated_by=actor,
            )
            session.add(attendance)
        attendance.duty_role_in_snapshot = str(entry["duty_role"])
        attendance.deputy_order_in_snapshot = _optional_order(entry)
        attendance.state = normalized_state
        attendance.updated_by = actor
        attendance.updated_at = now
        if normalized_state == "present":
            attendance.checked_in_at = attendance.checked_in_at or now
            attendance.checked_out_at = None
        elif normalized_state == "departed":
            attendance.checked_out_at = now
        else:
            attendance.checked_in_at = None
            attendance.checked_out_at = None
        await session.flush()
        return attendance

    async def resolve_confirming_authority(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        group_code: str,
    ) -> ConfirmingAuthority | None:
        normalized_code = _workgroup_code(group_code)
        roster = await self.repository.get_roster_snapshot(
            session, event_id, normalized_code
        )
        if roster is None:
            return None
        attendance = {
            row.user_id: row.state
            for row in await self.repository.list_attendance(
                session, event_id, normalized_code
            )
        }
        if (
            roster.leader_user_id is not None
            and attendance.get(roster.leader_user_id) == "present"
        ):
            return ConfirmingAuthority(
                user_id=roster.leader_user_id,
                role=DutyRole.LEADER,
                deputy_order=None,
            )
        for deputy in sorted(roster.deputies, key=_entry_order):
            user_id = uuid.UUID(str(deputy["user_id"]))
            if attendance.get(user_id) == "present":
                return ConfirmingAuthority(
                    user_id=user_id,
                    role=DutyRole.DEPUTY,
                    deputy_order=_optional_order(deputy),
                )
        return None

    async def can_manage_attendance(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
        group_code: str,
        target_user_id: uuid.UUID,
        actor_username: str,
    ) -> bool:
        actor = await self.repository.get_user_by_username(
            session, actor_username
        )
        if actor is None:
            return False
        roster = await self.repository.get_roster_snapshot(
            session, event_id, group_code
        )
        if roster is None:
            return False
        entries = _snapshot_entries(roster)
        actor_entry = next(
            (
                entry
                for entry in entries
                if uuid.UUID(str(entry["user_id"])) == actor.id
            ),
            None,
        )
        if actor_entry is None:
            return False
        actor_role = DutyRole(str(actor_entry["duty_role"]))
        if actor_role is DutyRole.LEADER:
            return any(
                uuid.UUID(str(entry["user_id"])) == target_user_id
                for entry in entries
            )
        if actor_role in {DutyRole.DEPUTY, DutyRole.MEMBER}:
            return actor.id == target_user_id
        return False

    async def event_rosters(
        self,
        session: AsyncSession,
        event_id: uuid.UUID,
    ) -> tuple[EventRosterGroup, ...]:
        event = await self.repository.get_event(session, event_id)
        if event is None:
            raise LookupError("event_not_found")
        snapshots = await self.repository.list_roster_snapshots(session, event_id)
        definitions = {
            definition.code: definition
            for definition in await self.repository.list_workgroups(session)
        }
        groups: list[EventRosterGroup] = []
        for snapshot in snapshots:
            definition = definitions[snapshot.workgroup_code]
            attendance = await self.repository.list_attendance(
                session, event_id, snapshot.workgroup_code
            )
            groups.append(
                EventRosterGroup(
                    code=definition.code,
                    name=definition.name,
                    display_order=definition.display_order,
                    roster_version=snapshot.roster_version,
                    roster_fingerprint=snapshot.roster_fingerprint,
                    leader=(
                        dict(snapshot.snapshot.get("leader"))
                        if snapshot.snapshot.get("leader") is not None
                        else None
                    ),
                    deputies=tuple(
                        dict(item) for item in snapshot.snapshot.get("deputies", ())
                    ),
                    members=tuple(
                        dict(item) for item in snapshot.snapshot.get("members", ())
                    ),
                    attendance=attendance,
                    authority=await self.resolve_confirming_authority(
                        session, event_id, snapshot.workgroup_code
                    ),
                )
            )
        return tuple(
            sorted(groups, key=lambda group: group.display_order)
        )


def _workgroup_code(group_code: str) -> str:
    try:
        return WorkgroupCode(str(group_code)).value
    except ValueError as exc:
        raise LookupError("workgroup_not_found") from exc


def _validate_members(members: Sequence[MemberInput]) -> None:
    user_ids: set[uuid.UUID] = set()
    leaders = 0
    deputy_orders: set[int] = set()
    for member in members:
        if not isinstance(member, MemberInput):
            raise TypeError("members must contain MemberInput values")
        if member.user_id in user_ids:
            raise ValueError("each user may appear only once in a roster")
        user_ids.add(member.user_id)
        if member.duty_role is DutyRole.LEADER:
            leaders += 1
            if member.deputy_order is not None:
                raise ValueError("deputy_order is only valid for deputies")
        elif member.duty_role is DutyRole.DEPUTY:
            if member.deputy_order is None or member.deputy_order <= 0:
                raise ValueError("deputy_order must be a positive integer")
            if member.deputy_order in deputy_orders:
                raise ValueError("deputy_order values must be unique")
            deputy_orders.add(member.deputy_order)
        elif member.deputy_order is not None:
            raise ValueError("deputy_order is only valid for deputies")
    if leaders > 1:
        raise ValueError("a workgroup may have at most one leader")


def _membership_entry(
    membership: WorkgroupMembership,
    user: User,
) -> dict[str, object]:
    return {
        "user_id": str(membership.user_id),
        "username": user.username,
        "duty_role": membership.duty_role,
        "deputy_order": membership.deputy_order,
    }


def _snapshot_entries(
    snapshot: WorkgroupRosterSnapshot,
) -> tuple[dict[str, object], ...]:
    entries: list[dict[str, object]] = []
    leader = snapshot.snapshot.get("leader")
    if isinstance(leader, dict):
        entries.append(leader)
    entries.extend(
        item
        for item in snapshot.snapshot.get("deputies", ())
        if isinstance(item, dict)
    )
    entries.extend(
        item
        for item in snapshot.snapshot.get("members", ())
        if isinstance(item, dict)
    )
    return tuple(entries)


def _optional_order(entry: dict[str, object]) -> int | None:
    value = entry.get("deputy_order")
    return int(value) if value is not None else None


def _entry_order(entry: dict[str, object]) -> int:
    return int(entry.get("deputy_order") or 0)
