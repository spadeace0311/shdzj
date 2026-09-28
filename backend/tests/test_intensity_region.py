from pathlib import Path

from app.intensity.region import load_region_profile, region_profile_checksum


REGION_PROFILE = Path("/config/intensity/shanghai-region.yaml")


def test_loads_versioned_shanghai_region_profile() -> None:
    profile = load_region_profile(REGION_PROFILE)

    assert profile.version == "shanghai-region-v1"
    assert profile.region_id == "shanghai"
    assert profile.administrative_level == "province"
    assert profile.grid_buffer_km == 100
    assert profile.grid_resolution_m == 1000
    assert profile.grid_crs == "EPSG:32651"
    assert profile.model_parameter_version == "shanghai-2019.1"
    assert profile.fusion_strategy_version == "fusion-inverse-variance-v1"
    assert profile.instrument_provider == "unavailable"
    assert profile.checksum == region_profile_checksum(REGION_PROFILE)
