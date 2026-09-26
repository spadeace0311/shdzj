import asyncio
import json
from datetime import UTC, datetime, timedelta
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
from app.collector.fan import FanCollector
from app.collector.models import CollectorRuntimeState
from app.collector.service import CollectorService
from app.collector.spool import CollectorSpool
from app.collector.supervisor import CollectorSupervisor
from app.collector.wolfx import WolfxMessageParser
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
        self.trigger_reasons: list[str] = []
        self.fail_with = fail_with

    def expand(self, envelope: CollectorEnvelope) -> list[CollectorEnvelope]:
        return [envelope]

    async def ingest(self, envelope: CollectorEnvelope, trigger_reason: str = "live"):
        self.calls.append(envelope)
        self.trigger_reasons.append(trigger_reason)
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
        self.health_updates: list[ProviderHealthUpdate] = []
        self.last_processed_source_time = None
        self.fail_dead_letter = fail_dead_letter
        self.watermarks = watermarks

    async def persist_health(self, update: ProviderHealthUpdate) -> None:
        self.health_updates.append(update)

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


class FailingAppendSpool(RecordingSpool):
    def append(self, envelope: CollectorEnvelope) -> Path:
        raise OSError("spool unavailable")


class NoopFanCollector:
    async def run(self, **kwargs) -> None:
        return None


class NoopWolfxCollector:
    async def run(self, **kwargs) -> None:
        return None


class PushFanCollector:
    def __init__(self, envelopes: list[CollectorEnvelope]) -> None:
        self.envelopes = envelopes
        self.recovery_since = None

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


class FanCriticalWebSocket:
    def __init__(self) -> None:
        self._frames = [
            json.dumps({"type": "auth_success"}),
            json.dumps({"type": "query_response", "cenc": {"Data": None}}),
            json.dumps(
                {
                    "type": "query_response",
                    "cenc": {"Data": {"No1": reviewed_wolfx_event()}},
                }
            ),
        ]
        self._closed = asyncio.Event()
        self.closed = False

    async def send(self, payload: str) -> None:
        return None

    async def recv(self) -> object:
        if self._frames:
            return self._frames.pop(0)
        await self._closed.wait()
        raise asyncio.CancelledError

    async def close(self) -> None:
        self.closed = True
        self._closed.set()


class HealthPulseCollector:
    def __init__(
        self,
        provider: CollectorProvider,
        health_time: datetime,
        last_transport_at: datetime | None = None,
    ) -> None:
        self._provider = provider
        self._health_time = health_time
        self._last_transport_at = last_transport_at
        self.recovery_since = None

    async def run(self, **kwargs) -> None:
        await kwargs["on_health"](
            ProviderHealthUpdate(
                provider=self._provider,
                state="healthy",
                connected=True,
                last_http_status=200,
                last_connected_at=self._health_time,
                last_message_at=self._health_time,
                last_success_at=self._health_time,
                last_transport_at=self._last_transport_at,
                consecutive_failures=0,
                reconnect_count=0,
                last_error=None,
                updated_at=self._health_time,
            )
        )
        await kwargs["stop_event"].wait()


class ParsingWolfxCollector:
    def __init__(
        self,
        payload: dict[str, object],
        received_at: datetime,
    ) -> None:
        self._payload = payload
        self._received_at = received_at
        self.recovery_since = None

    async def run(self, **kwargs) -> None:
        result = WolfxMessageParser().parse_result(
            self._payload,
            self._received_at,
            self.recovery_since,
        )
        for envelope in result.envelopes:
            await kwargs["on_envelope"](envelope)
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


async def test_provider_without_watermark_gets_its_own_lookback_bound() -> None:
    fan_watermark = datetime(2026, 9, 25, 1, 4, tzinfo=UTC)
    service = RecordingCollectorService(watermarks={"fan": fan_watermark, "wolfx": None})
    fan_collector = RecoveryBoundSpy()
    wolfx_collector = RecoveryBoundSpy()
    stop_event = asyncio.Event()
    fixed_now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=RecordingCoordinator(),
        spool=RecordingSpool(),
        fan_collector=fan_collector,
        wolfx_collector=wolfx_collector,
        now=lambda: fixed_now,
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    await asyncio.wait_for(fan_collector.started.wait(), timeout=1)
    await asyncio.wait_for(wolfx_collector.started.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert fan_collector.recovery_since == fan_watermark
    assert wolfx_collector.recovery_since == fixed_now - timedelta(hours=24)


async def test_fan_watermark_does_not_disable_wolfx_first_start_cutoff() -> None:
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    old_formal = {
        **reviewed_wolfx_event(),
        "EventID": "CENC-OLD",
        "time": "2026-09-25T10:00:00Z",
        "ReportTime": "2026-09-25T11:00:00Z",
    }
    new_formal = {
        **reviewed_wolfx_event(),
        "EventID": "CENC-NEW",
        "time": "2026-09-25T13:00:00Z",
        "ReportTime": "2026-09-25T13:05:00Z",
    }
    service = RecordingCollectorService(
        watermarks={
            "fan": datetime(2026, 9, 25, 1, 0, tzinfo=UTC),
            "wolfx": None,
        }
    )
    event_service = RecordingEventService()
    coordinator = CollectorCoordinator(
        event_service,
        RecordingRegionResolver(),
    )
    wolfx_collector = ParsingWolfxCollector(
        {"No1": old_formal, "No2": new_formal},
        now,
    )
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=coordinator,
        spool=RecordingSpool(),
        fan_collector=NoopFanCollector(),
        wolfx_collector=wolfx_collector,
        now=lambda: now,
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    for _ in range(100):
        if event_service.calls:
            break
        await asyncio.sleep(0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert [call["event"].source_event_id for call in event_service.calls] == [
        "CENC-NEW"
    ]
    assert event_service.calls[0]["trigger_reason"] == "recovery"
    assert wolfx_collector.recovery_since is None


async def test_supervisor_honors_envelope_provenance_and_clears_after_completion() -> None:
    bound = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)
    service = RecordingCollectorService(
        watermarks={"fan": bound, "wolfx": datetime(2026, 9, 25, 1, 0, tzinfo=UTC)}
    )
    coordinator = RecordingCoordinator()
    envelopes = [
        CollectorEnvelope(
            provider=CollectorProvider.FAN,
            lane=CollectorLane.WEBSOCKET,
            received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
            payload={"No1": {**reviewed_wolfx_event(), "EventID": "A"}},
            trigger_reason="recovery",
        ),
        CollectorEnvelope(
            provider=CollectorProvider.FAN,
            lane=CollectorLane.WEBSOCKET,
            received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
            payload={"No1": {**reviewed_wolfx_event(), "EventID": "B"}},
            trigger_reason="recovery",
            recovery_complete=True,
        ),
        CollectorEnvelope(
            provider=CollectorProvider.FAN,
            lane=CollectorLane.WEBSOCKET,
            received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
            payload={"No1": {**reviewed_wolfx_event(), "EventID": "C"}},
            trigger_reason="live",
        ),
    ]
    fan_collector = PushFanCollector(envelopes)
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=coordinator,
        spool=RecordingSpool(),
        fan_collector=fan_collector,
        wolfx_collector=NoopWolfxCollector(),
        now=lambda: datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    for _ in range(100):
        if len(coordinator.calls) == 3:
            break
        await asyncio.sleep(0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert coordinator.trigger_reasons == ["recovery", "recovery", "live"]
    assert fan_collector.recovery_since is None


async def test_active_recovery_bound_drops_old_live_frame_before_completion() -> None:
    bound = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)
    service = RecordingCollectorService(
        watermarks={"fan": bound, "wolfx": bound}
    )
    coordinator = RecordingCoordinator()
    old_live = CollectorEnvelope(
        provider=CollectorProvider.FAN,
        lane=CollectorLane.WEBSOCKET,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={
            "No1": {
                **reviewed_wolfx_event(),
                "EventID": "OLD-LIVE",
                "ReportTime": "2026-09-25T00:45:00Z",
            }
        },
        trigger_reason="live",
    )
    completion = CollectorEnvelope(
        provider=CollectorProvider.FAN,
        lane=CollectorLane.WEBSOCKET,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={},
        trigger_reason="recovery",
        recovery_complete=True,
    )
    fan_collector = PushFanCollector([old_live, completion])
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=coordinator,
        spool=RecordingSpool(),
        fan_collector=fan_collector,
        wolfx_collector=NoopWolfxCollector(),
        now=lambda: datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    for _ in range(100):
        if coordinator.calls and fan_collector.recovery_since is None:
            break
        await asyncio.sleep(0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert coordinator.calls == [completion]
    assert fan_collector.recovery_since is None


async def test_recovery_completion_does_not_clear_bound_after_dead_letter() -> None:
    bound = datetime(2026, 9, 25, 1, 0, tzinfo=UTC)
    service = RecordingCollectorService(
        watermarks={"fan": bound, "wolfx": bound}
    )
    completion = CollectorEnvelope(
        provider=CollectorProvider.FAN,
        lane=CollectorLane.WEBSOCKET,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": {"id": "MALFORMED"}},
        trigger_reason="recovery",
        recovery_complete=True,
    )
    fan_collector = PushFanCollector([completion])
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=RecordingCoordinator(fail_with=ValueError("invalid history")),
        spool=RecordingSpool(),
        fan_collector=fan_collector,
        wolfx_collector=NoopWolfxCollector(),
        now=lambda: datetime(2026, 9, 25, 2, 0, tzinfo=UTC),
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    for _ in range(100):
        if service.dead_letters:
            break
        await asyncio.sleep(0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert service.dead_letters
    assert fan_collector.recovery_since == bound


async def test_critical_state_survives_real_collector_task_shutdown() -> None:
    fixed_now = datetime(2026, 9, 25, 1, 6, tzinfo=UTC)
    service = RecordingCollectorService()
    websocket = FanCriticalWebSocket()

    async def connect(url: str):
        return websocket

    fan_collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary",),
        query_interval_seconds=60,
        connect=connect,
        sleep=fake_sleep,
        now=lambda: fixed_now,
    )
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=RecordingCoordinator(fail_with=SQLAlchemyError("database unavailable")),
        spool=FailingAppendSpool(),
        fan_collector=fan_collector,
        wolfx_collector=NoopWolfxCollector(),
        sleep=fake_sleep,
        now=lambda: fixed_now,
    )

    await asyncio.wait_for(supervisor.run(stop_event), timeout=2)

    fan_states = [
        update.state
        for update in service.health_updates
        if update.provider is CollectorProvider.FAN
    ]
    assert supervisor.provider_states["fan"] == "critical"
    assert fan_states[-1] == "critical"
    assert "stopped" not in fan_states[fan_states.index("critical") + 1 :]


async def test_critical_state_survives_real_shutdown_in_persisted_runtime_state(
    session_factory,
) -> None:
    fixed_now = datetime(2026, 9, 25, 1, 6, tzinfo=UTC)
    await engine.dispose()
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(delete(CollectorRuntimeState))

        service = CollectorService(session_factory)
        websocket = FanCriticalWebSocket()

        async def connect(url: str):
            return websocket

        fan_collector = FanCollector(
            app_id="app-id",
            api_key="secret",
            urls=("wss://primary",),
            query_interval_seconds=60,
            connect=connect,
            sleep=fake_sleep,
            now=lambda: fixed_now,
        )
        stop_event = asyncio.Event()
        supervisor = CollectorSupervisor(
            settings=collector_settings(),
            service=service,
            coordinator=RecordingCoordinator(
                fail_with=SQLAlchemyError("database unavailable")
            ),
            spool=FailingAppendSpool(),
            fan_collector=fan_collector,
            wolfx_collector=NoopWolfxCollector(),
            sleep=fake_sleep,
            now=lambda: fixed_now,
        )

        await asyncio.wait_for(supervisor.run(stop_event), timeout=2)

        async with session_factory() as session:
            row = await session.get(CollectorRuntimeState, "fan")
            assert row is not None
            assert row.state == "critical"
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(delete(CollectorRuntimeState))
        await engine.dispose()


async def test_connected_provider_degrades_after_no_recent_message() -> None:
    started_at = datetime(2026, 9, 25, 1, 6, tzinfo=UTC)
    clock = {"now": started_at}
    service = RecordingCollectorService()
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=RecordingCoordinator(),
        spool=RecordingSpool(),
        fan_collector=HealthPulseCollector(CollectorProvider.FAN, started_at),
        wolfx_collector=NoopWolfxCollector(),
        now=lambda: clock["now"],
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    for _ in range(100):
        if any(
            update.provider is CollectorProvider.FAN and update.state == "healthy"
            for update in service.health_updates
        ):
            break
        await asyncio.sleep(0)

    clock["now"] = started_at + timedelta(seconds=61)
    degraded_observed = False
    for _ in range(100):
        if any(
            update.provider is CollectorProvider.FAN and update.state == "degraded"
            for update in service.health_updates
        ):
            degraded_observed = True
            break
        await asyncio.sleep(0.01)
    assert degraded_observed is True
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    fan_updates = [
        update
        for update in service.health_updates
        if update.provider is CollectorProvider.FAN
    ]
    assert any(update.state == "healthy" for update in fan_updates)
    degraded_update = next(
        update for update in fan_updates if update.state == "degraded"
    )
    assert degraded_update.connected is True
    assert degraded_update.last_error == "no recent messages"
    assert fan_updates[-1].state == "stopped"


async def test_recent_transport_activity_keeps_provider_healthy_despite_stale_message() -> None:
    started_at = datetime(2026, 9, 25, 1, 6, tzinfo=UTC)
    service = RecordingCollectorService()
    stop_event = asyncio.Event()
    supervisor = CollectorSupervisor(
        settings=collector_settings(),
        service=service,
        coordinator=RecordingCoordinator(),
        spool=RecordingSpool(),
        fan_collector=HealthPulseCollector(
            CollectorProvider.FAN,
            started_at - timedelta(minutes=2),
            last_transport_at=started_at,
        ),
        wolfx_collector=NoopWolfxCollector(),
        now=lambda: started_at,
    )

    task = asyncio.create_task(supervisor.run(stop_event))
    for _ in range(100):
        if any(
            update.provider is CollectorProvider.FAN and update.state == "healthy"
            for update in service.health_updates
        ):
            break
        await asyncio.sleep(0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    fan_updates = [
        update
        for update in service.health_updates
        if update.provider is CollectorProvider.FAN
    ]
    assert [update.state for update in fan_updates] == ["starting", "healthy", "stopped"]
    assert fan_updates[1].last_transport_at == started_at


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
