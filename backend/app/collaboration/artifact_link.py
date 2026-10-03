from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts.models import ArtifactPublication, GeneratedArtifact
from app.collaboration.models import (
    CollaborationOutbox,
    CollaborationTaskTemplateVersion,
    TaskDeliverable,
    TaskDeliverableVersion,
    WorkgroupTask,
)


@dataclass(frozen=True, slots=True)
class ArtifactLinkResult:
    linked_count: int = 0
    created_deliverable_count: int = 0
    outbox_count: int = 0

    @property
    def changed(self) -> bool:
        return self.linked_count > 0 or self.created_deliverable_count > 0


class ArtifactLinkService:
    async def sync_run_publications(
        self,
        session: AsyncSession,
        production_run_id: object,
    ) -> ArtifactLinkResult:
        run_id = _coerce_uuid(production_run_id, "production_run_id")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": _advisory_lock_key(run_id)},
        )
        publication_rows = (
            await session.execute(
                select(ArtifactPublication, GeneratedArtifact)
                .join(
                    GeneratedArtifact,
                    GeneratedArtifact.id == ArtifactPublication.artifact_id,
                )
                .where(ArtifactPublication.production_run_id == run_id)
                .order_by(ArtifactPublication.id)
            )
        ).all()
        if not publication_rows:
            return ArtifactLinkResult()

        event_ids = {publication.event_id for publication, _ in publication_rows}
        task_rows = (
            await session.execute(
                select(WorkgroupTask, CollaborationTaskTemplateVersion)
                .join(
                    CollaborationTaskTemplateVersion,
                    CollaborationTaskTemplateVersion.id
                    == WorkgroupTask.template_version_id,
                )
                .where(WorkgroupTask.event_id.in_(event_ids))
                .order_by(WorkgroupTask.created_at, WorkgroupTask.id)
            )
        ).all()
        task_ids = [task.id for task, _ in task_rows]
        existing_deliverables = (
            await session.scalars(
                select(TaskDeliverable).where(
                    TaskDeliverable.task_id.in_(task_ids)
                )
            )
        ).all()
        deliverables_by_task_code: dict[
            tuple[uuid.UUID, str],
            TaskDeliverable,
        ] = {}
        task_by_id = {task.id: task for task, _ in task_rows}
        for deliverable in existing_deliverables:
            deliverables_by_task_code[
                (deliverable.task_id, deliverable.deliverable_code)
            ] = deliverable

        created_deliverable_count = 0
        changed_event_ids: set[uuid.UUID] = set()
        for task, template_version in task_rows:
            next_display_order = _next_display_order(
                existing_deliverables,
                task.id,
            )
            for binding in _binding_items(template_version.artifact_bindings):
                artifact_key = _binding_value(binding, "artifact_key")
                output_profile = _binding_value(binding, "output_profile")
                if not artifact_key or not output_profile:
                    continue
                code = str(artifact_key)
                key = (task.id, code)
                if key in deliverables_by_task_code:
                    continue
                deliverable = TaskDeliverable(
                    task_id=task.id,
                    deliverable_code=code,
                    title=code,
                    is_required=True,
                    requirement_kind="automatic_artifact",
                    artifact_binding={
                        "artifact_key": artifact_key,
                        "output_profile": output_profile,
                    },
                    display_order=next_display_order,
                )
                session.add(deliverable)
                deliverables_by_task_code[key] = deliverable
                created_deliverable_count += 1
                changed_event_ids.add(task.event_id)
                next_display_order += 1

            for required_code in _required_deliverable_items(
                template_version.required_deliverables
            ):
                code = str(required_code)
                key = (task.id, code)
                if key in deliverables_by_task_code:
                    continue
                deliverable = TaskDeliverable(
                    task_id=task.id,
                    deliverable_code=code,
                    title=code,
                    is_required=True,
                    requirement_kind="manual_file_or_text",
                    artifact_binding=None,
                    display_order=next_display_order,
                )
                session.add(deliverable)
                deliverables_by_task_code[key] = deliverable
                created_deliverable_count += 1
                changed_event_ids.add(task.event_id)
                next_display_order += 1

        if created_deliverable_count:
            await session.flush()

        publication_ids = [
            publication.id for publication, _ in publication_rows
        ]
        existing_versions = (
            await session.scalars(
                select(TaskDeliverableVersion).where(
                    TaskDeliverableVersion.artifact_publication_id.in_(
                        publication_ids
                    )
                )
            )
        ).all()
        existing_version_keys = {
            (version.deliverable_id, version.artifact_publication_id)
            for version in existing_versions
        }

        linked_count = 0
        created_versions: list[TaskDeliverableVersion] = []
        for publication, artifact in publication_rows:
            matching_deliverables = [
                deliverable
                for task in task_by_id.values()
                if task.event_id == publication.event_id
                for deliverable in (
                    deliverables_by_task_code.get(
                        (task.id, artifact.artifact_key),
                    ),
                )
                if deliverable is not None
                and deliverable.requirement_kind == "automatic_artifact"
                and _binding_value(
                    deliverable.artifact_binding or {},
                    "artifact_key",
                )
                == artifact.artifact_key
                and _binding_value(
                    deliverable.artifact_binding or {},
                    "output_profile",
                )
                == artifact.output_profile
            ]
            for deliverable in matching_deliverables:
                identity = (deliverable.id, publication.id)
                if identity in existing_version_keys:
                    continue
                await session.get(
                    TaskDeliverable,
                    deliverable.id,
                    with_for_update=True,
                )
                previous = await session.scalar(
                    select(TaskDeliverableVersion)
                    .where(
                        TaskDeliverableVersion.deliverable_id
                        == deliverable.id
                    )
                    .order_by(TaskDeliverableVersion.version_no.desc())
                    .limit(1)
                )
                version = TaskDeliverableVersion(
                    deliverable_id=deliverable.id,
                    version_no=(previous.version_no + 1 if previous else 1),
                    source_kind="automatic",
                    artifact_id=artifact.id,
                    artifact_publication_id=publication.id,
                    storage_key=None,
                    file_name=None,
                    checksum=artifact.checksum,
                    mime_type=None,
                    size_bytes=None,
                    text_result=None,
                    created_by="system",
                    basis_text=None,
                    supersedes_version_id=(
                        previous.id if previous is not None else None
                    ),
                )
                session.add(version)
                created_versions.append(version)
                linked_count += 1
                changed_event_ids.add(publication.event_id)

        if created_versions:
            await session.flush()

        outbox_count = 0
        if changed_event_ids:
            now = datetime.now(UTC)
            for event_id in sorted(changed_event_ids, key=str):
                session.add(
                    CollaborationOutbox(
                        event_id=event_id,
                        task_id=None,
                        event_type="artifact_linked",
                        idempotency_key=_outbox_key(event_id, run_id),
                        payload={
                            "event_id": str(event_id),
                            "production_run_id": str(run_id),
                            "linked_count": linked_count,
                            "created_deliverable_count": (
                                created_deliverable_count
                            ),
                        },
                        status="pending",
                        attempt_count=0,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                outbox_count += 1

        return ArtifactLinkResult(
            linked_count=linked_count,
            created_deliverable_count=created_deliverable_count,
            outbox_count=outbox_count,
        )


def _coerce_uuid(value: object, field_name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _binding_items(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _required_deliverable_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _binding_value(binding: Any, key: str) -> str | None:
    if not isinstance(binding, dict):
        return None
    value = binding.get(key)
    if value is None:
        return None
    return str(value)


def _next_display_order(
    deliverables: list[TaskDeliverable],
    task_id: uuid.UUID,
) -> int:
    values = [
        deliverable.display_order
        for deliverable in deliverables
        if deliverable.task_id == task_id
    ]
    return max(values, default=-1) + 1


def _outbox_key(event_id: uuid.UUID, production_run_id: uuid.UUID) -> str:
    material = f"artifact-link:{event_id}:{production_run_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _advisory_lock_key(production_run_id: uuid.UUID) -> str:
    return f"artifact-link:{production_run_id}"
