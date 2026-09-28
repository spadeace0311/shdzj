import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.data_assets.import_jobs import fail_import_job
from app.data_assets.models import (
    DataAsset,
    DataAssetAuditLog,
    DataAssetImportJob,
    DataAssetRecord,
    DataAssetVersion,
)
from app.data_assets.service import DataAssetDecisionError, DataAssetService


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_fixture_versions(session_factory):
    from tests.data_asset_helpers import _cleanup_fixture_data

    yield
    await _cleanup_fixture_data(session_factory)


async def test_candidate_validation_publish_and_single_published_version(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    first_id = await candidate_factory("2022.1")
    second_id = await candidate_factory("2022.2")

    async with session_factory() as session:
        async with session.begin():
            first = await service.validate_version(session, first_id)
            assert first.publishable
            await service.publish_version(session, first_id, "publisher", "initial")
            second = await service.validate_version(session, second_id)
            assert second.publishable
            await service.publish_version(session, second_id, "publisher", "annual update")
            rows = (
                await session.scalars(
                    select(DataAssetVersion).where(
                        DataAssetVersion.status == "published",
                        DataAssetVersion.id.in_([first_id, second_id]),
                    )
                )
            ).all()
            audit_rows = (
                await session.scalars(
                    select(DataAssetAuditLog).where(
                        DataAssetAuditLog.version_id.in_(
                            [first_id, second_id]
                        )
                    )
                )
            ).all()

    assert [str(row.id) for row in rows] == [str(second_id)]
    assert sorted(row.action for row in audit_rows) == [
        "import",
        "import",
        "publish",
        "publish",
        "retire",
        "validate",
        "validate",
    ]


async def test_failed_import_writes_failed_job_and_audit(
    session_factory,
    candidate_factory,
) -> None:
    version_id = await candidate_factory("2022.3")
    async with session_factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(DataAssetImportJob).where(
                    DataAssetImportJob.asset_version_id == version_id
                )
            )
            await fail_import_job(session, job.id, ValueError("synthetic parse failure"))

    async with session_factory() as session:
        failed_job = await session.get(DataAssetImportJob, job.id)
        audit_actions = (
            await session.scalars(
                select(DataAssetAuditLog).where(
                    DataAssetAuditLog.version_id == version_id
                )
            )
        ).all()
    assert failed_job.status == "failed"
    assert failed_job.error_summary == "synthetic parse failure"
    assert [row.action for row in audit_actions] == ["import", "import_failed"]


async def test_published_version_is_immutable_and_rollback_republishes(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    first_id = await candidate_factory("2022.1")
    second_id = await candidate_factory("2022.2")
    async with session_factory() as session:
        async with session.begin():
            await service.validate_version(session, first_id)
            await service.publish_version(session, first_id, "publisher", "initial")
            await service.validate_version(session, second_id)
            await service.publish_version(session, second_id, "publisher", "update")
            await service.rollback_version(session, first_id, "publisher", "bad update")
            with pytest.raises(ValueError, match="transition"):
                await service.publish_version(session, first_id, "publisher", "duplicate")

    async with session_factory() as session:
        async with session.begin():
            first = await session.get(DataAssetVersion, first_id)

    assert first.status == "published"


async def test_published_version_rejects_metadata_mutation(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    version_id = await candidate_factory("2022.1")
    async with session_factory() as session:
        async with session.begin():
            await service.validate_version(session, version_id)
            await service.publish_version(session, version_id, "publisher", "initial")
            with pytest.raises(ValueError, match="immutable"):
                await service.update_candidate_metadata(
                    session,
                    version_id,
                    change_note="changed after publication",
                    actor="publisher",
                )


async def test_concurrent_publish_keeps_single_published_version(
    session_factory,
    candidate_factory,
) -> None:
    service = DataAssetService()
    first_id = await candidate_factory("2022.1")
    second_id = await candidate_factory("2022.2")
    async with session_factory() as session:
        async with session.begin():
            await service.validate_version(session, first_id)
            await service.validate_version(session, second_id)

    async def publish(version_id):
        async with session_factory() as session:
            async with session.begin():
                await DataAssetService().publish_version(
                    session,
                    version_id,
                    "publisher",
                    "concurrent update",
                )

    await asyncio.gather(publish(first_id), publish(second_id))

    async with session_factory() as session:
        published = (
            await session.scalars(
                select(DataAssetVersion).where(
                    DataAssetVersion.status == "published",
                    DataAssetVersion.id.in_([first_id, second_id]),
                )
            )
        ).all()
    assert len(published) == 1


async def test_population_publish_requires_published_admin_town(
    session_factory,
) -> None:
    from tests.data_asset_helpers import (
        _population_records,
        _populate,
        _queue_candidate,
    )

    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key="shanghai.population.town",
                version=f"no-admin-{uuid4()}",
            )
            await _populate(
                session,
                job.asset_version_id,
                _population_records(),
                importer="geojson",
            )
            version = await session.get(
                DataAssetVersion,
                job.asset_version_id,
                with_for_update=True,
            )
            version.status = "validated"
            with pytest.raises(DataAssetDecisionError) as exc_info:
                await DataAssetService().publish_version(
                    session,
                    job.asset_version_id,
                    "publisher",
                    "missing admin town",
                )
    assert exc_info.value.issue.code == "business_key_set_mismatch"


async def test_population_validation_reports_missing_business_keys(
    session_factory,
) -> None:
    await _publish_admin_town_with_keys(
        session_factory,
        set(_town_codes()[:-1]),
    )
    report = await _validate_population_with_keys(
        session_factory,
        tuple(_town_codes()),
    )

    assert report.publishable is False
    assert any(
        issue.code == "business_key_set_mismatch"
        for issue in report.errors
    )


async def test_population_validation_reports_extra_business_keys(
    session_factory,
) -> None:
    await _publish_admin_town_with_keys(
        session_factory,
        set(_town_codes()),
    )
    report = await _validate_population_with_keys(
        session_factory,
        (*_town_codes(), "310115999999"),
    )

    assert report.publishable is False
    assert any(
        issue.code == "business_key_set_mismatch"
        for issue in report.errors
    )


async def test_aggregate_hierarchy_matching_parent_has_no_issue(
    session_factory,
) -> None:
    from tests.data_asset_helpers import _ensure_published_admin_town

    await _ensure_published_admin_town(session_factory)
    await _seed_published_parent_asset(
        session_factory,
        "shanghai.population.county",
        _population_parent_rows(17),
    )
    report = await _validate_population_with_total(session_factory, 100.0)

    assert not any(
        issue.code == "aggregate_difference_exceeded"
        for issue in (*report.errors, *report.warnings)
    )


async def test_aggregate_check_warns_within_tolerance(
    session_factory,
) -> None:
    from tests.data_asset_helpers import _ensure_published_admin_town

    await _ensure_published_admin_town(session_factory)
    await _seed_published_parent_asset(
        session_factory,
        "shanghai.population.county",
        _population_parent_rows(17),
    )
    report = await _validate_population_with_total(session_factory, 100.2)

    assert report.publishable is True
    assert any(
        issue.code == "aggregate_difference_exceeded"
        for issue in report.warnings
    )


async def test_aggregate_check_rejects_excessive_drift(
    session_factory,
) -> None:
    from tests.data_asset_helpers import _ensure_published_admin_town

    await _ensure_published_admin_town(session_factory)
    await _seed_published_parent_asset(
        session_factory,
        "shanghai.population.county",
        _population_parent_rows(17),
    )
    report = await _validate_population_with_total(session_factory, 101.0)

    assert report.publishable is False
    assert any(
        issue.code == "aggregate_difference_exceeded"
        for issue in report.errors
    )


async def test_aggregate_check_uses_parent_not_prior_child_version(
    session_factory,
) -> None:
    from tests.data_asset_helpers import _ensure_published_admin_town

    await _ensure_published_admin_town(session_factory)
    await _publish_population_version_with_total(session_factory, 999.0)
    await _seed_published_parent_asset(
        session_factory,
        "shanghai.population.county",
        _population_parent_rows(17),
    )
    report = await _validate_population_with_total(session_factory, 100.0)

    assert not any(
        issue.code == "aggregate_difference_exceeded"
        for issue in (*report.errors, *report.warnings)
    )


async def test_table_repopulation_same_checksum_is_idempotent(
    session_factory,
) -> None:
    from tests.data_asset_helpers import (
        _population_records,
        _populate,
        _queue_candidate,
    )

    version_id = None
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key="shanghai.population.town",
                version=f"same-pop-{uuid4()}",
            )
            version_id = job.asset_version_id
            await _populate(
                session,
                version_id,
                _population_records(),
                importer="geojson",
            )

    async with session_factory() as session:
        async with session.begin():
            await _populate(
                session,
                version_id,
                _population_records(),
                importer="geojson",
            )
            count = await session.scalar(
                select(func.count())
                .select_from(DataAssetRecord)
                .where(DataAssetRecord.version_id == version_id)
            )
    assert count == 212


async def test_table_repopulation_rejects_different_checksum(
    session_factory,
) -> None:
    from app.data_assets.domain import NormalizedRecord, NormalizedTableData
    from tests.data_asset_helpers import (
        _population_records,
        _populate,
        _queue_candidate,
    )

    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key="shanghai.population.town",
                version=f"diff-pop-{uuid4()}",
            )
            version_id = job.asset_version_id
            await _populate(
                session,
                version_id,
                _population_records(),
                importer="geojson",
            )

    base = _population_records()
    changed = NormalizedTableData(
        base.columns,
        tuple(
            NormalizedRecord(
                record.row_number,
                record.business_key,
                {**record.properties, "total": 101},
            )
            for record in base.records
        ),
        base.source_crs,
        base.spatial_extent,
    )
    async with session_factory() as session:
        async with session.begin():
            with pytest.raises(ValueError, match="checksum"):
                await _populate(
                    session,
                    version_id,
                    changed,
                    importer="geojson",
                )


def _town_codes():
    from tests.data_asset_helpers import _town_codes as helper

    return helper()


async def _publish_admin_town_with_keys(session_factory, keys) -> None:
    from app.data_assets.domain import NormalizedTableData
    from app.data_assets.registry import get_asset_definition
    from tests.data_asset_helpers import (
        FIXTURE_ACTOR,
        _populate,
        _queue_candidate,
        _town_records,
    )

    base = _town_records()
    records = tuple(
        record for record in base.records if record.business_key in keys
    )
    normalized = NormalizedTableData(
        base.columns,
        records,
        base.source_crs,
        base.spatial_extent,
    )
    definition = get_asset_definition("shanghai.admin.town")
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key=definition.asset_key,
                version=f"admin-test-{uuid4()}",
            )
            await _populate(
                session,
                job.asset_version_id,
                normalized,
                importer="geojson",
            )
            service = DataAssetService()
            report = await service.validate_version(
                session,
                job.asset_version_id,
            )
            if not report.publishable:
                raise AssertionError("test admin town version is not publishable")
            await service.publish_version(
                session,
                job.asset_version_id,
                FIXTURE_ACTOR,
                "admin town dependency fixture",
            )


async def _validate_population_with_keys(
    session_factory,
    keys,
):
    from app.data_assets.domain import NormalizedRecord, NormalizedTableData
    from tests.data_asset_helpers import _populate, _queue_candidate

    records = tuple(
        NormalizedRecord(
            row_number=index,
            business_key=key,
            properties={
                "ID": key,
                "NAME": f"town-{index}",
                "total": 100,
                "resident": 70,
                "floating": 20,
                "family": 50,
                "under14": 10,
                "over65": 15,
            },
        )
        for index, key in enumerate(keys, start=1)
    )
    normalized = NormalizedTableData(
        ("ID", "NAME", "family", "floating", "over65", "resident", "total", "under14"),
        records,
        "EPSG:4326",
        None,
    )
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key="shanghai.population.town",
                version=f"population-test-{uuid4()}",
            )
            await _populate(
                session,
                job.asset_version_id,
                normalized,
                importer="geojson",
            )
            return await DataAssetService().validate_version(
                session,
                job.asset_version_id,
            )


async def _validate_population_with_total(
    session_factory,
    total: float,
):
    from app.data_assets.domain import NormalizedRecord, NormalizedTableData
    from tests.data_asset_helpers import (
        _population_records,
        _populate,
        _queue_candidate,
    )

    base = _population_records()
    changed = NormalizedTableData(
        base.columns,
        tuple(
            NormalizedRecord(
                record.row_number,
                record.business_key,
                {**record.properties, "total": total},
            )
            for record in base.records
        ),
        base.source_crs,
        base.spatial_extent,
    )
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key="shanghai.population.town",
                version=f"aggregate-test-{uuid4()}",
            )
            await _populate(
                session,
                job.asset_version_id,
                changed,
                importer="geojson",
            )
            return await DataAssetService().validate_version(
                session,
                job.asset_version_id,
            )


async def _publish_population_version_with_total(
    session_factory,
    total: float,
):
    from app.data_assets.domain import NormalizedRecord, NormalizedTableData
    from tests.data_asset_helpers import (
        FIXTURE_ACTOR,
        _population_records,
        _populate,
        _queue_candidate,
    )

    base = _population_records()
    changed = NormalizedTableData(
        base.columns,
        tuple(
            NormalizedRecord(
                record.row_number,
                record.business_key,
                {**record.properties, "total": total},
            )
            for record in base.records
        ),
        base.source_crs,
        base.spatial_extent,
    )
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_candidate(
                session,
                asset_key="shanghai.population.town",
                version=f"prior-child-{uuid4()}",
            )
            await _populate(
                session,
                job.asset_version_id,
                changed,
                importer="geojson",
            )
            service = DataAssetService()
            report = await service.validate_version(
                session,
                job.asset_version_id,
            )
            if not report.publishable:
                raise AssertionError("prior population version is not publishable")
            await service.publish_version(
                session,
                job.asset_version_id,
                FIXTURE_ACTOR,
                "prior child fixture",
            )


def _population_parent_rows(record_count: int) -> list[dict]:
    totals = {
        "TOTAL": 21200,
        "RESIDENT": 14840,
        "FAMILY": 10600,
        "OVER65": 3180,
        "UNDER14": 2120,
    }
    distributions = {
        field: _distribute_total(value, record_count)
        for field, value in totals.items()
    }
    return [
        {
            "ID": f"parent-{index}",
            **{
                field: values[index]
                for field, values in distributions.items()
            },
        }
        for index in range(record_count)
    ]


def _distribute_total(total: int, record_count: int) -> list[int]:
    base = total // record_count
    remainder = total % record_count
    return [base + 1] * remainder + [base] * (record_count - remainder)


async def _seed_published_parent_asset(
    session_factory,
    asset_key: str,
    rows: list[dict],
) -> None:
    from datetime import UTC, datetime

    from app.data_assets.registry import get_asset_definition
    from tests.data_asset_helpers import FIXTURE_ACTOR, _asset_contract

    definition = get_asset_definition(asset_key)
    async with session_factory() as session:
        async with session.begin():
            asset = await session.scalar(
                select(DataAsset).where(
                    DataAsset.asset_key == asset_key,
                    DataAsset.region_id == definition.region_id,
                )
            )
            if asset is None:
                asset = DataAsset(
                    asset_key=definition.asset_key,
                    region_id=definition.region_id,
                    name=definition.name,
                    data_type=definition.data_type.value,
                    spatial_granularity=definition.spatial_granularity,
                    responsibility_unit=definition.responsibility_unit,
                    update_interval_days=definition.update_interval_days,
                    is_core=definition.is_core,
                    contract=_asset_contract(definition),
                )
                session.add(asset)
                await session.flush()
            now = datetime.now(UTC)
            version = DataAssetVersion(
                asset_id=asset.id,
                version=f"parent-{uuid4()}",
                status="published",
                source_uri="https://example.gov.invalid/parent",
                schema_summary={},
                record_count=len(rows),
                spatial_extent=None,
                source_crs=definition.contract.source_crs,
                checksum="a" * 64,
                imported_by=FIXTURE_ACTOR,
                reviewed_by=FIXTURE_ACTOR,
                imported_at=now,
                validated_at=now,
                published_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(version)
            await session.flush()
            for row_number, row in enumerate(rows, start=1):
                business_key = str(
                    row[definition.contract.business_key_fields[0]]
                )
                session.add(
                    DataAssetRecord(
                        version_id=version.id,
                        row_number=row_number,
                        business_key=business_key,
                        properties=row,
                    )
                )
            await session.flush()
