from __future__ import annotations

import pytest

from app.qa import dependencies
from app.qa.dependencies import QuestionOrchestratorRuntime


class FakeQdrantClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.close_count = 0

    async def close(self) -> None:
        self.close_count += 1


class FakeHttpClient:
    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.aclose_count = 0

    async def aclose(self) -> None:
        self.aclose_count += 1


async def test_question_orchestrator_runtime_closes_owned_clients(
    monkeypatch,
) -> None:
    qdrant = FakeQdrantClient("http://qdrant.invalid")
    http_client = FakeHttpClient(30)
    monkeypatch.setattr(
        dependencies,
        "AsyncQdrantClient",
        lambda url: qdrant,
    )
    monkeypatch.setattr(
        dependencies.httpx,
        "AsyncClient",
        lambda timeout: http_client,
    )

    runtime = QuestionOrchestratorRuntime(deepseek=object())
    await runtime.close()

    assert qdrant.close_count == 1
    assert http_client.aclose_count == 1


@pytest.mark.asyncio
async def test_runtime_accepts_injected_clients() -> None:
    qdrant = FakeQdrantClient("http://qdrant.invalid")
    http_client = FakeHttpClient(30)
    runtime = QuestionOrchestratorRuntime(
        deepseek=object(),
        qdrant=qdrant,
        http_client=http_client,
    )

    await runtime.close()

    assert qdrant.close_count == 1
    assert http_client.aclose_count == 1
