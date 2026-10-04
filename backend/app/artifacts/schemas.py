from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ArtifactSummaryResponse(BaseModel):
    artifact_id: UUID
    artifact_key: str
    output_profile: str
    display_name: str
    artifact_version: int
    status: str
    quality_grade: str
    needs_review: bool
    production_mode: str
    publication_mode: str
    file_name: str
    format: str
    size_bytes: int
    generated_at: datetime
    download_url: str
    thumbnail_url: str | None


class ProductionRunResponse(BaseModel):
    production_run_id: UUID
    assessment_run_id: UUID
    status: str
    production_mode: str
    launch_mode: str
    generation_seq: int
    generation_scope: str
    deadline_basis_at: datetime
    deadline_at: datetime
    required_output_count: int
    complete_count: int
    degraded_count: int
    failed_count: int
    timeout_count: int
    needs_review_count: int
    is_current: bool
    artifacts: list[ArtifactSummaryResponse]
    context_fingerprint: str
    catalog_version: str
    template_versions: dict[str, str]
    data_asset_versions: dict[str, str]
    renderer_versions: dict[str, str]
    marker: str | None


class ArtifactRebuildRequest(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
