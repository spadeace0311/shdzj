from pathlib import Path

import pytest

from app.artifacts.catalog import ArtifactCatalog
from app.artifacts.dependencies import ArtifactDependencyGraph
from app.artifacts.domain import (
    ArtifactDefinition,
    ArtifactKind,
    DependencyKind,
    DependencySpec,
)

CATALOG_PATH = Path("/config/artifacts/catalog.yaml")

EXPECTED_ARTIFACT_KEYS = (
    "map.shelter_emergency",
    "map.intensity",
    "map.economic_loss",
    "map.rescue_demand",
    "map.deaths",
    "map.injuries",
    "map.buried",
    "map.material_demand",
    "map.gdp",
    "map.pga_zoning",
    "map.transport",
    "map.historical_earthquakes",
    "map.population",
    "map.reservoirs",
    "map.hazard_sources",
    "map.schools",
    "map.hospitals",
    "map.metro",
    "map.seismic_stations",
    "map.active_faults",
    "map.building_damage",
    "map.building_grid",
    "map.rescue_teams",
    "map.cultural_relics",
    "map.key_targets",
    "map.epicenter",
    "map.city_distances",
    "doc.background",
    "doc.housing",
    "doc.economy",
    "doc.population",
    "doc.key_targets",
    "doc.spatial_distances",
    "doc.area_overview",
    "doc.historical_catalog",
    "doc.rapid_brief",
    "doc.rapid_report",
    "doc.decision_report",
    "deck.decision_report",
)

A_CLASS_MAPS = (
    "map.intensity",
    "map.economic_loss",
    "map.rescue_demand",
    "map.deaths",
    "map.injuries",
    "map.buried",
    "map.material_demand",
    "map.gdp",
    "map.transport",
    "map.historical_earthquakes",
    "map.population",
    "map.hazard_sources",
    "map.schools",
    "map.hospitals",
    "map.active_faults",
    "map.building_damage",
    "map.key_targets",
    "map.epicenter",
    "map.city_distances",
)

B_CLASS_ONLY_MAPS = (
    "map.shelter_emergency",
    "map.pga_zoning",
    "map.reservoirs",
    "map.metro",
    "map.seismic_stations",
    "map.rescue_teams",
    "map.cultural_relics",
)

RAPID_REPORT_PRODUCTS = (
    "intensity.fusion",
    "loss.buildings",
    "loss.population",
    "loss.casualties",
    "loss.economic",
    "loss.resources",
    "loss.validate",
)


def _dependency(
    kind: DependencyKind,
    key: str,
    output_profile: str | None = None,
) -> dict[str, str | None]:
    return DependencySpec(
        kind=kind,
        key=key,
        output_profile=output_profile,
    ).to_dict()


def test_catalog_contains_exactly_39_professional_outputs() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    outputs = catalog.full_required_outputs()

    assert len(outputs) == 39
    assert sum(
        catalog.get(key, "a3v-professional").kind == ArtifactKind.MAP
        for key, _ in outputs
    ) == 27
    assert sum(
        catalog.get(key, "a3v-professional").kind == ArtifactKind.DOCX
        for key, _ in outputs
    ) == 11
    assert sum(
        catalog.get(key, "a3v-professional").kind == ArtifactKind.PPTX
        for key, _ in outputs
    ) == 1
    assert tuple(key for key, _ in outputs) == EXPECTED_ARTIFACT_KEYS
    assert all(profile == "a3v-professional" for _, profile in outputs)
    assert all(
        catalog.get(key, profile).format in {"jpg", "png", "docx", "pptx"}
        for key, profile in outputs
    )


def test_catalog_locks_legacy_codes_and_map_output_specifications() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    outputs = catalog.full_required_outputs()

    expected_codes = tuple(
        [f"M{index:02d}" for index in range(1, 28)]
        + [f"D{index:02d}" for index in range(1, 13)]
    )
    assert tuple(
        catalog.get(key, profile).legacy_code for key, profile in outputs
    ) == expected_codes

    for key in EXPECTED_ARTIFACT_KEYS[:27]:
        definition = catalog.get(key, "a3v-professional")
        assert definition.format == "jpg"
        assert definition.dimensions == (4761, 3369)
        assert definition.dpi == 300

    for key in EXPECTED_ARTIFACT_KEYS[27:36]:
        assert catalog.get(key, "a3v-professional").format == "docx"
    assert catalog.get("deck.decision_report", "a3v-professional").format == "pptx"


def test_catalog_dependencies_are_typed_and_acyclic() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    decision = catalog.get("doc.decision_report", "a3v-professional")

    assert _dependency(
        DependencyKind.ARTIFACT,
        "doc.rapid_report",
        "a3v-professional",
    ) in decision.depends_on
    assert catalog.assert_acyclic() is None


def test_catalog_has_exact_assessment_product_dependencies_for_maps() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    expected = {
        "map.intensity": ("intensity.fusion",),
        "map.economic_loss": ("loss.economic",),
        "map.rescue_demand": ("loss.resources",),
        "map.deaths": ("loss.casualties",),
        "map.injuries": ("loss.casualties",),
        "map.buried": ("loss.casualties",),
        "map.material_demand": ("loss.resources",),
        "map.building_damage": ("loss.buildings",),
        "map.building_grid": ("loss.buildings",),
    }

    for key in EXPECTED_ARTIFACT_KEYS[:27]:
        definition = catalog.get(key, "a3v-professional")
        actual = tuple(
            dependency.to_dict()
            for dependency in definition.depends_on
            if dependency.kind == DependencyKind.ASSESSMENT_PRODUCT
        )
        assert actual == tuple(
            _dependency(DependencyKind.ASSESSMENT_PRODUCT, product_key)
            for product_key in expected.get(key, ())
        )


def test_catalog_has_exact_document_dependencies() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)

    def hard(key: str) -> set[tuple[str, str, str | None]]:
        return {
            (dependency.kind.value, dependency.key, dependency.output_profile)
            for dependency in catalog.get(
                key,
                "a3v-professional",
            ).depends_on
        }

    profile = "a3v-professional"
    assert hard("doc.background") == set()
    assert hard("doc.housing") == {
        ("assessment_product", "loss.buildings", None),
        ("assessment_product", "loss.population", None),
    }
    assert hard("doc.economy") == {("assessment_product", "loss.economic", None)}
    assert hard("doc.population") == {("assessment_product", "loss.population", None)}
    assert hard("doc.area_overview") == {
        ("artifact", "doc.background", profile),
        ("artifact", "doc.housing", profile),
        ("artifact", "doc.economy", profile),
        ("artifact", "doc.population", profile),
        ("artifact", "doc.key_targets", profile),
        ("artifact", "doc.spatial_distances", profile),
    }
    assert hard("doc.rapid_brief") == {
        ("assessment_product", "intensity.fusion", None),
        ("assessment_product", "loss.buildings", None),
        ("assessment_product", "loss.population", None),
        ("assessment_product", "loss.casualties", None),
        ("assessment_product", "loss.economic", None),
        ("assessment_product", "loss.resources", None),
        ("assessment_product", "loss.validate", None),
        ("artifact", "map.intensity", profile),
        ("artifact", "map.deaths", profile),
        ("artifact", "map.injuries", profile),
        ("artifact", "map.population", profile),
        ("artifact", "map.building_damage", profile),
        ("artifact", "map.epicenter", profile),
    }
    assert hard("doc.rapid_report") == {
        *{
            ("assessment_product", product_key, None)
            for product_key in RAPID_REPORT_PRODUCTS
        },
        ("artifact", "doc.background", profile),
        ("artifact", "doc.housing", profile),
        ("artifact", "doc.economy", profile),
        ("artifact", "doc.population", profile),
        ("artifact", "doc.key_targets", profile),
        ("artifact", "doc.spatial_distances", profile),
        ("artifact", "doc.area_overview", profile),
        ("artifact", "doc.historical_catalog", profile),
        *{("artifact", key, profile) for key in A_CLASS_MAPS},
    }
    assert hard("doc.decision_report") == {
        ("artifact", "doc.rapid_brief", profile),
        ("artifact", "doc.rapid_report", profile),
        ("artifact", "map.intensity", profile),
        ("artifact", "map.economic_loss", profile),
        ("artifact", "map.rescue_demand", profile),
        ("artifact", "map.deaths", profile),
        ("artifact", "map.injuries", profile),
        ("artifact", "map.buried", profile),
        ("artifact", "map.material_demand", profile),
        ("artifact", "map.active_faults", profile),
        ("artifact", "map.key_targets", profile),
        ("artifact", "map.epicenter", profile),
        ("artifact", "map.city_distances", profile),
    }
    assert hard("deck.decision_report") == {
        ("artifact", "doc.decision_report", profile),
    }


def test_catalog_encodes_optional_and_conditional_degradation_rules() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    reported_optional = {
        dependency.key
        for dependency in catalog.get(
            "doc.rapid_report",
            "a3v-professional",
        ).optional_depends_on
    }
    assert reported_optional == set(B_CLASS_ONLY_MAPS) | {"map.building_grid"}

    for key in A_CLASS_MAPS:
        definition = catalog.get(key, "a3v-professional")
        assert definition.failure_policy == "block"

    for key in B_CLASS_ONLY_MAPS:
        definition = catalog.get(key, "a3v-professional")
        assert definition.failure_policy == "degrade"

    building_grid = catalog.get("map.building_grid", "a3v-professional")
    assert building_grid.quality_policy == "C"
    assert building_grid.failure_policy == "degrade"
    assert building_grid.required_assets == (
        "shanghai.admin.town",
        "shanghai.building.town",
        "basemap.gaode.offline",
    )
    assert "spatial_allocation_rule" not in building_grid.required_assets
    assert building_grid.allows_degraded_output(spatialized_estimate=False) is False
    assert building_grid.allows_degraded_output(spatialized_estimate=True) is True

    transport = catalog.get("map.transport", "a3v-professional")
    assert "shanghai.road.network" in transport.optional_assets
    assert transport.allows_degraded_output() is False
    assert transport.allows_degraded_output(
        condition="optional_asset_missing"
    ) is True
    assert transport.allows_degraded_output(
        condition="required_asset_missing"
    ) is False

    for key in set(A_CLASS_MAPS) - {"map.transport"}:
        definition = catalog.get(key, "a3v-professional")
        assert definition.allows_degraded_output(
            condition="optional_asset_missing"
        ) is False


def test_dependency_cycle_detection_includes_optional_dependencies() -> None:
    def definition(
        key: str,
        optional_depends_on: tuple[DependencySpec, ...] = (),
    ) -> ArtifactDefinition:
        return ArtifactDefinition(
            artifact_key=key,
            display_name=key,
            kind=ArtifactKind.MAP,
            output_profile="test-profile",
            format="jpg",
            legacy_code=key,
            priority=1,
            depends_on=(),
            optional_depends_on=optional_depends_on,
            required_assets=(),
            optional_assets=(),
            template_key=key,
            marker_policy="mode",
            failure_policy="degrade",
            quality_policy="B",
        )

    catalog = ArtifactCatalog(
        catalog_version="test",
        definitions=(
            definition(
                "map.left",
                (
                    DependencySpec(
                        DependencyKind.ARTIFACT,
                        "map.right",
                        "test-profile",
                    ),
                ),
            ),
            definition(
                "map.right",
                (
                    DependencySpec(
                        DependencyKind.ARTIFACT,
                        "map.left",
                        "test-profile",
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="cycle"):
        catalog.assert_acyclic()


def test_catalog_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    catalog_text = CATALOG_PATH.read_text(encoding="utf-8")
    artifact_marker = "\n  map.epicenter:\n"
    assert catalog_text.count(artifact_marker) == 1
    duplicate = catalog_text.replace(
        artifact_marker,
        (
            f"{artifact_marker}"
            "    display_name: 重复项\n\n"
            "  map.epicenter:\n"
        ),
        1,
    )
    invalid_catalog = tmp_path / "duplicate.yaml"
    invalid_catalog.write_text(duplicate, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        ArtifactCatalog.load(invalid_catalog)

    message = str(error.value)
    assert "duplicate key 'map.epicenter'" in message
    assert "line" in message


def test_catalog_rejects_non_string_artifact_keys(tmp_path: Path) -> None:
    catalog_text = CATALOG_PATH.read_text(encoding="utf-8")
    invalid_catalog_text = catalog_text.replace(
        "artifacts:\n",
        (
            "artifacts:\n"
            "  123:\n"
            "    display_name: invalid\n"
        ),
        1,
    )
    invalid_catalog = tmp_path / "non-string-key.yaml"
    invalid_catalog.write_text(invalid_catalog_text, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        ArtifactCatalog.load(invalid_catalog)

    message = str(error.value)
    assert "artifacts key at index 0" in message
    assert "123" in message


@pytest.mark.parametrize(
    ("yaml_value", "label"),
    (
        ("null", "null"),
        ('""', "empty"),
        ("unsupported", "unsupported"),
    ),
)
def test_catalog_rejects_missing_or_invalid_marker_policy(
    tmp_path: Path,
    yaml_value: str,
    label: str,
) -> None:
    catalog_text = CATALOG_PATH.read_text(encoding="utf-8")
    invalid_catalog_text = catalog_text.replace(
        "marker_policy: mode",
        f"marker_policy: {yaml_value}",
        1,
    )
    invalid_catalog = tmp_path / f"marker-policy-{label}.yaml"
    invalid_catalog.write_text(invalid_catalog_text, encoding="utf-8")

    with pytest.raises(ValueError, match=r"marker_policy"):
        ArtifactCatalog.load(invalid_catalog)


def test_scope_parser_rejects_ambiguous_artifact_scope() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    assert catalog.scope_required_outputs("full", "a3v-professional") == (
        catalog.full_required_outputs()
    )
    assert catalog.scope_required_outputs(
        "artifact:map.epicenter:a3v-professional",
        "a3v-professional",
    ) == (("map.epicenter", "a3v-professional"),)
    with pytest.raises(ValueError):
        catalog.scope_required_outputs("artifact:map.epicenter", "a3v-professional")
    with pytest.raises(ValueError):
        catalog.scope_required_outputs(
            "artifact:map.unknown:a3v-professional",
            "a3v-professional",
        )


def test_dependency_graph_ready_keys_respects_required_outputs_and_statuses() -> None:
    catalog = ArtifactCatalog.load(CATALOG_PATH)
    graph = ArtifactDependencyGraph(catalog)

    assert graph.ready_keys({"map.epicenter"}, set()) == ("map.epicenter",)
    assert graph.ready_keys({"map.intensity"}, {"intensity.fusion"}) == ("map.intensity",)
    assert graph.ready_keys({"map.intensity"}, {"intensity.fusion": "failed"}) == ()
    assert graph.ready_keys({"map.intensity"}, {"intensity.fusion": "timed_out"}) == ()
    assert graph.ready_keys({"map.intensity"}, {"intensity.fusion:failed"}) == ()
    assert graph.ready_keys(
        {"map.intensity", "map.epicenter"},
        {"intensity.fusion": "succeeded"},
    ) == ("map.intensity", "map.epicenter")
