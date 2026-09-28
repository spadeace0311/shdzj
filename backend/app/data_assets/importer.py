from pathlib import Path
from typing import Protocol

from app.data_assets.domain import DataAssetDefinition, NormalizedAssetData


class AssetImporter(Protocol):
    def load(
        self,
        path: Path,
        definition: DataAssetDefinition,
    ) -> NormalizedAssetData: ...


class UnsupportedImportFormat(ValueError):
    pass
