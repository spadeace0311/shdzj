from datetime import UTC, datetime

import pytest

from app.collector.domain import CollectorEnvelope, CollectorLane, CollectorProvider
from app.collector.spool import CollectorSpool


def test_spool_round_trip_preserves_receipt_time(tmp_path) -> None:
    envelope = CollectorEnvelope(
        provider=CollectorProvider.WOLFX,
        lane=CollectorLane.HTTP,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": {"EventID": "CENC-1"}},
    )
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)

    spool.append(envelope)
    path, restored = next(spool.iter_pending())

    assert restored == envelope
    spool.remove(path)
    assert list(spool.iter_pending()) == []


def test_spool_rejects_write_over_capacity(tmp_path) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=1)

    try:
        spool.append(
            CollectorEnvelope(
                provider=CollectorProvider.WOLFX,
                lane=CollectorLane.HTTP,
                received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
                payload={"No1": {"EventID": "CENC-1"}},
            )
        )
    except OverflowError:
        pass
    else:
        raise AssertionError("spool must reject writes beyond capacity")


def test_spool_quarantines_corrupt_file_and_accounts_for_capacity(tmp_path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)

    assert list(spool.iter_pending()) == []
    assert not corrupt.exists()
    assert len(list(tmp_path.glob("*.corrupt"))) == 1
    assert spool._usage_bytes() > 0


def test_spool_remove_propagates_unlink_failure_and_retains_file(
    tmp_path,
    monkeypatch,
) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)
    path = spool.append(
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
            payload={"No1": {"EventID": "CENC-1"}},
        )
    )
    original_unlink = type(path).unlink

    def fail_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError("unlink failed")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "unlink", fail_unlink)

    with pytest.raises(OSError, match="unlink failed"):
        spool.remove(path)

    assert path.exists()


def test_spool_remove_propagates_directory_fsync_failure(
    tmp_path,
    monkeypatch,
) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)
    path = spool.append(
        CollectorEnvelope(
            provider=CollectorProvider.WOLFX,
            lane=CollectorLane.HTTP,
            received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
            payload={"No1": {"EventID": "CENC-1"}},
        )
    )

    def fail_directory_fsync() -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(spool, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError, match="directory fsync failed"):
        spool.remove(path)
