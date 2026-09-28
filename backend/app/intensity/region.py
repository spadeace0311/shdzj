from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RegionProfile:
    version: str
    region_id: str
    name: str
    administrative_level: str
    boundary_version: str | None
    grid_buffer_km: int
    grid_resolution_m: int
    grid_crs: str
    model_parameter_version: str
    fusion_strategy_version: str
    instrument_provider: str
    fault_data_version: str | None
    checksum: str

    def __post_init__(self) -> None:
        required = (
            self.version,
            self.region_id,
            self.name,
            self.administrative_level,
            self.grid_crs,
            self.model_parameter_version,
            self.fusion_strategy_version,
            self.instrument_provider,
        )
        if any(not value.strip() for value in required):
            raise ValueError("region profile text fields must not be empty")
        if self.grid_buffer_km < 0:
            raise ValueError("grid_buffer_km must not be negative")
        if self.grid_resolution_m <= 0:
            raise ValueError("grid_resolution_m must be positive")
        if len(self.checksum) != 64:
            raise ValueError("checksum must be a SHA-256 hex digest")


def load_region_profile(path: str | Path) -> RegionProfile:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return RegionProfile(
        version=str(payload["version"]),
        region_id=str(payload["region_id"]),
        name=str(payload["name"]),
        administrative_level=str(payload["administrative_level"]),
        boundary_version=(
            str(payload["boundary_version"])
            if payload.get("boundary_version") is not None
            else None
        ),
        grid_buffer_km=int(payload["grid_buffer_km"]),
        grid_resolution_m=int(payload["grid_resolution_m"]),
        grid_crs=str(payload["grid_crs"]),
        model_parameter_version=str(payload["model_parameter_version"]),
        fusion_strategy_version=str(payload["fusion_strategy_version"]),
        instrument_provider=str(payload["instrument_provider"]),
        fault_data_version=(
            str(payload["fault_data_version"])
            if payload.get("fault_data_version") is not None
            else None
        ),
        checksum=region_profile_checksum(path),
    )


def region_profile_checksum(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
