import asyncio
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.collector.domain import CollectorLane, CollectorProvider
from app.collector.wolfx import WolfxCollector, WolfxMessageParser


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "wolfx_cenc_eqlist.json").read_text(encoding="utf-8")
)


def fixed_now() -> datetime:
    return datetime(2026, 9, 25, 1, 6, tzinfo=UTC)


async def wait_until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0)


class HealthRecorder:
    def __init__(self) -> None:
        self.updates = []

    async def __call__(self, update) -> None:
        self.updates.append(update)


def timeout_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def invalid_json_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="not-json")


def non_object_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=["not", "an", "object"])


def test_wolfx_parser_emits_only_cenc_automatic_and_reviewed() -> None:
    payload = {
        **FIXTURE,
        "No3": {**FIXTURE["No2"], "type": "cancelled"},
    }

    result = WolfxMessageParser().parse(
        payload,
        datetime(2026, 9, 25, 1, 6, tzinfo=UTC),
    )

    assert [item.provider for item in result] == [
        CollectorProvider.WOLFX,
        CollectorProvider.WOLFX,
    ]
    assert [item.lane for item in result] == [CollectorLane.HTTP, CollectorLane.HTTP]


def test_wolfx_parser_orders_numeric_keys_and_preserves_wrappers() -> None:
    payload = {
        "No10": {**FIXTURE["No1"]},
        "No2": {**FIXTURE["No2"]},
        "No1": {**FIXTURE["No1"]},
    }

    result = WolfxMessageParser().parse(payload, fixed_now())

    assert [next(iter(item.payload)) for item in result] == ["No1", "No2", "No10"]
    assert result[0].payload["No1"]["EventID"] == FIXTURE["No1"]["EventID"]
    assert result[1].payload["No2"]["type"] == "reviewed"


def test_wolfx_parser_skips_non_mapping_values() -> None:
    payload = {
        "No1": FIXTURE["No1"],
        "No2": "not-a-mapping",
        "No3": FIXTURE["No2"],
    }

    result = WolfxMessageParser().parse(payload, fixed_now())

    assert [next(iter(item.payload)) for item in result] == ["No1", "No3"]


def test_wolfx_parser_ignores_unknown_types() -> None:
    payload = {
        "No1": FIXTURE["No1"],
        "No2": {**FIXTURE["No2"], "type": "cancelled"},
        "No3": FIXTURE["No2"],
        "No4": {**FIXTURE["No1"], "type": "unknown"},
    }

    result = WolfxMessageParser().parse(payload, fixed_now())

    assert [next(iter(item.payload)) for item in result] == ["No1", "No3"]
    assert {item.payload[next(iter(item.payload))]["type"] for item in result} == {
        "automatic",
        "reviewed",
    }


def test_wolfx_parser_recovery_since_is_inclusive_and_excludes_older_history() -> None:
    parser = WolfxMessageParser()
    equal_boundary = datetime(2026, 9, 25, 1, 1, 0, tzinfo=UTC)
    newer_boundary = datetime(2026, 9, 25, 1, 1, 1, tzinfo=UTC)

    equal_result = parser.parse(FIXTURE, fixed_now(), equal_boundary)
    filtered_result = parser.parse(FIXTURE, fixed_now(), newer_boundary)

    assert {
        item.payload[next(iter(item.payload))]["EventID"] for item in equal_result
    } == {"CENC-AUTO-2026092501", "CENC-REVIEWED-2026092502"}
    assert [
        item.payload[next(iter(item.payload))]["EventID"] for item in filtered_result
    ] == ["CENC-REVIEWED-2026092502"]


async def test_poll_once_returns_502_as_failure() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
    )

    with pytest.raises(httpx.HTTPStatusError):
        await collector.poll_once()

    await client.aclose()


async def test_poll_once_parses_object_and_uses_received_at() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FIXTURE)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
        now=fixed_now,
    )

    result = await collector.poll_once()

    assert [item.provider for item in result] == [CollectorProvider.WOLFX] * 2
    assert [item.lane for item in result] == [CollectorLane.HTTP] * 2
    assert [item.received_at for item in result] == [fixed_now(), fixed_now()]
    assert result[0].payload["No1"]["EventID"] == FIXTURE["No1"]["EventID"]

    await client.aclose()


async def test_poll_once_rejects_non_object_json() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
    )

    with pytest.raises(ValueError, match="object"):
        await collector.poll_once()

    await client.aclose()


async def test_poll_once_rejects_invalid_json() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
    )

    with pytest.raises(json.JSONDecodeError):
        await collector.poll_once()

    await client.aclose()


async def test_run_polls_immediately_then_waits_interval() -> None:
    request_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, json=FIXTURE)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stop_event = asyncio.Event()
    delays: list[float] = []

    async def sleep_until_stop(delay: float) -> None:
        delays.append(delay)
        await stop_event.wait()

    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=13,
        client=client,
        now=fixed_now,
        sleep=sleep_until_stop,
    )
    health = HealthRecorder()
    envelopes = []

    async def on_envelope(envelope) -> None:
        envelopes.append(envelope)

    task = asyncio.create_task(collector.run(on_envelope, health, stop_event))
    await wait_until(lambda: len(envelopes) == 2)
    await wait_until(lambda: len(delays) == 1)
    assert request_count == 1
    assert delays == [13.0]

    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    await client.aclose()

    assert [update.state for update in health.updates] == [
        "starting",
        "healthy",
        "stopped",
    ]


async def test_run_clears_recovery_since_after_first_full_poll() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(200, json=FIXTURE)
        return httpx.Response(200, json={"No1": FIXTURE["No1"]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stop_event = asyncio.Event()
    delays: list[float] = []

    async def sleep_once_then_stop(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 1:
            return
        await stop_event.wait()

    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=13,
        client=client,
        now=fixed_now,
        sleep=sleep_once_then_stop,
        recovery_since=datetime(2026, 9, 25, 1, 1, 30, tzinfo=UTC),
    )
    envelopes: list[object] = []

    async def on_envelope(envelope) -> None:
        envelopes.append(envelope)

    task = asyncio.create_task(
        collector.run(on_envelope=on_envelope, on_health=AsyncMock(), stop_event=stop_event)
    )
    await wait_until(lambda: len(envelopes) == 2)
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    await client.aclose()

    assert [
        item.payload[next(iter(item.payload))]["EventID"] for item in envelopes
    ] == ["CENC-REVIEWED-2026092502", "CENC-AUTO-2026092501"]
    assert request_count == 2


async def test_run_emits_healthy_on_empty_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stop_event = asyncio.Event()

    async def sleep_until_stop(delay: float) -> None:
        await stop_event.wait()

    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
        now=fixed_now,
        sleep=sleep_until_stop,
    )
    health = HealthRecorder()
    task = asyncio.create_task(
        collector.run(on_envelope=AsyncMock(), on_health=health, stop_event=stop_event)
    )

    await wait_until(lambda: any(update.state == "healthy" for update in health.updates))
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    await client.aclose()

    healthy = next(update for update in health.updates if update.state == "healthy")
    assert healthy.connected is True
    assert healthy.last_http_status == 200
    assert healthy.last_connected_at == fixed_now()
    assert healthy.consecutive_failures == 0
    assert healthy.reconnect_count == 0


async def test_run_http_error_emits_degraded_with_bounded_backoff() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stop_event = asyncio.Event()
    delays: list[float] = []

    async def sleep_until_stop(delay: float) -> None:
        delays.append(delay)
        await stop_event.wait()

    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
        now=fixed_now,
        sleep=sleep_until_stop,
        rng=random.Random(1),
    )
    health = HealthRecorder()
    task = asyncio.create_task(
        collector.run(on_envelope=AsyncMock(), on_health=health, stop_event=stop_event)
    )

    await wait_until(lambda: any(update.state == "degraded" for update in health.updates))
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    await client.aclose()

    degraded = next(update for update in health.updates if update.state == "degraded")
    assert degraded.connected is False
    assert degraded.last_error == "http status 503"
    assert degraded.consecutive_failures == 1
    assert len(delays) == 1
    assert 1.0 <= delays[0] <= 1.25


@pytest.mark.parametrize(
    ("handler", "expected_error"),
    [
        (timeout_handler, "http timeout"),
        (invalid_json_handler, "invalid JSON response"),
        (non_object_handler, "unexpected response shape"),
    ],
)
async def test_run_classifies_timeout_json_and_shape_failures(
    handler,
    expected_error: str,
) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stop_event = asyncio.Event()
    delays: list[float] = []

    async def sleep_until_stop(delay: float) -> None:
        delays.append(delay)
        await stop_event.wait()

    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
        now=fixed_now,
        sleep=sleep_until_stop,
    )
    health = HealthRecorder()
    task = asyncio.create_task(
        collector.run(on_envelope=AsyncMock(), on_health=health, stop_event=stop_event)
    )

    await wait_until(lambda: any(update.state == "degraded" for update in health.updates))
    stop_event.set()
    await asyncio.wait_for(task, timeout=1)
    await client.aclose()

    degraded = next(update for update in health.updates if update.state == "degraded")
    assert degraded.last_error == expected_error
    assert degraded.last_http_status is None
    assert degraded.last_connected_at is None
    assert len(delays) == 1
    assert 1.0 <= delays[0] <= 1.25


async def test_run_callback_exception_propagates_without_degraded() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FIXTURE)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
        now=fixed_now,
    )

    async def failing_envelope(envelope) -> None:
        raise RuntimeError("callback failed")

    health = HealthRecorder()
    task = asyncio.create_task(collector.run(on_envelope=failing_envelope, on_health=health))

    with pytest.raises(RuntimeError, match="callback failed"):
        await asyncio.wait_for(task, timeout=1)

    await client.aclose()
    assert [update.state for update in health.updates] == ["starting"]


def test_next_delay_is_capped_with_jitter_and_overflow_safe() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    collector = WolfxCollector(
        url="https://wolfx.test/cenc_eqlist.json",
        poll_interval_seconds=10,
        client=client,
        now=fixed_now,
        rng=random.Random(1),
    )

    collector._consecutive_failures = 1
    assert 1.0 <= collector._next_delay() <= 1.25

    collector._consecutive_failures = 10**18
    assert 29.0 <= collector._next_delay() <= 30.0
