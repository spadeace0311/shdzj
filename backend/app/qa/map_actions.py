from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.qa.domain import MapIntent

_ALLOWED = {
    "locate": {"target_ref", "reason"},
    "fit_bounds": {"bounds", "reason"},
    "buffer": {"target_ref", "radius_km", "reason"},
    "highlight": {"target_ref", "layer_id", "reason"},
    "set_layers": {"layers", "reason"},
}
_ALLOWED_LAYERS = frozenset(
    {
        "epicenter",
        "faults",
        "historical_earthquakes",
        "population",
        "intensity",
        "loss",
        "artifacts",
    }
)
_UNSAFE_TEXT = (
    "<",
    ">",
    "http://",
    "https://",
    "javascript:",
    "data:",
    "\r",
    "\n",
)
_ACTION_TTL = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class ValidatedMapAction:
    action_type: str
    payload: dict[str, Any]
    valid_until: datetime
    source_tool: str | None = None

    @property
    def target_ref(self) -> str | None:
        return self.payload.get("target_ref")

    @property
    def reason(self) -> str:
        return str(self.payload.get("reason") or "")

    @property
    def bounds(self) -> list[float] | None:
        value = self.payload.get("bounds")
        return list(value) if value is not None else None

    @property
    def radius_km(self) -> float | None:
        value = self.payload.get("radius_km")
        return float(value) if value is not None else None

    @property
    def layer_id(self) -> str | None:
        return self.payload.get("layer_id")

    @property
    def layers(self) -> list[str] | dict[str, bool] | None:
        value = self.payload.get("layers")
        if isinstance(value, dict):
            return dict(value)
        return list(value) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_type": self.action_type,
            **self.payload,
            "source_tool": self.source_tool,
            "valid_until": self.valid_until.isoformat(),
        }


class MapActionBuilder:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))

    def build(
        self,
        map_intents: Iterable[MapIntent | Mapping[str, Any]],
        tool_results: Iterable[Any],
    ) -> list[ValidatedMapAction]:
        results = list(tool_results)
        actions: list[ValidatedMapAction] = []
        for intent in map_intents:
            payload = _normalized_intent(intent)
            if payload is None:
                continue
            action_type = payload["action_type"]
            if action_type not in _ALLOWED:
                continue
            if set(payload) - ({"action_type"} | _ALLOWED[action_type]):
                continue
            safe_payload = _validate_payload(action_type, payload)
            if safe_payload is None:
                continue
            sourced = _source_action(action_type, safe_payload, results)
            if sourced is None:
                continue
            source_tool, normalized = sourced
            now = self._clock()
            if now.tzinfo is None:
                now = now.replace(tzinfo=UTC)
            actions.append(
                ValidatedMapAction(
                    action_type=action_type,
                    payload=normalized,
                    valid_until=now.astimezone(UTC) + _ACTION_TTL,
                    source_tool=source_tool,
                )
            )
        return actions


def _normalized_intent(
    intent: MapIntent | Mapping[str, Any],
) -> dict[str, Any] | None:
    if isinstance(intent, Mapping):
        return dict(intent)
    if isinstance(intent, MapIntent) or is_dataclass(intent):
        payload = asdict(intent)
        return {key: value for key, value in payload.items() if value is not None}
    return None


def _validate_payload(
    action_type: str,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    reason = payload.get("reason")
    if not _safe_text(reason):
        return None
    normalized: dict[str, Any] = {"reason": reason}

    if action_type in {"locate", "buffer", "highlight"}:
        target_ref = payload.get("target_ref")
        if not _safe_text(target_ref):
            return None
        normalized["target_ref"] = target_ref

    if action_type == "locate":
        return normalized

    if action_type == "fit_bounds":
        return normalized

    if action_type == "buffer":
        return normalized

    if action_type == "highlight":
        layer_id = payload.get("layer_id")
        if layer_id not in _ALLOWED_LAYERS:
            return None
        normalized["layer_id"] = layer_id
        return normalized

    layers = _layers(payload.get("layers"))
    if layers is None:
        return None
    normalized["layers"] = layers
    return normalized


def _source_action(
    action_type: str,
    payload: dict[str, Any],
    results: list[Any],
) -> tuple[str | None, dict[str, Any]] | None:
    if action_type == "set_layers":
        return None, payload

    if action_type == "fit_bounds":
        execution, bounds = _result_with_bounds(results)
        if execution is None or bounds is None:
            return None
        payload["bounds"] = bounds
        return _execution_name(execution), payload

    target_ref = payload["target_ref"]
    if action_type == "buffer":
        match = _result_for_target(
            target_ref,
            results,
            required_number="radius_km",
        )
        if match is None:
            return None
        execution, radius = match
        payload["radius_km"] = radius
        return _execution_name(execution), payload

    execution = _result_for_target(target_ref, results)
    if execution is None:
        return None
    return _execution_name(execution), payload


def _result_with_bounds(
    results: list[Any],
) -> tuple[Any | None, list[float] | None]:
    for execution in results:
        if not _is_successful(execution):
            continue
        value = _execution_value(execution)
        bounds = _bounds_from_value(value)
        if bounds is not None:
            return execution, bounds
    return None, None


def _result_for_target(
    target_ref: str,
    results: list[Any],
    *,
    required_number: str | None = None,
) -> tuple[Any, float] | Any | None:
    namespace, _, reference = target_ref.partition(":")
    reference_key = reference.strip().lower()
    for execution in results:
        if not _is_successful(execution):
            continue
        value = _execution_value(execution)
        if not _target_matches(
            namespace.lower(),
            reference_key,
            _execution_name(execution),
            value,
        ):
            continue
        if required_number is None:
            return execution
        number = _top_level_number(value, required_number)
        if number is None or not 0.1 <= number <= 500:
            continue
        return execution, number
    return None


def _target_matches(
    namespace: str,
    reference: str,
    tool_name: str,
    value: Any,
) -> bool:
    tool_parts = tool_name.lower().split(".", 1)
    same_namespace = bool(tool_parts and tool_parts[0] == namespace)
    if (
        same_namespace
        and len(tool_parts) > 1
        and reference
        in {
            tool_parts[1],
            tool_parts[1].removeprefix("get_"),
            tool_parts[1].removeprefix("search_"),
        }
    ):
        return True
    if namespace == "event" and reference == "epicenter":
        return (
            _top_level_number(value, "longitude") is not None
            and _top_level_number(value, "latitude") is not None
        ) or (isinstance(value, Mapping) and value.get("event_id") is not None)
    return reference in _target_identifiers(value)


def _target_identifiers(value: Any) -> set[str]:
    identifiers: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in _TARGET_IDENTIFIER_KEYS and isinstance(
                    child,
                    (str, int),
                ):
                    identifiers.add(str(child).lower())
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return identifiers


def _bounds_from_value(value: Any) -> list[float] | None:
    if isinstance(value, Mapping):
        bounds = _bounds(value.get("bounds"))
        if bounds is not None:
            return bounds
        for child in value.values():
            found = _bounds_from_value(child)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for child in value:
            found = _bounds_from_value(child)
            if found is not None:
                return found
    return None


def _top_level_number(value: Any, key: str) -> float | None:
    if not isinstance(value, Mapping):
        return None
    return _number(value.get(key))


def _is_successful(execution: Any) -> bool:
    result = getattr(execution, "result", getattr(execution, "tool_result", None))
    return getattr(result, "status", None) == "ok"


def _execution_name(execution: Any) -> str:
    return str(
        getattr(execution, "name", None) or getattr(execution, "tool_name", None) or "unknown"
    )


def _execution_value(execution: Any) -> Any:
    result = getattr(execution, "result", getattr(execution, "tool_result", None))
    return getattr(result, "value", None)


def _bounds(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    numbers = [_number(item) for item in value]
    if any(item is None for item in numbers):
        return None
    west, south, east, north = (float(item) for item in numbers)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        return None
    return [west, south, east, north]


def _layers(value: Any) -> list[str] | dict[str, bool] | None:
    if isinstance(value, Mapping):
        if not value:
            return None
        layers: dict[str, bool] = {}
        for key, visible in value.items():
            if key not in _ALLOWED_LAYERS or not isinstance(visible, bool):
                return None
            layers[str(key)] = visible
        return layers
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if any(not isinstance(layer, str) or layer not in _ALLOWED_LAYERS for layer in value):
        return None
    if len(value) != len(set(value)):
        return None
    return list(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _safe_text(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        return False
    lowered = value.lower()
    return not any(marker in lowered for marker in _UNSAFE_TEXT)


_TARGET_IDENTIFIER_KEYS = frozenset(
    {
        "artifact_id",
        "area_code",
        "business_key",
        "event_id",
        "fault_key",
        "historical_event_id",
        "publication_id",
        "revision_id",
    }
)


__all__ = ["MapActionBuilder", "ValidatedMapAction"]
