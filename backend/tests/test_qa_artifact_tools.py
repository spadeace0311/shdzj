from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.artifacts.models import GeneratedArtifact
from app.artifacts.repository import ArtifactProductionRepository
from app.auth.service import AuthUser
from app.db import engine
from app.qa.tools import build_default_registry
from app.qa.tools.artifacts import (
    ArtifactSearchPublishedInput,
    ArtifactSearchPublishedTool,
)
from app.qa.tools.registry import ToolContext


@pytest.fixture(autouse=True)
async def dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


def context_for(seeded_artifact_assessment, session) -> ToolContext:
    return ToolContext(
        session=session,
        user=AuthUser(username="qa-task9", role="viewer", workgroup=None),
        event_id=seeded_artifact_assessment.event_id,
        revision_id=seeded_artifact_assessment.revision_id,
        assessment_run_id=seeded_artifact_assessment.assessment_run_id,
        snapshot_id=uuid4(),
        index_version_id=uuid4(),
    )


async def test_artifact_search_returns_only_current_publications_and_preserves_marker(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    old = await seeded_artifact_assessment.publish_epicenter_artifact()
    current = await seeded_artifact_assessment.published_artifact(
        "map.epicenter",
        version=2,
    )
    test_artifact = await seeded_artifact_assessment.generated_artifact(
        "map.epicenter",
        version=3,
        production_mode="test",
    )
    marker = {"marker": "【测试】", "notice": "演练数据"}
    async with session_factory() as session:
        async with session.begin():
            stored = await session.get(GeneratedArtifact, test_artifact.id)
            assert stored is not None
            stored.marker = marker
            await ArtifactProductionRepository().publish_artifact(
                session,
                stored.id,
                published_by="qa-task9-test",
                forced=False,
            )

    async with session_factory() as session:
        context = context_for(seeded_artifact_assessment, session)
        result = await ArtifactSearchPublishedTool().handle(
            {
                "artifact_key": "map.epicenter",
                "status": "complete",
            },
            context,
        )

    assert result.status == "ok"
    assert [item["artifact_id"] for item in result.value["artifacts"]] == [
        str(current.id),
        str(test_artifact.id),
    ]
    assert old.id not in {
        UUID(item["artifact_id"]) for item in result.value["artifacts"]
    }
    test_result = next(
        item
        for item in result.value["artifacts"]
        if item["production_mode"] == "test"
    )
    assert test_result["marker"] == marker
    assert test_result["checksum"] == test_artifact.checksum
    assert test_result["status"] == "complete"
    assert test_result["quality_grade"] == "A"


async def test_artifact_search_rejects_unknown_catalog_key(
    seeded_artifact_assessment,
    session_factory,
) -> None:
    async with session_factory() as session:
        context = context_for(seeded_artifact_assessment, session)
        result = await ArtifactSearchPublishedTool().handle(
            {"artifact_key": "map.unknown"},
            context,
        )

    assert result.status == "invalid"
    assert result.limitations == ("unknown_artifact_key",)


async def test_default_registry_registers_assessment_and_artifact_tools() -> None:
    registry = build_default_registry()

    for name in (
        "exposure.population",
        "intensity.get",
        "loss.get_metrics",
        "artifact.search_published",
    ):
        definition = registry.get(name)
        assert definition is not None
        assert definition.parallel_safe is False


def test_artifact_search_accepts_persisted_artifact_statuses() -> None:
    assert (
        ArtifactSearchPublishedInput.model_validate(
            {"status": "complete"}
        ).status
        == "complete"
    )
    assert (
        ArtifactSearchPublishedInput.model_validate(
            {"status": "degraded"}
        ).status
        == "degraded"
    )
