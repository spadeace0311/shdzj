from datetime import UTC, datetime

import pytest

import app.collector.spool as spool_module
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


def test_spool_remove_fsync_failure_keeps_recoverable_pending_marker(
    tmp_path,
    monkeypatch,
) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)
    envelope = CollectorEnvelope(
        provider=CollectorProvider.WOLFX,
        lane=CollectorLane.HTTP,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": {"EventID": "CENC-1"}},
    )
    path = spool.append(envelope)

    def fail_directory_fsync() -> None:
        raise OSError("directory fsync failed")

    monkeypatch.setattr(spool, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError, match="directory fsync failed"):
        spool.remove(path)

    deleting_path = path.with_name(path.name + ".deleting")
    assert not path.exists()
    assert deleting_path.exists()

    restarted = CollectorSpool(tmp_path, max_bytes=1_000_000)
    assert list(restarted.iter_pending()) == [(deleting_path, envelope)]


def test_spool_remove_final_fsync_failure_after_durable_delete_propagates(
    tmp_path,
    monkeypatch,
) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)
    envelope = CollectorEnvelope(
        provider=CollectorProvider.WOLFX,
        lane=CollectorLane.HTTP,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": {"EventID": "CENC-1"}},
    )
    path = spool.append(envelope)
    calls = 0

    def fail_directory_fsync() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("final directory fsync failed")

    monkeypatch.setattr(spool, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError, match="final directory fsync failed"):
        spool.remove(path)

    assert calls == 2
    assert not path.exists()
    assert not path.with_name(path.name + ".deleting").exists()
    assert list(spool.iter_pending()) == []


def test_spool_directory_fsync_is_noop_when_platform_unsupported(
    tmp_path,
    monkeypatch,
) -> None:
    envelope = CollectorEnvelope(
        provider=CollectorProvider.WOLFX,
        lane=CollectorLane.HTTP,
        received_at=datetime(2026, 9, 25, 1, 5, tzinfo=UTC),
        payload={"No1": {"EventID": "CENC-1"}},
    )
    spool = CollectorSpool(tmp_path, max_bytes=1_000_000)

    def fail_open(*args, **kwargs):
        raise AssertionError("directory fsync must not open a directory")

    monkeypatch.setattr(spool_module.os, "name", "nt")
    monkeypatch.setattr(spool_module.os, "open", fail_open)

    path = spool.append(envelope)
    restored_path, restored = next(spool.iter_pending())
    assert restored_path == path
    assert restored == envelope
    spool.remove(path)
    assert list(spool.iter_pending()) == []
