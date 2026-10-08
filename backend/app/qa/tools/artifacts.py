from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.artifacts.catalog import load_catalog
from app.artifacts.models import ArtifactPublication, GeneratedArtifact
from app.config import settings
from app.qa.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)


class ArtifactSearchPublishedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None
    artifact_key: str | None = Field(default=None, max_length=160)
    output_profile: str | None = Field(default=None, max_length=64)
    production_mode: Literal[
        "live",
        "manual",
        "test",
        "drill",
        "replay",
    ] | None = None
    status: Literal[
        "complete",
        "degraded",
        "failed",
    ] | None = None


class ArtifactSearchPublishedTool:
    async def handle(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        requested_event_id = arguments.get("event_id")
        if (
            requested_event_id is not None
            and context.event_id is not None
            and str(requested_event_id) != str(context.event_id)
        ):
            return ToolResult.invalid(
                limitations=("event_context_mismatch",),
                parameters={"event_id": str(requested_event_id)},
            )
        event_id = context.event_id or requested_event_id
        if event_id is None:
            return ToolResult.not_found(limitations=("event_required",))

        catalog = load_catalog(settings.artifact_catalog_path)
        artifact_key = arguments.get("artifact_key")
        if artifact_key is not None and artifact_key not in {
            definition.artifact_key for definition in catalog.definitions
        }:
            return ToolResult.invalid(
                limitations=("unknown_artifact_key",),
                parameters={
                    "event_id": str(event_id),
                    "artifact_key": artifact_key,
                },
            )

        statement = (
            select(ArtifactPublication, GeneratedArtifact)
            .join(
                GeneratedArtifact,
                GeneratedArtifact.id == ArtifactPublication.artifact_id,
            )
            .where(ArtifactPublication.event_id == event_id)
        )
        if context.artifact_production_run_id is not None:
            statement = statement.where(
                ArtifactPublication.production_run_id
                == context.artifact_production_run_id
            )
        else:
            statement = statement.where(
                ArtifactPublication.superseded_at.is_(None)
            )
        if artifact_key is not None:
            statement = statement.where(
                ArtifactPublication.artifact_key == artifact_key
            )
        if arguments.get("output_profile") is not None:
            statement = statement.where(
                ArtifactPublication.output_profile
                == arguments["output_profile"]
            )
        if arguments.get("production_mode") is not None:
            statement = statement.where(
                ArtifactPublication.production_mode
                == arguments["production_mode"]
            )
        if arguments.get("status") is not None:
            statement = statement.where(
                GeneratedArtifact.status == arguments["status"]
            )

        rows = (
            await context.session.execute(
                statement.order_by(
                    ArtifactPublication.artifact_key,
                    ArtifactPublication.output_profile,
                    ArtifactPublication.production_mode,
                    GeneratedArtifact.artifact_version.desc(),
                )
            )
        ).all()
        if not rows:
            return ToolResult.not_found(
                source="artifact_publications",
                version=catalog.catalog_version,
                parameters={
                    "event_id": str(event_id),
                    "artifact_key": artifact_key,
                    "output_profile": arguments.get("output_profile"),
                    "production_mode": arguments.get("production_mode"),
                    "status": arguments.get("status"),
                    "artifact_production_run_id": (
                        str(context.artifact_production_run_id)
                        if context.artifact_production_run_id is not None
                        else None
                    ),
                },
                limitations=("published_artifact_not_found",),
            )

        artifacts = [
            _artifact_payload(publication, artifact)
            for publication, artifact in rows
        ]
        return ToolResult.ok(
            value={
                "event_id": str(event_id),
                "catalog_version": catalog.catalog_version,
                "artifacts": artifacts,
                "count": len(artifacts),
            },
            source="artifact_publications",
            version=catalog.catalog_version,
            parameters={
                "event_id": str(event_id),
                "artifact_key": artifact_key,
                "output_profile": arguments.get("output_profile"),
                "production_mode": arguments.get("production_mode"),
                "status": arguments.get("status"),
                "artifact_production_run_id": (
                    str(context.artifact_production_run_id)
                    if context.artifact_production_run_id is not None
                    else None
                ),
            },
        )


def register_artifact_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="artifact.search_published",
            description="查询事件当前未失效的专业图件、报告和演示成果",
            input_model=ArtifactSearchPublishedInput,
            handler=ArtifactSearchPublishedTool().handle,
            timeout_seconds=1.5,
            parallel_safe=False,
        )
    )


def _artifact_payload(
    publication: ArtifactPublication,
    artifact: GeneratedArtifact,
) -> dict[str, Any]:
    return {
        "publication_id": str(publication.id),
        "artifact_id": str(artifact.id),
        "artifact_key": publication.artifact_key,
        "output_profile": publication.output_profile,
        "artifact_version": artifact.artifact_version,
        "file_name": artifact.file_name,
        "format": artifact.format,
        "checksum": artifact.checksum,
        "size_bytes": artifact.size_bytes,
        "generated_at": artifact.generated_at.isoformat(),
        "published_at": publication.published_at.isoformat(),
        "production_mode": publication.production_mode,
        "publication_mode": artifact.publication_mode,
        "status": artifact.status,
        "quality_grade": artifact.quality_grade,
        "needs_review": artifact.needs_review,
        "marker": artifact.marker,
    }
