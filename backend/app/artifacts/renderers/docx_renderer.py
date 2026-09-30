from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm
from PIL import Image

from app.artifacts.catalog import load_catalog
from app.artifacts.context import DocumentRenderContext, FrozenAssetVersion
from app.artifacts.domain import ArtifactNameContext, DependencyKind, ProductionMode
from app.artifacts.naming import build_artifact_file_name
from app.artifacts.renderers.base import RenderQuality, RenderResult
from app.artifacts.renderers.chart_renderer import ChartRenderer, ChartSpec
from app.config import settings

DOCX_RENDERER_VERSION = "artifact-docx-renderer-v1"
PLACEHOLDER_PATTERN = re.compile(r"\{\{[^{}]*\}\}")
BACKGROUND_DOC_KEYS = {
    "doc.background",
    "doc.housing",
    "doc.economy",
    "doc.population",
    "doc.key_targets",
    "doc.spatial_distances",
    "doc.area_overview",
    "doc.historical_catalog",
}
CORE_DOC_KEYS = {
    "doc.rapid_brief",
    "doc.rapid_report",
    "doc.decision_report",
    "deck.decision_report",
}
DECISION_DECK_MAP_KEYS = (
    "map.intensity",
    "map.economic_loss",
    "map.rescue_demand",
    "map.deaths",
    "map.injuries",
    "map.buried",
    "map.material_demand",
    "map.active_faults",
    "map.key_targets",
    "map.building_damage",
    "map.epicenter",
    "map.city_distances",
)
_OPTIONAL_ARTIFACT_REVIEW_LABELS = {
    "map.reservoirs": "水库数据待复核",
    "map.metro": "地铁数据待复核",
    "map.seismic_stations": "地震台站数据待复核",
    "map.rescue_teams": "救援队伍数据待复核",
    "map.cultural_relics": "文物单位数据待复核",
    "map.pga_zoning": "地震动区划数据待复核",
    "map.shelter_emergency": "避难场所数据待复核",
    "map.building_grid": "建筑物公里格网数据待复核",
}


@dataclass(frozen=True, slots=True)
class DocumentImage:
    key: str
    path: Path
    checksum: str | None = None
    width: int | None = None
    height: int | None = None
    caption: str = ""


@dataclass(frozen=True, slots=True)
class DocumentSection:
    title: str
    body: tuple[str, ...] = ()
    table: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentRenderSpec:
    artifact_key: str
    display_name: str
    title: str
    document_type: str
    template_path: Path
    template_version: str
    template_checksum: str
    context_fingerprint: str
    input_fingerprint: str
    production_mode: str
    marker: str | None
    control_fields: Mapping[str, Any]
    sections: tuple[DocumentSection, ...]
    images: tuple[DocumentImage, ...]
    charts: tuple[ChartSpec, ...] = ()
    quality: RenderQuality = field(default_factory=RenderQuality)
    event: Mapping[str, Any] = field(default_factory=dict)
    asset_versions: Mapping[str, FrozenAssetVersion] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_key.strip():
            raise ValueError("artifact_key must not be empty")
        if not self.display_name.strip():
            raise ValueError("display_name must not be empty")
        if not self.title.strip():
            raise ValueError("document title must not be empty")
        if not self.template_path.is_file():
            raise FileNotFoundError(f"document template does not exist: {self.template_path}")
        if len(self.template_checksum) != 64:
            raise ValueError("template checksum must be a SHA-256 hex digest")
        if len(self.context_fingerprint) != 64:
            raise ValueError("context_fingerprint must be a SHA-256 hex digest")
        if len(self.input_fingerprint) != 64:
            raise ValueError("input_fingerprint must be a SHA-256 hex digest")
        if not self.sections:
            raise ValueError("document spec must contain at least one section")
        image_keys = [image.key for image in self.images]
        if len(image_keys) != len(set(image_keys)):
            raise ValueError("document image keys must be unique")


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _isoformat(value: Any) -> str:
    if value is None:
        return "不适用"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _t1_display(value: Any) -> str:
    if value is None:
        return "不适用（人工/测试/演练）"
    return f"T1: {_isoformat(value)}"


def _mode_marker(production_mode: str) -> str | None:
    return {
        "live": None,
        "manual": None,
        "test": "【测试】",
        "drill": "【演练】",
        "replay": "【测试回放】",
    }.get(production_mode)


def _product_payload(manifest: Mapping[str, Any], product_key: str) -> Mapping[str, Any]:
    assessment = manifest.get("assessment")
    if isinstance(assessment, Mapping):
        products = assessment.get("products")
        if isinstance(products, Mapping) and isinstance(products.get(product_key), Mapping):
            return dict(products[product_key])
    loss = manifest.get("loss")
    if isinstance(loss, Mapping):
        products = loss.get("products")
        if isinstance(products, Mapping) and isinstance(products.get(product_key), Mapping):
            return dict(products[product_key])
    return {}


def _metric(
    payload: Mapping[str, Any],
    *keys: str,
    source: str = "document context",
) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return f"数据不可用，待复核：{source}"


def _manifest_value(
    manifest: Mapping[str, Any],
    key: str,
    *,
    source: str,
) -> str:
    value = manifest.get(key)
    if value is None or str(value).strip() == "":
        return f"数据不可用，待复核：{source}"
    return str(value)


def _asset_summary(
    asset_versions: Mapping[str, FrozenAssetVersion],
    asset_key: str,
) -> str:
    asset = asset_versions.get(asset_key)
    if asset is None or asset.checksum is None:
        return f"{asset_key}: 数据缺失，待复核"
    return f"{asset_key}: 已冻结 / 版本 {asset.version or '未知'} / {asset.checksum[:12]}"


def _catalog_control_values(
    artifact_key: str,
    declared_fields: tuple[str, ...],
    manifest: Mapping[str, Any],
    asset_versions: Mapping[str, FrozenAssetVersion],
) -> dict[str, Any]:
    event = dict(manifest.get("event") or {})
    buildings = _product_payload(manifest, "loss.buildings")
    population = _product_payload(manifest, "loss.population")
    economy = _product_payload(manifest, "loss.economic")
    intensity = _product_payload(manifest, "intensity.fusion")
    historical = (
        dict(manifest["historical_earthquakes"])
        if isinstance(manifest.get("historical_earthquakes"), Mapping)
        else {}
    )
    spatial = (
        dict(manifest["spatial_distances"])
        if isinstance(manifest.get("spatial_distances"), Mapping)
        else {}
    )
    targets = (
        dict(manifest["targets"])
        if isinstance(manifest.get("targets"), Mapping)
        else {}
    )
    overview = (
        dict(manifest["area_overview"])
        if isinstance(manifest.get("area_overview"), Mapping)
        else {}
    )
    building_town = (
        dict(manifest["building_town"])
        if isinstance(manifest.get("building_town"), Mapping)
        else {}
    )
    faults = (
        dict(manifest["faults"])
        if isinstance(manifest.get("faults"), Mapping)
        else {}
    )
    population_town = (
        dict(manifest["population_town"])
        if isinstance(manifest.get("population_town"), Mapping)
        else {}
    )
    economy_county = (
        dict(manifest["economy_county"])
        if isinstance(manifest.get("economy_county"), Mapping)
        else {}
    )
    coordinate = (
        f"{float(event.get('longitude', 0.0)):.6f} E, "
        f"{float(event.get('latitude', 0.0)):.6f} N"
    )
    common = {
        "event": event.get("place") or "未命名震中",
        "place": event.get("place") or "未命名震中",
        "origin_time": _isoformat(event.get("origin_time")),
        "coordinate": coordinate,
        "depth": f"{float(event.get('depth_km', 0.0)):.2f} km",
    }
    values: dict[str, Any] = dict(common)
    values.update(
        {
            "historical_earthquakes": _metric(historical, "summary"),
            "nearby_faults": _metric(faults, "summary", source="shanghai.fault"),
            "geographic_notes": _metric(overview, "geography"),
            "town_totals": _metric(
                building_town,
                "town_totals",
                source="shanghai.building.town",
            ),
            "structure_type": _metric(
                building_town,
                "structure_type",
                source="shanghai.building.town",
            ),
            "damage_statistics": (
                f"{_metric(buildings, 'slight')}/{_metric(buildings, 'moderate')}/"
                f"{_metric(buildings, 'severe')}"
            ),
            "coverage_quality": _metric(
                building_town,
                "coverage_quality",
                source="shanghai.building.town",
            ),
            "gdp": _metric(economy_county, "gdp", source="shanghai.economy.county"),
            "industry_structure": (
                f"{_metric(economy_county, 'primary', source='shanghai.economy.county')}/"
                f"{_metric(economy_county, 'secondary', source='shanghai.economy.county')}/"
                f"{_metric(economy_county, 'tertiary', source='shanghai.economy.county')}"
            ),
            "economic_loss": _metric(economy, "loss", "economic_loss"),
            "source_scenario": "loss.economic",
            "resident_population": _metric(
                population_town,
                "resident",
                source="shanghai.population.town",
            ),
            "floating_population": _metric(
                population_town,
                "floating",
                source="shanghai.population.town",
            ),
            "household": _metric(
                population_town,
                "household",
                source="shanghai.population.town",
            ),
            "age_structure": _metric(
                population_town,
                "age_structure",
                source="shanghai.population.town",
            ),
            "affected_population": _metric(population, "affected"),
            "shelter": _metric(targets, "shelter"),
            "school": _metric(targets, "school"),
            "hospital": _metric(targets, "hospital"),
            "hazard_source": _metric(targets, "hazard_source"),
            "rescue_team": _metric(targets, "rescue_team"),
            "cultural_relic": _metric(targets, "cultural_relic"),
            "key_target_list": _metric(targets, "key_target"),
            "city_distance": _metric(spatial, "city_distance"),
            "county_distance": _metric(spatial, "county_distance"),
            "town_distance": _metric(spatial, "town_distance"),
            "major_city_distance": _metric(spatial, "major_city_distance"),
            "key_target_distance": _metric(spatial, "key_target_distance"),
            "fault_distance": _metric(spatial, "fault_distance"),
            "geography": _metric(overview, "geography"),
            "administration": _metric(overview, "administration"),
            "intensity": _metric(intensity, "summary", "grade"),
            "population": _metric(population, "affected", "resident"),
            "buildings": _metric(buildings, "total", "severe"),
            "economy": _metric(economy, "gdp", "loss"),
            "key_risks": _metric(overview, "key_risks"),
            "radius": _metric(historical, "radius_km"),
            "magnitude_threshold": _metric(historical, "magnitude_threshold"),
            "historical_earthquake_catalog": _metric(historical, "summary"),
            "disaster_earthquake_catalog": _metric(historical, "disaster_summary"),
            "statistics": _metric(historical, "statistics"),
        }
    )
    return {
        key: values.get(key, "数据不可用，待复核")
        for key in declared_fields
    }


def build_background_document_spec(
    context: DocumentRenderContext,
    artifact_key: str,
) -> DocumentRenderSpec:
    if artifact_key not in BACKGROUND_DOC_KEYS:
        raise ValueError(f"unsupported background document key: {artifact_key}")
    if context.artifact_key != artifact_key:
        raise ValueError("document context artifact_key does not match requested artifact")

    catalog = load_catalog(settings.artifact_catalog_path)
    definition = catalog.get(artifact_key, context.output_profile)
    if definition.template_package != "background-template":
        raise ValueError(
            f"{artifact_key} must use the background-template package"
        )
    manifest = dict(context.manifest)
    event = dict(manifest.get("event") or {})
    t1_at = event.get("t1_at", manifest.get("t1_at"))
    production_mode = context.production_mode
    marker = context.marker or _mode_marker(production_mode)

    template = context.template_versions.get("background-template")
    template_version = (
        str(template.get("version"))
        if isinstance(template, Mapping) and template.get("version")
        else "v1"
    )
    template_path = Path(settings.artifact_template_root) / "background-template.docx"
    template_checksum = (
        str(template.get("checksum"))
        if isinstance(template, Mapping) and template.get("checksum")
        else _sha256_path(template_path)
    )
    if len(template_checksum) != 64:
        template_checksum = _sha256_path(template_path)

    asset_versions = dict(context.asset_versions)
    missing_required = [
        key
        for key in definition.required_assets
        if asset_versions.get(key) is None
        or asset_versions[key].checksum is None
    ]
    missing_optional = [
        key
        for key in definition.optional_assets
        if asset_versions.get(key) is None
        or asset_versions[key].checksum is None
    ]
    degradation_reasons = [
        f"{key} 数据缺失" for key in missing_required + missing_optional
    ]
    needs_review = bool(missing_required or missing_optional)

    input_payload: dict[str, Any] = {
        "artifact_key": artifact_key,
        "output_profile": context.output_profile,
        "context_fingerprint": context.context_fingerprint,
        "template_version": template_version,
        "template_checksum": template_checksum,
        "assets": {
            key: {
                "checksum": asset.checksum,
                "version": asset.version,
            }
            for key, asset in sorted(asset_versions.items())
        },
    }
    input_fingerprint = _sha256_bytes(
        str(input_payload).encode("utf-8")
    )

    control_fields = {
        "event_name": event.get("place") or "未命名震中",
        "magnitude": f"{float(event.get('magnitude', 0.0)):.1f} 级",
        "origin_time": _isoformat(event.get("origin_time")),
        "longitude": f"{float(event.get('longitude', 0.0)):.6f}",
        "latitude": f"{float(event.get('latitude', 0.0)):.6f}",
        "depth_km": f"{float(event.get('depth_km', 0.0)):.2f} km",
        "t1_at": _t1_display(t1_at),
        "data_source": "离线冻结数据资产 / 评估产品 / 系统受控模板",
        "report_type": definition.display_name,
        "mode_marker": marker or "正式",
        "generated_at": datetime.now(UTC).isoformat(),
        "artifact_version": "V001",
        "revision_no": str(event.get("revision_no") or manifest.get("event", {}).get("revision_no") or "1"),
        "assessment_run_no": (
            str(manifest.get("assessment", {}).get("assessment_run_id"))
            if isinstance(manifest.get("assessment"), Mapping)
            else "不适用"
        ),
        "data_asset_snapshot_fingerprint": (
            str(manifest.get("assessment", {}).get("data_asset_snapshot_fingerprint"))
            if isinstance(manifest.get("assessment"), Mapping)
            else "未冻结"
        ),
        "model_parameter_version": (
            str(manifest.get("loss", {}).get("parameter_package", {}).get("version"))
            if isinstance(manifest.get("loss"), Mapping)
            and isinstance(manifest["loss"].get("parameter_package"), Mapping)
            else "未冻结"
        ),
        "template_version": template_version,
        "quality_grade": definition.quality_policy,
        "degradation_reasons": "；".join(degradation_reasons) or "无",
        "needs_review": "待复核" if needs_review else "无",
        "version": template_version,
        "file_name": "",
        "page_number": "",
        "generated_by": "地震应急辅助决策系统",
        "title": f"{marker or ''}{definition.display_name}",
    }
    control_fields.update(
        _catalog_control_values(
            artifact_key,
            definition.control_fields,
            manifest,
            asset_versions,
        )
    )
    missing_control_fields = [
        key for key in definition.control_fields if key not in control_fields
    ]
    if missing_control_fields:
        raise ValueError(
            f"missing declared control fields for {artifact_key}: "
            + ", ".join(missing_control_fields)
        )

    sections = _background_sections(artifact_key, manifest, asset_versions)
    if sections:
        first = sections[0]
        control_body = (
            *first.body,
            f"事件名称：{control_fields['event_name']}",
            f"数据来源：{control_fields['data_source']}",
            f"T1: {control_fields['t1_at'].removeprefix('T1: ')}",
        )
        sections = (
            DocumentSection(
                title=first.title,
                body=control_body,
                table=first.table,
            ),
            *sections[1:],
        )
    images = _background_images(context, artifact_key)

    return DocumentRenderSpec(
        artifact_key=artifact_key,
        display_name=definition.display_name,
        title=f"{marker or ''}{definition.display_name}",
        document_type=context.document_type,
        template_path=template_path,
        template_version=template_version,
        template_checksum=template_checksum,
        context_fingerprint=context.context_fingerprint,
        input_fingerprint=input_fingerprint,
        production_mode=production_mode,
        marker=marker,
        control_fields=control_fields,
        sections=sections,
        images=images,
        charts=_background_charts(artifact_key, manifest, marker=marker),
        quality=RenderQuality(
            grade=definition.quality_policy,
            needs_review=needs_review,
            degradation_reasons=tuple(degradation_reasons),
        ),
        event=event,
        asset_versions=asset_versions,
    )


def build_core_document_spec(
    context: DocumentRenderContext,
    artifact_key: str,
) -> DocumentRenderSpec:
    if artifact_key not in CORE_DOC_KEYS:
        raise ValueError(f"unsupported core document key: {artifact_key}")
    if context.artifact_key != artifact_key:
        raise ValueError("document context artifact_key does not match requested artifact")

    catalog = load_catalog(settings.artifact_catalog_path)
    definition = catalog.get(artifact_key, context.output_profile)
    manifest = dict(context.manifest)
    event = dict(manifest.get("event") or {})
    t1_at = event.get("t1_at", manifest.get("t1_at"))
    production_mode = context.production_mode
    marker = context.marker or _mode_marker(production_mode)

    template_key, template_file = _core_template_identity(artifact_key)
    template = context.template_versions.get(template_key)
    template_version = (
        str(template.get("version"))
        if isinstance(template, Mapping) and template.get("version")
        else "v1"
    )
    template_path = Path(settings.artifact_template_root) / template_file
    template_checksum = (
        str(template.get("checksum"))
        if isinstance(template, Mapping) and template.get("checksum")
        else _sha256_path(template_path)
    )
    if len(template_checksum) != 64:
        template_checksum = _sha256_path(template_path)

    hard_product_keys, hard_artifact_keys, optional_artifact_keys = (
        _core_dependency_keys(definition)
    )
    if artifact_key == "deck.decision_report":
        required_map_keys = DECISION_DECK_MAP_KEYS
        artifact_dependency_keys = (
            tuple(hard_artifact_keys) + DECISION_DECK_MAP_KEYS
        )
    else:
        required_map_keys = tuple(
            key for key in hard_artifact_keys if key.startswith("map.")
        )
        artifact_dependency_keys = tuple(hard_artifact_keys)

    missing_products = [
        key for key in hard_product_keys if not _product_payload(manifest, key)
    ]
    missing_required_artifacts = [
        key
        for key in artifact_dependency_keys
        if not _artifact_available(context, key)
    ]
    missing_optional_artifacts = [
        key
        for key in optional_artifact_keys
        if not _artifact_available(context, key)
    ]
    degradation_reasons = [
        _dependency_review_label(key)
        for key in (
            missing_products
            + missing_required_artifacts
            + missing_optional_artifacts
        )
    ]
    needs_review = bool(degradation_reasons)

    if definition.failure_policy == "block" and (
        missing_products or missing_required_artifacts
    ):
        first_missing = (missing_products + missing_required_artifacts)[0]
        raise FileNotFoundError(
            f"required core document dependency unavailable: {first_missing}"
        )

    asset_versions = dict(context.asset_versions)
    input_payload: dict[str, Any] = {
        "artifact_key": artifact_key,
        "output_profile": context.output_profile,
        "context_fingerprint": context.context_fingerprint,
        "template_key": template_key,
        "template_version": template_version,
        "template_checksum": template_checksum,
        "products": {
            key: {
                "version": _product_payload(manifest, key).get("version"),
                "checksum": _product_payload(manifest, key).get("checksum"),
            }
            for key in sorted(hard_product_keys)
        },
        "artifacts": {
            key: {
                "checksum": asset.checksum,
                "version": asset.version,
            }
            for key, asset in sorted(asset_versions.items())
            if key in artifact_dependency_keys
            or key in optional_artifact_keys
        },
    }
    input_fingerprint = _sha256_bytes(
        str(input_payload).encode("utf-8")
    )

    control_fields = _core_control_fields(
        definition=definition,
        event=event,
        manifest=manifest,
        t1_at=t1_at,
        marker=marker,
        template_version=template_version,
        degradation_reasons=degradation_reasons,
        needs_review=needs_review,
        product_keys=hard_product_keys,
        artifact_keys=artifact_dependency_keys,
        asset_versions=asset_versions,
    )
    sections = _core_sections(
        artifact_key,
        manifest,
        asset_versions,
        degradation_reasons=degradation_reasons,
        needs_review=needs_review,
        control_fields=control_fields,
    )
    images = _core_images(
        context,
        artifact_key,
        required_map_keys,
        optional_artifact_keys,
    )

    return DocumentRenderSpec(
        artifact_key=artifact_key,
        display_name=definition.display_name,
        title=f"{marker or ''}{definition.display_name}",
        document_type=context.document_type,
        template_path=template_path,
        template_version=template_version,
        template_checksum=template_checksum,
        context_fingerprint=context.context_fingerprint,
        input_fingerprint=input_fingerprint,
        production_mode=production_mode,
        marker=marker,
        control_fields=control_fields,
        sections=sections,
        images=images,
        charts=(),
        quality=RenderQuality(
            grade=definition.quality_policy,
            needs_review=needs_review,
            degradation_reasons=tuple(degradation_reasons),
        ),
        event=event,
        asset_versions=asset_versions,
    )


def _core_template_identity(artifact_key: str) -> tuple[str, str]:
    if artifact_key == "deck.decision_report":
        return "decision-template", "decision-template.pptx"
    return "background-template", "background-template.docx"


def _core_dependency_keys(
    definition: Any,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    hard_products = tuple(
        dependency.key
        for dependency in definition.depends_on
        if dependency.kind is DependencyKind.ASSESSMENT_PRODUCT
    )
    hard_artifacts = tuple(
        dependency.key
        for dependency in definition.depends_on
        if dependency.kind is DependencyKind.ARTIFACT
    )
    optional_artifacts = tuple(
        dependency.key
        for dependency in definition.optional_depends_on
        if dependency.kind is DependencyKind.ARTIFACT
    )
    return hard_products, hard_artifacts, optional_artifacts


def _artifact_available(
    context: DocumentRenderContext,
    artifact_key: str,
) -> bool:
    asset = context.asset_versions.get(artifact_key)
    if asset is None or asset.checksum is None:
        return False
    path = context.artifact_paths.get(artifact_key)
    if artifact_key.startswith("map."):
        return path is not None and path.is_file()
    return True


def _dependency_review_label(key: str) -> str:
    if key in _OPTIONAL_ARTIFACT_REVIEW_LABELS:
        return _OPTIONAL_ARTIFACT_REVIEW_LABELS[key]
    if key.startswith("map."):
        catalog = load_catalog(settings.artifact_catalog_path)
        display_name = catalog.get(key, "a3v-professional").display_name
        compact = (
            display_name
            .removeprefix("震区")
            .removeprefix("震中附近")
            .removesuffix("分布图")
            .removesuffix("专题图")
            .removesuffix("图")
        )
        return f"{compact}数据待复核"
    if "." in key:
        return f"{key} 数据缺失"
    return f"{key} 数据待复核"


def _core_control_fields(
    *,
    definition: Any,
    event: Mapping[str, Any],
    manifest: Mapping[str, Any],
    t1_at: Any,
    marker: str | None,
    template_version: str,
    degradation_reasons: Sequence[str],
    needs_review: bool,
    product_keys: Sequence[str],
    artifact_keys: Sequence[str],
    asset_versions: Mapping[str, FrozenAssetVersion],
) -> dict[str, Any]:
    coordinate = (
        f"{float(event.get('longitude', 0.0)):.6f} E, "
        f"{float(event.get('latitude', 0.0)):.6f} N"
    )
    common = {
        "event_name": event.get("place") or "未命名震中",
        "magnitude": f"{float(event.get('magnitude', 0.0)):.1f} 级",
        "origin_time": _isoformat(event.get("origin_time")),
        "longitude": f"{float(event.get('longitude', 0.0)):.6f}",
        "latitude": f"{float(event.get('latitude', 0.0)):.6f}",
        "depth_km": f"{float(event.get('depth_km', 0.0)):.2f} km",
        "coordinate": coordinate,
        "t1_at": _t1_display(t1_at),
        "data_source": "离线冻结数据资产 / 评估产品 / 系统受控模板",
        "report_type": definition.display_name,
        "mode_marker": marker or "正式",
        "generated_at": datetime.now(UTC).isoformat(),
        "artifact_version": "V001",
        "revision_no": str(event.get("revision_no") or "1"),
        "assessment_run_no": (
            str(manifest.get("assessment", {}).get("assessment_run_id"))
            if isinstance(manifest.get("assessment"), Mapping)
            else "不适用"
        ),
        "data_asset_snapshot_fingerprint": (
            str(manifest.get("assessment", {}).get("data_asset_snapshot_fingerprint"))
            if isinstance(manifest.get("assessment"), Mapping)
            else "未冻结"
        ),
        "model_parameter_version": (
            str(manifest.get("loss", {}).get("parameter_package", {}).get("version"))
            if isinstance(manifest.get("loss"), Mapping)
            and isinstance(manifest["loss"].get("parameter_package"), Mapping)
            else "未冻结"
        ),
        "template_version": template_version,
        "quality_grade": definition.quality_policy,
        "degradation_reasons": "；".join(degradation_reasons) or "无",
        "needs_review": "待复核" if needs_review else "无",
        "version": template_version,
        "file_name": "",
        "page_number": "",
        "generated_by": "地震应急辅助决策系统",
        "title": f"{marker or ''}{definition.display_name}",
    }
    dynamic = _core_dynamic_fields(manifest)
    source_versions = _core_source_versions(
        manifest,
        product_keys,
        artifact_keys,
        context_assets=asset_versions,
    )
    event_facts = (
        f"事件名称：{common['event_name']}；震级：{common['magnitude']}；"
        f"发震时刻：{common['origin_time']}；坐标：{coordinate}；"
        f"震源深度：{common['depth_km']}；{common['t1_at']}"
    )
    return {
        **common,
        **dynamic,
        "event_facts": event_facts,
        "recommendation": _core_recommendation(manifest),
        "source_versions": "\n".join(source_versions),
        "spatial_conclusions": _core_spatial_conclusions(manifest),
    }


def _core_dynamic_fields(manifest: Mapping[str, Any]) -> dict[str, Any]:
    intensity = _product_payload(manifest, "intensity.fusion")
    buildings = _product_payload(manifest, "loss.buildings")
    population = _product_payload(manifest, "loss.population")
    casualties = _product_payload(manifest, "loss.casualties")
    economy = _product_payload(manifest, "loss.economic")
    resources = _product_payload(manifest, "loss.resources")
    spatial = (
        dict(manifest["spatial_distances"])
        if isinstance(manifest.get("spatial_distances"), Mapping)
        else {}
    )
    targets = (
        dict(manifest["targets"])
        if isinstance(manifest.get("targets"), Mapping)
        else {}
    )
    faults = (
        dict(manifest["faults"])
        if isinstance(manifest.get("faults"), Mapping)
        else {}
    )
    return {
        "intensity": _metric(intensity, "summary", "grade"),
        "population": _metric(population, "affected", "resident"),
        "buildings": _metric(buildings, "total", "severe"),
        "economy": _metric(economy, "loss", "gdp"),
        "resources": _metric(resources, "summary", "demand"),
        "casualties": _metric(casualties, "summary", "deaths", "injuries"),
        "key_targets": _metric(targets, "key_target"),
        "faults": _metric(faults, "summary", source="shanghai.fault"),
        "city_distance": _metric(spatial, "city_distance"),
        "fault_distance": _metric(spatial, "fault_distance"),
    }


def _core_source_versions(
    manifest: Mapping[str, Any],
    product_keys: Sequence[str],
    artifact_keys: Sequence[str],
    *,
    context_assets: Mapping[str, FrozenAssetVersion],
) -> tuple[str, ...]:
    lines: list[str] = []
    for key in product_keys:
        payload = _product_payload(manifest, key)
        if payload:
            lines.append(
                f"{key}: 版本 {payload.get('version') or '未知'} / "
                f"{str(payload.get('checksum') or '')[:12]}"
            )
        else:
            lines.append(f"{key}: 数据缺失，待复核")
    for key in artifact_keys:
        asset = context_assets.get(key)
        if asset is not None and asset.checksum:
            lines.append(
                f"{key}: 版本 {asset.version or '未知'} / {asset.checksum[:12]}"
            )
        else:
            lines.append(f"{key}: 数据缺失，待复核")
    return tuple(lines)


def _core_recommendation(manifest: Mapping[str, Any]) -> str:
    intensity = _product_payload(manifest, "intensity.fusion")
    population = _product_payload(manifest, "loss.population")
    if not intensity and not population:
        return "评估产品数据不足，建议人工复核后启动响应。"
    return (
        f"根据烈度 {_metric(intensity, 'summary', 'grade')} 与受灾人口 "
        f"{_metric(population, 'affected')}，建议按预案启动相应级别响应，"
        "优先保障人员搜救和生命线抢修。"
    )


def _core_spatial_conclusions(manifest: Mapping[str, Any]) -> str:
    spatial = (
        dict(manifest["spatial_distances"])
        if isinstance(manifest.get("spatial_distances"), Mapping)
        else {}
    )
    return (
        f"主要城市距离 {_metric(spatial, 'city_distance')} km；"
        f"断裂距离 {_metric(spatial, 'fault_distance')} km。"
        "空间距离信息用于快速圈定应急影响范围。"
    )


def _core_sections(
    artifact_key: str,
    manifest: Mapping[str, Any],
    asset_versions: Mapping[str, FrozenAssetVersion],
    *,
    degradation_reasons: Sequence[str],
    needs_review: bool,
    control_fields: Mapping[str, Any],
) -> tuple[DocumentSection, ...]:
    event = dict(manifest.get("event") or {})
    intensity = _product_payload(manifest, "intensity.fusion")
    buildings = _product_payload(manifest, "loss.buildings")
    population = _product_payload(manifest, "loss.population")
    casualties = _product_payload(manifest, "loss.casualties")
    economy = _product_payload(manifest, "loss.economic")
    resources = _product_payload(manifest, "loss.resources")
    validation = _product_payload(manifest, "loss.validate")
    overview = (
        dict(manifest["area_overview"])
        if isinstance(manifest.get("area_overview"), Mapping)
        else {}
    )
    spatial = (
        dict(manifest["spatial_distances"])
        if isinstance(manifest.get("spatial_distances"), Mapping)
        else {}
    )
    targets = (
        dict(manifest["targets"])
        if isinstance(manifest.get("targets"), Mapping)
        else {}
    )
    faults = (
        dict(manifest["faults"])
        if isinstance(manifest.get("faults"), Mapping)
        else {}
    )
    quality_lines = [
        f"质量等级：{control_fields['quality_grade']}",
        f"降级原因：{control_fields['degradation_reasons']}",
        f"待复核项：{control_fields['needs_review']}",
        f"来源与版本：{control_fields['source_versions']}",
    ]
    if degradation_reasons:
        quality_lines.extend(degradation_reasons)
    quality_section = DocumentSection(
        "来源与质量",
        tuple(quality_lines),
    )

    if artifact_key == "doc.rapid_brief":
        return (
            DocumentSection(
                "核心结论",
                (
                    f"事件名称：{event.get('place') or '未命名震中'}",
                    f"震级：{float(event.get('magnitude', 0.0)):.1f} 级",
                    f"发震时刻：{_isoformat(event.get('origin_time'))}",
                    f"T1: {control_fields['t1_at'].removeprefix('T1: ')}",
                    f"烈度：{_metric(intensity, 'summary', 'grade')}",
                    f"伤亡：{_metric(casualties, 'summary', 'deaths', 'injuries')}",
                ),
            ),
            DocumentSection(
                "房屋与人口",
                (
                    f"房屋破坏：{_metric(buildings, 'slight')} / "
                    f"{_metric(buildings, 'moderate')} / "
                    f"{_metric(buildings, 'severe')}",
                    f"受灾人口：{_metric(population, 'affected')}",
                ),
            ),
            DocumentSection(
                "经济与资源",
                (
                    f"经济损失：{_metric(economy, 'loss', 'gdp')}",
                    f"资源需求：{_metric(resources, 'summary', 'demand')}",
                ),
            ),
            quality_section,
        )

    if artifact_key == "doc.rapid_report":
        return (
            DocumentSection(
                "背景资料综述",
                (
                    _manifest_value(
                        overview,
                        "geography",
                        source="assessment manifest / area_overview.geography",
                    ),
                    _metric(targets, "key_target"),
                    _metric(faults, "summary", source="shanghai.fault"),
                    _metric(spatial, "city_distance"),
                    _metric(spatial, "fault_distance"),
                ),
            ),
            DocumentSection(
                "核心评估结果",
                (
                    f"烈度：{_metric(intensity, 'summary', 'grade')}",
                    (
                        "房屋破坏："
                        f"{_metric(buildings, 'slight')} / "
                        f"{_metric(buildings, 'moderate')} / "
                        f"{_metric(buildings, 'severe')}"
                    ),
                    f"受灾人口：{_metric(population, 'affected')}",
                    f"伤亡：{_metric(casualties, 'summary', 'deaths', 'injuries')}",
                    f"经济损失：{_metric(economy, 'loss', 'gdp')}",
                    f"资源需求：{_metric(resources, 'summary', 'demand')}",
                    f"校验结论：{_metric(validation, 'summary', 'grade')}",
                ),
            ),
            quality_section,
        )

    if artifact_key == "doc.decision_report":
        return (
            DocumentSection(
                "快速简报结论",
                (
                    f"烈度：{_metric(intensity, 'summary', 'grade')}",
                    f"受灾人口：{_metric(population, 'affected')}",
                    f"房屋破坏：{_metric(buildings, 'total', 'severe')}",
                    f"经济损失：{_metric(economy, 'loss', 'gdp')}",
                ),
            ),
            DocumentSection(
                "快速专报结论",
                (
                    _metric(faults, "summary", source="shanghai.fault"),
                    _metric(targets, "key_target"),
                    _metric(spatial, "city_distance"),
                    _metric(spatial, "fault_distance"),
                ),
            ),
            DocumentSection(
                "重点图件",
                (
                    "地震影响场、经济与资源需求、人员伤亡、房屋破坏、"
                    "活动断裂、重点目标、震中及城市距离图件已嵌入正文。",
                ),
            ),
            quality_section,
        )

    return (
        DocumentSection(
            "辅助决策演示稿固定版式",
            (
                "共 8 页：封面、事件参数、响应建议、融合烈度、"
                "伤亡与房屋破坏、经济与资源需求、重点目标与断裂、"
                "空间距离与结论。",
                "所有图件均按本地绝对路径和 checksum 嵌入。",
            ),
        ),
        quality_section,
    )


def _core_images(
    context: DocumentRenderContext,
    artifact_key: str,
    required_map_keys: Sequence[str],
    optional_map_keys: Sequence[str],
) -> tuple[DocumentImage, ...]:
    catalog = load_catalog(settings.artifact_catalog_path)
    keys: tuple[str, ...]
    if artifact_key == "deck.decision_report":
        keys = DECISION_DECK_MAP_KEYS
    else:
        keys = tuple(
            key
            for key in (*required_map_keys, *optional_map_keys)
            if key.startswith("map.")
        )
    images: list[DocumentImage] = []
    for key in keys:
        path = context.artifact_paths.get(key)
        asset = context.asset_versions.get(key)
        if path is None or not path.is_file():
            continue
        checksum = asset.checksum if asset is not None else _sha256_path(path)
        definition = catalog.get(key, context.output_profile)
        images.append(
            DocumentImage(
                key=key,
                path=path,
                checksum=checksum,
                caption=f"{definition.display_name}（{key}）",
            )
        )
    return tuple(images)


def _background_images(
    context: DocumentRenderContext,
    artifact_key: str,
) -> tuple[DocumentImage, ...]:
    if artifact_key != "doc.area_overview":
        return ()
    path = context.artifact_paths.get("map.epicenter")
    if path is None or not path.is_file():
        return ()
    checksum = context.asset_versions.get("map.epicenter")
    width, height = _image_dimensions(path)
    return (
        DocumentImage(
            key="map.epicenter",
            path=path,
            checksum=checksum.checksum if checksum is not None else _sha256_path(path),
            width=width,
            height=height,
            caption="震中位置分布图",
        ),
    )


def _background_charts(
    artifact_key: str,
    manifest: Mapping[str, Any],
    *,
    marker: str | None,
) -> tuple[ChartSpec, ...]:
    if artifact_key == "doc.population":
        population = _product_payload(manifest, "loss.population")
        labels = ("常住人口", "流动人口", "受灾人口")
        values = (
            _optional_float(population.get("resident")),
            _optional_float(population.get("floating")),
            _optional_float(population.get("affected")),
        )
        return (
            ChartSpec(
                chart_id="population-impact",
                title="人口影响概览",
                labels=labels,
                series=(_chart_series("人口影响", values),),
                y_label="数量",
                marker=marker,
            ),
        )
    if artifact_key == "doc.housing":
        housing = _product_payload(manifest, "loss.buildings")
        labels = ("房屋总量", "轻度破坏", "中度破坏", "严重破坏")
        values = (
            _optional_float(housing.get("total")),
            _optional_float(housing.get("slight")),
            _optional_float(housing.get("moderate")),
            _optional_float(housing.get("severe")),
        )
        return (
            ChartSpec(
                chart_id="housing-damage",
                title="房屋破坏统计",
                labels=labels,
                series=(_chart_series("房屋数量", values),),
                y_label="数量",
                marker=marker,
            ),
        )
    if artifact_key == "doc.economy":
        economy = _product_payload(manifest, "loss.economic")
        labels = ("第一产业", "第二产业", "第三产业", "经济损失")
        values = (
            _optional_float(economy.get("primary")),
            _optional_float(economy.get("secondary")),
            _optional_float(economy.get("tertiary")),
            _optional_float(economy.get("loss")),
        )
        return (
            ChartSpec(
                chart_id="economy-loss",
                title="经济结构与损失",
                labels=labels,
                series=(_chart_series("金额", values),),
                y_label="金额",
                marker=marker,
            ),
        )
    return ()


def _chart_series(name: str, values: tuple[float, ...]) -> Any:
    from app.artifacts.renderers.chart_renderer import ChartSeries

    return ChartSeries(name=name, values=values)


def _optional_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _background_sections(
    artifact_key: str,
    manifest: Mapping[str, Any],
    asset_versions: Mapping[str, FrozenAssetVersion],
) -> tuple[DocumentSection, ...]:
    event = dict(manifest.get("event") or {})
    buildings = _product_payload(manifest, "loss.buildings")
    population = _product_payload(manifest, "loss.population")
    economy = _product_payload(manifest, "loss.economic")
    intensity = _product_payload(manifest, "intensity.fusion")
    historical_manifest = (
        dict(manifest["historical_earthquakes"])
        if isinstance(manifest.get("historical_earthquakes"), Mapping)
        else {}
    )
    spatial = (
        dict(manifest["spatial_distances"])
        if isinstance(manifest.get("spatial_distances"), Mapping)
        else {}
    )
    targets = (
        dict(manifest["targets"])
        if isinstance(manifest.get("targets"), Mapping)
        else {}
    )
    overview = (
        dict(manifest["area_overview"])
        if isinstance(manifest.get("area_overview"), Mapping)
        else {}
    )
    building_town = (
        dict(manifest["building_town"])
        if isinstance(manifest.get("building_town"), Mapping)
        else {}
    )
    faults = (
        dict(manifest["faults"])
        if isinstance(manifest.get("faults"), Mapping)
        else {}
    )
    population_town = (
        dict(manifest["population_town"])
        if isinstance(manifest.get("population_town"), Mapping)
        else {}
    )
    economy_county = (
        dict(manifest["economy_county"])
        if isinstance(manifest.get("economy_county"), Mapping)
        else {}
    )

    if artifact_key == "doc.background":
        return (
            DocumentSection(
                "事件参数",
                (
                    f"事件名称：{event.get('place') or '未命名震中'}",
                    f"震级：{float(event.get('magnitude', 0.0)):.1f} 级",
                    f"发震时刻：{_isoformat(event.get('origin_time'))}",
                    (
                        "震中坐标："
                        f"{float(event.get('longitude', 0.0)):.6f} E, "
                        f"{float(event.get('latitude', 0.0)):.6f} N"
                    ),
                    f"震源深度：{float(event.get('depth_km', 0.0)):.2f} km",
                ),
            ),
            DocumentSection(
                "所在行政区",
                (
                    _manifest_value(
                        overview,
                        "administration",
                        source="assessment manifest / area_overview.administration",
                    )
                ),
            ),
            DocumentSection(
                "历史地震",
                (
                    _asset_summary(asset_versions, "shanghai.historical.earthquakes"),
                    _metric(historical_manifest, "summary"),
                ),
            ),
            DocumentSection(
                "邻近断裂",
                (
                    _metric(
                        faults,
                        "summary",
                        source="shanghai.fault",
                    ),
                    _asset_summary(asset_versions, "shanghai.fault"),
                ),
            ),
            DocumentSection(
                "基础地理背景",
                (
                    _manifest_value(
                        overview,
                        "geography",
                        source="assessment manifest / area_overview.geography",
                    ),
                ),
            ),
        )
    if artifact_key == "doc.housing":
        return (
            DocumentSection(
                "街镇房屋总量",
                (
                    (
                        "房屋总量："
                        f"{_metric(building_town, 'town_totals', source='shanghai.building.town')}"
                    ),
                    _asset_summary(asset_versions, "shanghai.building.town"),
                ),
            ),
            DocumentSection(
                "结构类型",
                (
                    _metric(
                        building_town,
                        "structure_type",
                        source="shanghai.building.town",
                    ),
                    _asset_summary(asset_versions, "shanghai.building.town"),
                ),
            ),
            DocumentSection(
                "破坏统计",
                (
                    f"轻度破坏：{_metric(buildings, 'slight')}",
                    f"中度破坏：{_metric(buildings, 'moderate')}",
                    f"严重破坏：{_metric(buildings, 'severe')}",
                ),
            ),
            DocumentSection(
                "覆盖与质量",
                (
                    _metric(
                        building_town,
                        "coverage_quality",
                        source="shanghai.building.town",
                    ),
                    (
                        "评估产品版本："
                        f"{buildings.get('version') or '数据不可用，待复核'}"
                    ),
                ),
            ),
        )
    if artifact_key == "doc.economy":
        return (
            DocumentSection(
                "GDP",
                (
                    _metric(
                        economy_county,
                        "gdp",
                        source="shanghai.economy.county",
                    ),
                ),
            ),
            DocumentSection(
                "产业结构",
                (
                    (
                        "第一产业："
                        f"{_metric(economy_county, 'primary', source='shanghai.economy.county')}"
                    ),
                    (
                        "第二产业："
                        f"{_metric(economy_county, 'secondary', source='shanghai.economy.county')}"
                    ),
                    (
                        "第三产业："
                        f"{_metric(economy_county, 'tertiary', source='shanghai.economy.county')}"
                    ),
                ),
            ),
            DocumentSection("经济损失", (_metric(economy, "loss", "economic_loss"),)),
            DocumentSection(
                "来源与情景",
                (
                    "来源：loss.economic",
                    (
                        "模型参数版本："
                        f"{manifest.get('loss', {}).get('parameter_package', {}).get('version')}"
                    ),
                ),
            ),
        )
    if artifact_key == "doc.population":
        return (
            DocumentSection(
                "常住人口",
                (
                    _metric(
                        population_town,
                        "resident",
                        source="shanghai.population.town",
                    ),
                ),
            ),
            DocumentSection(
                "流动人口",
                (
                    _metric(
                        population_town,
                        "floating",
                        source="shanghai.population.town",
                    ),
                ),
            ),
            DocumentSection(
                "家庭户",
                (
                    _metric(
                        population_town,
                        "household",
                        source="shanghai.population.town",
                    ),
                ),
            ),
            DocumentSection(
                "年龄结构",
                (
                    _metric(
                        population_town,
                        "age_structure",
                        source="shanghai.population.town",
                    ),
                ),
            ),
            DocumentSection("受灾人口", (_metric(population, "affected"),)),
        )
    if artifact_key == "doc.key_targets":
        return (
            DocumentSection(
                "避难场所",
                (
                    _metric(targets, "shelter"),
                    _asset_summary(asset_versions, "shanghai.shelter.emergency"),
                ),
            ),
            DocumentSection(
                "学校",
                (
                    _metric(targets, "school"),
                    _asset_summary(asset_versions, "shanghai.education.school"),
                ),
            ),
            DocumentSection(
                "医院",
                (
                    _metric(targets, "hospital"),
                    _asset_summary(asset_versions, "shanghai.health.hospital"),
                ),
            ),
            DocumentSection(
                "危险源",
                (
                    _metric(targets, "hazard_source"),
                    _asset_summary(asset_versions, "shanghai.hazard_source"),
                ),
            ),
            DocumentSection(
                "救援队伍",
                (
                    _metric(targets, "rescue_team"),
                    _asset_summary(asset_versions, "shanghai.rescue_team"),
                ),
            ),
            DocumentSection(
                "文物单位",
                (
                    _metric(targets, "cultural_relic"),
                    _asset_summary(asset_versions, "shanghai.cultural_relic"),
                ),
            ),
            DocumentSection(
                "重点目标列表",
                (
                    _metric(targets, "key_target"),
                    _asset_summary(asset_versions, "shanghai.key_target"),
                ),
            ),
        )
    if artifact_key == "doc.spatial_distances":
        return (
            DocumentSection("城市距离", (f"{_metric(spatial, 'city_distance')} km",)),
            DocumentSection("区县距离", (f"{_metric(spatial, 'county_distance')} km",)),
            DocumentSection("街镇距离", (f"{_metric(spatial, 'town_distance')} km",)),
            DocumentSection(
                "主要城市距离",
                (f"{_metric(spatial, 'major_city_distance')} km",),
            ),
            DocumentSection(
                "重点目标距离",
                (f"{_metric(spatial, 'key_target_distance')} km",),
            ),
            DocumentSection("断裂距离", (f"{_metric(spatial, 'fault_distance')} km",)),
        )
    if artifact_key == "doc.area_overview":
        return (
            DocumentSection(
                "地理概况",
                (
                    _manifest_value(
                        overview,
                        "geography",
                        source="assessment manifest / area_overview.geography",
                    ),
                ),
            ),
            DocumentSection(
                "行政区",
                (
                    _manifest_value(
                        overview,
                        "administration",
                        source="assessment manifest / area_overview.administration",
                    ),
                ),
            ),
            DocumentSection("烈度", (_metric(intensity, "summary", "grade"),)),
            DocumentSection("人口", (_metric(population, "affected", "resident"),)),
            DocumentSection("房屋", (_metric(buildings, "total", "severe"),)),
            DocumentSection("经济", (_metric(economy, "gdp", "loss"),)),
            DocumentSection(
                "关键风险",
                (
                    _manifest_value(
                        overview,
                        "key_risks",
                        source="assessment manifest / area_overview.key_risks",
                    ),
                ),
            ),
        )
    if artifact_key == "doc.historical_catalog":
        return (
            DocumentSection(
                "检索半径",
                (f"{_metric(historical_manifest, 'radius_km')} km",),
            ),
            DocumentSection(
                "震级阈值",
                (f"M >= {_metric(historical_manifest, 'magnitude_threshold')}",),
            ),
            DocumentSection(
                "历史地震目录",
                (
                    _metric(historical_manifest, "summary"),
                    _asset_summary(asset_versions, "shanghai.historical.earthquakes"),
                ),
            ),
            DocumentSection(
                "灾害地震目录",
                (_metric(historical_manifest, "disaster_summary"),),
            ),
            DocumentSection("统计", (_metric(historical_manifest, "statistics"),)),
        )
    raise ValueError(f"unsupported background document key: {artifact_key}")


class DocxRenderer:
    def __init__(
        self,
        *,
        template_path: str | Path | None = None,
        chart_renderer: ChartRenderer | None = None,
    ) -> None:
        self._template_path = Path(
            template_path or Path(settings.artifact_template_root) / "background-template.docx"
        )
        self._chart_renderer = chart_renderer or ChartRenderer()

    async def render(self, spec: DocumentRenderSpec, output_path: Path) -> RenderResult:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.docx")
        actual_template_checksum = _sha256_path(spec.template_path)
        if actual_template_checksum != spec.template_checksum:
            raise ValueError("template checksum mismatch")
        file_name = build_artifact_file_name(
            ArtifactNameContext(
                place=str(spec.event.get("place") or "未命名震中"),
                magnitude=float(spec.event.get("magnitude", 0.0)),
                display_name=spec.display_name,
                version=1,
                generated_at=datetime.now(UTC),
                production_mode=ProductionMode(spec.production_mode),
            ),
            extension="docx",
        )
        control_fields = dict(spec.control_fields)
        control_fields["file_name"] = file_name
        control_fields["page_number"] = ""
        spec = replace(spec, control_fields=control_fields)

        document = Document(str(spec.template_path))

        self._replace_placeholders(document, spec)
        self._remove_section_placeholders(document)
        self._append_sections(document, spec)
        images = self._all_images(spec, target.parent)
        self._embed_images(document, images)
        self._set_footer(document, spec, file_name)
        self._assert_no_placeholders(document)

        page_count = self._page_count(document)
        document.save(temporary)
        os.replace(temporary, target)

        checksum = _sha256_path(target)
        image_checksums = self._image_checksums(images)
        render_manifest: dict[str, Any] = {
            "marker": spec.marker,
            "template_version": spec.template_version,
            "template_checksum": spec.template_checksum,
            "image_checksums": image_checksums,
            "image_paths": {
                key: str(image.path)
                for key, image in images.items()
            },
            "images": list(images),
            "control_fields": dict(spec.control_fields),
            "quality": spec.quality.to_dict(),
            "page_count": page_count,
            "page_count_method": "declared_page_breaks_plus_one",
            "unresolved_placeholder_count": 0,
            "renderer_version": DOCX_RENDERER_VERSION,
            "context_fingerprint": spec.context_fingerprint,
            "input_fingerprint": spec.input_fingerprint,
        }
        return RenderResult(
            path=target,
            format="docx",
            width=794,
            height=1123,
            dpi=96,
            checksum=checksum,
            quality=spec.quality,
            task_status=(
                "degraded"
                if spec.quality.needs_review
                or spec.quality.degradation_reasons
                else "succeeded"
            ),
            file_name=file_name,
            render_manifest=render_manifest,
            non_empty_ratio=1.0,
            size_bytes=target.stat().st_size,
            generated_at=datetime.now(UTC),
            page_count=page_count,
            control_fields=dict(spec.control_fields),
        )

    def _replace_placeholders(
        self,
        document: DocumentObject,
        spec: DocumentRenderSpec,
    ) -> None:
        values = dict(spec.control_fields)
        values["file_name"] = spec.control_fields.get("file_name", "")
        values["page_number"] = ""
        values["version"] = spec.template_version
        values["generated_by"] = "地震应急辅助决策系统"
        for paragraph in _iter_document_paragraphs(document):
            self._replace_paragraph_placeholders(paragraph, values)

    def _replace_paragraph_placeholders(
        self,
        paragraph: Any,
        values: Mapping[str, Any],
    ) -> None:
        if "{{" not in paragraph.text:
            return
        text = paragraph.text
        for key, value in values.items():
            text = text.replace(f"{{{{{key}}}}}", "" if value is None else str(value))
        paragraph.text = text

    def _remove_section_placeholders(self, document: DocumentObject) -> None:
        for paragraph in _iter_document_paragraphs(document):
            if (
                "{{section_1_title}}" in paragraph.text
                or "{{section_1_body}}" in paragraph.text
                or "{{section_1_image}}" in paragraph.text
            ):
                paragraph.text = ""

    def _append_sections(
        self,
        document: DocumentObject,
        spec: DocumentRenderSpec,
    ) -> None:
        for index, section in enumerate(spec.sections):
            if index > 0:
                document.add_page_break()
            document.add_heading(section.title, level=1)
            for line in section.body:
                document.add_paragraph(line)
            if section.table:
                self._add_table(document, section.table)

    def _add_table(self, document: DocumentObject, rows: Sequence[Sequence[str]]) -> None:
        table = document.add_table(rows=0, cols=max(len(row) for row in rows))
        table.style = "Table Grid"
        for row_values in rows:
            row = table.add_row()
            for index, value in enumerate(row_values):
                row.cells[index].text = str(value)

    def _embed_images(
        self,
        document: DocumentObject,
        images: Mapping[str, DocumentImage],
    ) -> None:
        if not images:
            return
        first = next(iter(images.values()))
        insertion = self._find_blank_placeholder_paragraph(document)
        if insertion is not None:
            self._add_image_to_paragraph(insertion, first)
        else:
            document.add_paragraph()
            self._add_image_to_paragraph(document.paragraphs[-1], first)
        for image in list(images.values())[1:]:
            paragraph = document.add_paragraph()
            self._add_image_to_paragraph(paragraph, image)

    def _add_image_to_paragraph(self, paragraph: Any, image: DocumentImage) -> None:
        paragraph.text = ""
        run = paragraph.add_run()
        run.add_picture(
            str(image.path),
            width=Cm(15),
        )
        if image.caption:
            paragraph.add_run(f" {image.caption}")

    def _find_blank_placeholder_paragraph(self, document: DocumentObject) -> Any | None:
        for paragraph in document.paragraphs:
            if paragraph.text.strip() == "":
                return paragraph
        return None

    def _set_footer(
        self,
        document: DocumentObject,
        spec: DocumentRenderSpec,
        file_name: str,
    ) -> None:
        footer = document.sections[0].footer
        paragraph = footer.paragraphs[0]
        paragraph.text = ""
        paragraph.add_run(f"{file_name} | 第 ")
        _add_page_number_field(paragraph)
        paragraph.add_run(
            f" 页 | 版本 {spec.template_version} | 地震应急辅助决策系统"
        )
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

    def _assert_no_placeholders(self, document: DocumentObject) -> None:
        text = "\n".join(
            paragraph.text for paragraph in _iter_document_paragraphs(document)
        )
        if PLACEHOLDER_PATTERN.search(text):
            raise ValueError("rendered DOCX still contains unresolved placeholders")

    def _page_count(self, document: DocumentObject) -> int:
        count = 1
        for paragraph in document.paragraphs:
            if _paragraph_has_page_break(paragraph):
                count += 1
        return count

    def _all_images(
        self,
        spec: DocumentRenderSpec,
        output_parent: Path,
    ) -> dict[str, DocumentImage]:
        images = {image.key: image for image in spec.images}
        chart_root = output_parent / ".charts"
        for chart_spec in spec.charts:
            chart_path = chart_root / f"{spec.artifact_key}-{chart_spec.chart_id}.png"
            result = self._chart_renderer.render(chart_spec, chart_path)
            images[chart_spec.chart_id] = DocumentImage(
                key=chart_spec.chart_id,
                path=result.path,
                checksum=result.checksum,
                width=result.width,
                height=result.height,
                caption=chart_spec.title,
            )
        return images

    def _image_checksums(
        self,
        images: Mapping[str, DocumentImage],
    ) -> dict[str, str]:
        checksums: dict[str, str] = {}
        for key, image in images.items():
            checksum = image.checksum or _sha256_path(image.path)
            if image.checksum and checksum != image.checksum:
                raise ValueError(f"image checksum mismatch for {key}")
            checksums[key] = checksum
        return checksums


def _iter_document_paragraphs(document: DocumentObject) -> tuple[Any, ...]:
    paragraphs: list[Any] = list(document.paragraphs)
    for table in document.tables:
        paragraphs.extend(_iter_table_paragraphs(table))
    for section in document.sections:
        for container in (section.header, section.footer):
            paragraphs.extend(container.paragraphs)
            for table in container.tables:
                paragraphs.extend(_iter_table_paragraphs(table))
    return tuple(paragraphs)


def _iter_table_paragraphs(table: Any) -> tuple[Any, ...]:
    paragraphs: list[Any] = []
    for row in table.rows:
        for cell in row.cells:
            paragraphs.extend(cell.paragraphs)
            for nested_table in cell.tables:
                paragraphs.extend(_iter_table_paragraphs(nested_table))
    return tuple(paragraphs)


def _paragraph_has_page_break(paragraph: Any) -> bool:
    return paragraph._element.xpath(".//w:br[@w:type='page']")


def _add_page_number_field(paragraph: Any) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instruction)
    run._r.append(end)


def _image_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size
