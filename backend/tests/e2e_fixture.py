from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from geoalchemy2.elements import WKTElement
from sqlalchemy import select

from app.assessment.models import AssessmentRun
from app.artifacts.catalog import load_catalog
from app.auth.models import User
from app.auth.service import UserRepository
from app.collector.models import CollectorRuntimeState
from app.config import settings
from app.db import SessionFactory
from app.events.models import (
    EarthquakeEvent,
    EarthquakeRevision,
    EventLifecycleOutbox,
    RawMessage,
)
from tests.artifact_helpers import (
    ArtifactAcceptanceEnvironment,
    ArtifactAssessmentFixture,
    SeededArtifactAssessment,
)
from app.security import hash_password


E2E_EVENT_ID = UUID("00000000-0000-4000-8000-000000000015")
E2E_RAW_MESSAGE_ID = UUID("00000000-0000-4000-8000-000000000019")
E2E_REVISION_ID = UUID("00000000-0000-4000-8000-000000000016")
E2E_OUTBOX_ID = UUID("00000000-0000-4000-8000-000000000018")
E2E_ASSESSMENT_RUN_ID = UUID("00000000-0000-4000-8000-000000000017")

_PLACE = "上海成果中心验收测试事件"


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def seed_fixture() -> dict[str, object]:
    await _configure_e2e_login()
    now = datetime.now(UTC)
    deadline_at = now + timedelta(seconds=300)
    async with SessionFactory() as session:
        async with session.begin():
            raw = await session.get(RawMessage, E2E_RAW_MESSAGE_ID)
            if raw is None:
                raw = RawMessage(id=E2E_RAW_MESSAGE_ID)
                session.add(raw)
            raw.source = "artifact-e2e"
            raw.source_message_id = "artifact-e2e-formal"
            raw.message_kind = "test"
            raw.provider = "cenc"
            raw.ingest_lane = "e2e"
            raw.received_at = now
            raw.checksum = _checksum("artifact-e2e-raw-message")
            raw.payload = {"fixture": "artifact-center-e2e"}

            event = await session.get(EarthquakeEvent, E2E_EVENT_ID)
            if event is None:
                event = EarthquakeEvent(id=E2E_EVENT_ID)
                session.add(event)
            event.source = "artifact-e2e"
            event.canonical_source_id = "artifact-e2e-cenc"
            event.event_type = "test"
            event.origin_time = now - timedelta(minutes=2)
            event.longitude = Decimal("121.500000")
            event.latitude = Decimal("31.200000")
            event.depth_km = Decimal("10.00")
            event.magnitude = Decimal("5.1")
            event.place = _PLACE
            event.geom = WKTElement("POINT(121.5 31.2)", srid=4326)
            event.t1_at = now
            event.lifecycle_state = "assessment_triggered"
            event.institutional_level = "三级"
            event.service_level = 3
            event.response_suggestion = {
                "institutional_level": "三级",
                "service_level": 3,
                "causes": ["E2E test fixture"],
            }
            event.response_rule_version = "e2e-v1"
            await session.flush()

            revision = await session.get(EarthquakeRevision, E2E_REVISION_ID)
            if revision is None:
                revision = EarthquakeRevision(id=E2E_REVISION_ID)
                session.add(revision)
            revision.event_id = event.id
            revision.raw_message_id = raw.id
            revision.revision_no = 1
            revision.revision_kind = "test"
            revision.source_event_id = "artifact-e2e-formal"
            revision.origin_time = event.origin_time
            revision.longitude = event.longitude
            revision.latitude = event.latitude
            revision.depth_km = event.depth_km
            revision.magnitude = event.magnitude
            revision.place = event.place
            revision.ingested_at = now
            revision.is_current = True
            revision.provider = "cenc"
            revision.ingest_lane = "e2e"
            revision.inside_shanghai = True
            revision.distance_to_boundary_km = Decimal("0")
            revision.note = "deterministic Playwright acceptance fixture"
            await session.flush()

            outbox = await session.get(EventLifecycleOutbox, E2E_OUTBOX_ID)
            if outbox is None:
                outbox = EventLifecycleOutbox(id=E2E_OUTBOX_ID)
                session.add(outbox)
            outbox.event_id = event.id
            outbox.revision_id = revision.id
            outbox.trigger_type = "assessment.requested"
            outbox.trigger_reason = "test"
            outbox.payload = {
                "event_id": str(event.id),
                "revision_id": str(revision.id),
            }
            outbox.status = "published"
            outbox.created_at = now
            outbox.available_at = now
            outbox.published_at = now

            assessment = await session.get(
                AssessmentRun,
                E2E_ASSESSMENT_RUN_ID,
            )
            if assessment is None:
                assessment = AssessmentRun(id=E2E_ASSESSMENT_RUN_ID)
                session.add(assessment)
            assessment.event_id = event.id
            assessment.revision_id = revision.id
            assessment.outbox_id = outbox.id
            assessment.run_no = 1
            assessment.trigger_reason = "test"
            assessment.status = "completed"
            assessment.priority = 1
            assessment.t1_at = now
            assessment.deadline_at = deadline_at
            assessment.started_at = now
            assessment.completed_at = now
            assessment.report_ingested_at = now
            assessment.deadline_basis_at = now
            assessment.snapshot = {"fixture": "artifact-center-e2e"}
            assessment.duration_ms = 0
            await session.flush()

            event.current_revision_id = revision.id
            event.latest_trigger_revision_id = revision.id
            event.latest_assessment_run_id = assessment.id
            event.effective_assessment_run_id = assessment.id

    seeded = ArtifactAssessmentFixture(
        SessionFactory,
        SeededArtifactAssessment(
            event_id=E2E_EVENT_ID,
            revision_id=E2E_REVISION_ID,
            assessment_run_id=E2E_ASSESSMENT_RUN_ID,
            deadline_basis_at=now,
            deadline_at=deadline_at,
            catalog=load_catalog(settings.artifact_catalog_path),
        ),
    )
    environment = ArtifactAcceptanceEnvironment(SessionFactory, seeded)
    try:
        result = await environment.run_test_event(
            magnitude=5.1,
            production_mode="test",
        )
        if (
            result.required_output_count != 39
            or result.complete_count + result.degraded_count != 39
            or result.failed_count != 0
            or result.timeout_count != 0
        ):
            raise RuntimeError(
                "E2E artifact fixture did not produce 39 accepted outputs"
            )
        async with SessionFactory() as session:
            async with session.begin():
                for provider, connected in (("fan", False), ("wolfx", True)):
                    runtime = await session.get(CollectorRuntimeState, provider)
                    if runtime is None:
                        runtime = CollectorRuntimeState(provider=provider)
                        session.add(runtime)
                    runtime.state = "healthy"
                    runtime.connected = connected
                    runtime.last_http_status = 200
                    runtime.last_connected_at = now
                    runtime.last_message_at = now
                    runtime.last_success_at = now
                    runtime.consecutive_failures = 0
                    runtime.reconnect_count = 0
                    runtime.last_error = None
                    runtime.updated_at = now
        return {
            "event_id": str(E2E_EVENT_ID),
            "production_run_id": result.production_run_id,
            "required_output_count": result.required_output_count,
            "complete_count": result.complete_count,
            "degraded_count": result.degraded_count,
            "failed_count": result.failed_count,
            "timeout_count": result.timeout_count,
            "elapsed_seconds": result.elapsed_seconds,
        }
    finally:
        await environment._stop_worker()


async def _configure_e2e_login() -> None:
    password = os.environ.get("E2E_SUPERADMIN_PASSWORD")
    if not password:
        return
    username = os.environ.get("E2E_SUPERADMIN_USERNAME", "superadmin")
    async with SessionFactory() as session:
        async with session.begin():
            user = await session.scalar(
                select(User).where(User.username == username)
            )
            if user is None:
                await UserRepository(SessionFactory).create(
                    session,
                    username=username,
                    password_hash=hash_password(password),
                    role="superadmin",
                )
                return
            user.password_hash = hash_password(password)
            user.role = "superadmin"
            user.workgroup = None
            user.is_active = True


async def main() -> None:
    result = await seed_fixture()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
