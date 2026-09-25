from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.events.domain import EventKind, NormalizedEvent

_CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
_EEW_PLACEHOLDER_DEPTH_KM = Decimal("10.0")


class _PayloadShape(StrEnum):
    INTERNAL = "internal"
    WOLFX_EQ = "wolfx_eq"
    WOLFX_EEW = "wolfx_eew"
    FAN_EQ = "fan_eq"
    FAN_EEW = "fan_eew"


class CencAdapter:
    _INTERNAL_KIND_MAP = {
        "automatic": EventKind.AUTO,
        "auto": EventKind.AUTO,
        "formal": EventKind.FORMAL,
        "reviewed": EventKind.FORMAL,
        "correction": EventKind.CORRECTION,
    }
    _INTERNAL_FIELDS = {
        "reportType",
        "originTime",
        "longitude",
        "latitude",
        "magnitude",
        "depth",
        "place",
    }
    _WOLFX_EQ_FIELDS = {
        "type",
        "EventID",
        "time",
        "placeName",
        "magnitude",
        "depth",
        "latitude",
        "longitude",
    }
    _WOLFX_EEW_FIELDS = {
        "EventID",
        "ReportTime",
        "ReportNum",
        "OriginTime",
        "HypoCenter",
        "Latitude",
        "Longitude",
        "Magnitude",
        "Depth",
    }
    _FAN_EQ_COMMON_FIELDS = {
        "shockTime",
        "infoTypeName",
        "placeName",
        "latitude",
        "longitude",
        "magnitude",
        "depth",
    }
    _FAN_EEW_FIELDS = {
        "eventId",
        "shockTime",
        "placeName",
        "updates",
        "latitude",
        "longitude",
        "magnitude",
        "depth",
    }

    def parse(self, payload: dict[str, object]) -> NormalizedEvent:
        if not isinstance(payload, dict):
            raise TypeError("CENC payload must be a dictionary")
        if _is_cancellation(payload):
            raise ValueError("CENC cancellation reports are not supported")

        shape = _detect_shape(payload)
        if shape is _PayloadShape.WOLFX_EQ:
            return self._parse_wolfx_eq(payload)
        if shape is _PayloadShape.WOLFX_EEW:
            return self._parse_wolfx_eew(payload)
        if shape is _PayloadShape.FAN_EQ:
            return self._parse_fan_eq(payload)
        if shape is _PayloadShape.FAN_EEW:
            return self._parse_fan_eew(payload)
        return self._parse_internal(payload)

    def _parse_internal(self, payload: dict[str, object]) -> NormalizedEvent:
        _require_fields(payload, self._INTERNAL_FIELDS)
        report_type_value = payload["reportType"]
        report_type = report_type_value.lower() if isinstance(report_type_value, str) else ""
        if report_type not in self._INTERNAL_KIND_MAP:
            raise ValueError(f"unsupported CENC report type: {report_type_value}")
        return _build_event(
            kind=self._INTERNAL_KIND_MAP[report_type],
            source_event_id=_optional_text(payload.get("eventId")),
            origin_time=_parse_origin_time(payload["originTime"], "originTime"),
            longitude=_decimal_field(payload, "longitude"),
            latitude=_decimal_field(payload, "latitude"),
            depth_km=_decimal_field(payload, "depth"),
            magnitude=_decimal_field(payload, "magnitude"),
            place=_required_text(payload, "place"),
        )

    def _parse_wolfx_eq(self, payload: dict[str, object]) -> NormalizedEvent:
        _require_fields(payload, self._WOLFX_EQ_FIELDS)
        report_type = _required_text(payload, "type").lower()
        if report_type == "automatic":
            kind = EventKind.AUTO
        elif report_type == "reviewed":
            kind = EventKind.FORMAL
        else:
            raise ValueError(f"unsupported Wolfx CENC report type: {report_type}")
        return _build_event(
            kind=kind,
            source_event_id=_required_text(payload, "EventID"),
            origin_time=_parse_origin_time(payload["time"], "time"),
            longitude=_decimal_field(payload, "longitude"),
            latitude=_decimal_field(payload, "latitude"),
            depth_km=_decimal_field(payload, "depth"),
            magnitude=_decimal_field(payload, "magnitude"),
            place=_required_text(payload, "placeName"),
        )

    def _parse_wolfx_eew(self, payload: dict[str, object]) -> NormalizedEvent:
        _require_fields(payload, self._WOLFX_EEW_FIELDS)
        return _build_event(
            kind=EventKind.AUTO,
            source_event_id=_required_text(payload, "EventID"),
            origin_time=_parse_origin_time(payload["OriginTime"], "OriginTime"),
            longitude=_decimal_field(payload, "Longitude"),
            latitude=_decimal_field(payload, "Latitude"),
            depth_km=_decimal_field(
                payload,
                "Depth",
                null_default=_EEW_PLACEHOLDER_DEPTH_KM,
            ),
            magnitude=_decimal_field(payload, "Magnitude"),
            place=_required_text(payload, "HypoCenter"),
        )

    def _parse_fan_eq(self, payload: dict[str, object]) -> NormalizedEvent:
        _require_fields(payload, self._FAN_EQ_COMMON_FIELDS)
        return _build_event(
            kind=_fan_report_kind(payload["infoTypeName"]),
            source_event_id=_required_any_text(payload, ("id", "eventId")),
            origin_time=_parse_origin_time(payload["shockTime"], "shockTime"),
            longitude=_decimal_field(payload, "longitude"),
            latitude=_decimal_field(payload, "latitude"),
            depth_km=_decimal_field(payload, "depth"),
            magnitude=_decimal_field(payload, "magnitude"),
            place=_required_text(payload, "placeName"),
        )

    def _parse_fan_eew(self, payload: dict[str, object]) -> NormalizedEvent:
        _require_fields(payload, self._FAN_EEW_FIELDS)
        return _build_event(
            kind=EventKind.AUTO,
            source_event_id=_required_text(payload, "eventId"),
            origin_time=_parse_origin_time(payload["shockTime"], "shockTime"),
            longitude=_decimal_field(payload, "longitude"),
            latitude=_decimal_field(payload, "latitude"),
            depth_km=_decimal_field(
                payload,
                "depth",
                null_default=_EEW_PLACEHOLDER_DEPTH_KM,
            ),
            magnitude=_decimal_field(payload, "magnitude"),
            place=_required_text(payload, "placeName"),
        )


def _detect_shape(payload: dict[str, object]) -> _PayloadShape:
    keys = set(payload)
    exact_matches: list[_PayloadShape] = []
    for shape, required in (
        (_PayloadShape.INTERNAL, CencAdapter._INTERNAL_FIELDS),
        (_PayloadShape.WOLFX_EQ, CencAdapter._WOLFX_EQ_FIELDS),
        (_PayloadShape.WOLFX_EEW, CencAdapter._WOLFX_EEW_FIELDS),
        (_PayloadShape.FAN_EEW, CencAdapter._FAN_EEW_FIELDS),
    ):
        if required <= keys:
            exact_matches.append(shape)
    if CencAdapter._FAN_EQ_COMMON_FIELDS <= keys and {"id", "eventId"} & keys:
        exact_matches.append(_PayloadShape.FAN_EQ)
    if len(exact_matches) > 1:
        raise ValueError("ambiguous CENC payload shape")
    if exact_matches:
        return exact_matches[0]

    partial_matches: list[_PayloadShape] = []
    if "EventID" in keys and ({"OriginTime", "HypoCenter"} & keys):
        partial_matches.append(_PayloadShape.WOLFX_EEW)
    if {"type", "EventID"} <= keys:
        partial_matches.append(_PayloadShape.WOLFX_EQ)
    if {"infoTypeName", "shockTime"} <= keys and {"id", "eventId"} & keys:
        partial_matches.append(_PayloadShape.FAN_EQ)
    if {"eventId", "shockTime", "updates"} <= keys:
        partial_matches.append(_PayloadShape.FAN_EEW)
    if len(partial_matches) > 1:
        raise ValueError("ambiguous CENC payload shape")
    if partial_matches:
        return partial_matches[0]
    return _PayloadShape.INTERNAL


def _is_cancellation(payload: dict[str, object]) -> bool:
    if any(payload.get(field) is True for field in ("cancel", "isCancel", "isCanceled")):
        return True
    for field in ("reportType", "type", "infoTypeName"):
        value = payload.get(field)
        if isinstance(value, str):
            normalized = value.casefold()
            if any(marker in normalized for marker in ("cancel", "取消")):
                return True
    return False


def _fan_report_kind(value: object) -> EventKind:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid FAN CENC infoTypeName")
    normalized = value.casefold()
    if "正式" in normalized or "已核实" in normalized:
        return EventKind.FORMAL
    if normalized in {"automatic", "auto"} or "自动" in normalized:
        return EventKind.AUTO
    raise ValueError(f"unsupported FAN CENC report type: {value}")


def _build_event(
    *,
    kind: EventKind,
    source_event_id: str | None,
    origin_time: datetime,
    longitude: Decimal,
    latitude: Decimal,
    depth_km: Decimal,
    magnitude: Decimal,
    place: str,
) -> NormalizedEvent:
    return NormalizedEvent(
        kind=kind,
        source="cenc",
        source_event_id=source_event_id,
        origin_time=origin_time,
        longitude=longitude,
        latitude=latitude,
        depth_km=depth_km,
        magnitude=magnitude,
        place=place,
    )


def _parse_origin_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid CENC time: {field}")
    normalized = value.strip().replace("/", "-")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid CENC time: {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_CHINA_STANDARD_TIME)
    return parsed.astimezone(UTC)


def _require_fields(payload: dict[str, object], fields: set[str]) -> None:
    missing = fields - payload.keys()
    if missing:
        raise ValueError(f"missing CENC fields: {sorted(missing)}")


def _decimal_field(
    payload: dict[str, object],
    field: str,
    *,
    null_default: Decimal | None = None,
) -> Decimal:
    raw_value = payload[field]
    if raw_value is None:
        if null_default is not None:
            return null_default
        raise ValueError(f"invalid CENC {field}")
    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid CENC {field}") from exc
    if not value.is_finite():
        raise ValueError(f"invalid CENC {field}")
    return value


def _required_text(payload: dict[str, object], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid CENC {field}")
    return value.strip()


def _required_any_text(payload: dict[str, object], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"missing CENC event id: {list(fields)}")


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
