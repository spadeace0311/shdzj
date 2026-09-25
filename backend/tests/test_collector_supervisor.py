import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from app.collector.coordinator import CollectorCoordinator
from app.collector.domain import (
    CollectorEnvelope,
    CollectorLane,
    CollectorProvider,
    ProviderHealthUpdate,
)
from app.collector.models import CollectorRuntimeState
from app.collector.service import CollectorService
from app.collector.spool import CollectorSpool
from app.collector.supervisor import CollectorSupervisor
from app.db import engine
from app.events.domain import EventKind
from app.events.service import LifecycleIngestOutcome
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


class RecoveryCoordinator(RecordingCoordinator):
    def __init__(self, remaining_failures: dict[str, int]) -> None:
        super().__init__()
        self.remaining_failures = remaining_failures
        self.ingested_order: list[str] = []
        self.c_ingested = asyncio.Event()

    async def ingest(self, envelope: CollectorEnvelope, trigger_reason: str = "live"):
        self.calls.append(envelope)
        event_id = str(envelope.payload["No1"]["EventID"])
        if self.remaining_failures.get(event_id, 0) > 0:
            self.remaining_failures[event_id] -= 1
            raise SQLAlchemyError("storage unavailable")
        self.ingested_order.append(event_id)
        if event_id == "C":
            self.c_ingested.set()
        return None


class SuccessfulCoordinator(RecordingCoordinator):
    async def ingest(self, envelope: CollectorEnvelope, trigger_reason: str = "live"):
        self.calls.append(envelope)
        return object()


class RecordingCollectorService:
    def __init__(
        self,
        *,
        fail_dead_letter: bool = False,
        watermarks: dict[str, datetime | None] | None = None,
    ) -> None:
        self.dead_letters: list[dict[str, object]] = []
        self.successes: list[tuple[str, datetime, datetime | None]] = []
        self.last_processed_source_time = None
        self.fail_dead_letter = fail_dead_letter
        self.watermarks = watermarks

    async def persist_health(self, update: ProviderHealthUpdate) -> None:
        return None

    async def get_last_processed_source_time(self, provider: str):
        if self.watermarks is not None:
            return self.watermarks.get(provider)
        return self.last_processed_source_time

    async def update_after_success(self, provider: str, ingested_at, source_time) -> None:
        self.successes.append((provider, ingested_at, source_time))
        self.last_processed_source_time = source_time

    async def record_dead_letter(self, **kwargs) -> None:
        if self.fail_dead_letter:
            raise SQLAlchemyError("dead-letter storage unavailable")
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


class PushFanCollector:
    def __init__(self, envelopes: list[CollectorEnvelope]) -> None:
        self.envelopes = envelopes

    async def run(self, **kwargs) -> None:
        on_envelope = kwargs["on_envelope"]
        stop_event = kwargs["stop_event"]
        for envelope in self.envelopes:
            await on_envelope(envelope)
        await stop_event.wait()


class RecoveryBoundSpy:
    def __init__(self) -> None:
        self.recovery_since = None
        self.started = asyncio.Event()

    async def run(self, **kwargs) -> None:
        self.started.set()
        await kwargs["stop_event"].wait()


async def fake_sleep(_delay: float) -> None:
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


async def test_live_spooled_item_is_replayed_before_newer_live_item() -> None:
    service = RecordingCollectorService()
    spool = RecordingSpool()
    coordinator = RecoveryCoordinator(remaining_failures={"A": 4})
    stop_event = asyncio.Event()
    received_at = datetime(2026, 9, 25, 1, 5, tzinfo=UTC)
    envelopes = [
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=received_at,
            payload={
                "No1": {
                    **reviewed_wolfx_event(),
                    "EventID": "A",
                    "ReportTime": "2026-09-25T01:04:00Z",
                }
            },
        ),
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=received_at,
            payload={
                "No1": {
                    **reviewed_wolfx_event(),
                    "EventID": "C",
                    "ReportTime": "2026-09-25T01:06:00Z",
                }
            },
        ),
    ]
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=coordinator,
        spool=spool,
        fan_collector=PushFanCollector(envelopes),
        wolfx_collector=NoopWolfxCollector(),
        sleep=fake_sleep,
        now=lambda: datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(coordinator.c_ingested.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert coordinator.ingested_order == ["A", "C"]
    assert list(spool.pending) == []


async def test_replay_keeps_spool_file_when_dead_letter_persistence_fails() -> None:
    service = RecordingCollectorService(fail_dead_letter=True)
    spool = RecordingSpool()
    malformed = envelope()
    spool.pending = [(Path("spool-1"), malformed)]
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=RecordingCoordinator(fail_with=ValueError("invalid CENC depth")),
        spool=spool,
        fan_collector=NoopFanCollector(),
        wolfx_collector=NoopWolfxCollector(),
        now=lambda: datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
    )

    await asyncio.wait_for(supervisor.run(stop_event), timeout=1)

    assert stop_event.is_set()
    assert list(spool.pending) == [(Path("spool-1"), malformed)]
    assert supervisor.provider_states == {"fan": "critical", "wolfx": "critical"}


async def test_runtime_spool_drain_failure_stops_before_newer_live_item() -> None:
    # A storage-fails live and is spooled, then its startup-equivalent drain also
    # fails. This is deliberately terminal: collection stops instead of letting
    # newer C run ahead of the retained A file.
    service = RecordingCollectorService()
    spool = RecordingSpool()
    coordinator = RecoveryCoordinator(remaining_failures={"A": 5})
    stop_event = asyncio.Event()
    received_at = datetime(2026, 9, 25, 1, 5, tzinfo=UTC)
    envelopes = [
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=received_at,
            payload={
                "No1": {
                    **reviewed_wolfx_event(),
                    "EventID": "A",
                    "ReportTime": "2026-09-25T01:04:00Z",
                }
            },
        ),
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=received_at,
            payload={
                "No1": {
                    **reviewed_wolfx_event(),
                    "EventID": "C",
                    "ReportTime": "2026-09-25T01:06:00Z",
                }
            },
        ),
    ]
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=coordinator,
        spool=spool,
        fan_collector=PushFanCollector(envelopes),
        wolfx_collector=NoopWolfxCollector(),
        sleep=fake_sleep,
        now=lambda: datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(stop_event.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    assert coordinator.ingested_order == []
    assert len(list(spool.pending)) == 1
    assert supervisor.provider_states == {"fan": "critical", "wolfx": "critical"}


async def test_spool_remove_commit_failure_keeps_pending_without_success(
    tmp_path,
    monkeypatch,
) -> None:
    service = RecordingCollectorService()
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)
    retained = envelope()
    path = spool.append(retained)

    def fail_directory_fsync() -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(spool, "_fsync_directory", fail_directory_fsync)
    coordinator = SuccessfulCoordinator()
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=coordinator,
        spool=spool,
        fan_collector=NoopFanCollector(),
        wolfx_collector=NoopWolfxCollector(),
        now=lambda: datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
    )

    await asyncio.wait_for(supervisor.run(stop_event), timeout=1)

    assert stop_event.is_set()
    assert coordinator.calls == [retained]
    assert service.successes == []
    deleting_path = path.with_name(path.name + ".deleting")
    assert not path.exists()
    assert deleting_path.exists()
    assert list(spool.iter_pending()) == [(deleting_path, retained)]
    assert supervisor.provider_states == {"fan": "critical", "wolfx": "critical"}


async def test_spool_remove_final_fsync_failure_stops_without_success(
    tmp_path,
    monkeypatch,
) -> None:
    service = RecordingCollectorService()
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)
    retained = envelope()
    path = spool.append(retained)
    calls = 0

    def fail_directory_fsync() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("final directory fsync failed")

    monkeypatch.setattr(spool, "_fsync_directory", fail_directory_fsync)
    coordinator = SuccessfulCoordinator()
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=coordinator,
        spool=spool,
        fan_collector=NoopFanCollector(),
        wolfx_collector=NoopWolfxCollector(),
        now=lambda: datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
    )

    await asyncio.wait_for(supervisor.run(stop_event), timeout=1)

    # The ingest committed before removal. The first directory sync durably
    # committed deletion, so the final cleanup failure must not report success
    # or claim the payload is still pending.
    assert calls == 2
    assert stop_event.is_set()
    assert coordinator.calls == [retained]
    assert service.successes == []
    assert not path.exists()
    assert not path.with_name(path.name + ".deleting").exists()
    assert list(spool.iter_pending()) == []
    assert supervisor.provider_states == {"fan": "critical", "wolfx": "critical"}


async def test_supervisor_assigns_independent_provider_watermarks() -> None:
    fan_watermark = datetime(2026, 9, 25, 1, 4, tzinfo=UTC)
    wolfx_watermark = datetime(2026, 9, 25, 1, 2, tzinfo=UTC)
    service = RecordingCollectorService(
        watermarks={"fan": fan_watermark, "wolfx": wolfx_watermark}
    )
    fan_collector = RecoveryBoundSpy()
    wolfx_collector = RecoveryBoundSpy()
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=RecordingCoordinator(),
        spool=RecordingSpool(),
        fan_collector=fan_collector,
        wolfx_collector=wolfx_collector,
        now=lambda: datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(fan_collector.started.wait(), timeout=1)
    await asyncio.wait_for(wolfx_collector.started.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert fan_collector.recovery_since == fan_watermark
    assert wolfx_collector.recovery_since == wolfx_watermark


async def test_update_after_success_advances_watermark_and_health_does_not_overwrite_ingest_time(
    session_factory,
) -> None:
    await engine.dispose()
    try:
        service = CollectorService(session_factory)
        async with session_factory() as session:
            async with session.begin():
                await session.execute(delete(CollectorRuntimeState))

        older = datetime(2026, 9, 25, 1, 4, tzinfo=UTC)
        newer = datetime(2026, 9, 25, 1, 6, tzinfo=UTC)
        ingest_at = datetime(2026, 9, 25, 1, 7, tzinfo=UTC)

        await service.update_after_success("fan", ingest_at, newer)
        await service.update_after_success("fan", ingest_at, older)

        assert await service.get_last_processed_source_time("fan") == newer

        health_time = datetime(2026, 9, 25, 1, 8, tzinfo=UTC)
        await service.persist_health(
            ProviderHealthUpdate(
                provider=CollectorProvider.FAN,
                state="healthy",
                connected=True,
                last_http_status=200,
                last_connected_at=health_time,
                last_message_at=health_time,
                last_success_at=health_time,
                consecutive_failures=0,
                reconnect_count=0,
                last_error=None,
                updated_at=health_time,
            )
        )

        async with session_factory() as session:
            row = await session.get(CollectorRuntimeState, "fan")
            assert row is not None
            assert row.last_success_at == ingest_at
            assert row.last_processed_source_time == newer

        async with session_factory() as session:
            async with session.begin():
                await session.execute(delete(CollectorRuntimeState))
    finally:
        await engine.dispose()
