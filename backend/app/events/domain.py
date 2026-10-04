import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum


class EventKind(StrEnum):
    AUTO = "auto"
    FORMAL = "formal"
    CORRECTION = "correction"
    MANUAL = "manual"
    TEST = "test"
    DRILL = "drill"


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    kind: EventKind
    source: str
    source_event_id: str | None
    origin_time: datetime
    longitude: Decimal
    latitude: Decimal
    depth_km: Decimal
    magnitude: Decimal
    place: str
    report_time: datetime | None = None
    report_number: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EventKind):
            raise TypeError("kind must be an EventKind")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if self.source_event_id is not None and not isinstance(self.source_event_id, str):
            raise TypeError("source_event_id must be a string or None")
        if not isinstance(self.origin_time, datetime):
            raise TypeError("origin_time must be a datetime")
        if self.origin_time.tzinfo is None or self.origin_time.utcoffset() is None:
            raise ValueError("origin_time must include timezone information")
        if not isinstance(self.place, str) or not self.place.strip():
            raise ValueError("place must not be empty")
        if self.report_time is not None:
            if not isinstance(self.report_time, datetime):
                raise TypeError("report_time must be a datetime or None")
            if self.report_time.tzinfo is None or self.report_time.utcoffset() is None:
                raise ValueError("report_time must include timezone information")
        if self.report_number is not None:
            if isinstance(self.report_number, bool) or not isinstance(self.report_number, int):
                raise TypeError("report_number must be a non-negative integer or None")
            if self.report_number < 0:
                raise ValueError("report_number must be a non-negative integer or None")

        object.__setattr__(self, "origin_time", self.origin_time.astimezone(UTC))
        if self.report_time is not None:
            object.__setattr__(self, "report_time", self.report_time.astimezone(UTC))
        object.__setattr__(
            self,
            "longitude",
            _quantize_decimal(self.longitude, "longitude", Decimal("0.000001")),
        )
        object.__setattr__(
            self,
            "latitude",
            _quantize_decimal(self.latitude, "latitude", Decimal("0.000001")),
        )
        object.__setattr__(
            self,
            "depth_km",
            _quantize_decimal(self.depth_km, "depth_km", Decimal("0.01")),
        )
        object.__setattr__(
            self,
            "magnitude",
            _quantize_decimal(self.magnitude, "magnitude", Decimal("0.1")),
        )

        _validate_decimal(self.longitude, "longitude", Decimal("-180"), Decimal("180"))
        _validate_decimal(self.latitude, "latitude", Decimal("-90"), Decimal("90"))
        _validate_decimal(self.depth_km, "depth_km", Decimal("0"), Decimal("1000"))
        _validate_decimal(self.magnitude, "magnitude", Decimal("-2"), Decimal("12"))


def _quantize_decimal(value: Decimal, field: str, quantum: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    try:
        quantized = value.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError(f"{field} cannot be quantized") from exc
    return Decimal(0).quantize(quantum) if quantized == 0 else quantized


def _validate_decimal(value: Decimal, field: str, minimum: Decimal, maximum: Decimal) -> None:
    if value < minimum or value > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")


def _stable_decimal(value: Decimal, precision: int) -> str:
    normalized = Decimal(0) if value == 0 else value
    return f"{normalized:.{precision}f}"


def canonical_source_id(event: NormalizedEvent) -> str:
    if event.source_event_id:
        return f"{event.source}:{event.source_event_id}"

    origin_minute = event.origin_time.astimezone(UTC).replace(second=0, microsecond=0)
    material = "|".join(
        [
            event.source,
            origin_minute.isoformat(),
            _stable_decimal(event.longitude, 3),
            _stable_decimal(event.latitude, 3),
            _stable_decimal(event.magnitude, 1),
            _stable_decimal(event.depth_km, 1),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{event.source}:fallback:{digest}"
