from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete, select

from app.assessment.models import AssessmentTask
from app.auth.service import AuthUser
from app.data_assets.models import (
    DataAsset,
    DataAssetRecord,
    DataAssetSnapshot,
    DataAssetVersion,
)
from app.db import engine
from app.intensity.models import IntensityFieldProduct
from app.loss.models import LossMetricValue, LossProduct
from app.qa.tools.exposure import ExposurePopulationTool
from app.qa.tools.intensity import IntensityGetTool
from app.qa.tools.loss import LossMetricsTool
from app.qa.tools.registry import ToolContext

SNAPSHOT_ASSET_KEYS = (
    "shanghai.admin.town",
    "shanghai.population.town",
)
FIXTURE_IMPORTER = "qa-task9-assessment-tool-test"


@dataclass(frozen=True, slots=True)
class AssessmentToolFixture:
    event_id: UUID
    revision_id: UUID
    assessment_run_id: UUID
    user: AuthUser


@pytest.fixture(autouse=True)
async def dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def assessment_tool_fixture(
    seeded_artifact_assessment,
    session_factory,
) -> AssessmentToolFixture:
    fixture = AssessmentToolFixture(
        event_id=seeded_artifact_assessment.event_id,
        revision_id=seeded_artifact_assessment.revision_id,
        assessment_run_id=seeded_artifact_assessment.assessment_run_id,
        user=AuthUser(username="qa-task9", role="viewer", workgroup=None),
    )
    created_assets: list[UUID] = []
    now = datetime.now(UTC)

    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(DataAssetVersion).where(
                        DataAssetVersion.imported_by == FIXTURE_IMPORTER
                    )
                )

                admin_asset = await _asset(
                    session,
                    asset_key="shanghai.admin.town",
                    data_type="vector",
                    expected_columns=("ID", "NAME"),
                )
                population_asset = await _asset(
                    session,
                    asset_key="shanghai.population.town",
                    data_type="table",
                    expected_columns=(
                        "ID",
                        "NAME",
                        "total",
                        "resident",
                        "floating",
                    ),
                )
                created_assets.extend((admin_asset.id, population_asset.id))

                admin_version = DataAssetVersion(
                    asset_id=admin_asset.id,
                    version="qa-admin-town-v1",
                    status="imported",
                    source_uri="test://qa-admin-town",
                    schema_summary={"columns": ["ID", "NAME"]},
                    record_count=2,
                    source_crs="EPSG:4326",
                    checksum="a" * 64,
                    quality_grade="A",
                    imported_by=FIXTURE_IMPORTER,
                    imported_at=now,
                )
                population_version = DataAssetVersion(
                    asset_id=population_asset.id,
                    version="qa-population-v1",
                    status="imported",
                    source_uri="test://qa-population",
                    schema_summary={
                        "columns": [
                            "ID",
                            "NAME",
                            "total",
                            "resident",
                            "floating",
                        ]
                    },
                    record_count=2,
                    source_crs="EPSG:4326",
                    checksum="b" * 64,
                    quality_grade="B",
                    imported_by=FIXTURE_IMPORTER,
                    imported_at=now,
                )
                session.add_all([admin_version, population_version])
                await session.flush()

                _add_town_asset_records(session, admin_version.id)
                _add_population_asset_records(session, population_version.id)
                await session.flush()
                admin_version.status = "validated"
                population_version.status = "validated"
                await session.flush()
                admin_version.status = "published"
                admin_version.published_at = now
                population_version.status = "published"
                population_version.published_at = now
                await session.flush()
                session.add_all(
                    [
                        DataAssetSnapshot(
                            run_id=fixture.assessment_run_id,
                            asset_id=admin_asset.id,
                            region_id="shanghai",
                            asset_version_id=admin_version.id,
                            asset_key="shanghai.admin.town",
                            version=admin_version.version,
                            checksum=admin_version.checksum or "",
                            role="required",
                            required=True,
                        ),
                        DataAssetSnapshot(
                            run_id=fixture.assessment_run_id,
                            asset_id=population_asset.id,
                            region_id="shanghai",
                            asset_version_id=population_version.id,
                            asset_key="shanghai.population.town",
                            version=population_version.version,
                            checksum=population_version.checksum or "",
                            role="required",
                            required=True,
                        ),
                    ]
                )
                await _seed_intensity_products(
                    session,
                    run_id=fixture.assessment_run_id,
                    now=now,
                )
                await _seed_loss_product(
                    session,
                    run_id=fixture.assessment_run_id,
                    now=now,
                )
                await _seed_validation_loss_product(
                    session,
                    run_id=fixture.assessment_run_id,
                    now=now,
                )

        yield fixture
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(DataAssetVersion).where(
                        DataAssetVersion.imported_by == FIXTURE_IMPORTER
                    )
                )
                for asset_id in created_assets:
                    remaining = await session.scalar(
                        select(DataAssetVersion.id)
                        .where(DataAssetVersion.asset_id == asset_id)
                        .limit(1)
                    )
                    if remaining is None:
                        asset = await session.get(DataAsset, asset_id)
                        if asset is not None:
                            await session.delete(asset)


def context_for(
    fixture: AssessmentToolFixture,
    session,
    *,
    revision_id: UUID | None = None,
    assessment_run_id: UUID | None = None,
) -> ToolContext:
    return ToolContext(
        session=session,
        user=fixture.user,
        event_id=fixture.event_id,
        revision_id=fixture.revision_id if revision_id is None else revision_id,
        assessment_run_id=(
            fixture.assessment_run_id
            if assessment_run_id is None
            else assessment_run_id
        ),
        snapshot_id=uuid4(),
        index_version_id=uuid4(),
    )


async def _asset(
    session,
    *,
    asset_key: str,
    data_type: str,
    expected_columns: tuple[str, ...],
) -> DataAsset:
    asset = await session.scalar(
        select(DataAsset).where(
            DataAsset.asset_key == asset_key,
            DataAsset.region_id == "shanghai",
        )
    )
    if asset is not None:
        return asset
    asset = DataAsset(
        asset_key=asset_key,
        region_id="shanghai",
        name=asset_key,
        data_type=data_type,
        spatial_granularity="town",
        responsibility_unit="qa-task9-test",
        update_interval_days=365,
        is_core=True,
        contract={"columns": list(expected_columns)},
    )
    session.add(asset)
    await session.flush()
    return asset


def _add_town_asset_records(session, version_id: UUID) -> None:
    session.add_all(
        [
            DataAssetRecord(
                version_id=version_id,
                row_number=1,
                business_key="310115000001",
                properties={"ID": "310115000001", "NAME": "near town"},
                geom=WKTElement(
                    "MULTIPOLYGON (((121.49 31.19, 121.51 31.19, "
                    "121.51 31.21, 121.49 31.21, 121.49 31.19)))",
                    srid=4326,
                ),
            ),
            DataAssetRecord(
                version_id=version_id,
                row_number=2,
                business_key="310115000002",
                properties={"ID": "310115000002", "NAME": "east town"},
                geom=WKTElement(
                    "MULTIPOLYGON (((121.59 31.19, 121.61 31.19, "
                    "121.61 31.21, 121.59 31.21, 121.59 31.19)))",
                    srid=4326,
                ),
            ),
        ]
    )


def _add_population_asset_records(session, version_id: UUID) -> None:
    session.add_all(
        [
            DataAssetRecord(
                version_id=version_id,
                row_number=1,
                business_key="310115000001",
                properties={
                    "ID": "310115000001",
                    "NAME": "near town",
                    "total": 100,
                    "resident": 80,
                    "floating": 15,
                },
            ),
            DataAssetRecord(
                version_id=version_id,
                row_number=2,
                business_key="310115000002",
                properties={
                    "ID": "310115000002",
                    "NAME": "east town",
                    "total": 150,
                    "resident": 110,
                    "floating": 25,
                },
            ),
        ]
    )


async def _seed_intensity_products(
    session,
    *,
    run_id: UUID,
    now: datetime,
) -> None:
    model_task = AssessmentTask(
        run_id=run_id,
        task_key="intensity.model",
        task_type="intensity",
        component="qa-task9-test",
        sequence=1,
        status="succeeded",
        deadline_at=now + timedelta(minutes=5),
    )
    fusion_task = AssessmentTask(
        run_id=run_id,
        task_key="intensity.fusion",
        task_type="intensity",
        component="qa-task9-test",
        sequence=2,
        status="succeeded",
        deadline_at=now + timedelta(minutes=5),
    )
    session.add_all([model_task, fusion_task])
    await session.flush()
    session.add_all(
        [
            IntensityFieldProduct(
                run_id=run_id,
                task_id=model_task.id,
                product_type="model",
                status="available",
                algorithm_version="model-axis-ratio-v1",
                parameter_version="shanghai-intensity-parameters-v1",
                strategy_version=None,
                grid_definition_version="grid-v1",
                region_profile_version="region-v1",
                input_fingerprint="1" * 64,
                input_checksum="2" * 64,
                output_checksum="3" * 64,
                quality_grade="A",
                coverage_ratio=1,
                statistics={"minimum": 4.0, "maximum": 6.0, "mean": 5.0},
                observed_at=now,
                completed_at=now,
                published_at=now,
            ),
            IntensityFieldProduct(
                run_id=run_id,
                task_id=fusion_task.id,
                product_type="fusion",
                status="available",
                algorithm_version="fusion-inverse-variance-v1",
                parameter_version="shanghai-intensity-parameters-v1",
                strategy_version="fusion-inverse-variance-v1",
                grid_definition_version="grid-v1",
                region_profile_version="region-v1",
                input_fingerprint="4" * 64,
                input_checksum="5" * 64,
                output_checksum="6" * 64,
                quality_grade="F1",
                coverage_ratio=1,
                statistics={"minimum": 4.5, "maximum": 6.0, "mean": 5.2},
                observed_at=now,
                completed_at=now,
                published_at=now,
            ),
        ]
    )


async def _seed_loss_product(
    session,
    *,
    run_id: UUID,
    now: datetime,
) -> None:
    task = AssessmentTask(
        run_id=run_id,
        task_key="loss.casualties",
        task_type="loss",
        component="qa-task9-test",
        sequence=3,
        status="succeeded",
        deadline_at=now + timedelta(minutes=5),
    )
    session.add(task)
    await session.flush()
    product = LossProduct(
        run_id=run_id,
        task_id=task.id,
        product_type="casualties",
        status="complete",
        quality_grade="L1",
        calibration_status="reference_uncalibrated",
        coverage_ratio=1,
        partial_scope=False,
        needs_review=False,
        spatialized_estimate=False,
        algorithm_version="casualties-reference-v1",
        parameter_version="shanghai-reference-uncalibrated-v1",
        region_profile_version="shanghai-loss-region-v1",
        input_fingerprint="7" * 64,
        input_checksum="8" * 64,
        output_checksum="9" * 64,
        statistics={"town_count": 1},
        reason=None,
        created_at=now,
        completed_at=now,
        published_at=now,
    )
    session.add(product)
    await session.flush()
    session.add(
        LossMetricValue(
            product_id=product.id,
            area_scope="city",
            area_code="310000",
            area_name="上海市",
            metric_key="deaths",
            value_type="central",
            value_status="available",
            numeric_value=Decimal("12.0"),
            unit="人",
            precision=0,
            quality_grade="L1",
            note=None,
            created_at=now,
        )
    )


async def _seed_validation_loss_product(
    session,
    *,
    run_id: UUID,
    now: datetime,
) -> None:
    task = AssessmentTask(
        run_id=run_id,
        task_key="loss.validate",
        task_type="loss",
        component="qa-task9-test",
        sequence=4,
        status="succeeded",
        deadline_at=now + timedelta(minutes=5),
    )
    session.add(task)
    await session.flush()
    session.add(
        LossProduct(
            run_id=run_id,
            task_id=task.id,
            product_type="validation",
            status="complete",
            quality_grade="L2",
            calibration_status="validated",
            coverage_ratio=1,
            partial_scope=False,
            needs_review=False,
            spatialized_estimate=False,
            algorithm_version="loss-validation-v1",
            parameter_version="validation-parameters-v1",
            region_profile_version="shanghai-loss-region-v1",
            input_fingerprint="a" * 64,
            input_checksum="b" * 64,
            output_checksum="c" * 64,
            statistics={"checked_products": 5, "failed_checks": 0},
            reason=None,
            created_at=now,
            completed_at=now,
            published_at=now,
        )
    )


async def test_exposure_population_uses_radius_and_locked_versions(
    assessment_tool_fixture,
    session_factory,
) -> None:
    async with session_factory() as session:
        context = context_for(assessment_tool_fixture, session)
        result = await ExposurePopulationTool().handle({"radius_km": "5"}, context)

    assert result.status == "ok"
    assert result.value["resident_population"] == 80
    assert result.value["floating_population"] == 15
    assert result.value["total_population"] == 100
    assert result.value["precision"] == 0
    assert result.value["unit"] == "人"
    assert result.value["run_revision_id"] == str(
        assessment_tool_fixture.revision_id
    )
    assert result.value["regions"] == [
        {
            "area_code": "310115000001",
            "area_name": "near town",
            "resident_population": 80,
            "floating_population": 15,
            "total_population": 100,
            "precision": 0,
            "unit": "人",
            "quality_grade": "B",
            "value_status": "published",
        }
    ]
    assert result.value["data_versions"] == {
        "shanghai.admin.town": "qa-admin-town-v1",
        "shanghai.population.town": "qa-population-v1",
    }
    assert result.value["checksums"] == {
        "shanghai.admin.town": "a" * 64,
        "shanghai.population.town": "b" * 64,
    }
    assert result.value["statuses"] == {
        "shanghai.admin.town": "published",
        "shanghai.population.town": "published",
    }
    assert result.value["quality_grades"] == {
        "shanghai.admin.town": "A",
        "shanghai.population.town": "B",
    }


async def test_exposure_population_area_code_selects_locked_subset(
    assessment_tool_fixture,
    session_factory,
) -> None:
    async with session_factory() as session:
        context = context_for(assessment_tool_fixture, session)
        result = await ExposurePopulationTool().handle(
            {"radius_km": "50", "area_code": "310115"},
            context,
        )

    assert result.status == "ok"
    assert result.value["resident_population"] == 190
    assert result.value["floating_population"] == 40
    assert result.value["total_population"] == 250
    assert [item["area_code"] for item in result.value["regions"]] == [
        "310115000001",
        "310115000002",
    ]


async def test_exposure_population_returns_unavailable_without_locked_asset(
    assessment_tool_fixture,
    session_factory,
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                delete(DataAssetSnapshot).where(
                    DataAssetSnapshot.run_id
                    == assessment_tool_fixture.assessment_run_id,
                    DataAssetSnapshot.asset_key == "shanghai.population.town",
                )
            )
        context = context_for(assessment_tool_fixture, session)
        result = await ExposurePopulationTool().handle({}, context)

    assert result.status == "unavailable"
    assert result.limitations == ("population_asset_missing",)


async def test_exposure_population_returns_unavailable_without_assessment_run(
    session_factory,
) -> None:
    context = ToolContext(
        session=None,  # type: ignore[arg-type]
        user=AuthUser(username="qa-task9", role="viewer", workgroup=None),
        event_id=uuid4(),
        revision_id=None,
        assessment_run_id=None,
        snapshot_id=uuid4(),
        index_version_id=uuid4(),
    )
    async with session_factory() as session:
        context = ToolContext(
            session=session,
            user=context.user,
            event_id=context.event_id,
            revision_id=context.revision_id,
            assessment_run_id=context.assessment_run_id,
            snapshot_id=context.snapshot_id,
            index_version_id=context.index_version_id,
        )
        result = await ExposurePopulationTool().handle({}, context)

    assert result.status == "unavailable"
    assert result.limitations == ("assessment_run_missing",)


async def test_exposure_population_rejects_run_from_another_event(
    assessment_tool_fixture,
    session_factory,
) -> None:
    async with session_factory() as session:
        context = context_for(
            assessment_tool_fixture,
            session,
            assessment_run_id=uuid4(),
        )
        result = await ExposurePopulationTool().handle({}, context)

    assert result.status == "unavailable"
    assert result.limitations == ("assessment_run_missing",)


async def test_intensity_tool_preserves_products_and_marks_missing_instrument(
    assessment_tool_fixture,
    session_factory,
) -> None:
    async with session_factory() as session:
        context = context_for(assessment_tool_fixture, session)
        result = await IntensityGetTool().handle({}, context)

    assert result.status == "ok"
    assert result.value["run_id"] == str(
        assessment_tool_fixture.assessment_run_id
    )
    assert result.value["run_revision_id"] == str(
        assessment_tool_fixture.revision_id
    )
    assert result.value["model"]["status"] == "available"
    assert result.value["model"]["quality_grade"] == "A"
    assert result.value["model"]["statistics"]["maximum"] == 6.0
    assert result.value["model"]["checksum"] == "3" * 64
    assert result.value["instrument"] == {
        "product_id": None,
        "status": "unavailable",
        "quality_grade": None,
        "coverage_ratio": None,
        "algorithm_version": None,
        "parameter_version": None,
        "strategy_version": None,
        "grid_definition_version": None,
        "region_profile_version": None,
        "statistics": {},
        "checksum": None,
    }
    assert result.value["fusion"]["quality_grade"] == "F1"
    assert result.value["fusion"]["statistics"]["mean"] == 5.2


async def test_loss_tool_returns_central_values_with_versions(
    assessment_tool_fixture,
    session_factory,
) -> None:
    async with session_factory() as session:
        context = context_for(assessment_tool_fixture, session)
        result = await LossMetricsTool().handle(
            {
                "event_id": str(assessment_tool_fixture.event_id),
                "product_type": "casualties",
                "area_scope": "city",
                "area_code": "310000",
                "metric_keys": ["deaths"],
                "value_type": "central",
            },
            context,
        )

    assert result.status == "ok"
    assert result.value["run_id"] == str(
        assessment_tool_fixture.assessment_run_id
    )
    assert result.value["run_revision_id"] == str(
        assessment_tool_fixture.revision_id
    )
    assert result.value["parameter_version"] == (
        "shanghai-reference-uncalibrated-v1"
    )
    assert result.value["quality_grade"] == "L1"
    assert result.value["metrics"][0]["numeric_value"] == 12.0
    assert result.value["metrics"][0]["unit"] == "人"
    assert result.value["metrics"][0]["precision"] == 0
    assert result.value["metrics"][0]["quality_grade"] == "L1"
    assert result.value["metrics"][0]["value_status"] == "available"


async def test_loss_tool_returns_validation_product_metadata_without_metrics(
    assessment_tool_fixture,
    session_factory,
) -> None:
    async with session_factory() as session:
        context = context_for(assessment_tool_fixture, session)
        result = await LossMetricsTool().handle(
            {
                "product_type": "validation",
                "area_scope": "city",
                "value_type": "central",
            },
            context,
        )

    assert result.status == "ok"
    assert result.limitations == ("no_metric_values",)
    assert result.value["metrics"] == []
    assert result.value["status"] == "complete"
    assert result.value["quality_grade"] == "L2"
    assert result.value["calibration_status"] == "validated"
    assert result.value["statistics"] == {
        "checked_products": 5,
        "failed_checks": 0,
    }
    assert result.value["algorithm_version"] == "loss-validation-v1"
    assert result.value["parameter_version"] == "validation-parameters-v1"
    assert result.value["region_profile_version"] == (
        "shanghai-loss-region-v1"
    )
    assert result.value["checksum"] == "c" * 64


async def test_run_tools_reject_assessment_revision_mismatch(
    assessment_tool_fixture,
    seeded_artifact_assessment,
    session_factory,
) -> None:
    correction = await seeded_artifact_assessment.create_correction_revision(
        revision_no=2
    )
    async with session_factory() as session:
        context = context_for(
            assessment_tool_fixture,
            session,
            revision_id=correction.id,
        )
        results = [
            await ExposurePopulationTool().handle({}, context),
            await IntensityGetTool().handle({}, context),
            await LossMetricsTool().handle(
                {
                    "product_type": "casualties",
                    "area_scope": "city",
                    "value_type": "central",
                },
                context,
            ),
        ]

    expected_parameters = {
        "event_id": str(assessment_tool_fixture.event_id),
        "run_id": str(assessment_tool_fixture.assessment_run_id),
        "run_revision_id": str(assessment_tool_fixture.revision_id),
        "context_revision_id": str(correction.id),
    }
    for result in results:
        assert result.status == "unavailable"
        assert result.limitations == ("assessment_revision_mismatch",)
        assert result.value == {}
        assert result.parameters == expected_parameters
