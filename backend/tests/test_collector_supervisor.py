from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.collector.coordinator import CollectorCoordinator
from app.collector.domain import CollectorEnvelope, CollectorLane, CollectorProvider
from app.collector.supervisor import CollectorSupervisor
from app.events.domain import EventKind
from app.events.repository import LifecycleIngestOutcome
from app.regions.domain import RegionContext


class RecordingRegionResolver:
    def __init__(self, inside: bool | None = True, distance: str | None = "0") -> None:
        self._context = RegionContext(
            inside_shanghai=inside,
            distance_to_boundary_km=Decimal(distance) if distance is not None else None,
            boundary_version="test-2026.1",
            computed_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        )

    async def resolve(self, longitude, latitude) -> RegionContext:
        return self._context


class RecordingEventService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ingest_collected(self, **kwargs) -> LifecycleIngestOutcome:
        self.calls.append(kwargs)
        event = kwargs["event"]
        return LifecycleIngestOutcome(
            event_id="event-1",
            revision_id="revision-1",
            revision_no=1,
            event_kind=event.kind,
            lifecycle_state=(
                "auto_pending"
                if event.kind is EventKind.AUTO
                else "formal_triggered"
            ),
            is_current=True,
            is_new=True,
            triggered_assessment=event.kind is not EventKind.AUTO,
            institutional_level="较大响应",
            service_level=2,
            t1_at=kwargs["received_at"],
        )


class RecordingCoordinator:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[CollectorEnvelope] = []
        self.fail_with = fail_with

    def expand(self, envelope: CollectorEnvelope) -> list[CollectorEnvelope]:
        return [envelope]

    async def ingest(self, envelope: CollectorEnvelope, trigger_reason: str = "live"):
        self.calls.append(envelope)
        if self.fail_with is not None:
            raise self.fail_with
        return None


class RecordingCollectorService:
    def __init__(self) -> None:
        self.dead_letters: list[dict[str, object]] = []
        self.last_processed_source_time = None

    async def get_last_processed_source_time(self, provider: str):
        return self.last_processed_source_time

    async def update_after_success(self, provider: str, ingested_at, source_time) -> None:
        self.last_processed_source_time = source_time

    async def record_dead_letter(self, **kwargs) -> None:
        self.dead_letters.append(kwargs)

    async def record_spool_overflow(self, **kwargs) -> None:
        self.dead_letters.append({**kwargs, "error_category": "internal_error"})


class RecordingSpool:
    def __init__(self) -> None:
        self.pending: list[tuple[Path, CollectorEnvelope]] = []

    def append(self, envelope: CollectorEnvelope) -> Path:
        path = Path(f"spool-{len(self.pending) + 1}")
        self.pending.append((path, envelope))
        return path

    def iter_pending(self):
        yield from self.pending

    def remove(self, path: Path) -> None:
        self.pending = [
            item for item in self.pending if item[0] != path
        ]


class NoopFanCollector:
    async def run(self, **kwargs) -> None:
        return None


class NoopWolfxCollector:
    async def run(self, **kwargs) -> None:
        return None


def collector_settings():
    from app.config import Settings
    from pydantic import SecretStr

    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        jwt_secret=SecretStr("jwt-secret-at-least-16-characters"),
        superadmin_initial_password=SecretStr("admin-secret-at-least-16-characters"),
        cenc_collector_enabled=False,
    )


def reviewed_wolfx_event() -> dict[str, object]:
    return {
        "EventID": "CENC-1",
        "type": "reviewed",
        "time": "2026-09-25T01:02:03Z",
        "placeName": "上海测试位置",
        "magnitude": 5.1,
        "depth": 10,
        "latitude": 31.2,
        "longitude": 121.5,
    }


def envelope(report_time: str = "2026-09-25T01:04:00Z") -> CollectorEnvelope:
    return CollectorEnvelope(
        provider=CollectorProvider.WOLFX,
        lane=CollectorLane.HTTP,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": {**reviewed_wolfx_event(), "ReportTime": report_time}},
    )


async def test_coordinator_uses_region_context_and_trigger_reason() -> None:
    event_service = RecordingEventService()
    regions = RecordingRegionResolver(inside=True, distance="0")
    coordinator = CollectorCoordinator(event_service, regions)
    envelope = CollectorEnvelope(
        provider=CollectorProvider.FAN,
        lane=CollectorLane.WEBSOCKET,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": reviewed_wolfx_event()},
    )

    result = await coordinator.ingest(envelope, trigger_reason="recovery")

    assert result is not None
    call = event_service.calls[0]
    assert call["provider"] == "fan"
    assert call["lane"] == "websocket"
    assert call["response_input"].inside_shanghai is True
    assert call["region_context"].boundary_version == "test-2026.1"
    assert call["trigger_reason"] == "recovery"


async def test_coordinator_skips_cancellation_without_dead_letter() -> None:
    coordinator = CollectorCoordinator(RecordingEventService(), RecordingRegionResolver())

    result = await coordinator.ingest(
        CollectorEnvelope(
            provider=CollectorProvider.FAN,
            lane=CollectorLane.WEBSOCKET,
            received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
            payload={"No1": {**reviewed_wolfx_event(), "infoTypeName": "取消"}},
        )
    )

    assert result is None


def test_coordinator_expands_fan_matrix_in_numeric_order() -> None:
    coordinator = CollectorCoordinator(RecordingEventService(), RecordingRegionResolver())
    received_at = datetime(2026, 9, 25, 1, 5, tzinfo=UTC)
    envelope = CollectorEnvelope(
        provider=CollectorProvider.FAN,
        lane=CollectorLane.WEBSOCKET,
        received_at=received_at,
        payload={
            "No2": {**reviewed_wolfx_event(), "EventID": "CENC-2"},
            "No1": {**reviewed_wolfx_event(), "EventID": "CENC-1"},
        },
    )

    expanded = coordinator.expand(envelope)

    assert [next(iter(item.payload)) for item in expanded] == ["No1", "No2"]
    assert [item.received_at for item in expanded] == [received_at, received_at]
    assert [item.provider for item in expanded] == [
        CollectorProvider.FAN,
        CollectorProvider.FAN,
    ]


async def test_supervisor_orders_batch_by_source_report_time() -> None:
    coordinator = RecordingCoordinator()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=RecordingCollectorService(),
        coordinator=coordinator,
        spool=RecordingSpool(),
        fan_collector=NoopFanCollector(),
        wolfx_collector=NoopWolfxCollector(),
    )

    await supervisor.process_envelopes(
        [
            envelope(report_time="2026-09-25T01:06:00Z"),
            envelope(report_time="2026-09-25T01:04:00Z"),
        ],
        trigger_reason="recovery",
    )

    assert [
        call.payload["No1"]["ReportTime"] for call in coordinator.calls
    ] == [
        "2026-09-25T01:04:00Z",
        "2026-09-25T01:06:00Z",
    ]


async def test_malformed_message_becomes_dead_letter() -> None:
    service = RecordingCollectorService()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=RecordingCoordinator(fail_with=ValueError("invalid CENC depth")),
        spool=RecordingSpool(),
        fan_collector=NoopFanCollector(),
        wolfx_collector=NoopWolfxCollector(),
    )

    outcome = await supervisor.process_one(envelope())

    assert outcome == "dead_letter"
    assert service.dead_letters[0]["error_category"] == "parse_error"


async def test_recovery_spool_is_not_filtered_by_newer_watermark() -> None:
    service = RecordingCollectorService()
    service.last_processed_source_time = datetime(2026, 9, 25, 1, 6, tzinfo=UTC)
    coordinator = RecordingCoordinator()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=coordinator,
        spool=RecordingSpool(),
        fan_collector=NoopFanCollector(),
        wolfx_collector=NoopWolfxCollector(),
    )
    older = envelope(report_time="2026-09-25T01:04:00Z")

    await supervisor.process_envelopes([older], trigger_reason="recovery")

    assert coordinator.calls == [older]
