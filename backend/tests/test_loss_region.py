from pathlib import Path

from app.loss.region import (
    load_region_loss_profile,
    load_region_loss_profile_from_mapping,
)


def test_loads_shanghai_loss_region_profile() -> None:
    profile = load_region_loss_profile(Path("/config/loss/shanghai-region.yaml"))
    assert profile.region_id == "shanghai"
    assert profile.area_crs == "EPSG:32651"
    assert profile.output_crs == "EPSG:4326"
    assert profile.population_field == "total"
    assert profile.affected_population_min_intensity == 6.0
    assert profile.spatial_allocation_rule == "town-uniform-v1"
    assert profile.grid_residual_review_threshold == 0.01
    assert profile.asset_keys.population_town == "shanghai.population.town"
    assert profile.asset_keys.loss_parameters == "shanghai.loss.parameters"
    frozen = load_region_loss_profile_from_mapping(profile.to_snapshot())
    assert frozen == profile
