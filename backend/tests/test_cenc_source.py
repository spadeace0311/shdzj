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


def test_parse_treats_time_without_timezone_as_china_standard_time() -> None:
    payload = formal_payload()
    payload["originTime"] = "2026-09-17 10:30:05"

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


def test_parse_preserves_explicit_correction_report_type() -> None:
    payload = formal_payload()
    payload["reportType"] = "correction"

    event = CencAdapter().parse(payload)

    assert event.kind is EventKind.CORRECTION


def test_parse_wolfx_cenc_automatic_eqlist_contract() -> None:
    event = CencAdapter().parse(
        {
            "type": "automatic",
            "EventID": "WOLFX-AUTO-2026091701",
            "time": "2026-09-17 10:30:05",
            "ReportTime": "2026-09-17 10:31:00",
            "location": "华东某地",
            "placeName": "华东某地",
            "magnitude": 5.2,
            "depth": 12.0,
            "latitude": 31.22,
            "longitude": 121.54,
            "intensity": 4,
        }
    )

    assert event.kind is EventKind.AUTO
    assert event.source_event_id == "WOLFX-AUTO-2026091701"
    assert event.origin_time == datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)
    assert event.place == "华东某地"


def test_parse_wolfx_cenc_reviewed_eqlist_contract() -> None:
    payload = {
        "type": "reviewed",
        "EventID": "WOLFX-REVIEWED-2026091701",
        "time": "2026-09-17 10:30:05",
        "ReportTime": "2026-09-17 10:35:00",
        "location": "华东某地",
        "placeName": "华东某地",
        "magnitude": 5.2,
        "depth": 12.0,
        "latitude": 31.22,
        "longitude": 121.54,
        "intensity": 4,
    }

    event = CencAdapter().parse(payload)

    assert event.kind is EventKind.FORMAL
    assert event.origin_time == datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)


def test_parse_rejects_reviewed_wolfx_report_without_depth() -> None:
    payload = {
        "type": "reviewed",
        "EventID": "WOLFX-REVIEWED-NO-DEPTH",
        "time": "2026-09-17 10:30:05",
        "ReportTime": "2026-09-17 10:35:00",
        "location": "华东某地",
        "placeName": "华东某地",
        "magnitude": 5.2,
        "depth": None,
        "latitude": 31.22,
        "longitude": 121.54,
        "intensity": 4,
    }

    with pytest.raises(ValueError, match="depth"):
        CencAdapter().parse(payload)


def test_parse_wolfx_cenc_eew_with_null_depth_uses_placeholder() -> None:
    event = CencAdapter().parse(
        {
            "EventID": "WOLFX-EEW-2026091701",
            "ReportTime": "2026/09/17 10:30:20",
            "ReportNum": 1,
            "OriginTime": "2026/09/17 10:30:05",
            "HypoCenter": "华东某地",
            "Latitude": 31.22,
            "Longitude": 121.54,
            "Magnitude": 4.8,
            "Depth": None,
        }
    )

    assert event.kind is EventKind.AUTO
    assert event.source_event_id == "WOLFX-EEW-2026091701"
    assert event.origin_time == datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)
    assert event.depth_km == Decimal("10.00")
    assert event.place == "华东某地"


@pytest.mark.parametrize("info_type", ["正式(已核实)", "正式测定", "已核实"])
def test_parse_fan_cenc_determination_formal_contract(info_type: str) -> None:
    event = CencAdapter().parse(
        {
            "id": "FAN-FORMAL-2026091701",
            "shockTime": "2026-09-17 10:30:05",
            "infoTypeName": info_type,
            "placeName": "华东某地",
            "latitude": 31.22,
            "longitude": 121.54,
            "magnitude": 5.2,
            "depth": 12.0,
        }
    )

    assert event.kind is EventKind.FORMAL
    assert event.source_event_id == "FAN-FORMAL-2026091701"
    assert event.origin_time == datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)


def test_parse_fan_cenc_eew_contract() -> None:
    event = CencAdapter().parse(
        {
            "eventId": "FAN-EEW-2026091701",
            "shockTime": "2026-09-17 10:30:05",
            "placeName": "华东某地",
            "updates": 3,
            "latitude": 31.22,
            "longitude": 121.54,
            "magnitude": 4.8,
            "depth": 12.0,
        }
    )

    assert event.kind is EventKind.AUTO
    assert event.source_event_id == "FAN-EEW-2026091701"
    assert event.origin_time == datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "reportType": "cancel",
            "originTime": "2026-09-17 10:30:05",
            "longitude": 121.54,
            "latitude": 31.22,
            "magnitude": 5.2,
            "depth": 12.0,
            "place": "华东某地",
        },
        {
            "EventID": "WOLFX-EEW-CANCEL",
            "ReportTime": "2026/09/17 10:30:20",
            "ReportNum": 1,
            "OriginTime": "2026/09/17 10:30:05",
            "HypoCenter": "华东某地",
            "Latitude": 31.22,
            "Longitude": 121.54,
            "Magnitude": 4.8,
            "Depth": None,
            "isCancel": True,
        },
        {
            "eventId": "FAN-EEW-CANCEL",
            "shockTime": "2026-09-17 10:30:05",
            "placeName": "华东某地",
            "updates": 4,
            "latitude": 31.22,
            "longitude": 121.54,
            "magnitude": 4.8,
            "depth": 12.0,
            "cancel": True,
        },
        {
            "id": "FAN-FORMAL-CANCEL",
            "shockTime": "2026-09-17 10:30:05",
            "infoTypeName": "取消",
            "placeName": "华东某地",
            "latitude": 31.22,
            "longitude": 121.54,
            "magnitude": 5.2,
            "depth": 12.0,
        },
    ],
)
def test_parse_rejects_cancellation_reports(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="cancel"):
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


def test_parse_rejects_invalid_time() -> None:
    payload = formal_payload()
    payload["originTime"] = "not-a-time"

    with pytest.raises(ValueError, match="time"):
        CencAdapter().parse(payload)
