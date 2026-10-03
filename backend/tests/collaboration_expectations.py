from __future__ import annotations


EXPECTED_ARTIFACT_OWNERS: dict[str, tuple[str, ...]] = {
    "coordination.response_suggestion": (
        "doc.decision_report",
        "deck.decision_report",
    ),
    "damage.background_materials": (
        "doc.background",
        "doc.housing",
        "doc.economy",
        "doc.population",
        "doc.key_targets",
        "doc.spatial_distances",
        "doc.area_overview",
        "doc.historical_catalog",
        "map.gdp",
        "map.pga_zoning",
        "map.transport",
        "map.population",
        "map.reservoirs",
        "map.hazard_sources",
        "map.schools",
        "map.hospitals",
        "map.metro",
        "map.active_faults",
        "map.cultural_relics",
        "map.key_targets",
    ),
    "damage.intensity_map": ("map.intensity",),
    "damage.loss_assessment": (
        "map.economic_loss",
        "map.rescue_demand",
        "map.deaths",
        "map.injuries",
        "map.buried",
        "map.material_demand",
        "map.building_damage",
        "map.building_grid",
        "map.rescue_teams",
    ),
    "monitoring.epicenter_maps": (
        "map.historical_earthquakes",
        "map.intensity",
        "map.epicenter",
        "map.city_distances",
    ),
    "monitoring.station_and_mechanism": ("map.seismic_stations",),
    "technology.professional_outputs": ("map.shelter_emergency",),
    "technology.rapid_brief": ("doc.rapid_brief",),
    "technology.rapid_special": ("doc.rapid_report",),
}

EXPECTED_TASK_OUTPUT_PAIRS = frozenset(
    (task_code, deliverable_code)
    for task_code, deliverable_codes in EXPECTED_ARTIFACT_OWNERS.items()
    for deliverable_code in deliverable_codes
)
