from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from app.embedding.schemas import EmbeddingBatch
from app.knowledge.index import (
    IndexedChunk,
    KnowledgeIndex,
    KnowledgeIndexNotPublishedError,
)
from app.knowledge.models import KnowledgeIndexVersion
from app.knowledge.retrieval import KnowledgeFilters
from qdrant_client.models import Distance
from qdrant_client.models import (
    FieldCondition,
    IsNullCondition,
    MatchAny,
    MatchValue,
)


def _index_version(
    *,
    version: str = "v1",
    manifest: dict | None = None,
    collection_name: str | None = None,
    source_version_id: UUID | None = None,
    status: str = "published",
) -> KnowledgeIndexVersion:
    return KnowledgeIndexVersion(
        id=uuid4(),
        source_version_id=source_version_id or uuid4(),
        version=version,
        status=status,
        collection_name=collection_name or f"shanghai-knowledge-{version}",
        embedding_model="BAAI/bge-m3",
        reranker_model="BAAI/bge-reranker-v2-m3",
        chunk_count=2,
        manifest=manifest or {},
        activated_at=datetime(2026, 10, 8, tzinfo=UTC),
    )


def _chunk(
    index: int,
    *,
    source_id: UUID | None = None,
    version_id: UUID | None = None,
) -> IndexedChunk:
    source_id = source_id or uuid4()
    version_id = version_id or uuid4()
    return IndexedChunk(
        chunk_id=uuid4(),
        version_id=version_id,
        source_id=source_id,
        source_key=f"source-{index}",
        layer="local_authority",
        access_level="internal",
        text=f"chunk text {index}",
        section_path=("第一章", "总则"),
        page_from=index + 1,
        page_to=index + 1,
        checksum="a" * 64,
    )


class FakeQdrantClient:
    def __init__(self) -> None:
        self.collections: set[str] = set()
        self.created: list[tuple[str, dict]] = []
        self.upserts: list[tuple[str, list, dict]] = []
        self.deletes: list[tuple[str, object, dict]] = []
        self.queries: list[dict] = []
        self.query_responses: list[SimpleNamespace] = []

    async def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.collections

    async def create_collection(
        self,
        collection_name: str,
        **kwargs: object,
    ) -> bool:
        self.collections.add(collection_name)
        self.created.append((collection_name, kwargs))
        return True

    async def upsert(
        self,
        collection_name: str,
        points: list,
        **kwargs: object,
    ) -> None:
        self.upserts.append((collection_name, points, kwargs))

    async def delete(
        self,
        collection_name: str,
        points_selector: object,
        **kwargs: object,
    ) -> None:
        self.deletes.append((collection_name, points_selector, kwargs))

    async def query_points(
        self,
        collection_name: str,
        *,
        query: object = None,
        using: str | None = None,
        query_filter: object = None,
        limit: int = 10,
        with_payload: bool = True,
        **kwargs: object,
    ) -> SimpleNamespace:
        del kwargs
        self.queries.append(
            {
                "collection_name": collection_name,
                "query": query,
                "using": using,
                "query_filter": query_filter,
                "limit": limit,
                "with_payload": with_payload,
            }
        )
        return self.query_responses.pop(0)


class FilterAwareQdrantClient:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads

    async def query_points(
        self,
        collection_name: str,
        *,
        query: object = None,
        using: str | None = None,
        query_filter: object = None,
        limit: int = 10,
        with_payload: bool = True,
        **kwargs: object,
    ) -> SimpleNamespace:
        del kwargs
        assert collection_name
        assert query is not None
        assert using == "dense"
        assert with_payload is True
        points = [
            SimpleNamespace(
                id=str(uuid5(NAMESPACE_URL, str(payload["chunk_id"]))),
                version=1,
                score=0.9,
                payload=payload,
                vector=None,
            )
            for payload in self.payloads
            if _filter_matches(query_filter, payload)
        ]
        return SimpleNamespace(points=points[:limit])


def _query_response(
    *,
    chunk_id: UUID,
    source_id: UUID,
    version_id: UUID,
    score: float,
) -> SimpleNamespace:
    return SimpleNamespace(
        points=[
            SimpleNamespace(
                id=str(uuid5(NAMESPACE_URL, str(chunk_id))),
                version=1,
                score=score,
                payload={
                    "chunk_id": str(chunk_id),
                    "version_id": str(version_id),
                    "source_id": str(source_id),
                    "source_key": "source-key",
                    "source_title": "Source Title",
                    "layer": "local_authority",
                    "access_level": "internal",
                    "event_id": str(uuid4()),
                    "section_path": ["第一章", "总则"],
                    "page_from": 1,
                    "page_to": 1,
                    "checksum": "b" * 64,
                    "source_uri": "https://example.invalid/source",
                    "published_at": "2026-10-08T00:00:00+00:00",
                    "text": "evidence text",
                },
                vector=None,
            )
        ]
    )


async def test_ensure_collection_creates_dense_and_sparse_vectors() -> None:
    client = FakeQdrantClient()
    index_version = _index_version(version="2026.1")

    await KnowledgeIndex(client=client).ensure_collection(index_version)

    assert client.created[0][0] == "shanghai-knowledge-2026.1"
    config = client.created[0][1]
    dense = config["vectors_config"]["dense"]
    sparse = config["sparse_vectors_config"]["sparse"]
    assert dense.size == 1024
    assert dense.distance == Distance.COSINE
    assert sparse.__class__.__name__ == "SparseVectorParams"


async def test_upsert_chunks_uses_stable_uuid5_and_idempotent_payload() -> None:
    client = FakeQdrantClient()
    source_id = uuid4()
    version_id = uuid4()
    chunks = [
        _chunk(0, source_id=source_id, version_id=version_id),
        _chunk(1, source_id=source_id, version_id=version_id),
    ]
    embeddings = EmbeddingBatch(
        dense=[[0.1] * 1024, [0.2] * 1024],
        sparse=[{7: 0.8}, {9: 0.6}],
    )
    index_version = _index_version(
        manifest={
            "source_title": "上海市地震应急预案",
            "source_uri": "https://example.invalid/preplan",
            "event_id": str(uuid4()),
            "published_at": "2026-10-08T00:00:00+00:00",
        }
    )

    index = KnowledgeIndex(client=client)
    await index.upsert_chunks(index_version, chunks, embeddings)
    await index.upsert_chunks(index_version, chunks, embeddings)

    first_points = client.upserts[0][1]
    second_points = client.upserts[1][1]
    assert [point.id for point in first_points] == [
        str(uuid5(NAMESPACE_URL, str(chunk.chunk_id))) for chunk in chunks
    ]
    assert [point.id for point in second_points] == [
        point.id for point in first_points
    ]
    assert first_points[0].vector["dense"] == [0.1] * 1024
    assert first_points[1].vector["sparse"].indices == [9]
    assert first_points[1].vector["sparse"].values == [0.6]
    assert first_points[0].payload["source_title"] == "上海市地震应急预案"
    assert first_points[0].payload["source_uri"] == (
        "https://example.invalid/preplan"
    )
    assert first_points[0].payload["version_id"] == str(version_id)


async def test_search_dense_maps_qdrant_payload_to_evidence() -> None:
    client = FakeQdrantClient()
    chunk_id = uuid4()
    source_id = uuid4()
    version_id = uuid4()
    client.query_responses.append(
        _query_response(
            chunk_id=chunk_id,
            source_id=source_id,
            version_id=version_id,
            score=0.91,
        )
    )
    index_version = _index_version()

    result = await KnowledgeIndex(client=client).search_dense(
        index_version,
        [0.25] * 1024,
        KnowledgeFilters(access_levels=("public", "internal")),
        limit=5,
    )

    assert len(result) == 1
    assert result[0].chunk_id == chunk_id
    assert result[0].source_title == "Source Title"
    assert result[0].scores == {"dense": 0.91}
    assert client.queries[0]["using"] == "dense"
    assert client.queries[0]["limit"] == 5


async def test_search_sparse_uses_sorted_bge_token_ids() -> None:
    client = FakeQdrantClient()
    chunk_id = uuid4()
    source_id = uuid4()
    version_id = uuid4()
    client.query_responses.append(
        _query_response(
            chunk_id=chunk_id,
            source_id=source_id,
            version_id=version_id,
            score=0.42,
        )
    )

    result = await KnowledgeIndex(client=client).search_sparse(
        _index_version(),
        {20: 0.3, 3: 0.9},
        KnowledgeFilters(),
        limit=3,
    )

    assert result[0].scores == {"sparse": 0.42}
    sparse_query = client.queries[0]["query"]
    assert sparse_query.indices == [3, 20]
    assert sparse_query.values == [0.9, 0.3]
    assert client.queries[0]["using"] == "sparse"


async def test_search_global_only_excludes_points_with_event_metadata() -> None:
    index_version = _index_version()
    global_chunk_id = uuid4()
    event_chunk_id = uuid4()
    client = FilterAwareQdrantClient(
        [
            _point_payload(
                chunk_id=global_chunk_id,
                source_id=uuid4(),
                version_id=index_version.source_version_id,
                event_id=None,
            ),
            _point_payload(
                chunk_id=event_chunk_id,
                source_id=uuid4(),
                version_id=index_version.source_version_id,
                event_id=str(uuid4()),
            ),
        ]
    )

    result = await KnowledgeIndex(client=client).search_dense(
        index_version,
        [0.25] * 1024,
        KnowledgeFilters(global_only=True),
        limit=3,
    )

    assert [item.chunk_id for item in result] == [global_chunk_id]


async def test_search_isolates_sources_with_same_version_string() -> None:
    client = FakeQdrantClient()
    source_version_a = uuid4()
    source_version_b = uuid4()
    index_a = _index_version(
        version="v1",
        collection_name="source-a",
        source_version_id=source_version_a,
    )
    index_b = _index_version(
        version="v1",
        collection_name="source-b",
        source_version_id=source_version_b,
    )
    client.query_responses.append(
        _query_response(
            chunk_id=uuid4(),
            source_id=uuid4(),
            version_id=source_version_a,
            score=0.9,
        )
    )
    client.query_responses.append(
        _query_response(
            chunk_id=uuid4(),
            source_id=uuid4(),
            version_id=source_version_b,
            score=0.8,
        )
    )
    index = KnowledgeIndex(client=client)

    result_a = await index.search_dense(
        index_a,
        [0.1] * 1024,
        KnowledgeFilters(),
        limit=3,
    )
    result_b = await index.search_dense(
        index_b,
        [0.2] * 1024,
        KnowledgeFilters(),
        limit=3,
    )

    assert client.queries[0]["collection_name"] == "source-a"
    assert client.queries[1]["collection_name"] == "source-b"
    assert _mandatory_version_filter(client.queries[0]["query_filter"]) == str(
        source_version_a
    )
    assert _mandatory_version_filter(client.queries[1]["query_filter"]) == str(
        source_version_b
    )
    assert result_a[0].version_id == source_version_a
    assert result_b[0].version_id == source_version_b


async def test_search_rejects_non_published_index_version() -> None:
    client = FakeQdrantClient()
    index_version = _index_version(status="indexed")

    with pytest.raises(KnowledgeIndexNotPublishedError):
        await KnowledgeIndex(client=client).search_dense(
            index_version,
            [0.1] * 1024,
            KnowledgeFilters(),
            limit=3,
        )

    assert client.queries == []


async def test_delete_version_filters_by_version_payload() -> None:
    client = FakeQdrantClient()
    version_id = uuid4()
    index = KnowledgeIndex(client=client)

    await index.delete_version(_index_version(), version_id)

    selector = client.deletes[0][1]
    condition = selector.filter.must[0]
    assert condition.key == "version_id"
    assert condition.match.value == str(version_id)


def _mandatory_version_filter(query_filter: object) -> str:
    return next(
        condition.match.value
        for condition in query_filter.must
        if condition.key == "version_id"
    )


def _point_payload(
    *,
    chunk_id: UUID,
    source_id: UUID,
    version_id: UUID,
    event_id: str | None,
) -> dict:
    return {
        "chunk_id": str(chunk_id),
        "version_id": str(version_id),
        "source_id": str(source_id),
        "source_key": "source-key",
        "source_title": "Source Title",
        "layer": "local_authority",
        "access_level": "internal",
        "event_id": event_id,
        "section_path": ["第一章"],
        "page_from": 1,
        "page_to": 1,
        "checksum": "b" * 64,
        "source_uri": None,
        "published_at": "2026-10-08T00:00:00+00:00",
        "text": "evidence text",
    }


def _filter_matches(query_filter: object, payload: dict) -> bool:
    must = getattr(query_filter, "must", None) or []
    must_not = getattr(query_filter, "must_not", None) or []
    return all(_condition_matches(condition, payload) for condition in must) and not any(
        _condition_matches(condition, payload) for condition in must_not
    )


def _condition_matches(condition: object, payload: dict) -> bool:
    if isinstance(condition, IsNullCondition):
        return payload.get(condition.is_null.key) is None
    if isinstance(condition, FieldCondition):
        value = payload.get(condition.key)
        if isinstance(condition.match, MatchValue):
            return value == condition.match.value
        if isinstance(condition.match, MatchAny):
            return value in condition.match.any
    raise AssertionError(f"unsupported Qdrant condition: {condition!r}")
