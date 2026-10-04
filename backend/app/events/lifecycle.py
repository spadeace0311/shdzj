import hashlib
import json
from decimal import Decimal
from enum import StrEnum

from app.events.domain import EventKind, NormalizedEvent


class MessageFamily(StrEnum):
    AUTO = "auto"
    REVIEWED = "reviewed"


def message_family(kind: EventKind) -> MessageFamily:
    if kind is EventKind.AUTO:
        return MessageFamily.AUTO
    if kind in {EventKind.FORMAL, EventKind.CORRECTION}:
        return MessageFamily.REVIEWED
    raise ValueError(f"unsupported CENC lifecycle kind: {kind}")


def semantic_fingerprint(event: NormalizedEvent, family: MessageFamily) -> str:
    material = {
        "source": "cenc",
        "family": family.value,
        "origin_time": event.origin_time.isoformat(),
        "longitude": _decimal_text(event.longitude, 6),
        "latitude": _decimal_text(event.latitude, 6),
        "depth_km": _decimal_text(event.depth_km, 2),
        "magnitude": _decimal_text(event.magnitude, 1),
    }
    if event.report_number is not None:
        material["report_number"] = event.report_number
    else:
        material["report_time"] = (
            event.report_time.isoformat() if event.report_time is not None else None
        )
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def classify_reviewed_kind(
    existing_reviewed_kinds: tuple[EventKind, ...],
) -> EventKind:
    if any(
        kind in {EventKind.FORMAL, EventKind.CORRECTION}
        for kind in existing_reviewed_kinds
    ):
        return EventKind.CORRECTION
    return EventKind.FORMAL


def _decimal_text(value: Decimal, places: int) -> str:
    zero = Decimal(0).quantize(Decimal(1).scaleb(-places))
    return format(zero if value == 0 else value, f".{places}f")
