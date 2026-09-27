from app.intensity.domain import (
    DirectionStatus,
    FusionMode,
    FusionQuality,
    InstrumentQuality,
    ProductStatus,
    ProductType,
)


def test_intensity_enums_have_stable_external_values() -> None:
    assert ProductType.MODEL == "model"
    assert ProductType.INSTRUMENT == "instrument"
    assert ProductType.FUSION == "fusion"
    assert ProductStatus.UNAVAILABLE == "unavailable"
    assert InstrumentQuality.Q1 == "Q1"
    assert FusionMode.MODEL_ONLY == "model_only"
    assert FusionQuality.F3 == "F3"
    assert DirectionStatus.UNCERTAIN == "uncertain"
