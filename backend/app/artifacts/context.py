from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.basemap import (
    AllBasemapsUnavailableError,
    BasemapValidationResult,
    MapViewportTileManifest,
    OfflineBasemapPackage,
    OfflineBasemapValidator,
    SelectedBasemap,
    VIEWPORT_RADII_KM,
    load_offline_basemap_candidates,
)
from app.artifacts.domain import ArtifactCatalog, ArtifactKind, DependencyKind
from app.artifacts.models import (
    ArtifactTaskDependencyBinding,
    ArtifactTemplate,
    ArtifactTemplateVersion,
    GeneratedArtifact,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionTask,
)
from app.artifacts.repository import ArtifactProductionRepository
from app.artifacts.service import sha256_json
from app.artifacts.storage import ArtifactStore
from app.assessment.models import AssessmentRun
from app.config import settings
from app.data_assets.locks import lock_data_asset_catalog
from app.data_assets.models import DataAssetSnapshot
from app.data_assets.repository import DataAssetRepository
from app.data_assets.registry import get_asset_definition
from app.data_assets.required_registry import (
    load_production_required_asset_registry,
)
from app.events.models import EarthquakeEvent, EarthquakeRevision
from app.intensity.models import IntensityFieldProduct
from app.loss.models import LossProduct
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
class MapRenderEvent:
    id: uuid.UUID
    revision_id: uuid.UUID
    place: str
    magnitude: float
    origin_time: datetime | None
    longitude: float
    latitude: float
    depth_km: float


@dataclass(frozen=True, slots=True)
class MapRenderRevision:
    id: uuid.UUID


@dataclass(frozen=True, slots=True)
class StaticProductionContext:
    production_run_id: uuid.UUID
    region_id: str
    catalog_version: str
    context_fingerprint: str
    manifest: Mapping[str, Any]
    items: Mapping[str, FrozenAssetVersion]
    selected_basemap: SelectedBasemap | None

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
    selected_basemap: SelectedBasemap | None
    event: MapRenderEvent | None = None
    revision: MapRenderRevision | None = None
    display_name: str = ""
    layers: tuple[Any, ...] = ()
    legend: tuple[Any, ...] = ()
    source_notes: tuple[str, ...] = ()
    quality: Mapping[str, Any] | None = None
    marker: str | None = None
    output_format: str = "jpg"
    production_mode: str = "live"
    resolved_sources: Mapping[str, Any] = field(default_factory=dict)


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
    manifest: Mapping[str, Any] = field(default_factory=dict)
    production_mode: str = "live"
    marker: str | None = None
    artifact_paths: Mapping[str, Path] = field(default_factory=dict)

    @property
    def artifacts(self) -> Mapping[str, FrozenAssetVersion]:
        return self.asset_versions

    @property
    def pptx_slide_count(self) -> int:
        return 8


def _decision_report_products_from_artifact(
    artifact: Any,
    control_fields: object,
) -> dict[str, dict[str, Any]]:
    if not isinstance(control_fields, Mapping):
        return {}
    fields = dict(control_fields)
    source_versions = str(fields.get("source_versions") or "")
    products: dict[str, dict[str, Any]] = {}
    for product_key, field_key in (
        ("intensity.fusion", "intensity"),
        ("loss.buildings", "buildings"),
        ("loss.population", "population"),
        ("loss.casualties", "casualties"),
        ("loss.economic", "economy"),
        ("loss.resources", "resources"),
        ("loss.validate", "resources"),
    ):
        summary = fields.get(field_key)
        if summary is None:
            continue
        payload: dict[str, Any] = {
            "version": _source_version(source_versions, product_key)
            or str(artifact.artifact_version),
            "checksum": artifact.checksum,
            "summary": summary,
        }
        if product_key == "intensity.fusion":
            payload["grade"] = summary
        elif product_key == "loss.buildings":
            payload.update({"total": summary, "severe": summary})
        elif product_key == "loss.population":
            payload.update({"affected": summary, "resident": summary})
        elif product_key == "loss.casualties":
            payload.update({"deaths": summary, "injuries": summary})
        elif product_key == "loss.economic":
            payload.update({"loss": summary, "gdp": summary})
        elif product_key == "loss.resources":
            payload["demand"] = summary
        elif product_key == "loss.validate":
            payload["grade"] = summary
        products[product_key] = payload
    return products


def _source_version(source_versions: str, product_key: str) -> str | None:
    prefix = f"{product_key}: 版本 "
    for line in source_versions.splitlines():
        if not line.startswith(prefix):
            continue
        value = line[len(prefix):].split(" /", 1)[0].strip()
        if value:
            return value
    return None


def _merge_decision_background_fields(
    manifest: dict[str, Any],
    control_fields: object,
) -> dict[str, Any]:
    if not isinstance(control_fields, Mapping):
        return manifest
    fields = dict(control_fields)
    targets = dict(manifest.get("targets") or {})
    if fields.get("key_targets") is not None:
        targets["key_target"] = fields["key_targets"]
        manifest["targets"] = targets
    faults = dict(manifest.get("faults") or {})
    if fields.get("faults") is not None:
        faults["summary"] = fields["faults"]
        manifest["faults"] = faults
    spatial = dict(manifest.get("spatial_distances") or {})
    for field_key, spatial_key in (
        ("city_distance", "city_distance"),
        ("fault_distance", "fault_distance"),
    ):
        if fields.get(field_key) is not None:
            spatial[spatial_key] = fields[field_key]
    if spatial:
        manifest["spatial_distances"] = spatial
    return manifest


class ProductionContextService:
    def __init__(
        self,
        *,
        repository: ArtifactProductionRepository | None = None,
        data_asset_repository: DataAssetRepository | None = None,
        artifact_asset_catalog_path: str | None = None,
        offline_basemap_packages: Mapping[str, OfflineBasemapPackage] | None = None,
    ) -> None:
        self._repository = repository or ArtifactProductionRepository()
        self._data_asset_repository = data_asset_repository or DataAssetRepository()
        self._artifact_asset_catalog_path = (
            artifact_asset_catalog_path or settings.artifact_asset_catalog_path
        )
        self._offline_basemap_packages = offline_basemap_packages

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
        basemap_viewport_manifests = tuple(
            MapViewportTileManifest.build(
                center_lon=float(event.longitude),
                center_lat=float(event.latitude),
                radius_km=radius_km,
                output_width=settings.artifact_basemap_output_width,
                output_height=settings.artifact_basemap_output_height,
                zoom_levels=settings.artifact_basemap_zoom_levels,
                padding=settings.artifact_basemap_buffer_pixels,
            )
            for radius_km in VIEWPORT_RADII_KM
        )
        basemap_tile_manifests = _basemap_tile_manifest_entries(
            basemap_viewport_manifests,
        )

        deadline_basis_at = (
            assessment_run.deadline_basis_at
            if assessment_run is not None
            else run.deadline_basis_at
        )
        requires_basemap = _run_requires_offline_basemap(
            catalog,
            run.required_outputs,
        )
        offline_packages = self._offline_basemap_packages
        offline_failures: dict[str, BasemapValidationResult] = {}
        if offline_packages is None:
            if requires_basemap:
                offline_packages, offline_failures = load_offline_basemap_candidates(
                    settings.artifact_basemap_root
                )
            else:
                offline_packages = {}
        selected_basemap_payload = self._select_basemap(
            basemap_viewport_manifests,
            observed_at=deadline_basis_at,
            packages=offline_packages,
            failures=offline_failures,
            required=requires_basemap,
        )
        selected_basemap = _selected_basemap(selected_basemap_payload)
        basemap_manifest = _basemap_manifest(
            sorted_asset_versions,
            basemap_tile_manifests,
            selected_basemap_payload,
            offline_packages,
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
                "longitude": float(event.longitude),
                "latitude": float(event.latitude),
                "place": event.place,
                "magnitude": float(event.magnitude),
                "origin_time": _isoformat(event.origin_time),
                "depth_km": float(event.depth_km),
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
            selected_basemap=selected_basemap,
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
        basemap_manifest = dict(manifest.get("basemaps", ()))
        event_payload = manifest.get("event")
        if not isinstance(event_payload, Mapping):
            raise ValueError("production input snapshot must contain a frozen event")
        event_id = uuid.UUID(str(event_payload["event_id"]))
        revision_id = uuid.UUID(str(event_payload["revision_id"]))
        origin_time = _optional_datetime(event_payload.get("origin_time"))
        catalog = ArtifactCatalog.load(settings.artifact_catalog_path)
        definition = catalog.get(task.artifact_key, task.output_profile)
        event = MapRenderEvent(
            id=event_id,
            revision_id=revision_id,
            place=str(event_payload.get("place") or ""),
            magnitude=float(event_payload["magnitude"]),
            origin_time=origin_time,
            longitude=float(event_payload["longitude"]),
            latitude=float(event_payload["latitude"]),
            depth_km=float(event_payload["depth_km"]),
        )
        asset_versions = _frozen_asset_map(manifest.get("assets", ()))
        resolved_sources: Mapping[str, Any] = {}
        from app.artifacts.renderers.map_layers import MapLayerRegistry
        from app.artifacts.renderers.map_sources import MapSourceResolver

        if task.artifact_key in MapLayerRegistry.artifact_keys():
            resolved_sources = await MapSourceResolver(
                self._data_asset_repository
            ).resolve(
                session,
                task=task,
                run=run,
                event=event,
                asset_versions=asset_versions,
            )
        return MapRenderContext(
            production_task_id=task.id,
            production_run_id=run.id,
            artifact_key=task.artifact_key,
            output_profile=task.output_profile,
            context_fingerprint=snapshot.context_fingerprint,
            basemap_manifest=basemap_manifest,
            asset_versions=asset_versions,
            selected_basemap=_selected_basemap(
                basemap_manifest.get("selected_basemap")
            ),
            event=event,
            revision=MapRenderRevision(id=revision_id),
            display_name=definition.display_name,
            source_notes=("offline validated basemap",),
            quality={
                "grade": definition.quality_policy,
                "needs_review": False,
                "missing_assets": [],
            },
            output_format=definition.format,
            production_mode=run.production_mode,
            marker=_mode_marker(run.production_mode),
            resolved_sources=resolved_sources,
        )

    def _select_basemap(
        self,
        manifests: tuple[MapViewportTileManifest, ...],
        *,
        observed_at: datetime,
        packages: Mapping[str, OfflineBasemapPackage],
        failures: Mapping[str, BasemapValidationResult],
        required: bool,
    ) -> dict[str, Any] | None:
        gaode = packages.get("gaode")
        tianditu = packages.get("tianditu")
        gaode_result = _basemap_candidate_result(
            "gaode",
            gaode,
            failures.get("gaode"),
            manifests,
            observed_at=observed_at,
        )
        tianditu_result = _basemap_candidate_result(
            "tianditu",
            tianditu,
            failures.get("tianditu"),
            manifests,
            observed_at=observed_at,
        )
        if gaode_result.valid:
            return SelectedBasemap(
                provider=gaode.provider,
                package_id=gaode.package_id,
                version=gaode.version,
                checksum=gaode.checksum,
                selection_reason="gaode validated",
            ).to_dict()
        if tianditu_result.valid:
            return SelectedBasemap(
                provider=tianditu.provider,
                package_id=tianditu.package_id,
                version=tianditu.version,
                checksum=tianditu.checksum,
                selection_reason=(
                    f"gaode invalid: {gaode_result.error_category}; "
                    "tianditu validated"
                ),
            ).to_dict()
        if required:
            raise AllBasemapsUnavailableError(
                "no validated offline basemap is available",
                gaode_error=gaode_result.error_category,
                tianditu_error=tianditu_result.error_category,
                gaode_failure=gaode_result,
                tianditu_failure=tianditu_result,
            )
        return None

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
        manifest = await self._merge_task_document_manifest(
            session,
            task,
            run,
            manifest,
        )
        artifact_paths, generated_assets = await self._resolve_generated_artifacts(
            session,
            run.id,
        )
        manifest = await self._merge_decision_report_products(
            session,
            run.id,
            manifest,
            task.artifact_key,
        )
        asset_versions = _frozen_asset_map(manifest.get("assets", ()))
        asset_versions.update(generated_assets)
        return DocumentRenderContext(
            production_task_id=task.id,
            production_run_id=run.id,
            artifact_key=task.artifact_key,
            output_profile=task.output_profile,
            document_type=document_type,
            context_fingerprint=snapshot.context_fingerprint,
            template_versions=_template_map(manifest.get("templates", ())),
            asset_versions=asset_versions,
            manifest=manifest,
            production_mode=run.production_mode,
            marker=_mode_marker(run.production_mode),
            artifact_paths=artifact_paths,
        )

    async def _merge_decision_report_products(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
        manifest: dict[str, Any],
        artifact_key: str,
    ) -> dict[str, Any]:
        if artifact_key not in {
            "doc.decision_report",
            "deck.decision_report",
        }:
            return manifest
        rows = (
            await session.scalars(
                select(GeneratedArtifact)
                .where(
                    GeneratedArtifact.production_run_id == production_run_id,
                    GeneratedArtifact.is_final.is_(True),
                    GeneratedArtifact.artifact_key.in_(
                        ("doc.rapid_brief", "doc.rapid_report")
                    ),
                )
                .order_by(GeneratedArtifact.created_at.desc())
            )
        ).all()
        run = await session.get(ProductionRun, production_run_id)
        for artifact in rows:
            task = await session.get(
                ProductionTask,
                artifact.production_task_id,
            )
            if task is None or run is None:
                continue
            products = await self._task_assessment_products(
                session,
                task,
                run,
            )
            render_manifest = dict(artifact.render_manifest or {})
            control_fields = render_manifest.get("control_fields")
            if not products:
                continue
            assessment = dict(manifest.get("assessment") or {})
            current_products = dict(assessment.get("products") or {})
            current_products.update(products)
            assessment["products"] = current_products
            manifest["assessment"] = assessment
            manifest = _merge_decision_background_fields(
                manifest,
                control_fields,
            )
            break
        return manifest

    async def _merge_task_document_manifest(
        self,
        session: AsyncSession,
        task: ProductionTask,
        run: ProductionRun,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        products = await self._task_assessment_products(
            session,
            task,
            run,
        )
        if products:
            assessment = dict(manifest.get("assessment") or {})
            assessment["products"] = products
            manifest["assessment"] = assessment

        asset_versions = _frozen_asset_map(manifest.get("assets", ()))
        manifest.update(
            await self._build_background_payload(
                session,
                run,
                asset_versions,
            )
        )
        return manifest

    async def _task_assessment_products(
        self,
        session: AsyncSession,
        task: ProductionTask,
        run: ProductionRun,
    ) -> dict[str, dict[str, Any]]:
        bindings = (
            await session.scalars(
                select(ArtifactTaskDependencyBinding).where(
                    ArtifactTaskDependencyBinding.production_task_id == task.id
                )
            )
        ).all()
        products: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            if (
                binding.dependency_kind != "assessment_product"
                or binding.resolution_status not in {"bound", "degraded"}
                or binding.bound_entity_id is None
            ):
                continue
            if binding.dependency_key == "intensity.fusion":
                product = await session.get(
                    IntensityFieldProduct,
                    binding.bound_entity_id,
                )
                if (
                    product is None
                    or product.run_id != run.assessment_run_id
                    or product.output_checksum != binding.bound_checksum
                ):
                    continue
                products[binding.dependency_key] = {
                    "version": product.algorithm_version,
                    "checksum": product.output_checksum,
                    **dict(product.statistics or {}),
                }
                continue
            loss_product_type = {
                "loss.buildings": "building_damage",
                "loss.population": "population_impact",
                "loss.casualties": "casualties",
                "loss.economic": "economic_loss",
                "loss.resources": "resource_demand",
                "loss.validate": "validation",
            }.get(binding.dependency_key)
            if loss_product_type is None:
                continue
            product = await session.get(LossProduct, binding.bound_entity_id)
            if (
                product is None
                or product.run_id != run.assessment_run_id
                or product.product_type != loss_product_type
                or product.output_checksum != binding.bound_checksum
            ):
                continue
            products[binding.dependency_key] = {
                "version": product.algorithm_version,
                "checksum": product.output_checksum,
                **dict(product.statistics or {}),
            }
        return products

    async def _build_background_payload(
        self,
        session: AsyncSession,
        run: ProductionRun,
        asset_versions: Mapping[str, FrozenAssetVersion],
    ) -> dict[str, Any]:
        historical_records, _ = await self._frozen_records(
            session,
            asset_versions,
            "shanghai.historical.earthquakes",
        )
        historical = _background_historical_payload(historical_records)

        distance_records, _ = await self._frozen_records(
            session,
            asset_versions,
            "shanghai.distance.reference_points",
        )
        spatial = _background_spatial_payload(distance_records)

        target_definitions = (
            ("shelter", "避难场所", "shanghai.shelter.emergency", "处"),
            ("school", "学校", "shanghai.education.school", "所"),
            ("hospital", "医院", "shanghai.health.hospital", "所"),
            ("hazard_source", "危险源", "shanghai.hazard_source", "处"),
            ("rescue_team", "救援队伍", "shanghai.rescue_team", "支"),
            ("cultural_relic", "文物单位", "shanghai.cultural_relic", "处"),
            ("key_target", "重点目标", "shanghai.key_target", "个"),
        )
        targets: dict[str, str] = {}
        for key, label, asset_key, unit in target_definitions:
            records, _ = await self._frozen_records(
                session,
                asset_versions,
                asset_key,
            )
            if records:
                targets[key] = f"{label} {len(records)}{unit}"
            else:
                targets[key] = f"数据不可用，待复核：{asset_key}"

        building_records, _ = await self._frozen_records(
            session,
            asset_versions,
            "shanghai.building.town",
        )
        building_town = _background_building_payload(building_records)

        fault_records, _ = await self._frozen_records(
            session,
            asset_versions,
            "shanghai.fault",
        )
        faults = _background_fault_payload(fault_records)

        population_records, _ = await self._frozen_records(
            session,
            asset_versions,
            "shanghai.population.town",
        )
        population_town = _background_population_asset_payload(population_records)

        economy_records, _ = await self._frozen_records(
            session,
            asset_versions,
            "shanghai.economy.county",
        )
        economy_county = _background_economy_asset_payload(economy_records)

        overview_source = "shanghai.admin.city"
        admin_records, _ = await self._frozen_records(
            session,
            asset_versions,
            "shanghai.admin.city",
        )
        if not admin_records:
            overview_source = "shanghai.admin.town"
            admin_records, _ = await self._frozen_records(
                session,
                asset_versions,
                "shanghai.admin.town",
            )
        area_overview = _background_overview_payload(
            admin_records,
            targets,
            source=overview_source,
        )
        return {
            "historical_earthquakes": historical,
            "spatial_distances": spatial,
            "targets": targets,
            "area_overview": area_overview,
            "building_town": building_town,
            "faults": faults,
            "population_town": population_town,
            "economy_county": economy_county,
        }

    async def _frozen_records(
        self,
        session: AsyncSession,
        asset_versions: Mapping[str, FrozenAssetVersion],
        asset_key: str,
    ) -> tuple[list[Any], FrozenAssetVersion | None]:
        item = asset_versions.get(asset_key)
        if (
            item is None
            or item.asset_version_id is None
            or item.resolution_status != "bound"
        ):
            return [], item
        records = await self._data_asset_repository.list_records(
            session,
            item.asset_version_id,
        )
        return records, item

    async def _resolve_generated_artifacts(
        self,
        session: AsyncSession,
        production_run_id: uuid.UUID,
    ) -> tuple[dict[str, Path], dict[str, FrozenAssetVersion]]:
        rows = (
            await session.scalars(
                select(GeneratedArtifact).where(
                    GeneratedArtifact.production_run_id == production_run_id,
                    GeneratedArtifact.is_final.is_(True),
                )
            )
        ).all()
        store = ArtifactStore(settings.artifact_storage_root)
        paths: dict[str, Path] = {}
        assets: dict[str, FrozenAssetVersion] = {}
        for artifact in rows:
            resolved = store.resolve(artifact.storage_path)
            if not resolved.is_file():
                continue
            paths[artifact.artifact_key] = resolved
            assets[artifact.artifact_key] = FrozenAssetVersion(
                asset_key=artifact.artifact_key,
                role="artifact",
                resolution_status=(
                    "bound" if artifact.status == "complete" else "degraded"
                ),
                asset_version_id=artifact.id,
                checksum=artifact.checksum,
                coverage={},
                version=str(artifact.artifact_version),
            )
        return paths, assets

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
        production_registry = load_production_required_asset_registry()
        required.update(production_registry.required)
        optional.update(production_registry.optional)
        optional.update(production_registry.artifact_assets)
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


def _background_historical_payload(records: list[Any]) -> dict[str, Any]:
    source = "shanghai.historical.earthquakes"
    if not records:
        return {
            "radius_km": f"数据不可用，待复核：{source}",
            "magnitude_threshold": f"数据不可用，待复核：{source}",
            "summary": f"数据不可用，待复核：{source}",
            "disaster_summary": f"数据不可用，待复核：{source}",
            "statistics": f"数据不可用，待复核：{source}",
        }
    properties = dict(records[0].properties or {})
    return {
        "radius_km": properties.get(
            "radius_km",
            f"数据不可用，待复核：{source}.radius_km",
        ),
        "magnitude_threshold": properties.get(
            "magnitude_threshold",
            f"数据不可用，待复核：{source}.magnitude_threshold",
        ),
        "summary": properties.get("summary") or f"历史地震 {len(records)} 条",
        "disaster_summary": properties.get("disaster_summary")
        or f"灾害地震 {len(records)} 条",
        "statistics": properties.get("statistics") or f"{len(records)} 条",
    }


def _background_spatial_payload(records: list[Any]) -> dict[str, Any]:
    source = "shanghai.distance.reference_points"
    if not records:
        return {
            key: f"数据不可用，待复核：{source}"
            for key in (
                "city_distance",
                "county_distance",
                "town_distance",
                "major_city_distance",
                "key_target_distance",
                "fault_distance",
            )
        }
    properties = dict(records[0].properties or {})
    return {
        key: properties.get(key, f"数据不可用，待复核：{source}.{key}")
        for key in (
            "city_distance",
            "county_distance",
            "town_distance",
            "major_city_distance",
            "key_target_distance",
            "fault_distance",
        )
    }


def _background_overview_payload(
    admin_records: list[Any],
    targets: Mapping[str, str],
    *,
    source: str,
) -> dict[str, str]:
    if not admin_records:
        return {
            "geography": f"数据不可用，待复核：{source}",
            "administration": f"数据不可用，待复核：{source}",
            "key_risks": f"数据不可用，待复核：{source}",
        }
    properties = dict(admin_records[0].properties or {})
    key_risks = properties.get("key_risks")
    if not key_risks:
        key_risks = "；".join(
            value
            for value in targets.values()
            if not value.startswith("数据不可用")
        ) or f"数据不可用，待复核：{source}"
    return {
        "geography": properties.get("geography", f"数据不可用，待复核：{source}.geography"),
        "administration": properties.get(
            "administration",
            f"数据不可用，待复核：{source}.administration",
        ),
        "key_risks": str(key_risks),
    }


@dataclass(frozen=True, slots=True)
class _NumericFieldSummary:
    value: float | None
    valid_record_count: int = 0
    missing_record_count: int = 0
    invalid_business_keys: tuple[str, ...] = ()


def _contract_field_required(asset_key: str, field_name: str) -> bool:
    definition = get_asset_definition(asset_key)
    for contract_field in definition.contract.fields:
        if contract_field.name == field_name:
            return contract_field.required
    raise KeyError(f"unknown contract field: {asset_key}.{field_name}")


def _numeric_summary(records: list[Any], field_name: str) -> _NumericFieldSummary:
    total = 0.0
    valid_count = 0
    missing_count = 0
    invalid_keys: list[str] = []
    for record in records:
        properties = record.properties or {}
        if field_name not in properties:
            missing_count += 1
            continue
        raw_value = properties[field_name]
        if raw_value is None or raw_value == "":
            missing_count += 1
            continue
        try:
            numeric_value = float(raw_value)
        except (TypeError, ValueError):
            numeric_value = math.nan
        if not math.isfinite(numeric_value):
            invalid_keys.append(record.business_key or f"row-{record.row_number}")
            continue
        total += numeric_value
        valid_count += 1
    return _NumericFieldSummary(
        value=total if valid_count else None,
        valid_record_count=valid_count,
        missing_record_count=missing_count,
        invalid_business_keys=tuple(invalid_keys),
    )


def _sum_records(records: list[Any], key: str) -> float | None:
    return _numeric_summary(records, key).value


def _unavailable(source: str, field_name: str | None = None) -> str:
    if field_name is None:
        return f"数据不可用，待复核：{source}"
    return f"数据不可用，待复核：{source}.{field_name}"


def _quality_issues(
    source: str,
    summaries: Mapping[str, _NumericFieldSummary],
) -> dict[str, str]:
    issues: dict[str, str] = {}
    for field_name, summary in summaries.items():
        if not summary.invalid_business_keys:
            continue
        sample = "、".join(summary.invalid_business_keys[:3])
        suffix = "等" if len(summary.invalid_business_keys) > 3 else ""
        issues[field_name] = (
            f"{source}.{field_name} 有 {len(summary.invalid_business_keys)} 条无效记录"
            f"（{sample}{suffix}），已按 {summary.valid_record_count} 条有效记录聚合"
        )
    return issues


def _background_building_payload(records: list[Any]) -> dict[str, Any]:
    source = "shanghai.building.town"
    unavailable_payload = {
        "town_totals": _unavailable(source),
        "structure_type": _unavailable(source),
        "coverage_quality": _unavailable(source),
    }
    if not records:
        return unavailable_payload

    total_area = _numeric_summary(records, "TOTAL_AREA")
    structure_fields = (
        ("HIGH_RISE", "高层"),
        ("RCFRAME", "框架"),
        ("BRICK_STRUCTURE", "砖混"),
        ("SINGLE_AREA", "单层"),
        ("OTHER_STRUCTURE", "其他"),
    )
    structure_summaries = {
        field_name: _numeric_summary(records, field_name)
        for field_name, _ in structure_fields
    }
    quality_summaries = {
        "TOTAL_AREA": total_area,
        **structure_summaries,
    }

    town_totals: Any = (
        total_area.value
        if total_area.value is not None
        else _unavailable(source, "TOTAL_AREA")
    )

    structure_denominator = sum(
        summary.value or 0.0 for summary in structure_summaries.values()
    )
    if structure_denominator > 0:
        structure_type = "；".join(
            f"{label} {(summary.value or 0.0) / structure_denominator * 100:.1f}%"
            for (_, label), summary in zip(
                structure_fields,
                structure_summaries.values(),
            )
        )
    else:
        structure_type = _unavailable(source, "structure_type")

    coverage_parts: list[str] = []
    if total_area.missing_record_count:
        coverage_parts.append(
            f"{total_area.missing_record_count} 条记录缺失 TOTAL_AREA"
        )
    if total_area.invalid_business_keys:
        coverage_parts.append(
            f"{len(total_area.invalid_business_keys)} 条记录 TOTAL_AREA 无效"
        )
    for field_name, _ in structure_fields:
        summary = structure_summaries[field_name]
        if not summary.invalid_business_keys:
            continue
        coverage_parts.append(
            f"{len(summary.invalid_business_keys)} 条记录 {field_name} 无效"
        )
    high_rise_summary = structure_summaries["HIGH_RISE"]
    if (
        high_rise_summary.value is None
        and not high_rise_summary.invalid_business_keys
        and not _contract_field_required(source, "HIGH_RISE")
    ):
        coverage_parts.append("缺失可选字段 HIGH_RISE")
    coverage_quality = (
        "完整覆盖"
        if not coverage_parts
        else "部分覆盖：" + "；".join(coverage_parts)
    )

    return {
        "town_totals": town_totals,
        "structure_type": structure_type,
        "coverage_quality": coverage_quality,
        "quality": _quality_issues(source, quality_summaries),
    }


def _background_fault_payload(records: list[Any]) -> dict[str, Any]:
    source = "shanghai.fault"
    if not records:
        return {
            "summary": f"数据不可用，待复核：{source}",
            "record_count": 0,
        }
    properties = dict(records[0].properties or {})
    return {
        "summary": properties.get("summary") or f"邻近断裂 {len(records)} 条",
        "record_count": len(records),
    }


def _background_population_asset_payload(records: list[Any]) -> dict[str, Any]:
    source = "shanghai.population.town"
    if not records:
        return {
            "resident": _unavailable(source),
            "floating": _unavailable(source),
            "household": _unavailable(source),
            "age_structure": _unavailable(source),
        }
    summaries = {
        field_name: _numeric_summary(records, field_name)
        for field_name in (
            "total",
            "resident",
            "floating",
            "family",
            "under14",
            "over65",
        )
    }
    resident = summaries["resident"].value
    floating = summaries["floating"].value
    family = summaries["family"].value

    age_structure = _unavailable(source, "age_structure")
    total = summaries["total"].value
    under14 = summaries["under14"].value
    over65 = summaries["over65"].value
    if (
        total is not None
        and under14 is not None
        and over65 is not None
        and total > 0
    ):
        middle = total - under14 - over65
        if middle >= 0:
            age_structure = (
                f"0-14岁 {under14 / total * 100:.1f}%；"
                f"15-64岁 {middle / total * 100:.1f}%；"
                f"65岁及以上 {over65 / total * 100:.1f}%"
            )
    return {
        "resident": resident if resident is not None else _unavailable(source, "resident"),
        "floating": floating if floating is not None else _unavailable(source, "floating"),
        "household": family if family is not None else _unavailable(source, "family"),
        "age_structure": age_structure,
        "quality": _quality_issues(source, summaries),
    }


def _background_economy_asset_payload(records: list[Any]) -> dict[str, Any]:
    source = "shanghai.economy.county"
    if not records:
        return {
            "gdp": _unavailable(source),
            "primary": _unavailable(source),
            "secondary": _unavailable(source),
            "tertiary": _unavailable(source),
        }
    mapping = (
        ("gdp", "gdp"),
        ("primary", "agri_value"),
        ("secondary", "industry_value"),
        ("tertiary", "service_value"),
    )
    summaries = {
        field_name: _numeric_summary(records, field_name)
        for _, field_name in mapping
    }
    payload: dict[str, Any] = {}
    for output_key, field_name in mapping:
        summary = summaries[field_name]
        payload[output_key] = (
            summary.value
            if summary.value is not None
            else _unavailable(source, field_name)
        )
    payload["quality"] = _quality_issues(source, summaries)
    return payload


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


def _basemap_tile_manifest_entries(
    manifests: Sequence[MapViewportTileManifest],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for manifest in manifests:
        entries.extend(manifest.static_entries())
    return sorted(
        entries,
        key=lambda item: (item["viewport_radius_km"], item["zoom_level"]),
    )


def _basemap_candidate_result(
    provider: str,
    package: OfflineBasemapPackage | None,
    failure: BasemapValidationResult | None,
    manifests: Sequence[MapViewportTileManifest],
    *,
    observed_at: datetime,
) -> BasemapValidationResult:
    if failure is not None:
        return failure
    if package is None:
        return BasemapValidationResult(
            provider=provider,
            package_id="",
            valid=False,
            error_category="package_missing",
            error_message="offline basemap package is missing",
        )
    return OfflineBasemapValidator().validate_package(
        package,
        manifests,
        observed_at=observed_at,
    )


def _selected_basemap(payload: object) -> SelectedBasemap | None:
    if not isinstance(payload, Mapping):
        return None
    provider = payload.get("provider")
    package_id = payload.get("package_id")
    version = payload.get("version")
    checksum = payload.get("checksum")
    selection_reason = payload.get("selection_reason")
    if not all(
        isinstance(value, str)
        for value in (provider, package_id, version, checksum, selection_reason)
    ):
        return None
    return SelectedBasemap(
        provider=provider,
        package_id=package_id,
        version=version,
        checksum=checksum,
        selection_reason=selection_reason,
    )


def _basemap_manifest(
    asset_entries: Sequence[Mapping[str, Any]],
    tile_manifest_entries: Sequence[Mapping[str, Any]],
    selected_basemap: Mapping[str, Any] | None,
    offline_packages: Mapping[str, OfflineBasemapPackage],
) -> dict[str, Any]:
    by_key = {item["asset_key"]: item for item in asset_entries}
    gaode = offline_packages.get("gaode")
    tianditu = offline_packages.get("tianditu")
    return {
        "gaode": (
            gaode.to_manifest_entry()
            if gaode is not None
            else dict(by_key.get("basemap.gaode.offline", {}))
        ),
        "tianditu": (
            tianditu.to_manifest_entry()
            if tianditu is not None
            else dict(by_key.get("basemap.tianditu.offline", {}))
        ),
        "manifests": [dict(entry) for entry in tile_manifest_entries],
        "selected_basemap": (
            dict(selected_basemap) if selected_basemap is not None else None
        ),
    }


def _run_requires_offline_basemap(
    catalog: ArtifactCatalog,
    required_outputs: object,
) -> bool:
    if not isinstance(required_outputs, Sequence) or isinstance(
        required_outputs,
        (str, bytes),
    ):
        return False
    output_keys = {
        (
            item["artifact_key"]
            if isinstance(item, Mapping)
            else item[0]
        )
        for item in required_outputs
        if isinstance(item, (Mapping, Sequence))
        and not isinstance(item, (str, bytes))
        and (
            isinstance(item.get("artifact_key"), str)
            if isinstance(item, Mapping)
            else item and isinstance(item[0], str)
        )
    }
    for definition in catalog.definitions:
        if definition.kind is not ArtifactKind.MAP:
            continue
        if definition.artifact_key not in output_keys:
            continue
        if any(key in definition.required_assets for key in _BASEMAP_KEYS):
            return True
    return False


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


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _mode_marker(production_mode: str) -> str | None:
    return {
        "live": None,
        "manual": None,
        "test": "【测试】",
        "drill": "【演练】",
        "replay": "【测试回放】",
    }.get(production_mode)
