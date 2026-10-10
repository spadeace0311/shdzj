from __future__ import annotations

from datetime import UTC, datetime, timedelta


TEST_RETENTION_DAYS = 400
DRILL_RETENTION_DAYS = 730


def retention_cutoff(event_type: str, observed_at: datetime) -> datetime:
    if event_type == "test":
        days = TEST_RETENTION_DAYS
    elif event_type == "drill":
        days = DRILL_RETENTION_DAYS
    else:
        raise ValueError(f"unsupported retention event type: {event_type}")
    return _as_utc(observed_at) - timedelta(days=days)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must include timezone information")
    return value.astimezone(UTC)
