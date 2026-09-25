import json
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app.auth.router import get_current_user
from app.auth.service import AuthUser
from app.events.repository import (
    EventDetailRecord,
    EventIngestOutcome,
    EventIngestResult,
    EventSummaryRecord,
)
from app.events.response_rules import ResponseSuggestion
from app.events.router import get_event_service
from app.main import app


def _formal_payload(*, region_context: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "eventId": "CENC-2026-0002",
        "reportType": "formal",
        "originTime": "2026-09-17T02:30:05Z",
        "longitude": 121.54,
        "latitude": 31.22,
        "magnitude": 5.2,
        "depth": 12.0,
        "place": "Shanghai Pudong",
    }
    if region_context:
        payload["regionContext"] = {
            "insideShanghai": True,
            "distanceToBoundaryKm": 0,
            "deaths": None,
            "maxIntensity": 6,
        }
    return payload


def _auto_payload() -> dict[str, object]:
    return {
        "eventId": "CENC-2026-AUTO-1",
        "reportType": "automatic",
        "originTime": "2026-09-17T02:30:05Z",
        "longitude": 121.54,
        "latitude": 31.22,
        "magnitude": 5.0,
        "depth": 12.0,
        "place": "Shanghai Pudong",
    }


class FakeEventService:
    def __init__(self) -> None:
        self._ingested: dict[str, EventIngestResult] = {}
        self._event_ids: dict[str, str] = {}
        self.suggestion = ResponseSuggestion(
            institutional_level="major",
            service_level=2,
            downgraded=False,
            causes=(),
            rule_version="2026.1",
        )
        self._current_suggestions: dict[str, tuple[str | None, int | None]] = {}
        self.summaries: list[EventSummaryRecord] = []
        self.details: dict[str, EventDetailRecord] = {}

    async def ingest(
        self,
        raw_payload: dict[str, object],
        event: object,
        received_at: datetime | None = None,
    ) -> EventIngestResult:
        del received_at
        key = json.dumps(raw_payload, sort_keys=True, ensure_ascii=False)
        existing = self._ingested.get(key)
        if existing is not None:
            return existing

        source_event_id = getattr(event, "source_event_id", None)
        source_key = source_event_id or f"{event.source}:{event.origin_time.isoformat()}"
        event_id = self._event_ids.get(source_key)
        if event_id is None:
            event_id = f"event-{len(self._event_ids) + 1}"
            self._event_ids[source_key] = event_id
            revision_no = 1
        else:
            revision_no = 2

        result = EventIngestResult(
            event_id=event_id,
            revision_id=f"revision-{event_id}-{revision_no}",
            revision_no=revision_no,
            event_kind=event.kind,
            is_current=True,
        )
        self._ingested[key] = result
        return result

    async def ingest_with_response_suggestion(
        self,
        raw_payload: dict[str, object],
        event: object,
        response_input: object | None = None,
        received_at: datetime | None = None,
    ) -> EventIngestOutcome:
        del received_at
        key = json.dumps(raw_payload, sort_keys=True, ensure_ascii=False)
        result = self._ingested.get(key)
        if result is None:
            result = await self.ingest(raw_payload, event)
        if response_input is not None:
            self._current_suggestions[result.event_id] = (
                self.suggestion.institutional_level,
                self.suggestion.service_level,
            )
        institutional_level, service_level = self._current_suggestions.get(
            result.event_id,
            (None, None),
        )
        return EventIngestOutcome(
            event_id=result.event_id,
            revision_id=result.revision_id,
            revision_no=result.revision_no,
            event_kind=result.event_kind,
            is_current=result.is_current,
            institutional_level=institutional_level,
            service_level=service_level,
        )

    async def list_events(self) -> list[EventSummaryRecord]:
        return self.summaries

    async def get_event(self, event_id: str) -> EventDetailRecord:
        if event_id not in self.details:
            raise LookupError(f"event not found: {event_id}")
        return self.details[event_id]


def _client(
    service: FakeEventService,
    *,
    current_user: AuthUser | None = None,
) -> TestClient:
    app.dependency_overrides[get_event_service] = lambda: service
    if current_user is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: current_user
    return TestClient(app)


def test_ingest_formal_event() -> None:
    client = _client(FakeEventService())

    response = client.post("/api/v1/ingest/formal", json=_formal_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["event_kind"] == "formal"
    assert body["revision_no"] == 1
    assert body["institutional_level"] == "major"
    assert body["service_level"] == 2


def test_ingest_auto_event_has_no_response_suggestion() -> None:
    client = _client(FakeEventService())

    response = client.post("/api/v1/ingest/auto", json=_auto_payload())

    assert response.status_code == 201
    body = response.json()
    assert body["event_kind"] == "auto"
    assert body["revision_no"] == 1
    assert body["institutional_level"] is None
    assert body["service_level"] is None


def test_correction_appends_revision() -> None:
    client = _client(FakeEventService())
    formal = _formal_payload()
    formal["eventId"] = "CENC-2026-0003"
    formal["magnitude"] = 4.8
    correction = {
        **formal,
        "reportType": "correction",
        "magnitude": 4.9,
    }

    first = client.post("/api/v1/ingest/formal", json=formal)
    response = client.post("/api/v1/ingest/correction", json=correction)

    assert first.status_code == 201
    assert first.json()["revision_no"] == 1
    assert response.status_code == 201
    assert response.json()["revision_no"] == 2
    assert response.json()["event_kind"] == "correction"
    assert response.json()["institutional_level"] == "major"
    assert response.json()["service_level"] == 2


def test_manual_event_requires_source() -> None:
    client = _client(
        FakeEventService(),
        current_user=AuthUser("operator", "group_leader", None),
    )

    response = client.post(
        "/api/v1/events/manual",
        json={
            "origin_time": "2026-09-17T02:30:05Z",
            "longitude": 121.54,
            "latitude": 31.22,
            "magnitude": 3.2,
            "depth_km": 8.0,
        },
    )

    assert response.status_code == 422


def test_manual_event_returns_created() -> None:
    client = _client(
        FakeEventService(),
        current_user=AuthUser("operator", "group_leader", None),
    )

    response = client.post(
        "/api/v1/events/manual",
        json={
            "origin_time": "2026-09-17T02:30:05Z",
            "longitude": 121.54,
            "latitude": 31.22,
            "magnitude": 3.2,
            "depth_km": 8.0,
            "source": "Shanghai-Network",
            "event_kind": "test",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["event_kind"] == "test"
    assert body["institutional_level"] is None
    assert body["service_level"] is None


def test_invalid_cenc_payload_returns_400() -> None:
    client = _client(FakeEventService())

    response = client.post(
        "/api/v1/ingest/formal",
        json={"reportType": "formal", "originTime": "2026-09-17T02:30:05Z"},
    )

    assert response.status_code == 400


def test_wrong_report_kind_returns_400() -> None:
    client = _client(FakeEventService())

    response = client.post("/api/v1/ingest/auto", json=_formal_payload())

    assert response.status_code == 400


def test_duplicate_auto_and_formal_messages_are_idempotent() -> None:
    service = FakeEventService()
    client = _client(service)

    first_auto = client.post("/api/v1/ingest/auto", json=_auto_payload())
    second_auto = client.post("/api/v1/ingest/auto", json=_auto_payload())
    first_formal = client.post("/api/v1/ingest/formal", json=_formal_payload())
    second_formal = client.post("/api/v1/ingest/formal", json=_formal_payload())

    assert first_auto.json() == second_auto.json()
    assert first_formal.json() == second_formal.json()
    assert first_auto.json()["event_id"] == first_auto.json()["event_id"]


def test_list_events_returns_summaries() -> None:
    service = FakeEventService()
    service.summaries = [
        EventSummaryRecord(
            event_id="event-1",
            source="cenc",
            event_kind="formal",
            place="Shanghai Pudong",
            magnitude=Decimal("5.2"),
            depth_km=Decimal("12.00"),
            origin_time=datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC),
            longitude=Decimal("121.540000"),
            latitude=Decimal("31.220000"),
            institutional_level="major",
            service_level=2,
            revision_no=1,
        )
    ]
    client = _client(service)

    response = client.get("/api/v1/events")

    assert response.status_code == 200
    assert response.json()[0]["id"] == "event-1"
    assert response.json()[0]["institutional_level"] == "major"
    assert response.json()[0]["service_level"] == 2


def test_get_event_returns_detail() -> None:
    service = FakeEventService()
    detail = EventDetailRecord(
        event_id="event-1",
        event_kind="formal",
        source="cenc",
        place="Shanghai Pudong",
        magnitude=Decimal("5.2"),
        depth_km=Decimal("12.00"),
        origin_time=datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC),
        longitude=Decimal("121.540000"),
        latitude=Decimal("31.220000"),
        institutional_level="major",
        service_level=2,
        response_suggestion={
            "institutional_level": "major",
            "service_level": 2,
            "downgraded": False,
            "causes": [],
            "rule_version": "2026.1",
        },
        response_rule_version="2026.1",
        revision_no=1,
    )
    service.details["event-1"] = detail
    client = _client(service)

    response = client.get("/api/v1/events/event-1")

    assert response.status_code == 200
    assert response.json()["id"] == "event-1"
    assert response.json()["event_kind"] == "formal"
    assert response.json()["institutional_level"] == "major"
    assert response.json()["response_rule_version"] == "2026.1"


def test_get_event_returns_current_revision_event_kind() -> None:
    service = FakeEventService()
    detail = EventDetailRecord(
        event_id="event-drill",
        event_kind="drill",
        source="operator",
        place="上海浦东新区",
        magnitude=Decimal("3.2"),
        depth_km=Decimal("8.00"),
        origin_time=datetime(2026, 9, 17, 2, 30, 5, tzinfo=UTC),
        longitude=Decimal("121.540000"),
        latitude=Decimal("31.220000"),
        institutional_level=None,
        service_level=None,
        response_suggestion=None,
        response_rule_version=None,
        revision_no=1,
    )
    service.details["event-drill"] = detail
    client = _client(service)

    response = client.get("/api/v1/events/event-drill")

    assert response.status_code == 200
    assert response.json()["event_kind"] == "drill"


def test_get_missing_event_returns_404() -> None:
    client = _client(FakeEventService())

    response = client.get("/api/v1/events/missing")

    assert response.status_code == 404


def test_formal_without_region_context_ingests_without_suggestion() -> None:
    client = _client(FakeEventService())

    response = client.post(
        "/api/v1/ingest/formal",
        json=_formal_payload(region_context=False),
    )

    assert response.status_code == 201
    assert response.json()["event_kind"] == "formal"
    assert response.json()["institutional_level"] is None
    assert response.json()["service_level"] is None


def test_formal_without_region_context_returns_existing_current_suggestion() -> None:
    service = FakeEventService()
    client = _client(service)

    first = client.post("/api/v1/ingest/formal", json=_formal_payload())
    second = client.post(
        "/api/v1/ingest/formal",
        json=_formal_payload(region_context=False),
    )

    assert first.status_code == 201
    assert first.json()["institutional_level"] == "major"
    assert second.status_code == 201
    assert second.json()["institutional_level"] == "major"
    assert second.json()["service_level"] == 2


def test_manual_event_with_naive_origin_time_returns_422() -> None:
    client = _client(
        FakeEventService(),
        current_user=AuthUser("operator", "group_leader", None),
    )

    response = client.post(
        "/api/v1/events/manual",
        json={
            "origin_time": "2026-09-17T02:30:05",
            "longitude": 121.54,
            "latitude": 31.22,
            "magnitude": 3.2,
            "depth_km": 8.0,
            "source": "shanghai-network",
        },
    )

    assert response.status_code == 422


def test_manual_event_with_blank_source_returns_422() -> None:
    client = _client(
        FakeEventService(),
        current_user=AuthUser("operator", "group_leader", None),
    )

    response = client.post(
        "/api/v1/events/manual",
        json={
            "origin_time": "2026-09-17T02:30:05Z",
            "longitude": 121.54,
            "latitude": 31.22,
            "magnitude": 3.2,
            "depth_km": 8.0,
            "source": "   ",
        },
    )

    assert response.status_code == 422
