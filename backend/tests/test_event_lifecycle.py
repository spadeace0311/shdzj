from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.events.domain import EventKind, NormalizedEvent
from app.events.lifecycle import (
    MessageFamily,
    classify_reviewed_kind,
    message_family,
    semantic_fingerprint,
)


BASE = NormalizedEvent(
    kind=EventKind.FORMAL,
    source="cenc",
    source_event_id="CENC-1",
    origin_time=datetime(2026, 9, 25, 1, 2, 3, tzinfo=UTC),
    longitude=Decimal("121.500000"),
    latitude=Decimal("31.200000"),
    depth_km=Decimal("10.00"),
    magnitude=Decimal("5.2"),
    place="上海测试位置",
    report_time=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
)


def test_fingerprint_ignores_provider_event_id() -> None:
    backup = replace(BASE, source_event_id="WOLFX-ALIAS")

    assert semantic_fingerprint(BASE, MessageFamily.REVIEWED) == semantic_fingerprint(
        backup,
        MessageFamily.REVIEWED,
    )


def test_fingerprint_changes_when_magnitude_changes() -> None:
    correction = replace(BASE, magnitude=Decimal("5.3"))

    assert semantic_fingerprint(BASE, MessageFamily.REVIEWED) != semantic_fingerprint(
        correction,
        MessageFamily.REVIEWED,
    )


def test_fingerprint_ignores_report_time_when_report_number_exists() -> None:
    numbered = replace(
        BASE,
        report_number=2,
        report_time=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
    )
    equivalent = replace(
        numbered,
        report_time=datetime(2026, 9, 25, 1, 5, 1, tzinfo=UTC),
    )

    assert semantic_fingerprint(numbered, MessageFamily.REVIEWED) == semantic_fingerprint(
        equivalent,
        MessageFamily.REVIEWED,
    )


def test_fingerprint_uses_report_time_when_report_number_is_missing() -> None:
    first = replace(BASE, report_number=None, report_time=datetime(2026, 9, 25, 1, 5, tzinfo=UTC))
    later = replace(first, report_time=datetime(2026, 9, 25, 1, 6, tzinfo=UTC))

    assert semantic_fingerprint(first, MessageFamily.REVIEWED) != semantic_fingerprint(
        later,
        MessageFamily.REVIEWED,
    )


def test_formal_and_correction_share_reviewed_family_but_auto_does_not() -> None:
    assert message_family(EventKind.FORMAL) is MessageFamily.REVIEWED
    assert message_family(EventKind.CORRECTION) is MessageFamily.REVIEWED
    assert message_family(EventKind.AUTO) is MessageFamily.AUTO


def test_reviewed_kind_is_formal_until_a_reviewed_revision_exists() -> None:
    assert classify_reviewed_kind(()) is EventKind.FORMAL
    assert classify_reviewed_kind((EventKind.AUTO,)) is EventKind.FORMAL
    assert classify_reviewed_kind((EventKind.FORMAL,)) is EventKind.CORRECTION
    assert classify_reviewed_kind((EventKind.FORMAL, EventKind.CORRECTION)) is EventKind.CORRECTION
