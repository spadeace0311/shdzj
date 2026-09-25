from datetime import UTC, datetime

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
