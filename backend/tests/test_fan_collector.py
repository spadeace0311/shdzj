import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

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


async def fake_sleep(delay: float) -> None:
    return None


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
