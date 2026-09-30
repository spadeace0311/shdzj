from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True, slots=True)
class RequiredAssetRegistry:
    region_id: str
    required: tuple[str, ...]
    optional: tuple[str, ...]
    artifact_assets: tuple[str, ...] = ()


def load_required_asset_registry(
    path: str | Path,
    *,
    artifact_asset_catalog_path: str | Path | None = None,
) -> RequiredAssetRegistry:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("required asset registry must be a mapping")
    region_id = str(payload.get("region_id", "")).strip()
    required = _asset_key_list(payload.get("required"), "required")
    optional = _asset_key_list(payload.get("optional"), "optional")
    artifact_assets = ()
    if artifact_asset_catalog_path is not None:
        from app.data_assets.registry import registered_artifact_asset_keys

        artifact_assets = registered_artifact_asset_keys(
            artifact_asset_catalog_path
        )
    if not region_id or not required:
        raise ValueError("required asset registry needs region_id and required keys")
    overlap = set(required) & set(optional)
    if overlap:
        raise ValueError("required and optional asset keys must not overlap")
    overlap = (set(required) | set(optional)) & set(artifact_assets)
    if overlap:
        raise ValueError("artifact asset keys must not overlap required or optional assets")
    return RequiredAssetRegistry(
        region_id,
        required,
        optional,
        artifact_assets,
    )


def _asset_key_list(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} asset keys must be a list")
    return tuple(str(item) for item in value)
