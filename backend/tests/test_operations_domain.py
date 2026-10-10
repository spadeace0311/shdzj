from datetime import UTC, datetime, time
from decimal import Decimal

import pytest
from geoalchemy2.elements import WKTElement
from sqlalchemy import delete

from app.events.domain import EventKind, canonical_source_id
from app.operations.domain import OperationsScenario
from app.operations.models import (
    OperationsAuditLog,
    OperationsRun,
    OperationsSettings,
    OperationsTestSchedule,
)
from app.operations.scenario import OperationsConfigurationError, ScenarioGenerator
from app.regions.models import RegionBoundary


@pytest.fixture(autouse=True)
async def _dispose_database_engine():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def _reset_domain_tables(session) -> None:
    await session.execute(delete(OperationsRun))
    await session.execute(delete(OperationsTestSchedule))
    await session.execute(delete(OperationsAuditLog))
    await session.execute(delete(OperationsSettings))
    await session.execute(delete(RegionBoundary))
    await session.flush()


async def _active_boundary(session) -> RegionBoundary:
    boundary = RegionBoundary(
        version="operations-test-boundary-v1",
        name="上海市测试边界",
        local_buffer_km=Decimal("50"),
        geom=WKTElement(
            "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, 122.2 31.8, "
            "121.0 31.8, 121.0 30.8)))",
            srid=4326,
        ),
        maritime_geom=WKTElement(
            "MULTIPOLYGON (((121.0 30.8, 122.2 30.8, 122.2 31.8, "
            "121.0 31.8, 121.0 30.8)))",
            srid=4326,
        ),
        source_uri="test://operations-boundary",
        checksum="a" * 64,
        is_active=True,
    )
    session.add(boundary)
    await session.flush()
    return boundary


async def _settings(session) -> OperationsSettings:
    record = OperationsSettings(
        settings_key="default",
        daily_test_enabled=True,
        timezone="Asia/Shanghai",
        daily_window_start=time(2, 0),
        daily_window_end=time(4, 0),
        test_magnitude_min=Decimal("5.0"),
        test_magnitude_max=Decimal("5.9"),
        test_depth_min_km=Decimal("5.00"),
        test_depth_max_km=Decimal("20.00"),
        drill_conflict_policy="pause",
    )
    session.add(record)
    await session.flush()
    return record


async def test_generated_test_scenario_is_shanghai_5_to_6(session) -> None:
    await _reset_domain_tables(session)
    await _active_boundary(session)
    settings_record = await _settings(session)

    generator = ScenarioGenerator()
    scenario = await generator.generate_test(
        session,
        settings_record,
        observed_at=datetime(2026, 10, 10, 2, tzinfo=UTC),
        seed=20261010,
    )
    event = generator.scenario_to_event(scenario)

    assert event.kind is EventKind.TEST
    assert Decimal("5.0") <= event.magnitude <= Decimal("5.9")
    assert Decimal("5.00") <= event.depth_km <= Decimal("20.00")
    assert scenario.place == "上海市（系统测试）"
    assert scenario.boundary_version == "operations-test-boundary-v1"
    assert await generator.region_contains(session, scenario) is True


async def test_generate_test_requires_active_boundary(session) -> None:
    await _reset_domain_tables(session)
    settings_record = await _settings(session)

    with pytest.raises(
        OperationsConfigurationError,
        match="active_shanghai_boundary_required",
    ):
        await ScenarioGenerator().generate_test(
            session,
            settings_record,
            observed_at=datetime(2026, 10, 10, 2, tzinfo=UTC),
            seed=20261010,
        )


async def test_generated_scenario_is_deterministic_and_idempotent(session) -> None:
    await _reset_domain_tables(session)
    await _active_boundary(session)
    settings_record = await _settings(session)
    observed_at = datetime(2026, 10, 10, 2, tzinfo=UTC)
    generator = ScenarioGenerator()

    first = await generator.generate_test(
        session,
        settings_record,
        observed_at=observed_at,
        seed=20261010,
    )
    second = await generator.generate_test(
        session,
        settings_record,
        observed_at=observed_at,
        seed=20261010,
    )

    assert first == second
    assert first.magnitude == first.magnitude.quantize(Decimal("0.1"))
    assert first.depth_km == first.depth_km.quantize(Decimal("0.01"))
    assert first.source_event_id == "operations-test:20261010:20261010"
    first_event = generator.scenario_to_event(first)
    second_event = generator.scenario_to_event(second)
    assert first_event == second_event
    assert first_event.source == "operations"
    assert first_event.source_event_id == first.source_event_id
    assert canonical_source_id(first_event) == (
        f"operations:{first.source_event_id}"
    )


async def test_random_sampling_reaches_endpoints_and_quantizes(session) -> None:
    await _reset_domain_tables(session)
    await _active_boundary(session)
    settings_record = await _settings(session)
    observed_at = datetime(2026, 10, 10, 2, tzinfo=UTC)
    generator = ScenarioGenerator()
    cases = (
        (2, Decimal("5.0"), Decimal("6.87")),
        (5, Decimal("5.9"), Decimal("10.23")),
        (475, Decimal("5.1"), Decimal("5.00")),
        (2246, Decimal("5.0"), Decimal("20.00")),
    )

    for seed, expected_magnitude, expected_depth in cases:
        scenario = await generator.generate_test(
            session,
            settings_record,
            observed_at=observed_at,
            seed=seed,
        )
        assert scenario.magnitude == expected_magnitude
        assert scenario.depth_km == expected_depth
        assert scenario.magnitude == scenario.magnitude.quantize(Decimal("0.1"))
        assert scenario.depth_km == scenario.depth_km.quantize(Decimal("0.01"))


async def test_region_contains_rejects_outside_point(session) -> None:
    await _reset_domain_tables(session)
    await _active_boundary(session)
    scenario = OperationsScenario(
        origin_time=datetime(2026, 10, 10, 2, tzinfo=UTC),
        longitude=Decimal("119.0"),
        latitude=Decimal("29.0"),
        depth_km=Decimal("10.00"),
        magnitude=Decimal("5.5"),
        place="上海市（系统测试）",
        source_event_id="operations-test:20261010:20261010",
        random_seed=20261010,
    )

    assert await ScenarioGenerator().region_contains(session, scenario) is False


async def test_generate_test_rejects_invalid_or_degenerate_boundary(session) -> None:
    await _reset_domain_tables(session)
    settings_record = await _settings(session)
    session.add(
        RegionBoundary(
            version="invalid-active-boundary",
            name="invalid boundary",
            local_buffer_km=Decimal("50"),
            geom=WKTElement(
                "MULTIPOLYGON (((121.0 30.8, 122.2 31.8, 121.0 31.8, "
                "122.2 30.8, 121.0 30.8)))",
                srid=4326,
            ),
            maritime_geom=WKTElement(
                "MULTIPOLYGON EMPTY",
                srid=4326,
            ),
            source_uri="test://operations-invalid-boundary",
            checksum="b" * 64,
            is_active=True,
        )
    )
    await session.flush()

    with pytest.raises(
        OperationsConfigurationError,
        match="invalid_active_shanghai_boundary",
    ):
        await ScenarioGenerator().generate_test(
            session,
            settings_record,
            observed_at=datetime(2026, 10, 10, 2, tzinfo=UTC),
            seed=99,
        )


async def test_region_contains_keeps_scenario_boundary_version(session) -> None:
    await _reset_domain_tables(session)
    first = await _active_boundary(session)
    settings_record = await _settings(session)
    generator = ScenarioGenerator()
    scenario = await generator.generate_test(
        session,
        settings_record,
        observed_at=datetime(2026, 10, 10, 2, tzinfo=UTC),
        seed=5,
    )

    first.is_active = False
    session.add(
        RegionBoundary(
            version="operations-test-boundary-v2",
            name="new boundary",
            local_buffer_km=Decimal("50"),
            geom=WKTElement(
                "MULTIPOLYGON (((119.0 29.0, 120.0 29.0, 120.0 30.0, "
                "119.0 30.0, 119.0 29.0)))",
                srid=4326,
            ),
            maritime_geom=WKTElement(
                "MULTIPOLYGON (((119.0 29.0, 120.0 29.0, 120.0 30.0, "
                "119.0 30.0, 119.0 29.0)))",
                srid=4326,
            ),
            source_uri="test://operations-boundary-v2",
            checksum="c" * 64,
            is_active=True,
        )
    )
    await session.flush()

    assert scenario.boundary_version == first.version
    assert await generator.region_contains(session, scenario) is True
