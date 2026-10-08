from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete

from app.embedding.schemas import EmbeddingBatch, RerankResult
from app.knowledge.adapters import RerankerUnavailableError
from app.knowledge.index import (
    KnowledgeIndexConfigurationError,
    RetrievedEvidence,
)
from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeIndexVersion,
    KnowledgeSource,
    KnowledgeSourceVersion,
)
from app.knowledge.retrieval import (
    HybridRetriever,
    KnowledgeFilters,
    PostgresLexicalIndex,
    _trigram_pattern,
)


def _published_index_version() -> KnowledgeIndexVersion:
    return KnowledgeIndexVersion(
        id=uuid4(),
        source_version_id=uuid4(),
        version="v1",
        status="published",
        collection_name=f"shanghai-knowledge-{uuid4()}",
        embedding_model="BAAI/bge-m3",
        reranker_model="BAAI/bge-reranker-v2-m3",
        chunk_count=0,
        manifest={},
        activated_at=None,
    )


def _evidence(
    chunk_id: UUID,
    *,
    text: str = "evidence text",
    scores: dict[str, float] | None = None,
) -> RetrievedEvidence:
    return RetrievedEvidence(
        chunk_id=chunk_id,
        version_id=uuid4(),
        source_title="Source Title",
        layer="local_authority",
        access_level="internal",
        text=text,
        section_path=("第一章",),
        page_from=1,
        page_to=1,
        source_uri="https://example.invalid/source",
        checksum="c" * 64,
        scores=scores or {},
    )


class FakeEmbeddingAdapter:
    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        return EmbeddingBatch(
            dense=[[0.25] * 1024 for _ in texts],
            sparse=[{1: 0.75} for _ in texts],
        )


class FailingQdrantIndex:
    async def search_dense(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("qdrant unavailable")

    async def search_sparse(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("qdrant unavailable")


class UnexpectedQdrantIndex:
    def __init__(self) -> None:
        self.call_count = 0

    async def search_dense(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.call_count += 1
        raise AssertionError("Qdrant should not be called without an index version")

    async def search_sparse(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.call_count += 1
        raise AssertionError("Qdrant should not be called without an index version")


class FakePostgresIndex:
    def __init__(
        self,
        evidence: list[RetrievedEvidence] | None = None,
        *,
        include_expected: bool = True,
    ) -> None:
        self.evidence = evidence or []
        self.include_expected = include_expected
        self.expected_id = uuid4()
        self.calls: list[tuple[str, KnowledgeFilters, int]] = []

    async def search(
        self,
        query: str,
        filters: KnowledgeFilters,
        limit: int,
    ) -> list[RetrievedEvidence]:
        self.calls.append((query, filters, limit))
        if self.evidence:
            return self.evidence
        if self.include_expected:
            return [_evidence(self.expected_id, scores={"lexical": 0.9})]
        return []


class FailingPostgresIndex:
    async def search(
        self,
        query: str,
        filters: KnowledgeFilters,
        limit: int,
    ) -> list[RetrievedEvidence]:
        del query, filters, limit
        raise RuntimeError("postgres unavailable")


class FakeQdrantIndex:
    def __init__(
        self,
        dense: list[RetrievedEvidence],
        sparse: list[RetrievedEvidence],
    ) -> None:
        self.dense = dense
        self.sparse = sparse

    async def search_dense(
        self,
        index_version: object,
        vector: list[float],
        filters: KnowledgeFilters,
        limit: int,
    ) -> list[RetrievedEvidence]:
        del index_version, vector, filters, limit
        return self.dense

    async def search_sparse(
        self,
        index_version: object,
        sparse: dict[int, float],
        filters: KnowledgeFilters,
        limit: int,
    ) -> list[RetrievedEvidence]:
        del index_version, sparse, filters, limit
        return self.sparse


class FakeReranker:
    def __init__(self, results: list[RerankResult] | None = None) -> None:
        self.results = results

    async def rerank(
        self,
        query: str,
        documents: list[str],
    ) -> list[RerankResult]:
        del query
        if self.results is not None:
            return self.results
        return [
            RerankResult(index=index, score=float(len(documents) - index))
            for index in range(len(documents))
        ]


class FailingReranker:
    async def rerank(
        self,
        query: str,
        documents: list[str],
    ) -> list[RerankResult]:
        del query, documents
        raise RerankerUnavailableError("reranker unavailable")


async def test_hybrid_retriever_falls_back_to_postgres() -> None:
    postgres = FakePostgresIndex()
    retriever = HybridRetriever(
        embedding=FakeEmbeddingAdapter(),
        qdrant=FailingQdrantIndex(),
        postgres=postgres,
        reranker=None,
        index_version=_published_index_version(),
    )

    result = await retriever.search(
        "最近断裂带",
        KnowledgeFilters(access_levels=("public", "internal")),
    )

    assert result.degraded is True
    assert "qdrant_unavailable" in result.degradation_reason
    assert result.evidence[0].chunk_id == postgres.expected_id


async def test_hybrid_retriever_locks_postgres_to_index_source_version() -> None:
    postgres = FakePostgresIndex()
    index_version = _published_index_version()
    retriever = HybridRetriever(
        embedding=FakeEmbeddingAdapter(),
        qdrant=FailingQdrantIndex(),
        postgres=postgres,
        reranker=None,
        index_version=index_version,
    )

    await retriever.search(
        "最近断裂带",
        KnowledgeFilters(access_levels=("public", "internal")),
    )

    assert postgres.calls[0][1].source_version_ids == (
        index_version.source_version_id,
    )


async def test_rrf_promotes_chunks_present_in_dense_and_sparse() -> None:
    shared = uuid4()
    dense_only = uuid4()
    sparse_only = uuid4()
    qdrant = FakeQdrantIndex(
        dense=[_evidence(shared), _evidence(dense_only)],
        sparse=[_evidence(shared), _evidence(sparse_only)],
    )
    postgres = FakePostgresIndex(evidence=[], include_expected=False)

    result = await HybridRetriever(
        embedding=FakeEmbeddingAdapter(),
        qdrant=qdrant,
        postgres=postgres,
        reranker=None,
        index_version=_published_index_version(),
    ).search("断裂带", KnowledgeFilters(), limit=10)

    chunk_ids = [evidence.chunk_id for evidence in result.evidence]
    assert len(chunk_ids) == len(set(chunk_ids))
    assert chunk_ids[0] == shared
    assert chunk_ids.index(shared) < chunk_ids.index(dense_only)
    assert chunk_ids.index(shared) < chunk_ids.index(sparse_only)


async def test_reranker_reorders_top_candidates() -> None:
    documents = [uuid4() for _ in range(4)]
    dense = [_evidence(chunk_id) for chunk_id in documents]
    reranker = FakeReranker(
        [
            RerankResult(index=3, score=0.9),
            RerankResult(index=2, score=0.8),
            RerankResult(index=1, score=0.7),
            RerankResult(index=0, score=0.6),
        ]
    )

    result = await HybridRetriever(
        embedding=FakeEmbeddingAdapter(),
        qdrant=FakeQdrantIndex(dense=dense, sparse=[]),
        postgres=FakePostgresIndex(evidence=[], include_expected=False),
        reranker=reranker,
        index_version=_published_index_version(),
    ).search("query", KnowledgeFilters(), limit=2)

    assert [evidence.chunk_id for evidence in result.evidence] == [
        documents[3],
        documents[2],
    ]
    assert result.degraded is False


async def test_reranker_unavailable_preserves_rrf_order() -> None:
    first = uuid4()
    second = uuid4()
    dense = [_evidence(first), _evidence(second)]

    result = await HybridRetriever(
        embedding=FakeEmbeddingAdapter(),
        qdrant=FakeQdrantIndex(dense=dense, sparse=[]),
        postgres=FakePostgresIndex(evidence=[], include_expected=False),
        reranker=FailingReranker(),
        index_version=_published_index_version(),
    ).search("query", KnowledgeFilters(), limit=2)

    assert [evidence.chunk_id for evidence in result.evidence] == [first, second]
    assert "reranker_unavailable" in result.degradation_reason


async def test_retriever_returns_retrieval_unavailable_when_all_routes_fail() -> None:
    result = await HybridRetriever(
        embedding=FakeEmbeddingAdapter(),
        qdrant=FailingQdrantIndex(),
        postgres=FailingPostgresIndex(),
        reranker=None,
        index_version=_published_index_version(),
    ).search("query", KnowledgeFilters(), limit=3)

    assert result.evidence == ()
    assert result.degraded is True
    assert "retrieval_unavailable" in result.degradation_reason


async def test_hybrid_retriever_requires_active_index_version() -> None:
    qdrant = UnexpectedQdrantIndex()
    retriever = HybridRetriever(
        embedding=FakeEmbeddingAdapter(),
        qdrant=qdrant,
        postgres=FakePostgresIndex(evidence=[], include_expected=False),
        reranker=None,
        index_version=None,
    )

    with pytest.raises(KnowledgeIndexConfigurationError):
        await retriever.search("query", KnowledgeFilters(), limit=3)

    assert qdrant.call_count == 0


def test_trigram_pattern_escapes_like_wildcards_and_backslash() -> None:
    assert _trigram_pattern("100%_\\") == r"%100\%\_\\%"


def test_postgres_statement_binds_trigram_pattern() -> None:
    index = PostgresLexicalIndex(session_factory=lambda: None)
    statement = index._build_statement(
        "100%_\\",
        KnowledgeFilters(access_levels=("public", "internal")),
        limit=5,
    )
    compiled = statement.compile()

    assert "lower(knowledge_chunks.search_text) LIKE lower(:search_text_1)" in str(
        compiled
    )
    assert compiled.params["search_text_1"] == r"%100\%\_\\%"


async def test_postgres_retrieval_isolated_by_locked_version_and_global_scope(
    session_factory,
) -> None:
    actor = f"retrieval-isolation-{uuid4()}"
    token = f"isolationtoken{uuid4().hex}"
    event_a = uuid4()
    event_b = uuid4()
    try:
        async with session_factory() as session:
            async with session.begin():
                global_source = KnowledgeSource(
                    source_key=f"global-{uuid4()}",
                    title="Global Source",
                    layer="local_authority",
                    source_type="preplan",
                    access_level="internal",
                    created_by=actor,
                )
                event_source = KnowledgeSource(
                    source_key=f"event-{event_a}",
                    title="Event Source",
                    layer="local_authority",
                    source_type="event",
                    access_level="internal",
                    created_by=actor,
                )
                other_source = KnowledgeSource(
                    source_key=f"event-{event_b}",
                    title="Other Event Source",
                    layer="local_authority",
                    source_type="event",
                    access_level="internal",
                    created_by=actor,
                )
                session.add_all([global_source, event_source, other_source])
                await session.flush()

                now = datetime.now(UTC)
                global_v1 = _source_version(
                    global_source.id,
                    version="v1",
                    event_id=None,
                    published_at=now,
                )
                global_v2 = _source_version(
                    global_source.id,
                    version="v2",
                    event_id=None,
                    published_at=now,
                )
                event_a_v1 = _source_version(
                    event_source.id,
                    version="v1",
                    event_id=event_a,
                    published_at=now,
                )
                event_a_v2 = _source_version(
                    event_source.id,
                    version="v2",
                    event_id=event_a,
                    published_at=now,
                )
                event_b_v1 = _source_version(
                    other_source.id,
                    version="v1",
                    event_id=event_b,
                    published_at=now,
                )
                session.add_all(
                    [
                        global_v1,
                        global_v2,
                        event_a_v1,
                        event_a_v2,
                        event_b_v1,
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        _chunk(global_v1.id, token, "global-v1"),
                        _chunk(global_v2.id, token, "global-v2"),
                        _chunk(event_a_v1.id, token, "event-a-v1"),
                        _chunk(event_a_v2.id, token, "event-a-v2"),
                        _chunk(event_b_v1.id, token, "event-b-v1"),
                    ]
                )

        index = PostgresLexicalIndex(session_factory)
        event_result = await index.search(
            token,
            KnowledgeFilters(
                access_levels=("public", "internal"),
                source_version_ids=(event_a_v1.id,),
                event_id=event_a,
            ),
            limit=10,
        )
        global_result = await index.search(
            token,
            KnowledgeFilters(
                access_levels=("public", "internal"),
                source_version_ids=(global_v1.id, event_a_v1.id),
                global_only=True,
            ),
            limit=10,
        )

        assert [item.text for item in event_result] == ["event-a-v1"]
        assert [item.text for item in global_result] == ["global-v1"]
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(KnowledgeSource).where(
                        KnowledgeSource.created_by == actor
                    )
                )


def _source_version(
    source_id: UUID,
    *,
    version: str,
    event_id: UUID | None,
    published_at: datetime,
) -> KnowledgeSourceVersion:
    metadata = {"event_id": str(event_id)} if event_id is not None else {}
    return KnowledgeSourceVersion(
        source_id=source_id,
        version=version,
        status="published",
        checksum="a" * 64,
        created_by="retrieval-isolation",
        version_metadata=metadata,
        published_at=published_at,
    )


def _chunk(version_id: UUID, search_text: str, text: str) -> KnowledgeChunk:
    return KnowledgeChunk(
        version_id=version_id,
        chunk_no=1,
        section_path=[],
        checksum="b" * 64,
        search_text=search_text,
        text=text,
    )
