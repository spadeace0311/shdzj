import pytest
from fastapi.testclient import TestClient

from app import system_health
from app.main import app


def test_health() -> None:
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_system_health_exposes_qa_dependency_checks() -> None:
    response = TestClient(app).get("/system/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded", "unavailable"}
    assert {"postgresql", "qdrant", "embedding", "knowledge_worker"} <= set(
        payload["checks"]
    )
    assert "postgresql+asyncpg" not in response.text
    assert "jwt" not in response.text


def test_system_health_reports_degraded_when_qa_dependencies_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok() -> system_health.DependencyHealth:
        return system_health.DependencyHealth(status="ok")

    async def unavailable() -> system_health.DependencyHealth:
        return system_health.DependencyHealth(status="unavailable")

    async def starting() -> system_health.DependencyHealth:
        return system_health.DependencyHealth(status="starting")

    monkeypatch.setattr(system_health, "probe_postgresql", ok)
    monkeypatch.setattr(system_health, "probe_qdrant", unavailable)
    monkeypatch.setattr(system_health, "probe_embedding", starting)
    monkeypatch.setattr(system_health, "probe_knowledge_worker", unavailable)

    response = TestClient(app).get("/system/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"]["qdrant"]["status"] == "unavailable"
    assert response.json()["checks"]["embedding"]["status"] == "starting"
    assert (
        response.json()["checks"]["knowledge_worker"]["status"]
        == "unavailable"
    )


def test_system_health_marks_postgresql_failure_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable() -> system_health.DependencyHealth:
        return system_health.DependencyHealth(status="unavailable")

    async def ok() -> system_health.DependencyHealth:
        return system_health.DependencyHealth(status="ok")

    monkeypatch.setattr(system_health, "probe_postgresql", unavailable)
    monkeypatch.setattr(system_health, "probe_qdrant", ok)
    monkeypatch.setattr(system_health, "probe_embedding", ok)
    monkeypatch.setattr(system_health, "probe_knowledge_worker", ok)

    response = TestClient(app).get("/system/health")

    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
