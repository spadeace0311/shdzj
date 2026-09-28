import json
from pathlib import Path

import yaml

from app.data_assets.domain import (
    DataAssetDefinition,
    NormalizedRecord,
    NormalizedTableData,
)


class ParameterFileImporter:
    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedTableData:
        if path.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        elif path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            raise ValueError("parameter file must be YAML or JSON")
        if not isinstance(payload, dict):
            raise ValueError("parameter file must contain one mapping")
        version = payload.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("parameter file requires a non-empty version")
        properties = {**payload, "parameter_set_id": version}
        record = NormalizedRecord(
            row_number=1,
            business_key=version,
            properties=properties,
        )
        return NormalizedTableData(
            columns=tuple(sorted(properties)),
            records=(record,),
            source_crs=definition.contract.source_crs,
            spatial_extent=None,
        )
