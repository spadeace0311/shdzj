from fastapi.testclient import TestClient

from app.embedding.main import app
from app.embedding.runtime import BgeRuntime, set_embedding_runtime


class FakeRuntime:
    def __init__(self, *, loaded: bool = True) -> None:
        self.loaded = loaded

    @property
    def is_loaded(self) -> bool:
        return self.loaded

    async def embed(self, texts: list[str]) -> dict:
        return {
            "dense": [[0.1] * 1024 for _ in texts],
            "sparse": [{"1": 0.5} for _ in texts],
        }

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        del query
        return [float(len(document)) for document in documents]


class _FailingEmbeddingModel:
    def encode(self, *args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("sensitive secret document text")


class _FailingRerankerModel:
    def compute_score(self, *args, **kwargs) -> None:
        del args, kwargs
        raise RuntimeError("sensitive reranker failure text")


def test_embed_and_rerank_contract() -> None:
    set_embedding_runtime(FakeRuntime())
    client = TestClient(app)
    embedded = client.post("/v1/embed", json={"texts": ["震中在哪里"]})
    assert embedded.status_code == 200
    assert len(embedded.json()["dense"][0]) == 1024
    assert embedded.json()["sparse"] == [{"1": 0.5}]

    reranked = client.post(
        "/v1/rerank",
        json={"query": "断层距离", "documents": ["近", "较远的文档"]},
    )
    assert reranked.status_code == 200
    assert reranked.json()["scores"][1] > reranked.json()["scores"][0]


def test_health_reports_starting_before_runtime_is_loaded() -> None:
    set_embedding_runtime(FakeRuntime(loaded=False))
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "starting"}


def test_health_reports_ok_after_runtime_is_loaded() -> None:
    set_embedding_runtime(FakeRuntime(loaded=True))
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_reports_error_after_runtime_load_failure() -> None:
    runtime = FakeRuntime(loaded=False)
    runtime.load_error = "sensitive load failure text"
    set_embedding_runtime(runtime)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "error", "detail": "model runtime unavailable"}
    assert "sensitive load failure text" not in response.text


def test_embed_endpoint_maps_inference_failure_to_503() -> None:
    runtime = BgeRuntime()
    runtime._loaded = True
    runtime._embedding = _FailingEmbeddingModel()
    set_embedding_runtime(runtime)

    client = TestClient(app)
    response = client.post("/v1/embed", json={"texts": ["震中在哪里"]})

    assert response.status_code == 503
    assert response.json() == {"detail": "embedding model is unavailable"}
    assert "sensitive secret document text" not in response.text


def test_rerank_endpoint_maps_inference_failure_to_503() -> None:
    runtime = BgeRuntime()
    runtime._loaded = True
    runtime._reranker = _FailingRerankerModel()
    set_embedding_runtime(runtime)

    client = TestClient(app)
    response = client.post(
        "/v1/rerank",
        json={"query": "断层距离", "documents": ["较远的文档"]},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "reranker model is unavailable"}
    assert "sensitive reranker failure text" not in response.text


def test_embed_rejects_empty_texts() -> None:
    set_embedding_runtime(FakeRuntime())
    client = TestClient(app)
    response = client.post("/v1/embed", json={"texts": []})
    assert response.status_code == 422


def test_rerank_rejects_empty_documents() -> None:
    set_embedding_runtime(FakeRuntime())
    client = TestClient(app)
    response = client.post(
        "/v1/rerank",
        json={"query": "断层距离", "documents": []},
    )
    assert response.status_code == 422
