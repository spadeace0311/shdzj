from uuid import uuid4

import pytest

from app.data_assets.repository import AssetRecord
from app.loss.asset_bridge import load_locked_parameter_set
from app.loss.exposure import LossExposureService, build_exposure_dataset
from app.loss.region import load_region_loss_profile


CHECKSUM = "f" * 64
GEOMETRY_WKT = "POLYGON((0 0,1 0,1 1,0 1,0 0))"
REGION_PROFILE = load_region_loss_profile("/config/loss/shanghai-region.yaml")


class FakeSnapshots:
    async def capture_required_assets(
        self,
        session,
        *,
        run_id,
        region_id,
        strict,
    ):
        assert region_id == "shanghai"
        assert strict is True
        return type(
            "SnapshotResult",
            (),
            {
                "snapshot_count": 7,
                "missing_required": (),
                "fingerprint": "f" * 64,
            },
        )()

    async def get_locked_version(self, session, *, run_id, asset_key):
        return type(
            "LockedVersion",
            (),
            {"id": uuid4(), "version": "2022.1", "checksum": "a" * 64},
        )()


class FakeRun:
    snapshot = {"region_id": "shanghai"}


class FakeSession:
    async def get(self, model, run_id):
        return FakeRun()


async def _locked_records(session, *, run_id, asset_key):
    payloads = {
        "shanghai.admin.city": (
            {"ID": "310000", "NAME": "上海市"},
        ),
        "shanghai.population.town": (
            {"ID": "310101001", "NAME": "测试街道", "total": 10000.0},
        ),
        "shanghai.building.town": (
            {
                "id": "310101001",
                "name": "测试街道",
                "TOTAL_AREA": 50000.0,
                "HIGH_RISE": 0.0,
                "RCFRAME": 50000.0,
                "BRICK_STRUCTURE": 0.0,
                "SINGLE_AREA": 0.0,
                "OTHER_STRUCTURE": 0.0,
            },
        ),
    }
    return [
        type("Record", (), {"properties": payload})()
        for payload in payloads.get(asset_key, ())
    ]


async def _locked_features(session, *, run_id, asset_key):
    return [
        type(
            "Feature",
            (),
            {
                "properties": {
                    "ID": "310101001",
                    "NAME": "测试街道",
                },
                "geometry_wkt": "POLYGON((0 0,1 0,1 1,0 1,0 0))",
            },
        )()
    ]


async def test_exposure_combines_population_buildings_and_geometry() -> None:
    service = LossExposureService(
        snapshots=FakeSnapshots(),
        list_records=_locked_records,
        list_features=_locked_features,
    )
    dataset = await service.prepare(
        session=FakeSession(),
        run_id=uuid4(),
    )
    assert dataset.snapshot_checksum == "f" * 64
    assert dataset.city.area_code == "310000"
    assert dataset.city.area_name == "上海市"
    assert len(dataset.towns) == 1
    assert dataset.towns[0].population_total == pytest.approx(10000.0)
    assert dataset.towns[0].buildings[0].area_m2 == pytest.approx(50000.0)


def test_exposure_accepts_locked_asset_records() -> None:
    dataset = build_exposure_dataset(
        snapshot_checksum="f" * 64,
        city=(
            AssetRecord(
                row_number=1,
                business_key="310000",
                properties={"ID": "310000", "NAME": "上海市"},
                geometry_wkt=None,
            ),
        ),
        towns=(
            AssetRecord(
                row_number=1,
                business_key="310101001",
                properties={"ID": "310101001", "NAME": "测试街道", "total": 10000.0},
                geometry_wkt=None,
            ),
        ),
        geometries=(
            AssetRecord(
                row_number=1,
                business_key="310101001",
                properties={"ID": "310101001", "NAME": "测试街道"},
                geometry_wkt="POLYGON((0 0,1 0,1 1,0 1,0 0))",
            ),
        ),
        buildings=(
            AssetRecord(
                row_number=1,
                business_key="310101001",
                properties={
                    "id": "310101001",
                    "name": "测试街道",
                    "TOTAL_AREA": 50000.0,
                    "HIGH_RISE": 0.0,
                    "RCFRAME": 50000.0,
                    "BRICK_STRUCTURE": 0.0,
                    "SINGLE_AREA": 0.0,
                    "OTHER_STRUCTURE": 0.0,
                },
                geometry_wkt=None,
            ),
        ),
    )

    assert dataset.towns[0].town_code == "310101001"
    assert dataset.city.area_code == "310000"
    assert dataset.city.area_name == "上海市"
    assert dataset.towns[0].buildings[0].area_m2 == pytest.approx(50000.0)


class FakeParameterSnapshots:
    def __init__(self, version, records=()):
        self.version = version
        self.records = records

    async def get_locked_version(self, session, *, run_id, asset_key):
        return self.version

    async def list_locked_records(self, session, *, run_id, asset_key):
        return list(self.records)


def _parameter_record(properties):
    return type("Record", (), {"properties": properties})()


def _parameter_version(checksum=CHECKSUM):
    return type("Version", (), {"id": uuid4(), "version": "2022.1", "checksum": checksum})()


async def test_load_locked_parameter_set_requires_locked_version() -> None:
    snapshots = FakeParameterSnapshots(version=None)
    with pytest.raises(LookupError, match="loss parameter asset is not locked"):
        await load_locked_parameter_set(
            None,
            run_id=uuid4(),
            profile=REGION_PROFILE,
            snapshots=snapshots,
        )


@pytest.mark.parametrize("record_count", [0, 2])
async def test_load_locked_parameter_set_requires_one_record(record_count) -> None:
    snapshots = FakeParameterSnapshots(
        version=_parameter_version(),
        records=[_parameter_record({}) for _ in range(record_count)],
    )
    with pytest.raises(ValueError, match="exactly one record"):
        await load_locked_parameter_set(
            None,
            run_id=uuid4(),
            profile=REGION_PROFILE,
            snapshots=snapshots,
        )


async def test_load_locked_parameter_set_maps_one_record() -> None:
    payload = {
        "version": "test-v1",
        "calibration_status": "reference_uncalibrated",
        "provenance": {},
        "models": {},
        "test_only": True,
    }
    snapshots = FakeParameterSnapshots(
        version=_parameter_version(),
        records=[_parameter_record(payload)],
    )
    parameter_set = await load_locked_parameter_set(
        None,
        run_id=uuid4(),
        profile=REGION_PROFILE,
        snapshots=snapshots,
    )
    assert parameter_set.version == "test-v1"
    assert parameter_set.checksum == CHECKSUM


def _town(code="310101001", **overrides):
    row = {"ID": code, "NAME": "测试街道", "total": 10000.0}
    row.update(overrides)
    return row


def _geometry(code="310101001", wkt=GEOMETRY_WKT, **overrides):
    row = {"ID": code, "NAME": "测试街道", "geometry_wkt": wkt}
    row.update(overrides)
    return row


def _building(code="310101001", **overrides):
    row = {
        "id": code,
        "name": "测试街道",
        "HIGH_RISE": 0.0,
        "RCFRAME": 50000.0,
        "BRICK_STRUCTURE": 0.0,
        "SINGLE_AREA": 0.0,
        "OTHER_STRUCTURE": 0.0,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("towns", "geometries", "buildings", "match"),
    [
        (
            [_town(), _town()],
            [_geometry()],
            [],
            "duplicate town code",
        ),
        (
            [_town("310101001")],
            [_geometry("310101002")],
            [],
            "business keys do not match",
        ),
        (
            [_town()],
            [_geometry(wkt="")],
            [],
            "geometry must not be empty",
        ),
        (
            [_town()],
            [_geometry()],
            [_building("310101999")],
            "unknown town code",
        ),
        (
            [_town(total=-1.0)],
            [_geometry()],
            [],
            "population must not be negative",
        ),
        (
            [_town()],
            [_geometry()],
            [_building(RCFRAME=-1.0)],
            "building area must not be negative",
        ),
        (
            [_town()],
            [_geometry()],
            [{"id": "310101001", "structure": "not-a-structure", "area_m2": 1.0}],
            "not-a-structure",
        ),
        (
            [_town()],
            [_geometry(), _geometry()],
            [],
            "duplicate town code in boundary asset",
        ),
    ],
)
def test_build_exposure_dataset_rejects_invalid_inputs(
    towns,
    geometries,
    buildings,
    match,
) -> None:
    with pytest.raises(ValueError, match=match):
        build_exposure_dataset(
            snapshot_checksum=CHECKSUM,
            city=[{"ID": "310000", "NAME": "上海市"}],
            towns=towns,
            geometries=geometries,
            buildings=buildings,
        )
