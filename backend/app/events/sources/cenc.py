from datetime import datetime
from decimal import Decimal, InvalidOperation

from app.events.domain import EventKind, NormalizedEvent


class CencAdapter:
    _KIND_MAP = {
        "automatic": EventKind.AUTO,
        "auto": EventKind.AUTO,
        "formal": EventKind.FORMAL,
        "correction": EventKind.CORRECTION,
    }
    _REQUIRED_FIELDS = {
        "reportType",
        "originTime",
        "longitude",
        "latitude",
        "magnitude",
        "depth",
        "place",
    }

    def parse(self, payload: dict[str, object]) -> NormalizedEvent:
        if not isinstance(payload, dict):
            raise TypeError("CENC payload must be a dictionary")

        missing = self._REQUIRED_FIELDS - payload.keys()
        if missing:
            raise ValueError(f"missing CENC fields: {sorted(missing)}")

        report_type_value = payload["reportType"]
        report_type = report_type_value.lower() if isinstance(report_type_value, str) else ""
        if report_type not in self._KIND_MAP:
            raise ValueError(f"unsupported CENC report type: {report_type_value}")

        origin_time_value = payload["originTime"]
        if not isinstance(origin_time_value, str):
            raise ValueError("invalid CENC originTime: expected an ISO 8601 string")
        try:
            origin_time = datetime.fromisoformat(origin_time_value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("invalid CENC originTime") from exc
        if origin_time.tzinfo is None or origin_time.utcoffset() is None:
            raise ValueError("invalid CENC originTime: timezone information is required")

        place = payload["place"]
        if not isinstance(place, str) or not place.strip():
            raise ValueError("invalid CENC place")

        return NormalizedEvent(
            kind=self._KIND_MAP[report_type],
            source="cenc",
            source_event_id=_optional_text(payload.get("eventId")),
            origin_time=origin_time,
            longitude=_decimal_field(payload, "longitude"),
            latitude=_decimal_field(payload, "latitude"),
            depth_km=_decimal_field(payload, "depth"),
            magnitude=_decimal_field(payload, "magnitude"),
            place=place.strip(),
        )


def _decimal_field(payload: dict[str, object], field: str) -> Decimal:
    try:
        value = Decimal(str(payload[field]))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid CENC {field}") from exc
    if not value.is_finite():
        raise ValueError(f"invalid CENC {field}")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
