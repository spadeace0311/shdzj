import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import serve as serve_websocket

from app.collector.domain import CollectorLane, CollectorProvider
from app.collector.fan import FanCollector, FanMessageParser


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "fan_cenc.json").read_text(encoding="utf-8")
)


def test_parse_fan_cenc_initial_list() -> None:
    parsed = FanMessageParser().parse(
        FIXTURE["initial"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is not None
    assert parsed.envelope.provider is CollectorProvider.FAN
    assert parsed.envelope.lane is CollectorLane.WEBSOCKET
    first = parsed.envelope.payload["No1"]
    assert first["eventId"] == "CENC-2026-0001"
    assert first["infoTypeName"] == "正式(已核实)"


def test_parse_fan_cenc_query_response() -> None:
    parsed = FanMessageParser().parse(
        FIXTURE["query_response"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is not None
    assert parsed.envelope.payload["No1"]["eventId"] == "CENC-2026-0002"


def test_parse_fan_cenclist_response_normalizes_history_keys() -> None:
    parsed = FanMessageParser().parse(
        FIXTURE["cenclist_response"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is not None
    assert list(parsed.envelope.payload) == ["No1", "No2"]
    assert parsed.envelope.payload["No1"]["id"] == "CENC-2026-0001"
    assert parsed.envelope.payload["No2"]["id"] == "CENC-2026-0002"
    assert parsed.envelope.payload["No1"]["shockTime"] == "2026-09-25T01:00:01Z"
    assert parsed.envelope.payload["No2"]["shockTime"] == "2026-09-25T01:00:02Z"
    assert {
        item["eventId"] for item in parsed.envelope.payload.values()
    } == {"CENC-2026-0001", "CENC-2026-0002"}


def test_parse_fan_auth_success() -> None:
    parsed = FanMessageParser().parse(
        {"type": "auth_success"},
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is None
    assert parsed.auth_state == "success"


def test_parse_fan_auth_failure_does_not_include_key() -> None:
    parsed = FanMessageParser().parse(
        {"type": "auth_fail", "message": "invalid key"},
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is None
    assert parsed.auth_state == "failed"
    assert "key" not in parsed.error_text.lower()


def test_parse_fan_update_requires_cenc_source() -> None:
    parsed = FanMessageParser().parse(
        FIXTURE["update"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert parsed.envelope is not None
    assert parsed.envelope.payload["No1"]["eventId"] == "CENC-2026-0003"

    ignored = FanMessageParser().parse(
        {"type": "update", "source": "wolfx", "Data": FIXTURE["update"]["Data"]},
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )
    assert ignored.envelope is None


def test_parse_fan_recovery_since_filters_history_but_not_live_update() -> None:
    parser = FanMessageParser()
    recovery_since = datetime(2026, 9, 25, 1, 0, 2, tzinfo=UTC)

    history = parser.parse(
        FIXTURE["cenclist_response"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
        recovery_since=recovery_since,
    )
    update = parser.parse(
        FIXTURE["update"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
        recovery_since=recovery_since,
    )

    assert history.envelope is not None
    assert list(history.envelope.payload) == ["No1"]
    assert history.envelope.payload["No1"]["id"] == "CENC-2026-0002"
    assert update.envelope is not None
    assert update.envelope.payload["No1"]["id"] == "CENC-2026-0003"


def test_parse_fan_recovery_since_is_inclusive_for_equal_source_time() -> None:
    history = FanMessageParser().parse(
        FIXTURE["cenclist_response"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
        recovery_since=datetime(2026, 9, 25, 1, 0, 1, tzinfo=UTC),
    )

    assert history.envelope is not None
    assert {
        item["id"] for item in history.envelope.payload.values()
    } == {"CENC-2026-0001", "CENC-2026-0002"}


def test_parse_fan_marks_history_recovery_and_live_update() -> None:
    parser = FanMessageParser()
    recovery_since = datetime(2026, 9, 25, 1, 0, 2, tzinfo=UTC)

    history = parser.parse(
        FIXTURE["cenclist_response"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
        recovery_since=recovery_since,
    )
    update = parser.parse(
        FIXTURE["update"],
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
        recovery_since=recovery_since,
    )

    assert history.envelope is not None
    assert history.envelope.trigger_reason == "recovery"
    assert history.recovery_complete is True
    assert update.envelope is not None
    assert update.envelope.trigger_reason == "live"
    assert update.recovery_complete is False


def test_parse_fan_invalid_cenclist_response_does_not_complete_recovery() -> None:
    parsed = FanMessageParser().parse(
        {"type": "cenclist_response"},
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
        recovery_since=datetime(2026, 9, 25, 1, 0, 2, tzinfo=UTC),
    )

    assert parsed.history_frame is True
    assert parsed.recovery_complete is False


def test_parse_fan_unparseable_history_item_does_not_complete_recovery() -> None:
    parsed = FanMessageParser().parse(
        {
            "type": "cenclist_response",
            "Data": {
                "No1": next(
                    iter(FIXTURE["cenclist_response"]["Data"].values())
                ),
                "No2": {"id": "CENC-INCOMPLETE"},
            },
        },
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
        recovery_since=datetime(2026, 9, 25, 1, 0, 2, tzinfo=UTC),
    )

    assert parsed.history_frame is True
    assert parsed.recovery_complete is False
    assert parsed.envelope is not None
    assert parsed.envelope.recovery_complete is False


async def fake_sleep(delay: float) -> None:
    return None


async def never_sleep(delay: float) -> None:
    await asyncio.Event().wait()


def fixed_now() -> datetime:
    return datetime(2026, 9, 25, 1, 6, tzinfo=UTC)


class IncrementingClock:
    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        self._current = start
        self._step = step

    def __call__(self) -> datetime:
        value = self._current
        self._current += self._step
        return value


class FakeWebSocket:
    def __init__(self, frames: tuple[object, ...] = ()) -> None:
        self._frames = list(frames)
        self.sent: list[str] = []
        self.closed = False
        self.drained = asyncio.Event()
        self.query_sent = asyncio.Event()
        self._wake = asyncio.Event()

    async def send(self, payload: str) -> None:
        self.sent.append(payload)
        if payload == '{"type":"query"}':
            self.query_sent.set()

    async def recv(self) -> object:
        if self._frames:
            return self._frames.pop(0)
        self.drained.set()
        self._wake.clear()
        await self._wake.wait()

    async def close(self) -> None:
        self.closed = True
        self._wake.set()


class HealthRecorder:
    def __init__(self) -> None:
        self.updates = []

    async def __call__(self, update) -> None:
        self.updates.append(update)


class EnvelopeRecorder:
    def __init__(self) -> None:
        self.envelopes = []

    async def __call__(self, envelope) -> None:
        self.envelopes.append(envelope)


class OneShotSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []
        self._next = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) == 1:
            return
        await self._next.wait()


async def test_fan_collector_rotates_urls_after_connection_failure() -> None:
    attempts: list[str] = []

    async def fake_connect(url: str):
        attempts.append(url)
        if len(attempts) == 1:
            raise OSError("primary unavailable")
        raise asyncio.CancelledError

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary", "wss://backup"),
        query_interval_seconds=1,
        connect=fake_connect,
        sleep=fake_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await collector.run(on_envelope=AsyncMock(), on_health=AsyncMock())

    assert attempts == ["wss://primary", "wss://backup"]


async def test_fan_collector_reconnects_to_backup_loopback_server() -> None:
    primary_messages: list[dict[str, object]] = []
    backup_messages: list[dict[str, object]] = []
    backup_event = asyncio.Event()

    async def primary_handler(websocket) -> None:
        primary_messages.append(json.loads(await websocket.recv()))
        primary_messages.append(json.loads(await websocket.recv()))
        await websocket.close(code=1012, reason="restart")

    async def backup_handler(websocket) -> None:
        backup_messages.append(json.loads(await websocket.recv()))
        backup_messages.append(json.loads(await websocket.recv()))
        await websocket.send(json.dumps({"type": "auth_success"}))
        await websocket.send(json.dumps(FIXTURE["query_response"]))
        backup_event.set()
        await websocket.wait_closed()

    async with (
        serve_websocket(primary_handler, "127.0.0.1", 0) as primary_server,
        serve_websocket(backup_handler, "127.0.0.1", 0) as backup_server,
    ):
        primary_port = primary_server.sockets[0].getsockname()[1]
        backup_port = backup_server.sockets[0].getsockname()[1]
        collector = FanCollector(
            app_id="app-id",
            api_key="secret",
            urls=(
                f"ws://127.0.0.1:{primary_port}",
                f"ws://127.0.0.1:{backup_port}",
            ),
            query_interval_seconds=60,
            sleep=fake_sleep,
            now=fixed_now,
        )
        envelopes = EnvelopeRecorder()
        health = HealthRecorder()
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            collector.run(envelopes, health, stop_event)
        )

        await asyncio.wait_for(backup_event.wait(), timeout=1)
        await asyncio.wait_for(websocket_drained(envelopes), timeout=1)
        stop_event.set()
        await asyncio.wait_for(task, timeout=1)

    assert primary_messages == [
        {"type": "auth", "appId": "app-id", "key": "secret"},
        {"type": "cenclist"},
    ]
    assert backup_messages == primary_messages
    assert [update.state for update in health.updates] == [
        "starting",
        "degraded",
        "healthy",
        "healthy",
        "stopped",
    ]
    assert len(envelopes.envelopes) == 1


async def websocket_drained(envelopes: EnvelopeRecorder) -> None:
    while not envelopes.envelopes:
        await asyncio.sleep(0)


async def test_authenticated_business_envelope_emits_healthy() -> None:
    websocket = FakeWebSocket(
        (
            json.dumps({"type": "auth_success"}),
            json.dumps(FIXTURE["query_response"]),
        )
    )

    async def connect(url: str):
        return websocket

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary",),
        query_interval_seconds=60,
        connect=connect,
        sleep=never_sleep,
        now=fixed_now,
    )
    envelopes = EnvelopeRecorder()
    health = HealthRecorder()
    stop_event = asyncio.Event()
    task = asyncio.create_task(collector.run(envelopes, health, stop_event))

    await asyncio.wait_for(websocket.drained.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert [update.state for update in health.updates] == [
        "starting",
        "healthy",
        "healthy",
        "stopped",
    ]
    healthy = health.updates[2]
    assert healthy.connected is True
    assert healthy.last_http_status is None
    assert healthy.last_connected_at == fixed_now()
    assert healthy.last_message_at == fixed_now()
    assert healthy.last_success_at == fixed_now()
    assert healthy.consecutive_failures == 0
    assert len(envelopes.envelopes) == 1


async def test_authenticated_heartbeat_advances_transport_activity() -> None:
    websocket = FakeWebSocket(
        (
            json.dumps({"type": "auth_success"}),
            json.dumps({"type": "pong"}),
        )
    )

    async def connect(url: str):
        return websocket

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary",),
        query_interval_seconds=60,
        connect=connect,
        sleep=never_sleep,
        now=IncrementingClock(fixed_now()),
    )
    health = HealthRecorder()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.run(on_envelope=AsyncMock(), on_health=health, stop_event=stop_event)
    )

    await asyncio.wait_for(websocket.drained.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    healthy_updates = [update for update in health.updates if update.state == "healthy"]
    assert len(healthy_updates) == 2
    assert healthy_updates[0].last_transport_at is not None
    assert healthy_updates[1].last_transport_at is not None
    assert (
        healthy_updates[1].last_transport_at
        > healthy_updates[0].last_transport_at
    )
    assert healthy_updates[-1].last_message_at is None
    assert healthy_updates[-1].last_success_at is None


async def test_authenticated_empty_query_response_advances_transport_activity() -> None:
    websocket = FakeWebSocket(
        (
            json.dumps({"type": "auth_success"}),
            json.dumps({"type": "query_response", "cenc": {"Data": None}}),
        )
    )

    async def connect(url: str):
        return websocket

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary",),
        query_interval_seconds=60,
        connect=connect,
        sleep=never_sleep,
        now=IncrementingClock(fixed_now()),
    )
    health = HealthRecorder()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.run(on_envelope=AsyncMock(), on_health=health, stop_event=stop_event)
    )

    await asyncio.wait_for(websocket.drained.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    healthy_updates = [update for update in health.updates if update.state == "healthy"]
    assert len(healthy_updates) == 2
    assert healthy_updates[0].last_transport_at is not None
    assert healthy_updates[1].last_transport_at is not None
    assert (
        healthy_updates[1].last_transport_at
        > healthy_updates[0].last_transport_at
    )
    assert healthy_updates[-1].last_message_at is None
    assert healthy_updates[-1].last_success_at is None


async def test_business_envelope_before_auth_does_not_emit_healthy() -> None:
    websocket = FakeWebSocket((json.dumps(FIXTURE["query_response"]),))

    async def connect(url: str):
        return websocket

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary",),
        query_interval_seconds=60,
        connect=connect,
        sleep=never_sleep,
        now=fixed_now,
    )
    envelopes = EnvelopeRecorder()
    health = HealthRecorder()
    stop_event = asyncio.Event()
    task = asyncio.create_task(collector.run(envelopes, health, stop_event))

    await asyncio.wait_for(websocket.drained.wait(), timeout=1)
    assert [update.state for update in health.updates] == ["starting"]
    assert len(envelopes.envelopes) == 1

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    assert [update.state for update in health.updates] == ["starting", "stopped"]
    assert health.updates[-1].connected is False


async def test_stop_event_closes_and_emits_disconnected_stopped() -> None:
    websocket = FakeWebSocket()

    async def connect(url: str):
        return websocket

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary",),
        query_interval_seconds=60,
        connect=connect,
        sleep=never_sleep,
        now=fixed_now,
    )
    health = HealthRecorder()
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.run(on_envelope=AsyncMock(), on_health=health, stop_event=stop_event)
    )

    await asyncio.wait_for(websocket.drained.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert health.updates[0].state == "starting"
    assert health.updates[0].connected is False
    assert health.updates[-1].state == "stopped"
    assert health.updates[-1].connected is False
    assert websocket.closed is True


async def test_auth_failure_records_degraded_and_rotates_without_credentials() -> None:
    websocket = FakeWebSocket(
        (json.dumps({"type": "auth_fail", "message": "invalid key"}),)
    )
    attempts: list[str] = []

    async def connect(url: str):
        attempts.append(url)
        if len(attempts) == 1:
            return websocket
        raise asyncio.CancelledError

    sleep_calls: list[float] = []

    async def recording_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        if delay >= 30:
            await asyncio.Event().wait()

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary", "wss://backup"),
        query_interval_seconds=60,
        connect=connect,
        sleep=recording_sleep,
        now=fixed_now,
        rng=random.Random(1),
    )
    health = HealthRecorder()

    with pytest.raises(asyncio.CancelledError):
        await collector.run(on_envelope=AsyncMock(), on_health=health)

    assert attempts == ["wss://primary", "wss://backup"]
    degraded = [update for update in health.updates if update.state == "degraded"]
    assert len(degraded) == 1
    assert degraded[0].connected is False
    assert degraded[0].last_error == "authentication failed"
    backoff_calls = [delay for delay in sleep_calls if delay < 30]
    assert len(backoff_calls) == 1
    assert 0.99 <= backoff_calls[0] <= 1.26
    for update in health.updates:
        assert "secret" not in str(update)
        assert "app-id" not in str(update)


async def test_query_is_sent_after_configured_interval() -> None:
    websocket = FakeWebSocket()

    async def connect(url: str):
        return websocket

    sleep = OneShotSleep()
    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary",),
        query_interval_seconds=7,
        connect=connect,
        sleep=sleep,
        now=fixed_now,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.run(on_envelope=AsyncMock(), on_health=AsyncMock(), stop_event=stop_event)
    )

    await asyncio.wait_for(websocket.query_sent.wait(), timeout=1)
    assert sleep.delays == [7.0]

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)


async def test_fan_collector_waits_for_cenclist_before_accepting_old_history() -> None:
    recovery_since = datetime(2026, 9, 25, 1, 0, 2, tzinfo=UTC)
    newer_query = {
        "type": "query_response",
        "cenc": {
            "Data": {
                "No1": {
                    **FIXTURE["initial"]["cenc"]["Data"]["No1"],
                    "id": "CENC-2026-NEWER",
                    "eventId": "CENC-2026-NEWER",
                    "shockTime": "2026-09-25T01:00:03Z",
                }
            }
        },
    }
    older_query = {
        "type": "query_response",
        "cenc": {"Data": FIXTURE["initial"]["cenc"]["Data"]},
    }
    websocket = FakeWebSocket(
        (
            json.dumps({"type": "auth_success"}),
            json.dumps(newer_query),
            json.dumps(older_query),
            json.dumps(FIXTURE["cenclist_response"]),
            json.dumps(older_query),
        )
    )

    async def connect(url: str):
        return websocket

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary",),
        query_interval_seconds=60,
        connect=connect,
        sleep=never_sleep,
        now=fixed_now,
        recovery_since=recovery_since,
    )
    envelopes = EnvelopeRecorder()

    async def clear_bound_after_completion(envelope) -> None:
        envelopes.envelopes.append(envelope)
        if envelope.recovery_complete:
            collector.recovery_since = None

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        collector.run(
            on_envelope=clear_bound_after_completion,
            on_health=AsyncMock(),
            stop_event=stop_event,
        )
    )

    await asyncio.wait_for(websocket.drained.wait(), timeout=1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)

    assert [item.trigger_reason for item in envelopes.envelopes] == [
        "recovery",
        "recovery",
        "live",
    ]
    assert [
        item.payload[next(iter(item.payload))]["id"] for item in envelopes.envelopes
    ] == ["CENC-2026-NEWER", "CENC-2026-0002", "CENC-2026-0001"]


async def test_callback_exception_propagates_without_reconnect() -> None:
    websocket = FakeWebSocket(
        (
            json.dumps({"type": "auth_success"}),
            json.dumps(FIXTURE["query_response"]),
        )
    )
    connect_count = 0

    async def connect(url: str):
        nonlocal connect_count
        connect_count += 1
        return websocket

    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary", "wss://backup"),
        query_interval_seconds=60,
        connect=connect,
        sleep=never_sleep,
        now=fixed_now,
    )

    async def failing_envelope(envelope) -> None:
        raise RuntimeError("callback failed")

    health = HealthRecorder()
    task = asyncio.create_task(
        collector.run(on_envelope=failing_envelope, on_health=health)
    )

    with pytest.raises(RuntimeError, match="callback failed"):
        await asyncio.wait_for(task, timeout=1)

    assert connect_count == 1
    assert [update.state for update in health.updates] == ["starting", "healthy"]
    assert websocket.closed is True


def test_next_delay_is_capped_with_jitter_and_overflow_safe() -> None:
    collector = FanCollector(
        app_id="app-id",
        api_key="secret",
        urls=("wss://primary",),
        query_interval_seconds=60,
        sleep=fake_sleep,
        now=fixed_now,
        rng=random.Random(1),
    )

    collector._consecutive_failures = 1
    assert 1.0 <= collector._next_delay() <= 1.25

    collector._consecutive_failures = 10**18
    assert 29.0 <= collector._next_delay() <= 30.0
