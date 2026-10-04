from __future__ import annotations

import collections.abc as cabc
import hashlib
import json
from dataclasses import asdict, fields, is_dataclass
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints

from app.loss.buildings import BuildingDamageResult
from app.loss.casualties import CasualtyResult
from app.loss.domain import LossProductResult, LossProductType
from app.loss.economic import EconomicLossResult
from app.loss.population import PopulationImpactResult
from app.loss.resources import ResourceDemandResult


_VALIDATION_RESULT_TYPES = {
    LossProductType.BUILDING_DAMAGE: BuildingDamageResult,
    LossProductType.POPULATION_IMPACT: PopulationImpactResult,
    LossProductType.CASUALTIES: CasualtyResult,
    LossProductType.ECONOMIC_LOSS: EconomicLossResult,
    LossProductType.RESOURCE_DEMAND: ResourceDemandResult,
}


class LossArtifactCodec:
    CHECKSUM_NAMESPACE = "loss-product-result-v1"

    @classmethod
    def checksum(cls, product: LossProductResult) -> str:
        payload = json.dumps(
            {
                "namespace": cls.CHECKSUM_NAMESPACE,
                "product": asdict(product),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def encode_validation_payload(
        product_type: LossProductType,
        result: object,
    ) -> dict[str, object]:
        if product_type not in _VALIDATION_RESULT_TYPES:
            raise ValueError(f"unsupported validation product type {product_type.value}")
        expected_type = _VALIDATION_RESULT_TYPES[product_type]
        if not isinstance(result, expected_type):
            raise TypeError(
                f"expected {expected_type.__name__} for {product_type.value}"
            )
        encoded = _encode_value(result)
        if not isinstance(encoded, dict):
            raise TypeError("validation payload must encode to a mapping")
        return encoded

    @staticmethod
    def decode_validation_payload(
        product_type: LossProductType,
        payload: object,
    ) -> object:
        result_type = _VALIDATION_RESULT_TYPES.get(product_type)
        if result_type is None:
            raise ValueError(f"unsupported validation product type {product_type.value}")
        return _decode_value(payload, result_type, product_type.value)


def _encode_value(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _encode_dataclass(value)
    if isinstance(value, cabc.Mapping):
        return {
            _encode_mapping_key(key): _encode_value(item)
            for key, item in sorted(
                value.items(),
                key=lambda item: _encode_mapping_key(item[0]),
            )
        }
    if isinstance(value, (list, tuple)):
        return [_encode_value(item) for item in value]
    raise TypeError(
        f"validation payload contains unsupported value of type "
        f"{type(value).__name__}"
    )


def _encode_dataclass(value: object) -> dict[str, object]:
    return {
        field.name: _encode_value(getattr(value, field.name))
        for field in fields(value)
    }


def _encode_mapping_key(key: object) -> str:
    if isinstance(key, Enum):
        return str(key.value)
    return str(key)


def _decode_value(value: object, annotation: Any, path: str) -> object:
    if _is_optional(annotation):
        if value is None:
            return None
        inner_type = next(
            item for item in get_args(annotation) if item is not type(None)
        )
        return _decode_value(value, inner_type, path)

    if is_dataclass(annotation):
        return _decode_dataclass(value, annotation, path)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string enum value")
        try:
            return annotation(value)
        except ValueError as exc:
            raise ValueError(f"{path} has unknown enum value {value!r}") from exc

    origin = get_origin(annotation)
    if origin in {
        dict,
        cabc.Mapping,
        cabc.MutableMapping,
    }:
        if not isinstance(value, cabc.Mapping):
            raise ValueError(f"{path} must be a mapping")
        args = get_args(annotation)
        key_type = args[0] if len(args) == 2 else str
        value_type = args[1] if len(args) == 2 else object
        return {
            _decode_value(key, key_type, f"{path}.<key>"): _decode_value(
                item,
                value_type,
                f"{path}.{key}",
            )
            for key, item in value.items()
        }
    if origin in {list, cabc.Sequence, cabc.MutableSequence}:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{path} must be a list")
        item_type = get_args(annotation)[0] if get_args(annotation) else object
        return [
            _decode_value(item, item_type, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{path} must be a tuple")
        args = get_args(annotation)
        if len(args) == 2 and args[1] is Ellipsis:
            item_type = args[0]
            return tuple(
                _decode_value(item, item_type, f"{path}[{index}]")
                for index, item in enumerate(value)
            )
        if len(args) != len(value):
            raise ValueError(f"{path} has the wrong tuple length")
        return tuple(
            _decode_value(item, item_type, f"{path}[{index}]")
            for index, (item, item_type) in enumerate(zip(value, args, strict=True))
        )

    if annotation in {str, int, float, bool}:
        return _decode_primitive(value, annotation, path)
    return value


def _decode_dataclass(
    value: object,
    value_type: type,
    path: str,
) -> object:
    if not isinstance(value, cabc.Mapping):
        raise ValueError(f"{path} must be a mapping")
    payload = dict(value)
    field_by_name = {field.name: field for field in fields(value_type)}
    unknown = sorted(set(payload) - set(field_by_name))
    if unknown:
        raise ValueError(f"{path} has unknown field {unknown[0]!r}")
    missing = sorted(set(field_by_name) - set(payload))
    if missing:
        raise ValueError(f"{path} is missing field {missing[0]!r}")
    hints = get_type_hints(value_type)
    return value_type(
        **{
            name: _decode_value(
                payload[name],
                hints[name],
                f"{path}.{name}",
            )
            for name in field_by_name
        }
    )


def _decode_primitive(value: object, annotation: type, path: str) -> object:
    if annotation is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")
        return value
    if annotation is str:
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        return value
    if annotation is int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} must be an integer")
        return int(value)
    if annotation is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path} must be a number")
        return float(value)
    return value


def _is_optional(annotation: Any) -> bool:
    return type(None) in get_args(annotation)
