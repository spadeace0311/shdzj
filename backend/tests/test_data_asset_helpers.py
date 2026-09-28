def test_shared_data_asset_fixture_functions_are_registered(
    request,
) -> None:
    fixture_manager = request.session._fixturemanager
    registered = set(fixture_manager._arg2fixturedefs)
    assert {
        "candidate_factory",
        "published_population_asset",
        "seeded_assessment_run",
        "data_asset_client",
        "geojson_town_file",
        "seeded_outbox",
        "seeded_imported_version",
    } <= registered
