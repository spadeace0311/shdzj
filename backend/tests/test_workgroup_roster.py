import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.auth.models import User
from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.collaboration.domain import DutyRole, WorkgroupCode
from app.collaboration.roster import (
    ConfirmingAuthority,
    MemberInput,
    RosterService,
)
from app.collaboration.router import get_roster_session
from app.config import settings
from app.events.models import EarthquakeEvent
from app.main import app


@pytest.fixture
async def session():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            transaction = await session.begin()
            try:
                yield session
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def user_factory(session):
    async def factory(
        label: str,
        role: str,
        workgroup: str | None,
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            username=f"roster-{label}-{uuid.uuid4().hex[:8]}",
            password_hash="not-used",
            role=role,
            workgroup=workgroup,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        return user

    return factory


@pytest.fixture
def event_factory(session):
    async def factory() -> EarthquakeEvent:
        event_id = uuid.uuid4()
        event = EarthquakeEvent(
            id=event_id,
            source="roster-test",
            canonical_source_id=f"roster-{event_id}",
            event_type="formal",
            origin_time=datetime.now(UTC),
            longitude=Decimal("121.500000"),
            latitude=Decimal("31.200000"),
            depth_km=Decimal("10.00"),
            magnitude=Decimal("5.2"),
            place="roster test event",
            geom=WKTElement("POINT(121.5 31.2)", srid=4326),
            lifecycle_state="active",
        )
        session.add(event)
        await session.flush()
        return event

    return factory


async def test_deputy_authority_skips_absent_deputy_by_order(
    session,
    event_factory,
    user_factory,
):
    event = await event_factory()
    leader = await user_factory("leader", "group_leader", "monitoring_forecast")
    deputy_1 = await user_factory(
        "deputy-1", "group_deputy", "monitoring_forecast"
    )
    deputy_2 = await user_factory(
        "deputy-2", "group_deputy", "monitoring_forecast"
    )
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [
            MemberInput(leader.id, DutyRole.LEADER, None),
            MemberInput(deputy_1.id, DutyRole.DEPUTY, 1),
            MemberInput(deputy_2.id, DutyRole.DEPUTY, 2),
        ],
        actor="superadmin",
    )
    await service.snapshot_for_event(session, event.id)
    await service.set_attendance(
        session, event.id, "monitoring_forecast", deputy_1.id, "absent", "superadmin"
    )
    await service.set_attendance(
        session, event.id, "monitoring_forecast", deputy_2.id, "present", "superadmin"
    )

    authority = await service.resolve_confirming_authority(
        session, event.id, "monitoring_forecast"
    )

    assert authority == ConfirmingAuthority(
        user_id=deputy_2.id,
        role=DutyRole.DEPUTY,
        deputy_order=2,
    )


async def test_deputy_authority_uses_lowest_order_when_multiple_deputies_present(
    session,
    event_factory,
    user_factory,
):
    event = await event_factory()
    leader = await user_factory("leader", "group_leader", "monitoring_forecast")
    deputy_1 = await user_factory(
        "deputy-1", "group_deputy", "monitoring_forecast"
    )
    deputy_2 = await user_factory(
        "deputy-2", "group_deputy", "monitoring_forecast"
    )
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [
            MemberInput(leader.id, DutyRole.LEADER, None),
            MemberInput(deputy_1.id, DutyRole.DEPUTY, 1),
            MemberInput(deputy_2.id, DutyRole.DEPUTY, 2),
        ],
        actor="superadmin",
    )
    await service.snapshot_for_event(session, event.id)
    for deputy in (deputy_1, deputy_2):
        await service.set_attendance(
            session,
            event.id,
            "monitoring_forecast",
            deputy.id,
            "present",
            "superadmin",
        )

    authority = await service.resolve_confirming_authority(
        session, event.id, "monitoring_forecast"
    )

    assert authority == ConfirmingAuthority(
        user_id=deputy_1.id,
        role=DutyRole.DEPUTY,
        deputy_order=1,
    )


async def test_confirmation_is_blocked_when_no_leader_or_deputy_is_present(
    session,
    event_factory,
    user_factory,
):
    event = await event_factory()
    member = await user_factory(
        "member", "group_member", "monitoring_forecast"
    )
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(member.id, DutyRole.MEMBER, None)],
        actor="superadmin",
    )
    await service.snapshot_for_event(session, event.id)

    assert (
        await service.resolve_confirming_authority(
            session, event.id, "monitoring_forecast"
        )
        is None
    )


async def test_present_leader_takes_precedence_over_present_deputy(
    session,
    event_factory,
    user_factory,
):
    event = await event_factory()
    leader = await user_factory("leader", "group_leader", "monitoring_forecast")
    deputy = await user_factory("deputy", "group_deputy", "monitoring_forecast")
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [
            MemberInput(leader.id, DutyRole.LEADER, None),
            MemberInput(deputy.id, DutyRole.DEPUTY, 1),
        ],
        actor="superadmin",
    )
    await service.snapshot_for_event(session, event.id)
    await service.set_attendance(
        session, event.id, "monitoring_forecast", leader.id, "present", "superadmin"
    )
    await service.set_attendance(
        session, event.id, "monitoring_forecast", deputy.id, "present", "superadmin"
    )

    authority = await service.resolve_confirming_authority(
        session, event.id, "monitoring_forecast"
    )

    assert authority == ConfirmingAuthority(
        user_id=leader.id,
        role=DutyRole.LEADER,
        deputy_order=None,
    )


async def test_event_snapshot_contains_all_groups_and_is_idempotent(
    session,
    event_factory,
    user_factory,
):
    event = await event_factory()
    leader = await user_factory("leader", "group_leader", "monitoring_forecast")
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(leader.id, DutyRole.LEADER, None)],
        actor="superadmin",
    )

    first = await service.snapshot_for_event(session, event.id)
    second = await service.snapshot_for_event(session, event.id)

    assert [item.workgroup_code for item in first] == [
        group.value for group in WorkgroupCode
    ]
    assert [item.id for item in second] == [item.id for item in first]
    monitoring = next(
        item for item in second if item.workgroup_code == "monitoring_forecast"
    )
    assert monitoring.leader_user_id == leader.id


async def test_snapshot_is_frozen_after_membership_replacement(
    session,
    event_factory,
    user_factory,
):
    event = await event_factory()
    first_leader = await user_factory(
        "first-leader", "group_leader", "monitoring_forecast"
    )
    second_leader = await user_factory(
        "second-leader", "group_leader", "monitoring_forecast"
    )
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(first_leader.id, DutyRole.LEADER, None)],
        actor="superadmin",
    )
    await service.snapshot_for_event(session, event.id)

    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(second_leader.id, DutyRole.LEADER, None)],
        actor="superadmin",
    )
    snapshots = await service.snapshot_for_event(session, event.id)

    monitoring = next(
        item for item in snapshots if item.workgroup_code == "monitoring_forecast"
    )
    assert monitoring.leader_user_id == first_leader.id


async def test_replace_members_validates_order_and_deactivates_previous_roster(
    session,
    user_factory,
):
    first = await user_factory("first", "group_leader", "monitoring_forecast")
    second = await user_factory("second", "group_leader", "monitoring_forecast")
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(first.id, DutyRole.LEADER, None)],
        actor="superadmin",
    )
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(second.id, DutyRole.LEADER, None)],
        actor="superadmin",
    )

    rows = (
        await session.execute(
            text(
                """
                SELECT user_id, is_active
                FROM workgroup_memberships
                WHERE workgroup_code = 'monitoring_forecast'
                ORDER BY created_at, user_id
                """
            )
        )
    ).all()
    states = {row.user_id: row.is_active for row in rows}
    assert states[first.id] is False
    assert states[second.id] is True

    with pytest.raises(ValueError, match="deputy_order"):
        await service.replace_group_members(
            session,
            "monitoring_forecast",
            [
                MemberInput(second.id, DutyRole.DEPUTY, 1),
                MemberInput(first.id, DutyRole.DEPUTY, 1),
            ],
            actor="superadmin",
        )


async def test_attendance_tracks_presence_and_requires_snapshot_member(
    session,
    event_factory,
    user_factory,
):
    event = await event_factory()
    member = await user_factory("member", "group_member", "monitoring_forecast")
    outsider = await user_factory(
        "outsider", "group_member", "monitoring_forecast"
    )
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(member.id, DutyRole.MEMBER, None)],
        actor="superadmin",
    )
    await service.snapshot_for_event(session, event.id)

    present = await service.set_attendance(
        session, event.id, "monitoring_forecast", member.id, "present", "superadmin"
    )
    assert present.state == "present"
    assert present.checked_in_at is not None

    departed = await service.set_attendance(
        session, event.id, "monitoring_forecast", member.id, "departed", "superadmin"
    )
    assert departed.checked_out_at is not None

    with pytest.raises(LookupError):
        await service.set_attendance(
            session,
            event.id,
            "monitoring_forecast",
            outsider.id,
            "present",
            "superadmin",
        )


async def test_workgroup_routes_enforce_read_and_mutation_permissions(
    session,
    event_factory,
    user_factory,
):
    event = await event_factory()
    leader = await user_factory("leader", "group_leader", "monitoring_forecast")
    member = await user_factory("member", "group_member", "monitoring_forecast")
    other_member = await user_factory(
        "other-member", "group_member", "monitoring_forecast"
    )
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [
            MemberInput(leader.id, DutyRole.LEADER, None),
            MemberInput(member.id, DutyRole.MEMBER, None),
            MemberInput(other_member.id, DutyRole.MEMBER, None),
        ],
        actor="superadmin",
    )
    await service.snapshot_for_event(session, event.id)

    async def override_session():
        yield session

    app.dependency_overrides[get_roster_session] = override_session
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            app.dependency_overrides[get_current_user] = lambda: AuthUser(
                username=member.username,
                role="group_member",
                workgroup="monitoring_forecast",
            )
            own = await client.put(
                f"/api/v1/events/{event.id}/workgroups/monitoring_forecast/attendance",
                json={"user_id": str(member.id), "state": "present"},
            )
            app.dependency_overrides[get_current_user] = lambda: AuthUser(
                username=member.username,
                role="group_member",
                workgroup="monitoring_forecast",
            )
            other = await client.put(
                f"/api/v1/events/{event.id}/workgroups/monitoring_forecast/attendance",
                json={"user_id": str(other_member.id), "state": "present"},
            )
            app.dependency_overrides[get_current_user] = lambda: AuthUser(
                username=leader.username,
                role="group_leader",
                workgroup="monitoring_forecast",
            )
            group_wide = await client.put(
                f"/api/v1/events/{event.id}/workgroups/monitoring_forecast/attendance",
                json={"user_id": str(other_member.id), "state": "present"},
            )
            app.dependency_overrides[get_current_user] = lambda: AuthUser(
                username="viewer",
                role="viewer",
                workgroup=None,
            )
            read = await client.get(
                f"/api/v1/events/{event.id}/workgroups"
            )
    finally:
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert own.status_code == 200
    assert other.status_code == 403
    assert group_wide.status_code == 200
    assert read.status_code == 200
    body = read.json()
    assert len(body["groups"]) == 7


async def test_membership_routes_require_superadmin(
    session,
    user_factory,
):
    leader = await user_factory("leader", "group_leader", "monitoring_forecast")
    deputy = await user_factory("deputy", "group_deputy", "monitoring_forecast")

    async def override_session():
        yield session

    app.dependency_overrides[get_roster_session] = override_session
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            app.dependency_overrides[get_current_user] = lambda: AuthUser(
                username="viewer",
                role="viewer",
                workgroup=None,
            )
            listed_groups = await client.get("/api/v1/workgroups")
            listed_members = await client.get(
                "/api/v1/workgroups/monitoring_forecast/memberships"
            )

            app.dependency_overrides[get_current_user] = lambda: AuthUser(
                username=leader.username,
                role="group_leader",
                workgroup="monitoring_forecast",
            )
            denied = await client.put(
                "/api/v1/workgroups/monitoring_forecast/memberships",
                json={
                    "members": [
                        {
                            "user_id": str(leader.id),
                            "duty_role": "leader",
                        }
                    ]
                },
            )

            app.dependency_overrides[get_current_user] = lambda: AuthUser(
                username="superadmin",
                role="superadmin",
                workgroup=None,
            )
            replaced = await client.put(
                "/api/v1/workgroups/monitoring_forecast/memberships",
                json={
                    "members": [
                        {
                            "user_id": str(leader.id),
                            "duty_role": "leader",
                        },
                        {
                            "user_id": str(deputy.id),
                            "duty_role": "deputy",
                            "deputy_order": 1,
                        },
                    ]
                },
            )
    finally:
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert listed_groups.status_code == 200
    assert len(listed_groups.json()) == 7
    assert listed_members.status_code == 200
    assert denied.status_code == 403
    assert replaced.status_code == 200
    assert {
        (item["user_id"], item["duty_role"], item["deputy_order"])
        for item in replaced.json()
    } == {
        (str(leader.id), "leader", None),
        (str(deputy.id), "deputy", 1),
    }


async def test_event_roster_read_does_not_create_snapshot(
    session,
    event_factory,
    user_factory,
):
    event = await event_factory()
    first_leader = await user_factory(
        "first-leader", "group_leader", "monitoring_forecast"
    )
    intended_leader = await user_factory(
        "intended-leader", "group_leader", "monitoring_forecast"
    )
    service = RosterService()
    await service.replace_group_members(
        session,
        "monitoring_forecast",
        [MemberInput(first_leader.id, DutyRole.LEADER, None)],
        actor="superadmin",
    )

    async def override_session():
        yield session

    app.dependency_overrides[get_roster_session] = override_session
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        username="viewer",
        role="viewer",
        workgroup=None,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            first_read = await client.get(
                f"/api/v1/events/{event.id}/workgroups"
            )

        await service.replace_group_members(
            session,
            "monitoring_forecast",
            [MemberInput(intended_leader.id, DutyRole.LEADER, None)],
            actor="superadmin",
        )

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            second_read = await client.get(
                f"/api/v1/events/{event.id}/workgroups"
            )
    finally:
        app.dependency_overrides.pop(get_roster_session, None)
        app.dependency_overrides.pop(get_current_user, None)

    assert first_read.status_code == 200
    assert first_read.json()["groups"] == []
    assert second_read.status_code == 200
    assert second_read.json()["groups"] == []
    assert not await service.repository.list_roster_snapshots(session, event.id)

    snapshots = await service.snapshot_for_event(session, event.id)

    monitoring = next(
        item for item in snapshots if item.workgroup_code == "monitoring_forecast"
    )
    assert monitoring.leader_user_id == intended_leader.id
