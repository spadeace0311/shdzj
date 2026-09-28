from pathlib import Path

import pytest

from app.data_assets.required_registry import load_required_asset_registry


def test_required_registry_distinguishes_required_and_optional(tmp_path: Path) -> None:
    path = tmp_path / "assets.yaml"
    path.write_text(
        "region_id: test\nrequired: [a]\noptional: [b]\n",
        encoding="utf-8",
    )
    registry = load_required_asset_registry(path)
    assert registry.region_id == "test"
    assert registry.required == ("a",)
    assert registry.optional == ("b",)


def test_required_registry_rejects_overlap(tmp_path: Path) -> None:
    path = tmp_path / "assets.yaml"
    path.write_text(
        "region_id: test\nrequired: [a]\noptional: [a]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must not overlap"):
        load_required_asset_registry(path)
