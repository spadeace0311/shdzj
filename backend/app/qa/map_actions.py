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
        *,
        snapshot_id: object | None = None,
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
            if snapshot_id is not None:
                normalized.setdefault("provenance", {})["snapshot_id"] = str(
                    snapshot_id
                )
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
        return _set_layers_action(payload, results)

    if action_type == "fit_bounds":
        execution, bounds = _result_with_bounds(results)
        if execution is None or bounds is None:
            return None
        payload["bounds"] = bounds
        _add_provenance(
            payload,
            execution,
            "bounds_tool",
            "bounds_source",
            "bounds_version",
        )
        return _execution_name(execution), payload

    target_ref = payload["target_ref"]
    if action_type == "buffer":
        radius_match = _result_for_target(
            target_ref,
            results,
            required_number="radius_km",
        )
        if radius_match is None:
            return None
        radius_execution, radius = radius_match
        payload["radius_km"] = radius
        target_execution, target_record = _materialize_target(
            target_ref,
            results,
        )
        if target_execution is None or target_record is None:
            return None
        feature_id = _feature_id(target_record, target_ref)
        if feature_id is None:
            return None
        center_execution, center = _coordinates_for_target(
            target_ref,
            results,
            preferred_execution=target_execution,
        )
        if center_execution is None or center is None:
            return None
        payload["feature_id"] = feature_id
        payload["center"] = center
        _add_provenance(
            payload,
            target_execution,
            "target_tool",
            "target_source",
            "target_version",
        )
        _add_provenance(
            payload,
            radius_execution,
            "radius_tool",
            "radius_source",
            "radius_version",
        )
        _add_coordinate_provenance(payload, center_execution)
        return _execution_name(radius_execution), payload

    target_execution, target_record = _materialize_target(target_ref, results)
    if target_execution is None or target_record is None:
        return None
    feature_id = _feature_id(target_record, target_ref)
    if feature_id is None:
        return None
    payload["feature_id"] = feature_id
    coordinate_execution = target_execution
    coordinates = _coordinates(target_record)
    if coordinates is None:
        coordinate_execution, coordinates = _coordinates_for_target(
            target_ref,
            results,
            preferred_execution=target_execution,
        )
    if action_type == "locate" and (
        coordinate_execution is None or coordinates is None
    ):
        return None
    if coordinates is not None:
        payload["coordinates"] = coordinates
    _add_provenance(
        payload,
        target_execution,
        "target_tool",
        "target_source",
        "target_version",
    )
    if coordinates is not None and coordinate_execution is not None:
        _add_coordinate_provenance(payload, coordinate_execution)
    return _execution_name(target_execution), payload


def _set_layers_action(
    payload: dict[str, Any],
    results: list[Any],
) -> tuple[None, dict[str, Any]] | None:
    visible: list[str] = []
    source_tools: list[str] = []
    layer_sources: dict[str, Any] = {}
    for execution in results:
        if not _is_successful(execution):
            continue
        layer = _layer_for_tool(_execution_name(execution))
        if layer is None or layer in visible:
            continue
        visible.append(layer)
        tool_name = _execution_name(execution)
        source_tools.append(tool_name)
        result = getattr(
            execution,
            "result",
            getattr(execution, "tool_result", None),
        )
        layer_sources[layer] = {
            "tool_name": tool_name,
            "source": getattr(result, "source", None),
            "version": getattr(result, "version", None),
        }
    if not visible:
        return None
    payload["layers"] = visible
    payload["visibility"] = {
        layer: layer in visible for layer in sorted(_ALLOWED_LAYERS)
    }
    payload["source_tools"] = source_tools
    payload["provenance"] = {
        "derivation": "successful_tool_executions",
        "source_tools": list(source_tools),
        "layer_sources": layer_sources,
    }
    return None, payload


def _materialize_target(
    target_ref: str,
    results: list[Any],
) -> tuple[Any | None, Mapping[str, Any] | None]:
    namespace, _, reference = target_ref.partition(":")
    namespace = namespace.lower()
    reference = reference.strip().lower()
    for execution in results:
        if not _is_successful(execution):
            continue
        value = _execution_value(execution)
        if not _target_matches(
            namespace,
            reference,
            _execution_name(execution),
            value,
        ):
            continue
        record = _target_record(
            value,
            namespace,
            reference,
            _execution_name(execution),
        )
        if record is not None:
            return execution, record
    return None, None


def _target_record(
    value: Any,
    namespace: str,
    reference: str,
    tool_name: str,
) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping) and _mapping_matches_target(
        value,
        namespace,
        reference,
        tool_name,
    ):
        return value
    for record in _iter_mappings(value):
        if _mapping_matches_target(record, namespace, reference, tool_name):
            return record
    return None


def _mapping_matches_target(
    record: Mapping[str, Any],
    namespace: str,
    reference: str,
    tool_name: str,
) -> bool:
    tool_parts = tool_name.lower().split(".", 1)
    symbolic_tool_target = (
        len(tool_parts) > 1
        and reference
        in {
            tool_parts[1],
            tool_parts[1].removeprefix("get_"),
            tool_parts[1].removeprefix("search_"),
        }
    )
    namespace_ids = _namespace_identifier_keys(namespace)
    if reference in {"nearest", "epicenter", "lookup"} or symbolic_tool_target:
        return bool(namespace_ids and any(key in record for key in namespace_ids))
    return any(
        _normalized_identifier(record.get(key)) == reference
        for key in namespace_ids
    )


def _coordinates_for_target(
    target_ref: str,
    results: list[Any],
    *,
    preferred_execution: Any,
) -> tuple[Any | None, list[float] | None]:
    preferred_value = _execution_value(preferred_execution)
    coordinates = _coordinates(preferred_value)
    if coordinates is not None:
        return preferred_execution, coordinates
    for execution in results:
        if not _is_successful(execution):
            continue
        coordinates = _coordinates(_execution_value(execution))
        if coordinates is not None:
            return execution, coordinates
    return None, None


def _coordinates(value: Any) -> list[float] | None:
    if isinstance(value, Mapping):
        direct = _coordinate_pair(value)
        if direct is not None:
            return direct
        for child in value.values():
            coordinates = _coordinates(child)
            if coordinates is not None:
                return coordinates
    elif isinstance(value, (list, tuple)):
        for child in value:
            coordinates = _coordinates(child)
            if coordinates is not None:
                return coordinates
    return None


def _coordinate_pair(value: Mapping[str, Any]) -> list[float] | None:
    longitude = _number(value.get("longitude"))
    latitude = _number(value.get("latitude"))
    if longitude is None or latitude is None:
        coordinates = value.get("coordinates")
        if (
            isinstance(coordinates, (list, tuple))
            and len(coordinates) == 2
            and all(_number(item) is not None for item in coordinates)
        ):
            longitude, latitude = (
                float(coordinates[0]),
                float(coordinates[1]),
            )
    if longitude is None or latitude is None:
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return [longitude, latitude]


def _feature_id(
    record: Mapping[str, Any],
    target_ref: str,
) -> str | None:
    namespace, _, _reference = target_ref.partition(":")
    for key in _namespace_identifier_keys(namespace.lower()):
        value = record.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    for key in _TARGET_IDENTIFIER_KEYS:
        value = record.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value)
    return None


def _namespace_identifier_keys(namespace: str) -> tuple[str, ...]:
    if namespace == "fault":
        return ("fault_key", "business_key")
    if namespace == "historical":
        return ("historical_event_id", "event_id", "business_key")
    if namespace in {"region", "area", "population", "loss"}:
        return ("area_code", "business_key")
    if namespace == "artifact":
        return ("artifact_id", "publication_id", "business_key")
    return ("event_id", "revision_id")


def _iter_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_mappings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_mappings(child)


def _normalized_identifier(value: Any) -> str | None:
    if not isinstance(value, (str, int)):
        return None
    return str(value).strip().lower()


def _layer_for_tool(tool_name: str) -> str | None:
    namespace = tool_name.split(".", 1)[0].lower()
    return {
        "event": "epicenter",
        "fault": "faults",
        "seismicity": "historical_earthquakes",
        "exposure": "population",
        "intensity": "intensity",
        "loss": "loss",
        "artifact": "artifacts",
    }.get(namespace)


def _add_provenance(
    payload: dict[str, Any],
    execution: Any,
    tool_key: str,
    source_key: str,
    version_key: str,
) -> None:
    result = getattr(
        execution,
        "result",
        getattr(execution, "tool_result", None),
    )
    provenance = payload.setdefault("provenance", {})
    provenance[tool_key] = _execution_name(execution)
    provenance[source_key] = getattr(result, "source", None)
    provenance[version_key] = getattr(result, "version", None)


def _add_coordinate_provenance(
    payload: dict[str, Any],
    execution: Any,
) -> None:
    _add_provenance(
        payload,
        execution,
        "coordinates_tool",
        "coordinates_source",
        "coordinates_version",
    )


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
