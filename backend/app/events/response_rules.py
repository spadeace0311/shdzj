from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class ResponseInput:
    magnitude: Decimal
    depth_km: Decimal
    inside_shanghai: bool | None
    distance_to_boundary_km: Decimal | None
    deaths: int | None
    max_intensity: Decimal | None


@dataclass(frozen=True, slots=True)
class ResponseSuggestion:
    institutional_level: str
    service_level: int | None
    downgraded: bool
    causes: tuple[str, ...]
    rule_version: str


class ResponseRuleEngine:
    def __init__(self, config: dict) -> None:
        self._config = config

    @classmethod
    def from_yaml(cls, path: str | Path) -> ResponseRuleEngine:
        config_path = Path(path)
        if not config_path.is_file():
            repo_root = Path(__file__).resolve().parents[3]
            fallback = repo_root / config_path.as_posix().lstrip("/")
            if fallback.is_file():
                config_path = fallback

        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("response rules YAML must contain a mapping")
        return cls(config)

    def suggest(self, value: ResponseInput) -> ResponseSuggestion:
        institutional, causes = self._institutional(value)
        service = self._service(value)
        downgraded = False

        local_depth_threshold = self._decimal(
            self._config["institutional"]["downgrade"]["local_depth_gt_km"]
        )
        if value.inside_shanghai and value.depth_km > local_depth_threshold:
            institutional = self._downgrade(institutional)
            causes.append(
                f"震源深度大于{self._number_text(local_depth_threshold)}公里，制度响应建议降低一级"
            )
            downgraded = True

        boundary = self._config["institutional"]["downgrade"]["external_boundary_distance_km"]
        boundary_min = self._decimal(boundary["min_inclusive"])
        boundary_max = self._decimal(boundary["max_inclusive"])
        if (
            value.inside_shanghai is False
            and value.distance_to_boundary_km is not None
            and boundary_min <= value.distance_to_boundary_km <= boundary_max
        ):
            institutional = self._downgrade(institutional)
            causes.append(
                f"外省震中距上海边界{self._number_text(boundary_min)}至"
                f"{self._number_text(boundary_max)}公里，制度响应建议降低一级"
            )
            downgraded = True

        intensity_threshold = self._decimal(
            self._config["trigger"]["default_max_intensity_threshold"]
        )
        if value.max_intensity is not None and value.max_intensity >= intensity_threshold:
            causes.append(
                f"预测最大烈度达到{self._number_text(intensity_threshold)}度，触发专项评估"
            )

        return ResponseSuggestion(
            institutional_level=institutional,
            service_level=service,
            downgraded=downgraded,
            causes=tuple(causes),
            rule_version=str(self._config["version"]),
        )

    def _institutional(self, value: ResponseInput) -> tuple[str, list[str]]:
        if value.inside_shanghai is None:
            return "pending", ["缺少上海行政边界和震中位置关系，暂不形成制度响应建议"]
        if value.inside_shanghai:
            for band in self._config["institutional"]["local_magnitude"]:
                if self._in_band(value.magnitude, band):
                    return str(band["level"]), ["上海行政区域震级满足制度响应条件"]
            return "none", ["上海市行政区域震级未达到制度响应条件"]

        deaths = value.deaths
        if deaths is None:
            return "none", ["外省地震缺少死亡人数，暂不建议制度响应"]
        for band in self._config["institutional"]["external_deaths"]:
            if self._in_band(Decimal(deaths), band):
                return str(band["level"]), ["外省波及上海且死亡人数满足制度响应条件"]
        return "none", ["外省波及上海但死亡人数未达到制度响应条件"]

    def _service(self, value: ResponseInput) -> int | None:
        if value.inside_shanghai is not True:
            return None
        for band in self._config["service"]["bands"]:
            if self._in_band(value.magnitude, band):
                level = int(band["level"])
                if value.depth_km > self._decimal(
                    self._config["service"]["downgrade_location_depth_gt_km"]
                ):
                    return min(4, level + 1)
                return level
        return None

    @staticmethod
    def _decimal(value: object) -> Decimal:
        return Decimal(str(value))

    @staticmethod
    def _number_text(value: object) -> str:
        return format(Decimal(str(value)).normalize(), "f")

    @staticmethod
    def _in_band(value: Decimal, band: dict) -> bool:
        if value < Decimal(str(band["min"])):
            return False
        maximum = band.get("max_exclusive")
        return maximum is None or value < Decimal(str(maximum))

    def _downgrade(self, level: str) -> str:
        order = [str(item) for item in self._config["institutional"]["downgrade"]["order"]]
        index = order.index(level)
        return order[min(index + 1, len(order) - 1)]
