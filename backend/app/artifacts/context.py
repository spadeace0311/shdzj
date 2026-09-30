from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.domain import ArtifactCatalog, DependencyKind
from app.artifacts.models import (
    ArtifactTemplate,
    ArtifactTemplateVersion,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.repository import ArtifactProductionRepository
from app.artifacts.service import sha256_json
from app.assessment.models import AssessmentRun
from app.config import settings
from app.data_assets.locks import lock_data_asset_catalog
from app.data_assets.models import DataAssetSnapshot
from app.data_assets.repository import DataAssetRepository
from app.events.models import EarthquakeEvent, EarthquakeRevision
from app.loss.region import load_region_loss_profile

_RENDERER_VERSIONS = {
    "maplibre": "5.6.0",
    "playwright": "1.49.1",
    "python-docx": "1.1.2",
    "python-pptx": "1.0.2",
}
_NAMING_VERSION = "artifact-name-v1"
_BASEMAP_KEYS = ("basemap.gaode.offline", "basemap.tianditu.offline")


@dataclass(frozen=True, slots=True)
class FrozenAssetVersion:
    asset_key: str
    role: str
    resolution_status: str
    asset_version_id: uuid.UUID | None
    checksum: str | None
    coverage: Mapping[str, Any]
    version: str | None = None


@dataclass(frozen=True, slots=True)
class StaticProductionContext:
    production_run_id: uuid.UUID
    region_id: str
    catalog_version: str
    context_fingerprint: str
    manifest: Mapping[str, Any]
    items: Mapping[str, FrozenAssetVersion]

    def item(self, asset_key: str) -> FrozenAssetVersion:
        try:
            return self.items[asset_key]
        except KeyError as error:
            raise KeyError(f"unknown asset key: {asset_key}") from error


@dataclass(frozen=True, slots=True)
class ArtifactTaskContext:
    production_task_id: uuid.UUID
    production_run_id: uuid.UUID
    artifact_key: str
    output_profile: str
    context_fingerprint: str
    manifest: Mapping[str, Any]
    template_versions: Mapping[str, Mapping[str, Any]]
    asset_versions: Mapping[str, FrozenAssetVersion]


@dataclass(frozen=True, slots=True)
class MapRenderContext:
    production_task_id: uuid.UUID
    production_run_id: uuid.UUID
    artifact_key: str
    output_profile: str
    context_fingerprint: str
    basemap_manifest: Mapping[str, Any]
    asset_versions: Mapping[str, FrozenAssetVersion]


@dataclass(frozen=True, slots=True)
class DocumentRenderContext:
    production_task_id: uuid.UUID
    production_run_id: uuid.UUID
    artifact_key: str
    output_profile: str
    document_type: str
    context_fingerprint: str
    template_versions: Mapping[str, Mapping[str, Any]]
    asset_versions: Mapping[str, FrozenAssetVersion]


class ProductionContextService:
    def __init__(
        self,
        *,
        repository: ArtifactProductionRepository | None = None,
        data_asset_repository: DataAssetRepository | None = None,
        artifact_asset_catalog_path: str | None = None,
    ) -> None:
        self._repository = repository or ArtifactProductionRepository()
        self._data_asset_repository = data_asset_repository or DataAssetRepository()
        self._artifact_asset_catalog_path = (
            artifact_asset_catalog_path or settings.artifact_asset_catalog_path
        )

    async def freeze_static_context(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
        catalog: ArtifactCatalog,
    ) -> StaticProductionContext:
        await lock_data_asset_catalog(session)
        run = await session.get(
            ProductionRun,
            production_run_id,
            with_for_update=True,
        )
        if run is None:
            raise LookupError("artifact production run not found")
        event = await session.get(EarthquakeEvent, run.event_id)
        revision = await session.get(EarthquakeRevision, run.revision_id)
        if event is None or revision is None:
            raise LookupError("artifact production inputs were not found")

        assessment_run: AssessmentRun | None = None
        if run.assessment_run_id is not None:
            assessment_run = await session.get(
                AssessmentRun,
                run.assessment_run_id,
            )

        asset_versions = await self._freeze_asset_versions(
            session,
            catalog,
        )
        template_versions = await self._published_template_versions(session)
        sorted_template_versions = sorted(
            template_versions,
            key=lambda item: item["template_key"],
        )
        sorted_asset_versions = sorted(
            asset_versions["manifest"],
            key=lambda item: (item["asset_key"], item["role"]),
        )
        basemap_manifest = _basemap_manifest(sorted_asset_versions)

        deadline_basis_at = (
            assessment_run.deadline_basis_at
            if assessment_run is not None
            else run.deadline_basis_at
        )
        data_asset_snapshot_fingerprint = (
            await self._data_asset_snapshot_fingerprint(
                session,
                assessment_run.id,
            )
            if assessment_run is not None
            else None
        )
        loss_parameter_item = asset_versions["items"].get(
            "shanghai.loss.parameters"
        )
        loss_profile = load_region_loss_profile(
            settings.loss_region_profile_path
        )
        manifest = {
            "catalog_version": catalog.catalog_version,
            "event": {
                "event_id": str(event.id),
                "revision_id": str(revision.id),
                "revision_no": revision.revision_no,
                "event_kind": revision.revision_kind,
                "t1_at": _isoformat(event.t1_at),
                "deadline_basis_at": deadline_basis_at.isoformat(),
            },
            "t1_at": _isoformat(event.t1_at),
            "assessment": {
                "assessment_run_id": (
                    str(assessment_run.id)
                    if assessment_run is not None
                    else None
                ),
                "algorithm_bundle_version": (
                    assessment_run.algorithm_bundle_version
                    if assessment_run is not None
                    else None
                ),
                "allowed_assessment_product_types": (
                    _assessment_product_types(catalog)
                ),
                "data_asset_snapshot_fingerprint": (
                    data_asset_snapshot_fingerprint
                ),
            },
            "loss": {
                "region_profile_version": loss_profile.version,
                "model_versions": dict(loss_profile.default_model_versions),
                "parameter_package": _loss_parameter_manifest(
                    loss_parameter_item
                ),
            },
            "assets": sorted_asset_versions,
            "templates": sorted_template_versions,
            "basemaps": basemap_manifest,
            "fonts": _font_bundle_manifest(),
            "renderer_versions": dict(_RENDERER_VERSIONS),
            "naming_version": _NAMING_VERSION,
        }
        context_fingerprint = sha256_json(manifest)

        persisted_items = [
            {
                "asset_key": item.asset_key,
                "asset_version_id": item.asset_version_id,
                "checksum": item.checksum,
                "role": item.role,
                "coverage": dict(item.coverage),
                "selected_for_render": False,
            }
            for item in asset_versions["items"].values()
        ]
        await self._repository.create_input_snapshot(
            session,
            run.id,
            context_fingerprint=context_fingerprint,
            region_id=settings.data_asset_region_id,
            manifest=manifest,
            items=persisted_items,
        )

        return StaticProductionContext(
            production_run_id=run.id,
            region_id=settings.data_asset_region_id,
            catalog_version=catalog.catalog_version,
            context_fingerprint=context_fingerprint,
            manifest=manifest,
            items=asset_versions["items"],
        )

    async def build_task_context(
        self,
        session: AsyncSession,
        production_task_id: uuid.UUID,
    ) -> ArtifactTaskContext:
        task, run, snapshot = await self._load_task_snapshot(
            session,
            production_task_id,
        )
        manifest = dict(snapshot.manifest)
        template_versions = _template_map(manifest.get("templates", ()))
        asset_versions = _frozen_asset_map(manifest.get("assets", ()))
        return ArtifactTaskContext(
            production_task_id=task.id,
            production_run_id=run.id,
            artifact_key=task.artifact_key,
            output_profile=task.output_profile,
            context_fingerprint=snapshot.context_fingerprint,
            manifest=manifest,
            template_versions=template_versions,
            asset_versions=asset_versions,
        )

    async def build_map_context(
        self,
        session: AsyncSession,
        production_task_id: uuid.UUID,
    ) -> MapRenderContext:
        task, run, snapshot = await self._load_task_snapshot(
            session,
            production_task_id,
        )
        manifest = dict(snapshot.manifest)
        return MapRenderContext(
            production_task_id=task.id,
            production_run_id=run.id,
            artifact_key=task.artifact_key,
            output_profile=task.output_profile,
            context_fingerprint=snapshot.context_fingerprint,
            basemap_manifest=dict(manifest.get("basemaps", ())),
            asset_versions=_frozen_asset_map(manifest.get("assets", ())),
        )

    async def build_document_context(
        self,
        session: AsyncSession,
        production_task_id: uuid.UUID,
        document_type: str,
    ) -> DocumentRenderContext:
        task, run, snapshot = await self._load_task_snapshot(
            session,
            production_task_id,
        )
        manifest = dict(snapshot.manifest)
        return DocumentRenderContext(
            production_task_id=task.id,
            production_run_id=run.id,
            artifact_key=task.artifact_key,
            output_profile=task.output_profile,
            document_type=document_type,
            context_fingerprint=snapshot.context_fingerprint,
            template_versions=_template_map(manifest.get("templates", ())),
            asset_versions=_frozen_asset_map(manifest.get("assets", ())),
        )

    async def _load_task_snapshot(
        self,
        session: AsyncSession,
        production_task_id: uuid.UUID,
    ) -> tuple[ProductionTask, ProductionRun, ProductionInputSnapshot]:
        task = await session.get(ProductionTask, production_task_id)
        if task is None:
            raise LookupError("artifact production task not found")
        run = await session.get(ProductionRun, task.production_run_id)
        if run is None:
            raise LookupError("artifact production run not found")
        snapshot = await session.scalar(
            select(ProductionInputSnapshot).where(
                ProductionInputSnapshot.production_run_id == run.id
            )
        )
        if snapshot is None:
            raise ValueError("production input context must be frozen before task preparation")
        return task, run, snapshot

    async def _freeze_asset_versions(
        self,
        session: AsyncSession,
        catalog: ArtifactCatalog,
    ) -> dict[str, Any]:
        required: set[str] = set()
        optional: set[str] = set()
        for definition in catalog.definitions:
            required.update(definition.required_assets)
            optional.update(definition.optional_assets)
        optional.difference_update(required)
        optional.update(_BASEMAP_KEYS)
        optional.difference_update(required)

        items: dict[str, FrozenAssetVersion] = {}
        manifest_entries: list[dict[str, Any]] = []
        for asset_key in sorted(required | optional):
            role = "required" if asset_key in required else "optional"
            version = await self._data_asset_repository.get_published_version(
                session,
                asset_key=asset_key,
                region_id=settings.data_asset_region_id,
            )
            coverage = _coverage(version)
            item = FrozenAssetVersion(
                asset_key=asset_key,
                role=role,
                resolution_status="bound" if version is not None else "missing",
                asset_version_id=version.id if version is not None else None,
                checksum=version.checksum if version is not None else None,
                coverage=coverage,
                version=version.version if version is not None else None,
            )
            items[asset_key] = item
            manifest_entries.append(
                {
                    "asset_key": asset_key,
                    "role": role,
                    "resolution_status": item.resolution_status,
                    "asset_version_id": (
                        str(version.id) if version is not None else None
                    ),
                    "version": version.version if version is not None else None,
                    "checksum": item.checksum,
                    "coverage": coverage,
                }
            )
        return {"items": items, "manifest": manifest_entries}

    async def _published_template_versions(
        self,
        session: AsyncSession,
    ) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(ArtifactTemplate, ArtifactTemplateVersion)
                .join(
                    ArtifactTemplateVersion,
                    ArtifactTemplateVersion.template_id == ArtifactTemplate.id,
                )
                .where(ArtifactTemplateVersion.status == "published")
                .order_by(
                    ArtifactTemplate.template_key,
                    ArtifactTemplateVersion.published_at.desc().nullslast(),
                    ArtifactTemplateVersion.created_at.desc(),
                    ArtifactTemplateVersion.id.desc(),
                )
            )
        ).all()
        latest: dict[str, dict[str, Any]] = {}
        for template, version in rows:
            if template.template_key not in latest:
                latest[template.template_key] = {
                    "template_key": template.template_key,
                    "kind": template.kind,
                    "display_name": template.display_name,
                    "version": version.version,
                    "checksum": version.checksum,
                    "published_at": _isoformat(version.published_at),
                }
        return list(latest.values())

    async def _data_asset_snapshot_fingerprint(
        self,
        session: AsyncSession,
        assessment_run_id: uuid.UUID,
    ) -> str | None:
        run = await session.get(AssessmentRun, assessment_run_id)
        if run is None:
            return None
        if run.data_asset_snapshot_fingerprint is not None:
            return run.data_asset_snapshot_fingerprint
        snapshots = (
            await session.scalars(
                select(DataAssetSnapshot)
                .where(DataAssetSnapshot.run_id == assessment_run_id)
                .order_by(
                    DataAssetSnapshot.asset_key,
                    DataAssetSnapshot.role,
                )
            )
        ).all()
        if not snapshots:
            return None
        payload = [
            {
                "asset_key": snapshot.asset_key,
                "version": snapshot.version,
                "checksum": snapshot.checksum,
                "role": snapshot.role,
            }
            for snapshot in snapshots
        ]
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _assessment_product_types(catalog: ArtifactCatalog) -> list[str]:
    product_types = {
        dependency.key
        for definition in catalog.definitions
        for dependency in (
            *definition.depends_on,
            *definition.optional_depends_on,
        )
        if dependency.kind is DependencyKind.ASSESSMENT_PRODUCT
    }
    return sorted(product_types)


def _loss_parameter_manifest(
    item: FrozenAssetVersion | None,
) -> dict[str, Any]:
    if item is None:
        return {
            "asset_key": "shanghai.loss.parameters",
            "resolution_status": "missing",
            "version": None,
            "checksum": None,
        }
    return {
        "asset_key": item.asset_key,
        "resolution_status": item.resolution_status,
        "version": item.version,
        "checksum": item.checksum,
    }


def _font_bundle_manifest() -> dict[str, str]:
    path = Path(settings.artifact_font_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"artifact font file does not exist: {path}"
        )
    return {
        "family": "Noto Sans CJK SC",
        "version": _font_version(path),
        "checksum": _sha256_file(path),
    }


def _font_version(path: Path) -> str:
    try:
        from fontTools.ttLib import TTCollection

        collection = TTCollection(str(path), lazy=True)
        version = collection.fonts[0]["name"].getDebugName(5)
        if version:
            return version
    except Exception:
        pass
    return path.name


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _coverage(version: object | None) -> dict[str, Any]:
    if version is None:
        return {}
    summary = getattr(version, "schema_summary", None) or {}
    statistics = summary.get("validation_statistics")
    if isinstance(statistics, Mapping):
        return dict(statistics)
    return {}


def _basemap_manifest(asset_entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_key = {item["asset_key"]: item for item in asset_entries}
    return {
        "gaode": dict(by_key.get("basemap.gaode.offline", {})),
        "tianditu": dict(by_key.get("basemap.tianditu.offline", {})),
    }


def _template_map(
    payload: object,
) -> dict[str, Mapping[str, Any]]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        template_key = item.get("template_key")
        if isinstance(template_key, str):
            result[template_key] = item
    return result


def _frozen_asset_map(
    payload: object,
) -> dict[str, FrozenAssetVersion]:
    if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
        return {}
    result: dict[str, FrozenAssetVersion] = {}
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        asset_key = item.get("asset_key")
        if not isinstance(asset_key, str):
            continue
        result[asset_key] = FrozenAssetVersion(
            asset_key=asset_key,
            role=str(item.get("role") or "optional"),
            resolution_status=str(item.get("resolution_status") or "missing"),
            asset_version_id=_optional_uuid(item.get("asset_version_id")),
            checksum=_optional_string(item.get("checksum")),
            coverage=(
                dict(item["coverage"])
                if isinstance(item.get("coverage"), Mapping)
                else {}
            ),
            version=_optional_string(item.get("version")),
        )
    return result


def _optional_uuid(value: object) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
