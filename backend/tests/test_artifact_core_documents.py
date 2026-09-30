from __future__ import annotations

import hashlib
import uuid
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from docx import Document
from PIL import Image, ImageDraw
from pptx import Presentation
from sqlalchemy import select

from app.artifacts.basemap import MapViewportTileManifest, VIEWPORT_RADII_KM
from app.artifacts.context import ProductionContextService
from app.artifacts.models import (
    ArtifactTaskDependencyBinding,
    GeneratedArtifact,
    ProductionTask,
)
from app.artifacts.repository import ArtifactProductionRepository
from app.artifacts.renderers.docx_renderer import (
    DocxRenderer,
    build_core_document_spec,
)
from app.artifacts.renderers.pptx_renderer import PptxRenderer
from app.artifacts.storage import ArtifactStore
from app.assessment.models import AssessmentTask
from app.config import settings
from app.intensity.models import IntensityFieldProduct
from app.loss.models import LossProduct
from tests.basemap_fixtures import make_in_memory_package


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
def docx_renderer() -> DocxRenderer:
    return DocxRenderer()


@pytest.fixture
def pptx_renderer() -> PptxRenderer:
    return PptxRenderer()


@pytest.fixture
def production_context_service() -> ProductionContextService:
    manifests = tuple(
        MapViewportTileManifest.build(
            center_lon=121.5,
            center_lat=31.2,
            radius_km=radius_km,
            output_width=settings.artifact_basemap_output_width,
            output_height=settings.artifact_basemap_output_height,
            zoom_levels=settings.artifact_basemap_zoom_levels,
            padding=settings.artifact_basemap_buffer_pixels,
        )
        for radius_km in VIEWPORT_RADII_KM
    )
    return ProductionContextService(
        repository=ArtifactProductionRepository(),
        offline_basemap_packages={
            "gaode": make_in_memory_package(manifests, provider="gaode"),
            "tianditu": make_in_memory_package(manifests, provider="tianditu"),
        },
    )


def _docx_text(path: Path) -> str:
    return "\n".join(paragraph.text for paragraph in Document(path).paragraphs)


def _pptx_text(path: Path) -> str:
    presentation = Presentation(path)
    return "\n".join(
        shape.text
        for slide in presentation.slides
        for shape in slide.shapes
        if hasattr(shape, "text")
    )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


EXPECTED_PRODUCT_METADATA = {
    "intensity.fusion": ("fusion-v1", "a" * 64),
    "loss.buildings": ("buildings-v1", "b" * 64),
    "loss.population": ("population-v1", "c" * 64),
    "loss.casualties": ("casualties-v1", "d" * 64),
    "loss.economic": ("economic-v1", "e" * 64),
    "loss.resources": ("resources-v1", "f" * 64),
    "loss.validate": ("validate-v1", "1" * 64),
}


def _map_bytes(marker: str | None = None) -> BytesIO:
    image = Image.new("RGB", (640, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 30, 610, 330), outline=(20, 30, 40), width=3)
    draw.text((45, 45), marker or "map", fill=(20, 30, 40))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    buffer.seek(0)
    return buffer


def _docx_bytes(label: str) -> BytesIO:
    document = Document()
    document.add_paragraph(f"{label} production artifact")
    buffer = BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer


def _store_artifact(
    store: ArtifactStore,
    *,
    artifact_key: str,
    file_format: str,
    marker: str | None = None,
):
    if file_format == "jpg":
        stream = _map_bytes(marker)
        file_name = f"{artifact_key}.jpg"
    else:
        stream = _docx_bytes(artifact_key)
        file_name = f"{artifact_key}.docx"
    return store.store_immutable_stream(stream, file_name=file_name)


def _generated_artifact(
    *,
    run_id,
    task_id,
    event_id,
    revision_id,
    artifact_key: str,
    stored,
    file_format: str,
    render_manifest=None,
):
    now = datetime.now(UTC)
    return GeneratedArtifact(
        production_run_id=run_id,
        production_task_id=task_id,
        event_id=event_id,
        revision_id=revision_id,
        artifact_key=artifact_key,
        output_profile="a3v-professional",
        artifact_version=1,
        is_final=True,
        production_mode="live",
        status="complete",
        quality_grade="core",
        needs_review=False,
        publication_mode="automatic",
        generation_reason=None,
        marker=None,
        file_name=stored.relative_path.rsplit("/", 1)[-1],
        format=file_format,
        storage_path=stored.relative_path,
        checksum=stored.checksum,
        size_bytes=stored.size_bytes,
        width=640 if file_format == "jpg" else None,
        height=360 if file_format == "jpg" else None,
        page_count=None,
        template_snapshot=None,
        data_snapshot=None,
        render_manifest=render_manifest,
        generated_at=now,
        published_at=now,
        superseded_by_id=None,
    )


async def _seed_assessment_products_and_bindings(
    session,
    seeded_artifact_assessment,
    run,
    tasks_by_key,
) -> None:
    now = datetime.now(UTC)
    assessment_run_id = seeded_artifact_assessment.assessment_run_id

    async def assessment_task(task_key: str, task_type: str) -> AssessmentTask:
        existing = await session.scalar(
            select(AssessmentTask).where(
                AssessmentTask.run_id == assessment_run_id,
                AssessmentTask.task_key == task_key,
            )
        )
        if existing is not None:
            return existing
        task = AssessmentTask(
            run_id=assessment_run_id,
            task_key=task_key,
            task_type=task_type,
            component="decision-fixture",
            sequence=1,
            status="succeeded",
            deadline_at=run.deadline_at,
        )
        session.add(task)
        await session.flush()
        return task

    product_ids: dict[str, uuid.UUID] = {}
    intensity_task = await assessment_task("intensity.fusion", "intensity")
    intensity_checksum = EXPECTED_PRODUCT_METADATA["intensity.fusion"][1]
    intensity = IntensityFieldProduct(
        run_id=assessment_run_id,
        task_id=intensity_task.id,
        product_type="fusion",
        status="complete",
        algorithm_version=EXPECTED_PRODUCT_METADATA["intensity.fusion"][0],
        parameter_version="parameters-v1",
        strategy_version="strategy-v1",
        grid_definition_version="grid-v1",
        region_profile_version="region-v1",
        input_fingerprint="a" * 64,
        input_checksum="b" * 64,
        output_checksum=intensity_checksum,
        quality_grade="A",
        coverage_ratio=1,
        statistics={"summary": "烈度 V"},
    )
    session.add(intensity)
    await session.flush()
    product_ids["intensity.fusion"] = intensity.id

    loss_products = (
        ("loss.buildings", "building_damage", {
            "total": 10000,
            "slight": 1200,
            "moderate": 300,
            "severe": 80,
        }),
        ("loss.population", "population_impact", {
            "resident": 120000,
            "affected": 4300,
        }),
        ("loss.casualties", "casualties", {
            "deaths": 18,
            "injuries": 42,
            "summary": "死亡 18 人，受伤 42 人",
        }),
        ("loss.economic", "economic_loss", {
            "gdp": 560000,
            "loss": 8800,
        }),
        ("loss.resources", "resource_demand", {
            "summary": "救援力量需求 5 支",
            "demand": "5",
        }),
        ("loss.validate", "validation", {
            "summary": "评估结果通过校验",
            "grade": "通过",
        }),
    )
    for product_key, product_type, statistics in loss_products:
        task = await assessment_task(product_key, "loss")
        version, checksum = EXPECTED_PRODUCT_METADATA[product_key]
        product = LossProduct(
            run_id=assessment_run_id,
            task_id=task.id,
            product_type=product_type,
            status="complete",
            quality_grade="L1",
            calibration_status="calibrated",
            coverage_ratio=1,
            partial_scope=False,
            needs_review=False,
            spatialized_estimate=False,
            algorithm_version=version,
            parameter_version="parameters-v1",
            region_profile_version="region-v1",
            input_fingerprint="a" * 64,
            input_checksum="b" * 64,
            output_checksum=checksum,
            statistics=statistics,
        )
        session.add(product)
        await session.flush()
        product_ids[product_key] = product.id

    rapid_report_task_id = tasks_by_key["doc.rapid_report"].id
    for product_key, (version, checksum) in EXPECTED_PRODUCT_METADATA.items():
        session.add(
            ArtifactTaskDependencyBinding(
                production_task_id=rapid_report_task_id,
                dependency_kind="assessment_product",
                dependency_key=product_key,
                dependency_output_profile=None,
                is_optional=False,
                bound_entity_id=product_ids[product_key],
                bound_version=version,
                bound_checksum=checksum,
                resolution_status="bound",
                resolved_at=now,
            )
        )


async def _production_document_context(
    seeded_artifact_assessment,
    production_context_service: ProductionContextService,
    session_factory,
    artifact_key: str,
):
    run = await seeded_artifact_assessment.create_full_run()
    async with session_factory() as session:
        async with session.begin():
            await production_context_service.freeze_static_context(
                session,
                run.id,
                seeded_artifact_assessment.catalog,
            )

    store = ArtifactStore(settings.artifact_storage_root)
    decision_map_keys = (
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
    rapid_report_control_fields = {
        "intensity": "烈度 V",
        "population": "4300",
        "buildings": "10000",
        "economy": "8800",
        "resources": "救援力量需求 5 支",
        "casualties": "死亡 18 人，受伤 42 人",
        "key_targets": "重点目标 64 处",
        "faults": "邻近断裂 3 条",
        "city_distance": "8.6",
        "fault_distance": "21.7",
        "source_versions": (
            "intensity.fusion: 版本 fusion-v1 / "
            "loss.buildings: 版本 buildings-v1 / "
            "loss.population: 版本 population-v1 / "
            "loss.casualties: 版本 casualties-v1 / "
            "loss.economic: 版本 economic-v1 / "
            "loss.resources: 版本 resources-v1"
        ),
    }

    async with session_factory() as session:
        async with session.begin():
            tasks = (
                await session.scalars(
                    select(ProductionTask).where(
                        ProductionTask.production_run_id == run.id
                    )
                )
            ).all()
            tasks_by_key = {task.artifact_key: task for task in tasks}
            await _seed_assessment_products_and_bindings(
                session,
                seeded_artifact_assessment,
                run,
                tasks_by_key,
            )
            for key in decision_map_keys:
                stored = _store_artifact(
                    store,
                    artifact_key=key,
                    file_format="jpg",
                )
                session.add(
                    _generated_artifact(
                        run_id=run.id,
                        task_id=tasks_by_key[key].id,
                        event_id=seeded_artifact_assessment.event_id,
                        revision_id=seeded_artifact_assessment.revision_id,
                        artifact_key=key,
                        stored=stored,
                        file_format="jpg",
                    )
                )

            rapid_report = _store_artifact(
                store,
                artifact_key="doc.rapid_report",
                file_format="docx",
            )
            session.add(
                _generated_artifact(
                    run_id=run.id,
                    task_id=tasks_by_key["doc.rapid_report"].id,
                    event_id=seeded_artifact_assessment.event_id,
                    revision_id=seeded_artifact_assessment.revision_id,
                    artifact_key="doc.rapid_report",
                    stored=rapid_report,
                    file_format="docx",
                    render_manifest={
                        "control_fields": rapid_report_control_fields,
                    },
                )
            )

            for key in ("doc.rapid_brief", "doc.decision_report"):
                stored = _store_artifact(
                    store,
                    artifact_key=key,
                    file_format="docx",
                )
                session.add(
                    _generated_artifact(
                        run_id=run.id,
                        task_id=tasks_by_key[key].id,
                        event_id=seeded_artifact_assessment.event_id,
                        revision_id=seeded_artifact_assessment.revision_id,
                        artifact_key=key,
                        stored=stored,
                        file_format="docx",
                    )
                )

    async with session_factory() as session:
        async with session.begin():
            task = tasks_by_key[artifact_key]
            return await production_context_service.build_document_context(
                session,
                task.id,
                "pptx" if artifact_key == "deck.decision_report" else "docx",
            )


async def test_rapid_brief_contains_t1_not_applicable_for_manual_event(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.rapid_brief",
        t1_at=None,
    )
    result = await docx_renderer.render(
        build_core_document_spec(context, "doc.rapid_brief"),
        tmp_path / "brief.docx",
    )
    text = _docx_text(result.path)

    assert "T1" in text
    assert "不适用（人工/测试/演练）" in text


async def test_rapid_report_omits_failed_optional_map_but_marks_degraded(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.rapid_report",
        failed_optional_artifacts={"map.reservoirs"},
    )
    result = await docx_renderer.render(
        build_core_document_spec(context, "doc.rapid_report"),
        tmp_path / "report.docx",
    )
    text = _docx_text(result.path)

    assert result.task_status == "degraded"
    assert "map.reservoirs" not in result.render_manifest["images"]
    assert "水库数据待复核" in text


async def test_pptx_replaces_all_placeholders_and_uses_fixed_pages(
    seeded_artifact_assessment,
    pptx_renderer: PptxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context("deck.decision_report")
    result = await pptx_renderer.render(
        build_core_document_spec(context, "deck.decision_report"),
        tmp_path / "decision.pptx",
    )
    presentation = Presentation(result.path)
    all_text = _pptx_text(result.path)

    assert len(presentation.slides) == context.pptx_slide_count
    assert "{{" not in all_text
    assert "辅助决策报告" in all_text
    assert result.render_manifest["marker"] in {"", "【测试】", "【演练】", "【测试回放】"}


async def test_core_document_fails_when_hard_map_dependency_is_missing(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.rapid_brief",
        missing_artifact_keys={"map.epicenter"},
    )

    with pytest.raises(FileNotFoundError, match="map.epicenter"):
        await docx_renderer.render(
            build_core_document_spec(context, "doc.rapid_brief"),
            tmp_path / "brief.docx",
        )


async def test_pptx_fails_when_hard_map_dependency_is_missing(
    seeded_artifact_assessment,
    pptx_renderer: PptxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "deck.decision_report",
        missing_artifact_keys={"map.epicenter"},
    )

    with pytest.raises(FileNotFoundError, match="map.epicenter"):
        await pptx_renderer.render(
            build_core_document_spec(context, "deck.decision_report"),
            tmp_path / "decision.pptx",
        )


async def test_rapid_report_fails_when_hard_map_dependency_is_missing(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.rapid_report",
        missing_artifact_keys={"map.epicenter"},
    )

    with pytest.raises(FileNotFoundError, match="map.epicenter"):
        await docx_renderer.render(
            build_core_document_spec(context, "doc.rapid_report"),
            tmp_path / "report.docx",
        )


async def test_rapid_report_fails_when_required_product_is_missing(
    seeded_artifact_assessment,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "doc.rapid_report",
        failed_products={"loss.casualties"},
    )

    with pytest.raises(FileNotFoundError, match="loss.casualties"):
        await docx_renderer.render(
            build_core_document_spec(context, "doc.rapid_report"),
            tmp_path / "report.docx",
        )


async def test_pptx_manifest_tracks_images_and_checksums(
    seeded_artifact_assessment,
    pptx_renderer: PptxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context("deck.decision_report")
    spec = build_core_document_spec(context, "deck.decision_report")
    result = await pptx_renderer.render(spec, tmp_path / "decision.pptx")

    assert result.render_manifest["page_count"] == 8
    assert result.render_manifest["template_version"] == spec.template_version
    assert result.render_manifest["unresolved_placeholder_count"] == 0
    for key, checksum in result.render_manifest["image_checksums"].items():
        path = Path(result.render_manifest["image_paths"][key])
        assert _sha256_path(path) == checksum


async def test_pptx_rejects_template_missing_declared_placeholder(
    seeded_artifact_assessment,
    pptx_renderer: PptxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context("deck.decision_report")
    spec = build_core_document_spec(context, "deck.decision_report")
    committed = Path(settings.artifact_template_root) / "decision-template.pptx"
    modified = tmp_path / "missing-placeholder.pptx"
    presentation = Presentation(str(committed))
    for slide in presentation.slides:
        for shape in list(slide.shapes):
            if shape.has_text_frame and "{{event_facts}}" in shape.text_frame.text:
                shape._element.getparent().remove(shape._element)
    presentation.save(modified)
    spec = replace(
        spec,
        template_path=modified,
        template_checksum=_sha256_path(modified),
    )

    with pytest.raises(ValueError, match="event_facts"):
        await pptx_renderer.render(
            spec,
            tmp_path / "bad.pptx",
        )


@pytest.mark.parametrize(
    ("production_mode", "marker"),
    (
        ("test", "【测试】"),
        ("drill", "【演练】"),
        ("replay", "【测试回放】"),
    ),
)
async def test_core_pptx_mode_marker_is_in_file_name_body_and_manifest(
    production_mode: str,
    marker: str,
    seeded_artifact_assessment,
    pptx_renderer: PptxRenderer,
    tmp_path: Path,
) -> None:
    context = await seeded_artifact_assessment.document_context(
        "deck.decision_report",
        production_mode=production_mode,
    )
    result = await pptx_renderer.render(
        build_core_document_spec(context, "deck.decision_report"),
        tmp_path / f"{production_mode}.pptx",
    )
    presentation = Presentation(result.path)

    assert marker in result.file_name
    assert marker in result.render_manifest["marker"]
    assert any(marker in shape.text for shape in presentation.slides[0].shapes if hasattr(shape, "text"))


def test_decision_template_is_deterministic_and_has_no_linked_media(tmp_path: Path) -> None:
    from app.artifacts.template_builder import build_decision_template

    first = build_decision_template(tmp_path / "first")
    second = build_decision_template(tmp_path / "second")
    committed = Path(settings.artifact_template_root) / "decision-template.pptx"

    assert _sha256_path(first) == _sha256_path(second)
    assert _sha256_path(first) == _sha256_path(committed)
    presentation = Presentation(first)
    assert len(presentation.slides) == 8
    assert round(presentation.slide_width.cm) == 34
    assert round(presentation.slide_height.cm) == 19

    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
    assert not any(name.startswith("ppt/media/") for name in names)
    assert not any("externalLink" in name for name in names)


async def test_real_production_context_resolves_core_document_inputs(
    seeded_artifact_assessment,
    production_context_service,
    session_factory,
    tmp_path: Path,
) -> None:
    run = await seeded_artifact_assessment.create_full_run()
    async with session_factory() as session:
        async with session.begin():
            await production_context_service.freeze_static_context(
                session,
                run.id,
                seeded_artifact_assessment.catalog,
            )

    async with session_factory() as session:
        async with session.begin():
            task = await seeded_artifact_assessment.first_task(
                run.id,
                "doc.rapid_report",
            )
            context = await production_context_service.build_document_context(
                session,
                task.id,
                "docx",
            )

    assert context.artifact_key == "doc.rapid_report"
    assert context.artifact_paths == {}
    with pytest.raises(FileNotFoundError, match="intensity.fusion"):
        build_core_document_spec(context, "doc.rapid_report")


async def test_real_production_decision_report_renders_available_values(
    seeded_artifact_assessment,
    production_context_service,
    session_factory,
    docx_renderer: DocxRenderer,
    tmp_path: Path,
) -> None:
    context = await _production_document_context(
        seeded_artifact_assessment,
        production_context_service,
        session_factory,
        "doc.decision_report",
    )
    products = context.manifest["assessment"]["products"]
    for product_key, (version, checksum) in EXPECTED_PRODUCT_METADATA.items():
        assert products[product_key]["version"] == version
        assert products[product_key]["checksum"] == checksum
    assert products["loss.validate"]["summary"] == "评估结果通过校验"
    assert products["loss.validate"]["checksum"] != products["loss.resources"]["checksum"]
    spec = build_core_document_spec(context, "doc.decision_report")
    result = await docx_renderer.render(
        spec,
        tmp_path / "decision.docx",
    )
    text = _docx_text(result.path)

    assert result.task_status == "succeeded"
    assert spec.quality.needs_review is False
    assert "数据不可用，待复核" not in text


async def test_real_production_deck_renders_available_values(
    seeded_artifact_assessment,
    production_context_service,
    session_factory,
    pptx_renderer: PptxRenderer,
    tmp_path: Path,
) -> None:
    context = await _production_document_context(
        seeded_artifact_assessment,
        production_context_service,
        session_factory,
        "deck.decision_report",
    )
    products = context.manifest["assessment"]["products"]
    for product_key, (version, checksum) in EXPECTED_PRODUCT_METADATA.items():
        assert products[product_key]["version"] == version
        assert products[product_key]["checksum"] == checksum
    assert products["loss.validate"]["summary"] == "评估结果通过校验"
    assert products["loss.validate"]["checksum"] != products["loss.resources"]["checksum"]
    spec = build_core_document_spec(context, "deck.decision_report")
    result = await pptx_renderer.render(
        spec,
        tmp_path / "decision.pptx",
    )
    presentation = Presentation(result.path)
    all_text = _pptx_text(result.path)

    assert len(presentation.slides) == context.pptx_slide_count
    assert spec.quality.needs_review is False
    assert "数据不可用，待复核" not in all_text
    assert result.render_manifest["unresolved_placeholder_count"] == 0
