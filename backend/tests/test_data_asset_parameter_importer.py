from pathlib import Path

from app.data_assets.parameter_importer import ParameterFileImporter
from app.data_assets.registry import get_asset_definition


def test_parameter_yaml_importer_uses_version_as_business_key(tmp_path: Path) -> None:
    source = tmp_path / "loss-parameters.yaml"
    source.write_text(
        """
version: "loss-test-only-v1"
calibration_status: "reference_uncalibrated"
test_only: true
provenance:
  owner: "synthetic equation test"
models:
  building_damage:
    model_id: "building-structure-matrix-v1"
    formula_version: "building-structure-matrix-v1"
    source_requirements: []
    scenarios:
      low: {}
      central: {}
      high: {}
""".strip(),
        encoding="utf-8",
    )

    result = ParameterFileImporter().load(
        source,
        get_asset_definition("shanghai.loss.parameters"),
    )

    assert result.record_count == 1
    assert result.records[0].business_key == "loss-test-only-v1"
    assert result.records[0].properties["parameter_set_id"] == "loss-test-only-v1"
    assert result.records[0].properties["test_only"] is True
