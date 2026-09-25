from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.events.domain import EventKind, NormalizedEvent, canonical_source_id


def make_event(**overrides: object) -> NormalizedEvent:
    values: dict[str, object] = {
        "kind": EventKind.AUTO,
        "source": "cenc",
        "source_event_id": None,
        "origin_time": datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC),
        "longitude": Decimal("121.540000"),
        "latitude": Decimal("31.220000"),
        "depth_km": Decimal("12.00"),
        "magnitude": Decimal("5.2"),
        "place": "上海浦东新区",
    }
    values.update(overrides)
    return NormalizedEvent(**values)  # type: ignore[arg-type]


def test_source_event_id_has_stable_preferred_identity() -> None:
    event = make_event(source_event_id="CENC-2026-0001")

    assert canonical_source_id(event) == "cenc:CENC-2026-0001"


def test_fallback_identity_is_stable() -> None:
    event = make_event()

    assert canonical_source_id(event) == canonical_source_id(event)
    assert canonical_source_id(event).startswith("cenc:fallback:")


def test_fallback_identity_ignores_place_and_sub_minute_time_changes() -> None:
    first = make_event()
    second = make_event(
        origin_time=datetime(2026, 9, 17, 2, 30, 59, 999999, tzinfo=UTC),
        longitude=Decimal("121.54"),
        latitude=Decimal("31.22"),
        depth_km=Decimal("12.0"),
        place="修订后的参考地名",
    )

    assert canonical_source_id(first) == canonical_source_id(second)


def test_fallback_identity_changes_with_material_event_fields() -> None:
    event = make_event()

    assert canonical_source_id(event) != canonical_source_id(make_event(magnitude=Decimal("5.3")))
    assert canonical_source_id(event) != canonical_source_id(
        make_event(longitude=Decimal("121.541"))
    )


def test_domain_normalizes_origin_time_to_utc() -> None:
    event = make_event(
        origin_time=datetime(
            2026,
            9,
            17,
            10,
            30,
            5,
            tzinfo=timezone(timedelta(hours=8)),
        )
    )

    assert event.origin_time == datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC)


def test_domain_quantizes_values_to_database_precision() -> None:
    event = make_event(
        longitude=Decimal("121.1234565"),
        latitude=Decimal("31.9876545"),
        depth_km=Decimal("12.345"),
        magnitude=Decimal("5.25"),
    )

    assert event.longitude == Decimal("121.123457")
    assert event.latitude == Decimal("31.987655")
    assert event.depth_km == Decimal("12.35")
    assert event.magnitude == Decimal("5.3")


def test_domain_validates_range_after_quantization() -> None:
    event = make_event(
        longitude=Decimal("180.0000004"),
        latitude=Decimal("-90.0000004"),
    )

    assert event.longitude == Decimal("180.000000")
    assert event.latitude == Decimal("-90.000000")

    with pytest.raises(ValueError, match="longitude"):
        make_event(longitude=Decimal("180.0000005"))


def test_domain_rejects_naive_origin_time() -> None:
    with pytest.raises(ValueError, match="timezone"):
        make_event(origin_time=datetime(2026, 9, 17, 2, 30, 5))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("longitude", Decimal("180.000001")),
        ("latitude", Decimal("-90.000001")),
        ("depth_km", Decimal("-0.01")),
        ("depth_km", Decimal("1000.01")),
        ("magnitude", Decimal("-2.1")),
        ("magnitude", Decimal("12.1")),
    ],
)
def test_domain_rejects_out_of_range_values(field: str, value: Decimal) -> None:
    with pytest.raises(ValueError, match=field):
        make_event(**{field: value})
