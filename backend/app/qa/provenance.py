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
    "lon",
    "lat",
)
_MAGNITUDE_WORDS = ("震级", "级", "magnitude")
_POPULATION_WORDS = (
    "人口",
    "常住",
    "流动",
    "居民",
    "population",
    "resident",
    "floating",
)
_AREA_WORDS = (
    "面积",
    "平方公里",
    "平方千米",
    "area",
    "km²",
)


@dataclass(frozen=True, slots=True)
class _Claim:
    kind: str
    raw: str
    value: Decimal
    start: int
    end: int


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

        tool_values = _tool_provenance(executions)
        evidence_values = _evidence_provenance(model_evidence)
        claims = _key_claims(text)
        unverified: list[str] = []
        for claim in claims:
            allowed = tool_values.get(claim.kind, set()) | evidence_values.get(
                claim.kind,
                set(),
            )
            if claim.value not in allowed:
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


def _tool_provenance(
    executions: Iterable[Any],
) -> dict[str, set[Decimal]]:
    values: dict[str, set[Decimal]] = {}
    for execution in executions:
        result = getattr(execution, "result", getattr(execution, "tool_result", None))
        if str(getattr(result, "status", "unknown")) != "ok":
            continue
        value = getattr(result, "value", None)
        if not isinstance(value, Mapping):
            continue
        _add_tool_value(values, value)
    return values


def _evidence_provenance(
    model_evidence: Iterable[Mapping[str, Any]],
) -> dict[str, set[Decimal]]:
    values: dict[str, set[Decimal]] = {}
    for item in model_evidence:
        text = item.get("text") if isinstance(item, Mapping) else None
        if not isinstance(text, str):
            continue
        for claim in _key_claims(text):
            values.setdefault(claim.kind, set()).add(claim.value)
    return values


def _add_tool_value(
    values: dict[str, set[Decimal]],
    value: Mapping[str, Any],
) -> None:
    metrics = value.get("metrics")
    if isinstance(metrics, list):
        for metric in metrics:
            if not isinstance(metric, Mapping):
                continue
            metric_key = metric.get("metric_key")
            metric_value = metric.get("numeric_value")
            kind = _kind_for_key(metric_key)
            parsed = _decimal(metric_value)
            if kind is not None and parsed is not None:
                values.setdefault(kind, set()).add(parsed)

    statistics = value.get("statistics")
    if isinstance(statistics, Mapping):
        for key, metric_value in statistics.items():
            kind = _kind_for_key(key)
            parsed = _decimal(metric_value)
            if kind is not None and parsed is not None:
                values.setdefault(kind, set()).add(parsed)

    for key, metric_value in value.items():
        if key in {"metrics", "statistics"}:
            continue
        kind = _kind_for_key(key)
        parsed = _decimal(metric_value)
        if kind is not None and parsed is not None:
            values.setdefault(kind, set()).add(parsed)
        elif isinstance(metric_value, Mapping):
            _add_tool_value(values, metric_value)
        elif isinstance(metric_value, list):
            for child in metric_value:
                if isinstance(child, Mapping):
                    _add_tool_value(values, child)


def _kind_for_key(key: Any) -> str | None:
    if not isinstance(key, str):
        return None
    normalized = key.strip().lower()
    if normalized in {"longitude", "latitude", "lon", "lat"}:
        return "coordinate"
    if normalized in {"magnitude"}:
        return "magnitude"
    if normalized in {"distance_km"}:
        return "distance"
    if normalized.endswith("_km"):
        return "distance"
    if normalized.endswith("_population") or "population" in normalized:
        return "population"
    if "area" in normalized:
        return "area"
    return None


def _key_claims(text: str) -> list[_Claim]:
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
        if not any(word in window for word in _COORDINATE_WORDS) and "震中" not in window:
            continue
        claims.extend(
            [
                _Claim(
                    kind="coordinate",
                    raw=match.group(1),
                    value=first,
                    start=match.start(1),
                    end=match.end(1),
                ),
                _Claim(
                    kind="coordinate",
                    raw=match.group(2),
                    value=second,
                    start=match.start(2),
                    end=match.end(2),
                ),
            ]
        )
        pair_ranges.add((match.start(), match.end()))

    for match in _NUMBER_PATTERN.finditer(sanitized):
        if any(start <= match.start() and match.end() <= end for start, end in pair_ranges):
            continue
        parsed = _decimal(match.group(0))
        if parsed is None:
            continue
        window = _window(sanitized, match.start(), match.end())
        if _non_metric_number(window):
            continue
        kind = _claim_kind(window)
        if kind is None:
            continue
        claims.append(
            _Claim(
                kind=kind,
                raw=match.group(0),
                value=parsed,
                start=match.start(),
                end=match.end(),
            )
        )
    return claims


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


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


__all__ = ["NumericProvenanceResult", "NumericProvenanceValidator"]
