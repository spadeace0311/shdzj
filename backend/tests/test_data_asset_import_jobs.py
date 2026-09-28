from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.data_assets.domain import (
    AssetVersionStatus,
    ValidationIssue,
    ValidationReport,
)
from app.data_assets.import_jobs import (
    QueueImportRequest,
    claim_next_import_job,
    complete_import_job,
    fail_import_job,
    queue_import_job,
    reject_import_job,
    sanitize_error,
)
from app.data_assets.models import DataAssetAuditLog, DataAssetImportJob


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


def _queue_request(version: str) -> QueueImportRequest:
    return QueueImportRequest(
        asset_key="shanghai.admin.town",
        version=version,
        source_uri="https://example.gov.invalid/town.geojson",
        license_name=None,
        acquired_at=None,
        valid_from=None,
        valid_to=None,
        change_note="initial import",
        file_name="town.geojson",
        file_format="geojson",
        file_size_bytes=12,
        checksum="a" * 64,
        relative_path="aa/aa/aaaaaaaa-aa.geojson",
        requested_by="tester",
    )


async def _queue_job(session, version: str) -> DataAssetImportJob:
    return await queue_import_job(session, _queue_request(version))


async def test_import_queue_claim_is_single_consumer(session_factory) -> None:
    version = f"2022.1-{uuid4()}"
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_job(session, version)
            job_id = job.id

    async with session_factory() as session:
        async with session.begin():
            claimed = await claim_next_import_job(session)
    async with session_factory() as session:
        async with session.begin():
            second = await claim_next_import_job(session)
            stored = await session.get(DataAssetImportJob, job_id)

    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.asset_version_id is not None
    assert second is None
    assert stored.status == "running"


async def test_complete_import_job_sets_terminal_status_and_timestamp(
    session_factory,
) -> None:
    version = f"2022.1-complete-{uuid4()}"
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_job(session, version)
            job_id = job.id
            version_id = job.asset_version_id

    async with session_factory() as session:
        async with session.begin():
            await complete_import_job(session, job_id, version_id)

    async with session_factory() as session:
        stored = await session.get(DataAssetImportJob, job_id)

    assert stored.status == "completed"
    assert stored.asset_version_id == version_id
    assert stored.completed_at is not None
    assert stored.completed_at.tzinfo is not None


async def test_reject_import_job_records_validation_and_audit(
    session_factory,
) -> None:
    version = f"2022.1-reject-{uuid4()}"
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_job(session, version)
            job_id = job.id
            version_id = job.asset_version_id

    report = ValidationReport(
        version_id=str(version_id),
        status=AssetVersionStatus.REJECTED,
        errors=(
            ValidationIssue(
                severity="error",
                code="invalid_field",
                message="bad row",
                row_number=1,
                field_name="ID",
            ),
        ),
        warnings=(
            ValidationIssue(
                severity="warning",
                code="empty_optional",
                message="optional field missing",
            ),
        ),
        statistics={"record_count": 0},
        checked_at=datetime.now(UTC),
    )

    async with session_factory() as session:
        async with session.begin():
            await reject_import_job(session, job_id, report)

    async with session_factory() as session:
        stored = await session.get(DataAssetImportJob, job_id)
        audit = await session.scalar(
            select(DataAssetAuditLog).where(
                DataAssetAuditLog.version_id == version_id,
                DataAssetAuditLog.action == "import_rejected",
            )
        )

    assert stored.status == "rejected"
    assert stored.completed_at is not None
    assert stored.error_summary == "bad row"
    assert stored.validation_errors == [
        {
            "code": "invalid_field",
            "message": "bad row",
            "row_number": 1,
            "field_name": "ID",
        }
    ]
    assert stored.validation_warnings[0]["code"] == "empty_optional"
    assert audit is not None


async def test_fail_import_job_records_sanitized_summary_and_audit(
    session_factory,
) -> None:
    version = f"2022.1-fail-{uuid4()}"
    async with session_factory() as session:
        async with session.begin():
            job = await _queue_job(session, version)
            job_id = job.id
            version_id = job.asset_version_id

    async with session_factory() as session:
        async with session.begin():
            await fail_import_job(
                session,
                job_id,
                ValueError("token=abc123 password = hunter2"),
            )

    async with session_factory() as session:
        stored = await session.get(DataAssetImportJob, job_id)
        audit = await session.scalar(
            select(DataAssetAuditLog).where(
                DataAssetAuditLog.version_id == version_id,
                DataAssetAuditLog.action == "import_failed",
            )
        )

    assert stored.status == "failed"
    assert stored.completed_at is not None
    assert "abc123" not in stored.error_summary
    assert "hunter2" not in stored.error_summary
    assert "[redacted]" in stored.error_summary
    assert audit is not None


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        ("token=abc123", "abc123"),
        ("api_key=secret-value", "secret-value"),
        ("password = hunter2", "hunter2"),
        ("postgres://user:pass@host/db", "pass"),
        ("redis://user:secret@host/0", "secret"),
        ("mongodb+srv://user:pass@host/db", "pass"),
        (
            "Driver={ODBC Driver 17 for SQL Server};Server=host;PWD=secret;",
            "secret",
        ),
        (r"D:\secrets\key.pem", r"D:\secrets\key.pem"),
        (
            "/var/lib/data-assets/private/key.pem",
            "/var/lib/data-assets/private/key.pem",
        ),
    ],
)
def test_sanitize_error_redacts_sensitive_forms(
    message: str,
    forbidden: str,
) -> None:
    result = sanitize_error(ValueError(message))

    assert forbidden not in result
    assert "[redacted]" in result
