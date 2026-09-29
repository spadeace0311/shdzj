from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

import yaml

from app.loss.domain import (
    LossCalibrationStatus,
    LossModelType,
    LossValueType,
    ModelDefinition,
    ParameterSet,
    ParameterSetModel,
    ScenarioParameters,
)


class LossParametersUnavailable(ValueError):
    pass


_ROMAN_INTENSITY = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
    "XI": 11,
    "XII": 12,
}


def canonical_intensity_bin(value: int | float | str) -> str:
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in _ROMAN_INTENSITY:
            return str(_ROMAN_INTENSITY[normalized])
        value = normalized
    number = float(value)
    if not number.is_integer():
        raise ValueError("intensity bin must be an integer")
    integer = int(number)
    if integer < 1 or integer > 12:
        raise ValueError("intensity bin must be between I and XII")
    return str(integer)


def _require_mapping(payload: object, *, label: str) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return dict(payload)


def _parse_scenario(payload: object) -> ScenarioParameters:
    scenario = _require_mapping(payload, label="scenario")
    raw_values = scenario.get("values")
    if raw_values is None:
        return ScenarioParameters(values={})
    values_mapping = _require_mapping(raw_values, label="scenario values")
    values = {
        str(key): float(value)
        for key, value in values_mapping.items()
    }
    return ScenarioParameters(values=values)


def _parse_parameter_model(payload: object) -> ParameterSetModel:
    model = _require_mapping(payload, label="parameter model")
    scenarios = _require_mapping(model["scenarios"], label="parameter scenarios")
    return ParameterSetModel(
        model_id=str(model["model_id"]),
        formula_version=str(model["formula_version"]),
        applicable_region=str(model["applicable_region"]),
        input_contract=tuple(str(item) for item in model["input_contract"]),
        output_contract=tuple(str(item) for item in model["output_contract"]),
        source_citations=tuple(str(item) for item in model["source_citations"]),
        source_requirements=tuple(
            str(item) for item in model["source_requirements"]
        ),
        scenarios={
            LossValueType(str(scenario_key)): _parse_scenario(scenario_value)
            for scenario_key, scenario_value in scenarios.items()
        },
    )


def load_parameter_set_from_mapping(
    payload: Mapping[str, object],
    *,
    checksum: str,
) -> ParameterSet:
    if len(checksum) != 64:
        raise ValueError("parameter set checksum must be a SHA-256 digest")
    parameter_set = ParameterSet(
        version=str(payload["version"]),
        calibration_status=LossCalibrationStatus(
            str(payload["calibration_status"])
        ),
        provenance=dict(payload["provenance"]),
        models={
            LossModelType(model_key): _parse_parameter_model(model_value)
            for model_key, model_value in dict(payload["models"]).items()
        },
        checksum=checksum,
        test_only=bool(payload.get("test_only", False)),
    )
    if (
        not parameter_set.test_only
        and parameter_set.calibration_status is LossCalibrationStatus.CALIBRATED
    ):
        raise ValueError("production calibrated parameters require review evidence")
    return parameter_set


def load_parameter_set(path: str | Path) -> ParameterSet:
    parameter_path = Path(path)
    raw = parameter_path.read_bytes()
    checksum = sha256(raw).hexdigest()
    payload = yaml.safe_load(raw)
    payload_mapping = _require_mapping(payload, label="parameter set")
    return load_parameter_set_from_mapping(payload_mapping, checksum=checksum)


def require_parameters(
    parameter_set: ParameterSet,
    model_type: LossModelType,
    scenario: LossValueType,
    names: tuple[str, ...],
) -> dict[str, float]:
    parameter_model = parameter_set.models[model_type]
    scenario_parameters = parameter_model.scenarios[scenario]
    missing = tuple(
        name for name in names if name not in scenario_parameters.values
    )
    if missing:
        raise LossParametersUnavailable(
            f"{model_type.value} parameters unavailable: {', '.join(missing)}"
        )
    return {
        name: float(scenario_parameters.values[name])
        for name in names
    }


class LossModelRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[LossModelType, str], ModelDefinition] = {}

    def register(self, definition: ModelDefinition) -> None:
        key = (definition.model_type, definition.formula_version)
        self._definitions[key] = definition

    def register_defaults(self, parameter_set: ParameterSet) -> None:
        for model_type, parameter_model in parameter_set.models.items():
            self.register(
                ModelDefinition(
                    model_id=parameter_model.model_id,
                    model_type=model_type,
                    formula_version=parameter_model.formula_version,
                    applicable_region=parameter_model.applicable_region,
                    input_contract=parameter_model.input_contract,
                    output_contract=parameter_model.output_contract,
                    source_citations=parameter_model.source_citations,
                    source_requirements=parameter_model.source_requirements,
                    calibration_status=parameter_set.calibration_status,
                    is_default=True,
                )
            )

    def resolve(
        self,
        model_type: LossModelType,
        formula_version: str,
    ) -> ModelDefinition:
        key = (model_type, formula_version)
        try:
            return self._definitions[key]
        except KeyError as exc:
            raise KeyError(
                f"unknown loss model {model_type.value}/{formula_version}"
            ) from exc
