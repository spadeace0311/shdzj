from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_FAILED_TEXT = "无法确认：回答包含缺少工具或证据支撑的关键值。"
_CITATION_PATTERN = re.compile(r"\[(?:C[0-9]+)\]", re.IGNORECASE)
_NUMBER_PATTERN = re.compile(
    r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:\s*[万亿])?(?![\w])",
)
_COORDINATE_PAIR_PATTERN = re.compile(
    r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)(?![\w])"
)

_AREA_WORDS = (
    "面积",
    "平方公里",
    "平方千米",
    "area",
    "km²",
    "km2",
)
_DISTANCE_WORDS = (
    "距离",
    "公里",
    "千米",
    "断层",
    "半径",
    "范围",
    "distance",
    "radius",
    "km",
)
_COORDINATE_WORDS = (
    "坐标",
    "经度",
    "纬度",
    "东经",
    "北纬",
    "经纬",
    "震中",
    "lon",
    "lat",
)
_MAGNITUDE_WORDS = ("震级", "级", "magnitude")
_POPULATION_WORDS = (
    "人口",
    "常住",
    "流动",
    "居民",
    "受灾",
    "死亡",
    "受伤",
    "压埋",
    "population",
    "resident",
    "floating",
)

_METRIC_ALIASES = (
    ("受灾人口", "affected_population"),
    ("受影响人口", "affected_population"),
    ("常住人口", "resident_population"),
    ("流动人口", "floating_population"),
    ("全覆盖人口", "full_population"),
    ("总人口", "full_population"),
    ("人口", "full_population"),
    ("死亡人数", "deaths"),
    ("受伤人数", "injuries"),
    ("压埋人数", "buried"),
    ("倒塌面积", "collapsed_area_m2"),
    ("破坏面积", "total_area_m2"),
    ("影响面积", "area_sq_km"),
    ("面积", "area_sq_km"),
    ("震级", "magnitude"),
    ("经度", "coordinate"),
    ("纬度", "coordinate"),
    ("坐标", "coordinate"),
)

_UNIT_ALIASES = (
    ("平方公里", "km2"),
    ("平方千米", "km2"),
    ("平方米", "m2"),
    ("公里", "km"),
    ("千米", "km"),
    ("万人", "person"),
    ("人", "person"),
    ("户", "household"),
    ("km²", "km2"),
    ("km2", "km2"),
    ("m2", "m2"),
    ("km", "km"),
    ("m", "m"),
)


@dataclass(frozen=True, slots=True)
class _Claim:
    kind: str
    raw: str
    value: Decimal
    start: int
    end: int
    metric: str | None = None
    unit: str | None = None
    entity: str | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class _SourceValue:
    kind: str
    value: Decimal
    metric: str | None
    unit: str | None
    entity: str | None
    scope: str | None
    structured: bool


@dataclass(frozen=True, slots=True)
class NumericProvenanceResult:
    safe: bool
    clean_text: str
    unverified_claim_kinds: tuple[str, ...]


class NumericProvenanceValidator:
    def validate(
        self,
        text: str,
        *,
        executions: Iterable[Any],
        model_evidence: Iterable[Mapping[str, Any]],
    ) -> NumericProvenanceResult:
        if not isinstance(text, str):
            return NumericProvenanceResult(False, _FAILED_TEXT, ("invalid_text",))

        sources = [
            *_tool_provenance(executions),
            *_evidence_provenance(model_evidence),
        ]
        entities = _ordered_unique(
            source.entity for source in sources if source.entity
        )
        claims = _key_claims(text, entities=entities)
        unverified: list[str] = []
        for claim in claims:
            if not any(_source_matches(claim, source) for source in sources):
                unverified.append(claim.kind)

        if unverified:
            return NumericProvenanceResult(
                safe=False,
                clean_text=_FAILED_TEXT,
                unverified_claim_kinds=_ordered_unique(unverified),
            )
        return NumericProvenanceResult(
            safe=True,
            clean_text=text,
            unverified_claim_kinds=(),
        )


def _tool_provenance(executions: Iterable[Any]) -> list[_SourceValue]:
    sources: list[_SourceValue] = []
    for execution in executions:
        result = getattr(execution, "result", getattr(execution, "tool_result", None))
        if str(getattr(result, "status", "unknown")) != "ok":
            continue
        value = getattr(result, "value", None)
        if not isinstance(value, Mapping):
            continue
        parameters = _mapping(getattr(result, "parameters", {}))
        scope = _area_scope(parameters)
        _add_tool_value(
            sources,
            value,
            inherited_unit=_normalize_unit(getattr(result, "unit", None)),
            inherited_scope=scope,
        )
    return sources


def _evidence_provenance(
    model_evidence: Iterable[Mapping[str, Any]],
) -> list[_SourceValue]:
    sources: list[_SourceValue] = []
    for item in model_evidence:
        if not isinstance(item, Mapping):
            continue
        if item.get("kind") == "structured":
            metric_key = item.get("metric_key")
            metric_value = item.get("value")
            kind = _kind_for_metric(metric_key, item.get("unit"))
            parsed = _decimal(metric_value)
            if kind is None or parsed is None:
                continue
            scope = _mapping(item.get("scope") or {})
            entity = _entity_value(scope)
            metric = _canonical_metric(metric_key)
            unit = _normalize_unit(item.get("unit")) or _unit_for_metric_key(
                metric_key
            )
            sources.append(
                _SourceValue(
                    kind=kind,
                    value=parsed,
                    metric=metric,
                    unit=unit,
                    entity=entity,
                    scope=_area_scope(scope),
                    structured=entity is not None,
                )
            )
            continue

        text = item.get("text")
        if not isinstance(text, str):
            continue
        for claim in _key_claims(text):
            sources.append(
                _SourceValue(
                    kind=claim.kind,
                    value=claim.value,
                    metric=claim.metric,
                    unit=claim.unit,
                    entity=claim.entity,
                    scope=claim.scope,
                    structured=claim.entity is not None,
                )
            )
    return sources


def _add_tool_value(
    sources: list[_SourceValue],
    value: Mapping[str, Any],
    *,
    inherited_unit: str | None,
    inherited_scope: str | None,
    inherited_entity: str | None = None,
) -> None:
    metrics = value.get("metrics")
    if isinstance(metrics, list):
        for metric in metrics:
            if not isinstance(metric, Mapping):
                continue
            metric_key = metric.get("metric_key")
            metric_value = metric.get("numeric_value")
            unit = _normalize_unit(metric.get("unit")) or inherited_unit
            kind = _kind_for_metric(metric_key, unit)
            parsed = _decimal(metric_value)
            if kind is None or parsed is None:
                continue
            entity = _entity_value(metric) or inherited_entity
            sources.append(
                _SourceValue(
                    kind=kind,
                    value=parsed,
                    metric=_canonical_metric(metric_key),
                    unit=unit or _unit_for_metric_key(metric_key),
                    entity=entity,
                    scope=_area_scope(metric) or inherited_scope,
                    structured=entity is not None,
                )
            )

    statistics = value.get("statistics")
    if isinstance(statistics, Mapping):
        for key, metric_value in statistics.items():
            kind = _kind_for_metric(key, inherited_unit)
            parsed = _decimal(metric_value)
            if kind is None or parsed is None:
                continue
            sources.append(
                _SourceValue(
                    kind=kind,
                    value=parsed,
                    metric=_canonical_metric(key),
                    unit=inherited_unit or _unit_for_metric_key(key),
                    entity=None,
                    scope=inherited_scope,
                    structured=False,
                )
            )

    for key, metric_value in value.items():
        if key in {"metrics", "statistics"}:
            continue
        kind = _kind_for_metric(key, inherited_unit)
        parsed = _decimal(metric_value)
        if kind is not None and parsed is not None:
            entity = _entity_value(value) or inherited_entity
            sources.append(
                _SourceValue(
                    kind=kind,
                    value=parsed,
                    metric=_canonical_metric(key),
                    unit=inherited_unit or _unit_for_metric_key(key),
                    entity=entity,
                    scope=_area_scope(value) or inherited_scope,
                    structured=entity is not None,
                )
            )
            continue
        if isinstance(metric_value, Mapping):
            _add_tool_value(
                sources,
                metric_value,
                inherited_unit=inherited_unit,
                inherited_scope=inherited_scope,
                inherited_entity=_entity_value(value) or inherited_entity,
            )
        elif isinstance(metric_value, list):
            for child in metric_value:
                if isinstance(child, Mapping):
                    _add_tool_value(
                        sources,
                        child,
                        inherited_unit=inherited_unit,
                        inherited_scope=inherited_scope,
                        inherited_entity=_entity_value(value) or inherited_entity,
                    )


def _source_matches(claim: _Claim, source: _SourceValue) -> bool:
    if source.kind != claim.kind or source.value != claim.value:
        return False
    if source.entity and claim.entity and source.entity != claim.entity:
        return False
    if source.structured and source.entity and not claim.entity:
        return False
    if claim.entity and not source.entity:
        return False
    if source.scope and claim.scope and source.scope != claim.scope:
        return False
    if source.metric and claim.metric and source.metric != claim.metric:
        return False
    if source.unit and claim.unit and source.unit != claim.unit:
        return False
    return True


def _key_claims(
    text: str,
    *,
    entities: Iterable[str] = (),
) -> list[_Claim]:
    known_entities = tuple(_ordered_unique(entities))
    sanitized = _CITATION_PATTERN.sub(" ", text)
    claims: list[_Claim] = []
    pair_ranges: set[tuple[int, int]] = set()

    for match in _COORDINATE_PAIR_PATTERN.finditer(sanitized):
        first = _decimal(match.group(1))
        second = _decimal(match.group(2))
        if first is None or second is None:
            continue
        if not _plausible_coordinate(first) or not _plausible_coordinate(second):
            continue
        window = _window(sanitized, match.start(), match.end())
        prefix = _prefix(sanitized, match.start())
        if not any(word in window for word in _COORDINATE_WORDS):
            continue
        common = _claim_metadata(window, prefix, "coordinate", known_entities)
        claims.extend(
            [
                _Claim(
                    kind="coordinate",
                    raw=match.group(1),
                    value=first,
                    start=match.start(1),
                    end=match.end(1),
                    **common,
                ),
                _Claim(
                    kind="coordinate",
                    raw=match.group(2),
                    value=second,
                    start=match.start(2),
                    end=match.end(2),
                    **common,
                ),
            ]
        )
        pair_ranges.add((match.start(), match.end()))

    for match in _NUMBER_PATTERN.finditer(sanitized):
        if any(
            start <= match.start() and match.end() <= end
            for start, end in pair_ranges
        ):
            continue
        parsed = _decimal(match.group(0))
        if parsed is None:
            continue
        window = _window(sanitized, match.start(), match.end())
        prefix = _prefix(sanitized, match.start())
        if _non_metric_number(window):
            continue
        kind = _claim_kind(window)
        if kind is None:
            continue
        metadata = _claim_metadata(window, prefix, kind, known_entities)
        claims.append(
            _Claim(
                kind=kind,
                raw=match.group(0),
                value=parsed,
                start=match.start(),
                end=match.end(),
                **metadata,
            )
        )
    return claims


def _claim_metadata(
    window: str,
    prefix: str,
    kind: str,
    entities: tuple[str, ...],
) -> dict[str, str | None]:
    return {
        "metric": _metric_for_window(window, kind),
        "unit": _unit_for_window(window, kind),
        "entity": _entity_in_window(prefix, entities),
        "scope": _scope_for_window(prefix),
    }


def _claim_kind(window: str) -> str | None:
    lowered = window.lower()
    if any(word in lowered for word in _AREA_WORDS):
        return "area"
    if any(word in lowered for word in _DISTANCE_WORDS):
        return "distance"
    if any(word in lowered for word in _COORDINATE_WORDS):
        return "coordinate"
    if any(word in lowered for word in _MAGNITUDE_WORDS):
        return "magnitude"
    if any(word in lowered for word in _POPULATION_WORDS):
        return "population"
    return None


def _metric_for_window(window: str, kind: str) -> str | None:
    lowered = window.lower()
    for alias, metric in _METRIC_ALIASES:
        if alias.lower() in lowered:
            return metric
    if kind == "distance":
        return "distance_km"
    if kind == "area":
        return "area_sq_km"
    if kind == "coordinate":
        return "coordinate"
    if kind == "magnitude":
        return "magnitude"
    return None


def _unit_for_window(window: str, kind: str) -> str | None:
    lowered = window.lower()
    for alias, unit in _UNIT_ALIASES:
        if alias.lower() in lowered:
            return unit
    if kind == "distance":
        return "km"
    if kind == "area":
        return "km2"
    return None


def _entity_in_window(
    window: str,
    entities: tuple[str, ...],
) -> str | None:
    normalized_window = window.lower()
    best: str | None = None
    best_position = -1
    for entity in entities:
        position = normalized_window.rfind(entity.lower())
        if position > best_position:
            best_position = position
            best = entity
    return best


def _scope_for_window(window: str) -> str | None:
    if "镇" in window or "街道" in window:
        return "town"
    if "县" in window or "区" in window:
        return "county"
    if "全市" in window or "市" in window:
        return "city"
    return None


def _kind_for_metric(key: Any, unit: str | None = None) -> str | None:
    if not isinstance(key, str):
        return None
    normalized = key.strip().lower()
    if normalized in {"longitude", "latitude", "lon", "lat"}:
        return "coordinate"
    if normalized == "magnitude":
        return "magnitude"
    if (
        "area" in normalized
        or normalized.endswith("_m2")
        or normalized.endswith("_sq_km")
    ):
        return "area"
    if normalized == "distance_km" or normalized.endswith("_km"):
        return "distance"
    if (
        normalized.endswith("_population")
        or "population" in normalized
        or normalized in {"deaths", "injuries", "buried"}
    ):
        return "population"
    if unit in {"person", "人"}:
        return "population"
    return None


def _canonical_metric(key: Any) -> str | None:
    if not isinstance(key, str):
        return None
    normalized = key.strip().lower()
    if normalized in {"longitude", "latitude", "lon", "lat"}:
        return "coordinate"
    aliases = {
        "total_population": "full_population",
        "population": "full_population",
        "area": "area_sq_km",
        "radius_km": "distance_km",
    }
    return aliases.get(normalized, normalized)


def _unit_for_metric_key(key: Any) -> str | None:
    if not isinstance(key, str):
        return None
    normalized = key.strip().lower()
    if normalized.endswith("_sq_km"):
        return "km2"
    if normalized.endswith("_m2") or "area" in normalized:
        return "m2"
    if normalized.endswith("_km"):
        return "km"
    if (
        normalized.endswith("_population")
        or "population" in normalized
        or normalized in {"deaths", "injuries", "buried"}
    ):
        return "person"
    return None


def _normalize_unit(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    aliases = {
        "km²": "km2",
        "square kilometers": "km2",
        "平方千米": "km2",
        "平方公里": "km2",
        "square meters": "m2",
        "平方米": "m2",
        "people": "person",
        "persons": "person",
        "人": "person",
        "户": "household",
    }
    return aliases.get(normalized, normalized)


def _entity_value(value: Mapping[str, Any]) -> str | None:
    for key in ("area_name", "area_code"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return None


def _area_scope(value: Mapping[str, Any]) -> str | None:
    candidate = value.get("area_scope")
    if candidate in {"city", "county", "town"}:
        return str(candidate)
    return None


def _non_metric_number(window: str) -> bool:
    lowered = window.lower()
    return any(
        word in lowered
        for word in (
            "年",
            "月",
            "日",
            "时",
            "分",
            "秒",
            "页",
            "章节",
            "编号",
            "版本",
        )
    ) or bool(re.search(r"(?<![A-Za-z])C[0-9]*$", lowered))


def _window(text: str, start: int, end: int) -> str:
    return text[max(0, start - 80) : min(len(text), end + 80)]


def _prefix(text: str, start: int) -> str:
    return text[max(0, start - 80) : start]


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return Decimal(str(value))
    if not isinstance(value, str):
        return None
    normalized = value.strip().replace(",", "")
    multiplier = Decimal(1)
    if normalized.endswith("万"):
        multiplier = Decimal(10_000)
        normalized = normalized[:-1]
    elif normalized.endswith("亿"):
        multiplier = Decimal(100_000_000)
        normalized = normalized[:-1]
    try:
        parsed = Decimal(normalized) * multiplier
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _plausible_coordinate(value: Decimal) -> bool:
    return Decimal("-180") <= value <= Decimal("180")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _ordered_unique(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value)
        if text not in result:
            result.append(text)
    return tuple(result)


__all__ = ["NumericProvenanceResult", "NumericProvenanceValidator"]
