from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.events.domain import EventKind
from app.events.sources.cenc import CencAdapter


def formal_payload() -> dict[str, object]:
    return {
        "eventId": "CENC-2026-0001",
        "reportType": "formal",
        "originTime": "2026-09-17T02:30:05Z",
        "longitude": 121.54,
        "latitude": 31.22,
        "magnitude": 5.2,
        "depth": 12.0,
        "place": "上海浦东新区",
    }


def test_parse_formal_cenc_message() -> None:
    event = CencAdapter().parse(formal_payload())

    assert event.kind is EventKind.FORMAL
    assert event.source == "cenc"
    assert event.source_event_id == "CENC-2026-0001"
    assert event.origin_time == datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)
    assert event.longitude == Decimal("121.54")
    assert event.latitude == Decimal("31.22")
    assert event.depth_km == Decimal("12.0")
    assert event.magnitude == Decimal("5.2")
    assert event.place == "上海浦东新区"


def test_parse_normalizes_offset_time_to_utc() -> None:
    payload = formal_payload()
    payload["originTime"] = "2026-09-17T10:30:05+08:00"

    event = CencAdapter().parse(payload)

    assert event.origin_time == datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)


def test_parse_without_event_id_uses_fallback_identity_inputs() -> None:
    payload = formal_payload()
    del payload["eventId"]

    event = CencAdapter().parse(payload)

    assert event.source_event_id is None


@pytest.mark.parametrize("place", [None, "", "   "])
def test_parse_rejects_invalid_place(place: object) -> None:
    payload = formal_payload()
    payload["place"] = place

    with pytest.raises(ValueError, match="place"):
        CencAdapter().parse(payload)


def test_parse_rejects_empty_payload() -> None:
    with pytest.raises(ValueError, match="missing CENC fields"):
        CencAdapter().parse({})


@pytest.mark.parametrize(
    "field",
    ["reportType", "originTime", "longitude", "latitude", "magnitude", "depth", "place"],
)
def test_parse_rejects_missing_required_fields(field: str) -> None:
    payload = formal_payload()
    del payload[field]

    with pytest.raises(ValueError, match="missing CENC fields"):
        CencAdapter().parse(payload)


def test_parse_rejects_unknown_report_type() -> None:
    payload = formal_payload()
    payload["reportType"] = "unknown"

    with pytest.raises(ValueError, match="unsupported CENC report type"):
        CencAdapter().parse(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("longitude", "not-a-number"),
        ("latitude", 91),
        ("depth", -0.01),
        ("magnitude", 12.1),
    ],
)
def test_parse_rejects_invalid_or_out_of_range_numbers(field: str, value: object) -> None:
    payload = formal_payload()
    payload[field] = value

    with pytest.raises(ValueError, match=field):
        CencAdapter().parse(payload)


def test_parse_rejects_time_without_timezone() -> None:
    payload = formal_payload()
    payload["originTime"] = "2026-09-17T02:30:05"

    with pytest.raises(ValueError, match="timezone"):
        CencAdapter().parse(payload)
