from __future__ import annotations

import json

import httpx
import pytest

from app.embedding.schemas import EmbeddingBatch, RerankResult
from app.knowledge.adapters import (
    EmbeddingAdapter,
    EmbeddingUnavailableError,
    RerankerAdapter,
    RerankerUnavailableError,
)


BASE_URL = "http://embedding.test:8080"


def _embedding_response(dense: list[list[float]], sparse: list[dict[str, float]]) -> dict:
    return {"dense": dense, "sparse": sparse}


async def test_embedding_adapter_posts_batches_and_parses_sparse_ids() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(f"{BASE_URL}/v1/embed")
        assert request.extensions["timeout"] == {
            "connect": 30.0,
            "pool": 30.0,
            "read": 30.0,
            "write": 30.0,
        }
        body = request.read().decode()
        requests.append(body)
        payload = httpx.Response(200, content=body).json()
        texts = payload["texts"]
        return httpx.Response(
            200,
            json=_embedding_response(
                [[0.25] * 1024 for _ in texts],
                [{"7": 0.75} for _ in texts],
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EmbeddingAdapter(
            base_url=BASE_URL,
            client=client,
        )
        result = await adapter.embed([f"text-{index}" for index in range(35)])

    assert len(requests) == 3
    assert all('"texts"' in body for body in requests)
    assert isinstance(result, EmbeddingBatch)
    assert len(result.dense) == 35
    assert all(len(vector) == 1024 for vector in result.dense)
    assert result.sparse == [{7: 0.75}] * 35


async def test_embedding_adapter_maps_503_to_unavailable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "model loading"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EmbeddingAdapter(
            base_url=BASE_URL,
            client=client,
        )
        with pytest.raises(EmbeddingUnavailableError):
            await adapter.embed(["document"])


async def test_embedding_adapter_maps_connection_error_to_unavailable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EmbeddingAdapter(
            base_url=BASE_URL,
            client=client,
        )
        with pytest.raises(EmbeddingUnavailableError):
            await adapter.embed(["document"])


@pytest.mark.parametrize(
    "bad_dense_body",
    [
        '{"dense":[[0.1]],"sparse":[{"1":0.5}]}',
        '{"dense":[[' + ",".join(["Infinity"] * 1024) + ']],"sparse":[{"1":0.5}]}',
    ],
)
async def test_embedding_adapter_rejects_invalid_dense_dimensions_and_values(
    bad_dense_body: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=bad_dense_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EmbeddingAdapter(
            base_url=BASE_URL,
            client=client,
        )
        with pytest.raises(EmbeddingUnavailableError):
            await adapter.embed(["document"])


async def test_embedding_adapter_rejects_non_finite_sparse_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='{"dense":[[' + ",".join(["0.1"] * 1024) + ']],"sparse":[{"1":NaN}]}',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EmbeddingAdapter(
            base_url=BASE_URL,
            client=client,
        )
        with pytest.raises(EmbeddingUnavailableError):
            await adapter.embed(["document"])


async def test_embedding_adapter_rejects_oversized_integer_dense_value() -> None:
    huge_integer = 10**1000
    dense = [huge_integer] + [0.1] * 1023

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "dense": [dense],
                    "sparse": [{"1": 0.5}],
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EmbeddingAdapter(
            base_url=BASE_URL,
            client=client,
        )
        with pytest.raises(EmbeddingUnavailableError):
            await adapter.embed(["document"])


async def test_embedding_adapter_rejects_oversized_integer_sparse_value() -> None:
    huge_integer = 10**1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "dense": [[0.1] * 1024],
                    "sparse": [{"1": huge_integer}],
                }
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EmbeddingAdapter(
            base_url=BASE_URL,
            client=client,
        )
        with pytest.raises(EmbeddingUnavailableError):
            await adapter.embed(["document"])


async def test_embedding_adapter_health_is_false_for_starting_service() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == httpx.URL(f"{BASE_URL}/health")
        return httpx.Response(200, json={"status": "starting"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EmbeddingAdapter(
            base_url=BASE_URL,
            client=client,
        )
        assert await adapter.health() is False


async def test_reranker_adapter_preserves_original_indices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(f"{BASE_URL}/v1/rerank")
        assert request.read() == b'{"query":"fault","documents":["first","second"]}'
        return httpx.Response(200, json={"scores": [0.9, 0.2]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RerankerAdapter(
            base_url=BASE_URL,
            client=client,
        )
        result = await adapter.rerank("fault", ["first", "second"])

    assert result == [RerankResult(index=0, score=0.9), RerankResult(index=1, score=0.2)]


async def test_reranker_adapter_rejects_non_finite_scores() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scores": [float("nan")]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RerankerAdapter(
            base_url=BASE_URL,
            client=client,
        )
        with pytest.raises(RerankerUnavailableError):
            await adapter.rerank("query", ["document"])


async def test_reranker_adapter_rejects_oversized_integer_score() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"scores": [10**1000]}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RerankerAdapter(
            base_url=BASE_URL,
            client=client,
        )
        with pytest.raises(RerankerUnavailableError):
            await adapter.rerank("query", ["document"])


async def test_reranker_adapter_maps_503_to_unavailable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "model loading"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RerankerAdapter(
            base_url=BASE_URL,
            client=client,
        )
        with pytest.raises(RerankerUnavailableError):
            await adapter.rerank("query", ["document"])
