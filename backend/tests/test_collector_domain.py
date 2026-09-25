from datetime import UTC, datetime

import pytest

from app.collector.domain import CollectorEnvelope, CollectorLane, CollectorProvider


def test_collector_envelope_normalizes_timestamp_and_copies_payload() -> None:
    source = {"type": "cenc_eqlist", "No1": {"EventID": "CENC-1"}}
    envelope = CollectorEnvelope(
        provider=CollectorProvider.FAN,
        lane=CollectorLane.WEBSOCKET,
        received_at=datetime(2026, 9, 25, 1, 2, tzinfo=UTC),
        payload=source,
    )

    source["type"] = "changed"

    assert envelope.provider is CollectorProvider.FAN
    assert envelope.lane is CollectorLane.WEBSOCKET
    assert envelope.payload["type"] == "cenc_eqlist"


def test_collector_envelope_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=datetime(2026, 9, 25, 1, 2),
            payload={"No1": {}},
        )
