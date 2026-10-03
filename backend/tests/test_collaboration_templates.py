import json
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from types import SimpleNamespace

import pytest

from app.artifacts.catalog import load_catalog
from app.artifacts.domain import ArtifactKind
from app.collaboration.domain import WorkgroupCode
from app.collaboration.templates import (
    ArtifactBinding,
    TaskTemplateCatalog,
    TaskTemplateDefinition,
    load_task_template_catalog,
)
from tests.collaboration_expectations import EXPECTED_ARTIFACT_OWNERS

CATALOG_PATH = Path("/config/collaboration/shanghai-2026-tasks.yaml")


def test_catalog_covers_all_groups_and_preplan_phases() -> None:
    catalog = load_task_template_catalog(CATALOG_PATH)

    assert {item.workgroup_code for item in catalog.definitions} == {
        "news_information",
        "monitoring_forecast",
        "comprehensive_coordination",
        "damage_assessment",
        "emergency_technology",
        "logistics",
        "center_station",
    }
    assert catalog.version == "shanghai-2026.1"
    assert len(catalog.definitions) == 61
    assert catalog.get("technology.rapid_brief").artifact_bindings == (
        ArtifactBinding("doc.rapid_brief", "a3v-professional"),
    )
    assert catalog.get("technology.rapid_special").due_offset_seconds == 3600
    assert catalog.get("technology.intensity_map").continues_until_response_end
    assert catalog.get("technology.professional_outputs").artifact_bindings == (
        ArtifactBinding("map.shelter_emergency", "a3v-professional"),
    )
    assert catalog.get("technology.rapid_brief").required_deliverables == ()


def test_catalog_artifact_bindings_exist_in_artifact_catalog() -> None:
    task_catalog = load_task_template_catalog(CATALOG_PATH)
    artifact_catalog = load_catalog(Path("/config/artifacts/catalog.yaml"))
    for definition in task_catalog.definitions:
        for binding in definition.artifact_bindings:
            artifact_catalog.get(binding.artifact_key, binding.output_profile)


def test_catalog_snapshot_round_trips_for_outbox_jsonb() -> None:
    catalog = TaskTemplateCatalog(
        version="snapshot.1",
        definitions=(
            TaskTemplateDefinition(
                template_code="snapshot.task",
                workgroup_code=WorkgroupCode.EMERGENCY_TECHNOLOGY,
                phase_code="within_30m",
                title="snapshot task",
                source="test",
                required_deliverables=("manual.result",),
                artifact_bindings=(
                    ArtifactBinding("doc.rapid_brief", "a3v-professional"),
                ),
                applicability=MappingProxyType(
                    {"minimum_max_intensity": Decimal("2.0")}
                ),
                due_offset_seconds=300,
                response_basis="test",
            ),
        ),
    )

    snapshot = catalog.to_snapshot()
    json.dumps(snapshot)

    assert TaskTemplateCatalog.from_snapshot(snapshot) == catalog


def test_catalog_binds_each_professional_artifact_to_intended_task() -> None:
    task_catalog = load_task_template_catalog(CATALOG_PATH)
    artifact_catalog = load_catalog(Path("/config/artifacts/catalog.yaml"))
    bindings = {
        (
            definition.template_code,
            binding.artifact_key,
            binding.output_profile,
        )
        for definition in task_catalog.definitions
        for binding in definition.artifact_bindings
    }
    artifact_keys = {
        definition.artifact_key for definition in artifact_catalog.definitions
    }
    map_keys = {
        definition.artifact_key
        for definition in artifact_catalog.definitions
        if definition.kind is ArtifactKind.MAP
    }
    expected_bindings = {
        (task_code, artifact_key, "a3v-professional")
        for task_code, artifact_keys in EXPECTED_ARTIFACT_OWNERS.items()
        for artifact_key in artifact_keys
    }
    bound_keys = {artifact_key for _, artifact_key, _ in bindings}
    rapid_brief_owners = {
        task_code
        for task_code, artifact_key, _ in bindings
        if artifact_key == "doc.rapid_brief"
    }

    assert len(map_keys) == 27
    assert bound_keys == artifact_keys
    assert bindings == expected_bindings
    assert rapid_brief_owners == {"technology.rapid_brief"}


def test_catalog_applicability_splits_in_scope_and_out_of_scope_events() -> None:
    catalog = load_task_template_catalog(CATALOG_PATH)
    event = SimpleNamespace(event_type="formal")
    in_scope_revision = SimpleNamespace(
        revision_kind="formal",
        inside_shanghai=True,
        distance_to_boundary_km=None,
    )
    out_of_scope_revision = SimpleNamespace(
        revision_kind="formal",
        inside_shanghai=False,
        distance_to_boundary_km=50,
    )
    low_intensity_revision = SimpleNamespace(
        revision_kind="formal",
        inside_shanghai=True,
        distance_to_boundary_km=None,
        response_suggestion={"max_intensity": "1.0"},
    )

    in_scope = catalog.get_applicable(event, in_scope_revision)
    in_scope_with_threshold = catalog.get_applicable(
        event,
        in_scope_revision,
        intensity_threshold="2.0",
    )
    out_of_scope = catalog.get_applicable(event, out_of_scope_revision)
    low_intensity = catalog.get_applicable(
        event,
        low_intensity_revision,
        intensity_threshold="2.0",
    )

    assert len(in_scope) == 60
    assert len(in_scope_with_threshold) == 60
    assert "news.external_event_record_notify" not in {
        definition.template_code for definition in in_scope
    }
    assert [definition.template_code for definition in out_of_scope] == [
        "news.external_event_record_notify"
    ]
    assert [definition.template_code for definition in low_intensity] == [
        "news.external_event_record_notify"
    ]


@pytest.mark.parametrize(
    ("inside_shanghai", "distance_to_boundary_km", "expected"),
    (
        (
            True,
            None,
            {
                "selector.default",
                "selector.any",
                "selector.inside_shanghai",
            },
        ),
        (
            False,
            10,
            {
                "selector.default",
                "selector.any",
                "selector.boundary_20km",
                "selector.outside_shanghai",
            },
        ),
        (
            False,
            50,
            {
                "selector.any",
                "selector.outside_shanghai",
                "selector.outside_assessment_scope",
            },
        ),
    ),
)
def test_spatial_selectors_do_not_apply_the_default_scope_gate(
    inside_shanghai: bool,
    distance_to_boundary_km: int | None,
    expected: set[str],
) -> None:
    catalog = TaskTemplateCatalog(
        version="spatial-selector-test",
        definitions=(
            _selector_definition("default"),
            _selector_definition("any"),
            _selector_definition("inside_shanghai"),
            _selector_definition("boundary_20km"),
            _selector_definition("outside_shanghai"),
            _selector_definition("outside_assessment_scope"),
        ),
    )
    event = SimpleNamespace(event_type="formal")
    revision = SimpleNamespace(
        revision_kind="formal",
        inside_shanghai=inside_shanghai,
        distance_to_boundary_km=distance_to_boundary_km,
    )

    applicable = catalog.get_applicable(event, revision)

    assert {definition.template_code for definition in applicable} == expected


def _selector_definition(spatial_class: str) -> TaskTemplateDefinition:
    applicability = (
        {}
        if spatial_class == "default"
        else {"spatial_class": spatial_class}
    )
    return TaskTemplateDefinition(
        template_code=f"selector.{spatial_class}",
        workgroup_code=WorkgroupCode.NEWS_INFORMATION,
        phase_code="within_30m",
        title=spatial_class,
        source="test",
        applicability=applicability,
    )


@pytest.mark.parametrize(
    ("original", "replacement", "message"),
    (
        ("group: news_information", "group: unknown_group", "unknown workgroup"),
        ("phase: within_30m", "phase: unknown_phase", "unknown phase"),
        (
            "[doc.rapid_brief, a3v-professional]",
            "[doc.unknown, a3v-professional]",
            "unknown artifact",
        ),
        (
            "start_offset_seconds: 1800",
            "start_offset_seconds: -1",
            "must not be negative",
        ),
        (
            "start_offset_seconds: 1800\n    due_offset_seconds: 3600",
            "start_offset_seconds: 1800\n    due_offset_seconds: 1799",
            "due_offset_seconds must be greater than or equal to",
        ),
    ),
)
def test_catalog_rejects_invalid_task_metadata(
    tmp_path: Path,
    original: str,
    replacement: str,
    message: str,
) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(
        CATALOG_PATH.read_text(encoding="utf-8").replace(
            original,
            replacement,
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_task_template_catalog(invalid)


def test_catalog_rejects_duplicate_task_codes(tmp_path: Path) -> None:
    invalid = tmp_path / "duplicate.yaml"
    invalid.write_text(
        CATALOG_PATH.read_text(encoding="utf-8")
        + """
  - code: news.takeover_duty
    group: news_information
    phase: within_30m
    title: duplicate
    source: test
    due_offset_seconds: 1800
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate task code"):
        load_task_template_catalog(invalid)
