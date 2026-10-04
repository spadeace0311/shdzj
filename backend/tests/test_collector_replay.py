from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.collector.coordinator import CollectorCoordinator
from app.collector.domain import CollectorEnvelope, CollectorLane, CollectorProvider
from app.collector.replay import replay_dead_letter
from app.collector.supervisor import CollectorSupervisor
from app.events.service import LifecycleIngestOutcome
from app.regions.domain import RegionContext


class FakeReplayService:
    def __init__(self) -> None:
        self.record: dict[str, object] | None = {
            "id": "dead-1",
            "raw_payload": {"EventID": "CENC-1"},
            "provider": "wolfx",
            "lane": "http",
            "received_at": datetime(2026, 9, 25, 1, 2, tzinfo=UTC),
            "status": "open",
        }
        self.statuses: list[str] = []
        self.errors: list[object | None] = []

    async def load_dead_letter(self, dead_letter_id: str):
        return self.record

    async def mark_dead_letter(
        self,
        dead_letter_id: str,
        status: str,
        error=None,
    ) -> None:
        self.statuses.append(status)
        self.errors.append(error)


class FakeReplayCoordinator:
    def __init__(self, error: Exception | None = None) -> None:
        self.envelopes = []
        self.trigger_reasons: list[str] = []
        self.error = error

    async def ingest(self, envelope, trigger_reason: str = "live"):
        self.envelopes.append(envelope)
        self.trigger_reasons.append(trigger_reason)
        if self.error is not None:
            raise self.error
        return None


async def test_replay_uses_original_received_at_and_recovery_reason() -> None:
    service = FakeReplayService()
    coordinator = FakeReplayCoordinator()

    await replay_dead_letter("dead-1", service, coordinator)

    assert coordinator.envelopes[0].received_at == service.record["received_at"]
    assert coordinator.envelopes[0].provider.value == "wolfx"
    assert coordinator.envelopes[0].lane.value == "http"
    assert coordinator.envelopes[0].payload == {"No1": {"EventID": "CENC-1"}}
    assert coordinator.trigger_reasons == ["recovery"]
    assert service.statuses == ["retried"]
    assert service.errors == [None]


async def test_replay_marks_second_success_resolved() -> None:
    service = FakeReplayService()
    service.record["status"] = "retried"

    await replay_dead_letter("dead-1", service, FakeReplayCoordinator())

    assert service.statuses == ["resolved"]


async def test_replay_reopens_dead_letter_with_failure_error() -> None:
    service = FakeReplayService()
    error = ValueError("invalid replay payload")
    coordinator = FakeReplayCoordinator(error=error)

    with pytest.raises(ValueError, match="invalid replay payload"):
        await replay_dead_letter("dead-1", service, coordinator)

    assert service.statuses == ["open"]
    assert service.errors == [error]


async def test_replay_rejects_missing_dead_letter() -> None:
    service = FakeReplayService()
    service.record = None

    with pytest.raises(LookupError, match="dead letter not found"):
        await replay_dead_letter("dead-1", service, FakeReplayCoordinator())


class SupervisorProducedDeadLetterService:
    def __init__(self) -> None:
        self.record: dict[str, object] | None = None
        self.statuses: list[str] = []

    async def record_dead_letter(self, **kwargs) -> None:
        self.record = {
            "id": "dead-1",
            "raw_payload": kwargs["raw_payload"],
            "provider": kwargs["provider"],
            "lane": kwargs["lane"],
            "received_at": kwargs["received_at"],
            "status": "open",
        }

    async def load_dead_letter(self, dead_letter_id: str):
        return self.record

    async def mark_dead_letter(self, dead_letter_id: str, status: str, error=None) -> None:
        self.statuses.append(status)


class RecordingReplayEventService:
    def __init__(self) -> None:
        self.fail = True
        self.calls: list[dict[str, object]] = []

    async def ingest_collected(
        self,
        raw_payload,
        event,
        provider,
        lane,
        received_at,
        response_input,
        region_context,
        trigger_reason="live",
    ) -> LifecycleIngestOutcome:
        self.calls.append(
            {
                "raw_payload": raw_payload,
                "provider": provider,
                "lane": lane,
                "received_at": received_at,
                "trigger_reason": trigger_reason,
            }
        )
        if self.fail:
            raise RuntimeError("initial processing failed")
        return LifecycleIngestOutcome(
            event_id="event-1",
            revision_id="revision-1",
            revision_no=1,
            event_kind=event.kind,
            lifecycle_state="formal_triggered",
            is_current=True,
            is_new=True,
            triggered_assessment=True,
            institutional_level=None,
            service_level=None,
            t1_at=received_at,
        )


class ReplayRegionResolver:
    async def resolve(self, longitude, latitude) -> RegionContext:
        return RegionContext(
            inside_shanghai=True,
            distance_to_boundary_km=Decimal("0"),
            boundary_version="test-2026.1",
            computed_at=datetime(2026, 9, 25, 1, 2, tzinfo=UTC),
        )


def _wolfx_payload() -> dict[str, object]:
    return {
        "EventID": "CENC-1",
        "type": "reviewed",
        "time": "2026-09-25T01:02:03Z",
        "ReportTime": "2026-09-25T01:04:00Z",
        "placeName": "Shanghai",
        "magnitude": 5.1,
        "depth": 10,
        "latitude": 31.2,
        "longitude": 121.5,
    }


async def test_replay_accepts_payload_persisted_by_supervisor() -> None:
    service = SupervisorProducedDeadLetterService()
    event_service = RecordingReplayEventService()
    coordinator = CollectorCoordinator(event_service, ReplayRegionResolver())
    supervisor = CollectorSupervisor(
        settings=object(),
        service=service,
        coordinator=coordinator,
        spool=object(),
        fan_collector=object(),
        wolfx_collector=object(),
    )
    received_at = datetime(2026, 9, 25, 1, 5, tzinfo=UTC)

    outcome = await supervisor.process_one(
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=received_at,
            payload={"No1": _wolfx_payload()},
        )
    )

    assert outcome == "dead_letter"
    assert service.record is not None
    assert service.record["raw_payload"] == _wolfx_payload()

    event_service.fail = False
    await replay_dead_letter("dead-1", service, coordinator)

    assert service.statuses == ["retried"]
    assert event_service.calls[-1]["received_at"] == received_at
    assert event_service.calls[-1]["provider"] == "wolfx"
    assert event_service.calls[-1]["lane"] == "http"
    assert event_service.calls[-1]["trigger_reason"] == "recovery"
