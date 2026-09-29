from app.loss.domain import (
    BuildingStructure,
    DamageState,
    LossCalibrationStatus,
    LossMetricValueStatus,
    LossModelType,
    LossProductStatus,
    LossProductType,
    LossQualityGrade,
    LossValueType,
    ResourceKind,
)
from app.loss.models_registry import canonical_intensity_bin


def test_loss_enums_have_stable_external_values() -> None:
    assert LossModelType.BUILDING_DAMAGE == "building_damage"
    assert LossProductType.VALIDATION == "validation"
    assert LossProductType.BUILDING_DAMAGE == "building_damage"
    assert LossProductStatus.COMPLETE == "complete"
    assert LossProductStatus.UNAVAILABLE == "unavailable"
    assert LossQualityGrade.L2 == "L2"
    assert LossCalibrationStatus.REFERENCE_UNCALIBRATED == "reference_uncalibrated"
    assert LossValueType.CENTRAL == "central"
    assert LossMetricValueStatus.UNAVAILABLE == "unavailable"
    assert LossMetricValueStatus.ROUNDED_TO_ZERO == "rounded_to_zero"
    assert DamageState.SEVERELY_DAMAGED == "severely_damaged"
    assert BuildingStructure.RC_FRAME == "rc_frame"
    assert ResourceKind.SICKBED == "sickbed"


def test_intensity_bin_has_one_canonical_key_format() -> None:
    assert canonical_intensity_bin(7) == "7"
    assert canonical_intensity_bin("7") == "7"
    assert canonical_intensity_bin("VII") == "7"
    assert canonical_intensity_bin("vii") == "7"
