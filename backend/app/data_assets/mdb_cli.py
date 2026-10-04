from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

from app.config import settings
from app.data_assets.domain import validate_source_uri
from app.data_assets.import_jobs import (
    QueueImportRequest,
    complete_import_job,
    queue_import_job,
    reject_import_job,
    sanitize_error,
)
from app.data_assets.mdb_importer import MdbAssetImporter
from app.data_assets.registry import get_asset_definition
from app.data_assets.storage import ManagedFileStore, copy_host_file
from app.db import SessionFactory


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.data_assets.mdb_cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("import")
    command.add_argument("--asset-key", required=True)
    command.add_argument("--file", required=True, type=Path)
    command.add_argument("--version", required=True)
    command.add_argument("--source-uri", required=True)
    command.add_argument("--actor", required=True)
    command.add_argument("--storage-root", required=True, type=Path)
    return parser


async def run_mdb_import(
    source_path: Path,
    asset_key: str,
    version: str,
    source_uri: str,
    actor: str,
    storage_root: Path,
) -> UUID:
    validate_source_uri(source_uri)
    definition = get_asset_definition(asset_key)
    normalized = MdbAssetImporter().load(source_path, definition)
    store = ManagedFileStore(
        storage_root,
        max_upload_bytes=settings.data_asset_max_upload_bytes,
    )
    stored = copy_host_file(store, source_path, file_name=source_path.name)
    async with SessionFactory() as session:
        async with session.begin():
            job = await queue_import_job(
                session,
                QueueImportRequest(
                    asset_key=asset_key,
                    version=version,
                    source_uri=source_uri,
                    license_name=None,
                    acquired_at=None,
                    valid_from=None,
                    valid_to=None,
                    change_note="MDB source imported from Windows host",
                    file_name=stored.file_name,
                    file_format="mdb",
                    file_size_bytes=stored.size_bytes,
                    checksum=stored.checksum,
                    relative_path=stored.relative_path,
                    requested_by=actor,
                ),
            )
            if job.asset_version_id is None:
                raise RuntimeError("MDB import job has no candidate version")
            from app.data_assets.service import DataAssetService

            service = DataAssetService()
            await service.populate_candidate_version(
                session,
                job.asset_version_id,
                normalized,
                {
                    "source_table": definition.source_table,
                    "record_count": normalized.record_count,
                    "source_crs": normalized.source_crs,
                    "importer": "mdb",
                },
            )
            report = await service.validate_version(
                session,
                job.asset_version_id,
                actor=actor,
            )
            if not report.publishable:
                await reject_import_job(session, job.id, report)
            else:
                await complete_import_job(
                    session,
                    job.id,
                    job.asset_version_id,
                )
            return job.id


def main() -> None:
    args = _build_parser().parse_args()
    if args.command != "import":
        raise SystemExit("expected the import subcommand")
    try:
        job_id = asyncio.run(
            run_mdb_import(
                source_path=args.file,
                asset_key=args.asset_key,
                version=args.version,
                source_uri=args.source_uri,
                actor=args.actor,
                storage_root=args.storage_root,
            )
        )
    except Exception as error:
        raise SystemExit(sanitize_error(error)) from error
    print(job_id)


if __name__ == "__main__":
    main()
