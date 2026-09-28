from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RequiredAssetRegistry:
    region_id: str
    required: tuple[str, ...]
    optional: tuple[str, ...]


def load_required_asset_registry(path: str | Path) -> RequiredAssetRegistry:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("required asset registry must be a mapping")
    region_id = str(payload.get("region_id", "")).strip()
    required = tuple(str(item) for item in payload.get("required", ()))
    optional = tuple(str(item) for item in payload.get("optional", ()))
    if not region_id or not required:
        raise ValueError("required asset registry needs region_id and required keys")
    if set(required) & set(optional):
        raise ValueError("required and optional asset keys must not overlap")
    return RequiredAssetRegistry(region_id, required, optional)
