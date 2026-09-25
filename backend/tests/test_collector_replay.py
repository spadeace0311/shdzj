from datetime import UTC, datetime

import pytest

from app.collector.replay import replay_dead_letter


class FakeReplayService:
    def __init__(self) -> None:
        self.record: dict[str, object] | None = {
            "id": "dead-1",
            "raw_payload": {"No1": {"EventID": "CENC-1"}},
            "provider": "wolfx",
            "lane": "http",
            "received_at": datetime(2026, 9, 25, 1, 2, tzinfo=UTC),
            "status": "open",
        }
        self.statuses: list[str] = []
        self.errors: list[object | None] = []

    async def load_dead_letter(self, dead_letter_id: str):
        return self.record

    async def mark_dead_letter(
        self,
        dead_letter_id: str,
        status: str,
        error=None,
    ) -> None:
        self.statuses.append(status)
        self.errors.append(error)


class FakeReplayCoordinator:
    def __init__(self, error: Exception | None = None) -> None:
        self.envelopes = []
        self.trigger_reasons: list[str] = []
        self.error = error

    async def ingest(self, envelope, trigger_reason: str = "live"):
        self.envelopes.append(envelope)
        self.trigger_reasons.append(trigger_reason)
        if self.error is not None:
            raise self.error
        return None


async def test_replay_uses_original_received_at_and_recovery_reason() -> None:
    service = FakeReplayService()
    coordinator = FakeReplayCoordinator()

    await replay_dead_letter("dead-1", service, coordinator)

    assert coordinator.envelopes[0].received_at == service.record["received_at"]
    assert coordinator.envelopes[0].provider.value == "wolfx"
    assert coordinator.envelopes[0].lane.value == "http"
    assert coordinator.trigger_reasons == ["recovery"]
    assert service.statuses == ["retried"]
    assert service.errors == [None]


async def test_replay_marks_second_success_resolved() -> None:
    service = FakeReplayService()
    service.record["status"] = "retried"

    await replay_dead_letter("dead-1", service, FakeReplayCoordinator())

    assert service.statuses == ["resolved"]


async def test_replay_reopens_dead_letter_with_failure_error() -> None:
    service = FakeReplayService()
    error = ValueError("invalid replay payload")
    coordinator = FakeReplayCoordinator(error=error)

    with pytest.raises(ValueError, match="invalid replay payload"):
        await replay_dead_letter("dead-1", service, coordinator)

    assert service.statuses == ["open"]
    assert service.errors == [error]


async def test_replay_rejects_missing_dead_letter() -> None:
    service = FakeReplayService()
    service.record = None

    with pytest.raises(LookupError, match="dead letter not found"):
        await replay_dead_letter("dead-1", service, FakeReplayCoordinator())
