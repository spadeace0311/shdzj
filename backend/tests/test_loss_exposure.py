from uuid import uuid4

import pytest

from app.data_assets.repository import AssetRecord
from app.loss.exposure import LossExposureService, build_exposure_dataset


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
    assert len(dataset.towns) == 1
    assert dataset.towns[0].population_total == pytest.approx(10000.0)
    assert dataset.towns[0].buildings[0].area_m2 == pytest.approx(50000.0)


def test_exposure_accepts_locked_asset_records() -> None:
    dataset = build_exposure_dataset(
        snapshot_checksum="f" * 64,
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
    assert dataset.towns[0].buildings[0].area_m2 == pytest.approx(50000.0)
