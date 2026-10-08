from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.adapters import EmbeddingAdapter, RerankerAdapter
from app.knowledge.index import (
    IndexedChunk,
    KnowledgeIndexConfigurationError,
    KnowledgeFilters,
    KnowledgeIndex,
    KnowledgeIndexNotPublishedError,
    RetrievedEvidence,
)
from app.knowledge.models import (
    KnowledgeChunk,
    KnowledgeIndexVersion,
    KnowledgeSource,
    KnowledgeSourceVersion,
)


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    evidence: tuple[RetrievedEvidence, ...]
    degraded: bool
    degradation_reason: tuple[str, ...]


class PostgresLexicalIndex:
    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def search(
        self,
        query: str,
        filters: KnowledgeFilters,
        limit: int,
    ) -> list[RetrievedEvidence]:
        if not query.strip():
            return []
        if limit < 1:
            raise ValueError("limit must be positive")
        statement = self._build_statement(query, filters, limit)
        async with self._session_factory() as session:
            result = await session.execute(statement)
            return [_evidence_from_row(row) for row in result.all()]

    def _build_statement(
        self,
        query: str,
        filters: KnowledgeFilters,
        limit: int,
    ):
        similarity = func.similarity(KnowledgeChunk.search_text, query).label(
            "lexical_score"
        )
        conditions = [
            KnowledgeSourceVersion.status == "published",
            KnowledgeSource.is_active.is_(True),
            KnowledgeChunk.search_text.ilike(
                _trigram_pattern(query),
                escape="\\",
            ),
        ]
        if filters.source_ids:
            conditions.append(KnowledgeSource.id.in_(filters.source_ids))
        if filters.layers:
            conditions.append(KnowledgeSource.layer.in_(filters.layers))
        if filters.access_levels:
            conditions.append(
                KnowledgeSource.access_level.in_(filters.access_levels)
            )
        if filters.event_id is not None:
            conditions.append(
                KnowledgeSourceVersion.version_metadata["event_id"].astext
                == str(filters.event_id)
            )
        if filters.published_before is not None:
            conditions.append(
                KnowledgeSourceVersion.published_at <= filters.published_before
            )
        return (
            select(
                KnowledgeChunk,
                KnowledgeSource.title,
                KnowledgeSource.layer,
                KnowledgeSource.access_level,
                KnowledgeSourceVersion.source_uri,
                similarity,
            )
            .select_from(KnowledgeChunk)
            .join(
                KnowledgeSourceVersion,
                KnowledgeChunk.version_id == KnowledgeSourceVersion.id,
            )
            .join(
                KnowledgeSource,
                KnowledgeSourceVersion.source_id == KnowledgeSource.id,
            )
            .where(*conditions)
            .order_by(similarity.desc())
            .limit(limit)
        )


class HybridRetriever:
    def __init__(
        self,
        *,
        embedding: EmbeddingAdapter,
        qdrant: KnowledgeIndex,
        postgres: PostgresLexicalIndex,
        reranker: RerankerAdapter | None = None,
        index_version: KnowledgeIndexVersion | None = None,
    ) -> None:
        self._embedding = embedding
        self._qdrant = qdrant
        self._postgres = postgres
        self._reranker = reranker
        self._index_version = index_version

    async def search(
        self,
        query: str,
        filters: KnowledgeFilters,
        limit: int = 20,
    ) -> RetrievalResult:
        if not query.strip():
            raise ValueError("query must not be blank")
        if limit < 1:
            raise ValueError("limit must be positive")
        if self._index_version is None:
            raise KnowledgeIndexConfigurationError(
                "an active knowledge index version is required for retrieval"
            )
        if self._index_version.status != "published":
            raise KnowledgeIndexNotPublishedError(
                "only published knowledge index versions can be retrieved"
            )

        reasons: list[str] = []
        candidate_limit = limit * 3
        dense_vector: list[float] | None = None
        sparse_vector: dict[int, float] | None = None

        try:
            embedding_batch = await self._embedding.embed([query])
            if not embedding_batch.dense or not embedding_batch.sparse:
                raise ValueError("embedding service returned empty results")
            dense_vector = embedding_batch.dense[0]
            sparse_vector = embedding_batch.sparse[0]
        except Exception:
            reasons.append("embedding_unavailable")

        async def _skip() -> list[RetrievedEvidence]:
            return []

        if dense_vector is not None and sparse_vector is not None:
            dense_task = self._qdrant.search_dense(
                self._index_version,
                dense_vector,
                filters,
                candidate_limit,
            )
            sparse_task = self._qdrant.search_sparse(
                self._index_version,
                sparse_vector,
                filters,
                candidate_limit,
            )
        else:
            dense_task = _skip()
            sparse_task = _skip()
        lexical_task = self._postgres.search(query, filters, candidate_limit)

        dense_result, sparse_result, lexical_result = await asyncio.gather(
            dense_task,
            sparse_task,
            lexical_task,
            return_exceptions=True,
        )

        qdrant_available = True
        if isinstance(dense_result, Exception) or isinstance(
            sparse_result,
            Exception,
        ):
            qdrant_available = False
            dense_result = []
            sparse_result = []
            reasons.append("qdrant_unavailable")
        elif dense_vector is None or sparse_vector is None:
            qdrant_available = False
            dense_result = []
            sparse_result = []

        postgres_available = True
        if isinstance(lexical_result, Exception):
            postgres_available = False
            lexical_result = []
            reasons.append("postgres_unavailable")

        ranked_lists: list[list[RetrievedEvidence]] = []
        if qdrant_available:
            ranked_lists.append(dense_result)
            ranked_lists.append(sparse_result)
        if postgres_available:
            ranked_lists.append(lexical_result)

        if not ranked_lists:
            reasons.append("retrieval_unavailable")
            return RetrievalResult(
                evidence=(),
                degraded=True,
                degradation_reason=tuple(_ordered_reasons(reasons)),
            )

        rrf_ordered, first_index = _fuse_rrf(ranked_lists)
        evidence = rrf_ordered
        if evidence and self._reranker is None:
            reasons.append("reranker_unavailable")
        elif evidence and self._reranker is not None:
            evidence = await _rerank_candidates(
                self._reranker,
                query,
                rrf_ordered,
                limit,
                reasons,
            )

        reasons = _ordered_reasons(reasons)
        return RetrievalResult(
            evidence=tuple(evidence[:limit]),
            degraded=bool(reasons),
            degradation_reason=tuple(reasons),
        )


def _trigram_pattern(query: str) -> str:
    escaped = (
        query.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _fuse_rrf(
    ranked_lists: list[list[RetrievedEvidence]],
) -> tuple[list[RetrievedEvidence], dict[UUID, int]]:
    fused: dict[UUID, RetrievedEvidence] = {}
    first_index: dict[UUID, int] = {}
    for route_results in ranked_lists:
        for rank, item in enumerate(route_results, start=1):
            rrf_score = 1.0 / (60.0 + rank)
            if item.chunk_id not in fused:
                first_index[item.chunk_id] = len(first_index)
                fused[item.chunk_id] = replace(
                    item,
                    scores={**item.scores, "rrf": rrf_score},
                )
                continue
            existing = fused[item.chunk_id]
            merged_scores = dict(existing.scores)
            for score_name, score_value in item.scores.items():
                merged_scores.setdefault(score_name, score_value)
            merged_scores["rrf"] = merged_scores.get("rrf", 0.0) + rrf_score
            fused[item.chunk_id] = replace(existing, scores=merged_scores)

    ordered = sorted(
        fused.values(),
        key=lambda item: (
            -item.scores.get("rrf", 0.0),
            first_index[item.chunk_id],
        ),
    )
    return ordered, first_index


async def _rerank_candidates(
    reranker: RerankerAdapter,
    query: str,
    rrf_ordered: list[RetrievedEvidence],
    limit: int,
    reasons: list[str],
) -> list[RetrievedEvidence]:
    pool = rrf_ordered[: limit * 2]
    try:
        results = await reranker.rerank(query, [item.text for item in pool])
        if len(results) != len(pool):
            raise ValueError("reranker returned an unexpected result count")
        by_index = {result.index: result.score for result in results}
        if len(by_index) != len(pool) or any(
            index < 0 or index >= len(pool) for index in by_index
        ):
            raise ValueError("reranker returned invalid indices")
        reranked_pool: list[RetrievedEvidence] = []
        for index, _ in sorted(
            by_index.items(),
            key=lambda entry: -entry[1],
        ):
            item = pool[index]
            scores = dict(item.scores)
            scores["rerank"] = by_index[index]
            reranked_pool.append(replace(item, scores=scores))
        return reranked_pool + rrf_ordered[len(pool) :]
    except Exception:
        reasons.append("reranker_unavailable")
        return rrf_ordered


def _evidence_from_row(row: tuple[Any, ...]) -> RetrievedEvidence:
    chunk, source_title, layer, access_level, source_uri, similarity = row
    return RetrievedEvidence(
        chunk_id=chunk.id,
        version_id=chunk.version_id,
        source_title=str(source_title or ""),
        layer=str(layer or ""),
        access_level=str(access_level or ""),
        text=chunk.text,
        section_path=tuple(chunk.section_path or ()),
        page_from=chunk.page_from,
        page_to=chunk.page_to,
        source_uri=source_uri,
        checksum=chunk.checksum,
        scores={"lexical": float(similarity)},
    )


def _ordered_reasons(reasons: list[str]) -> list[str]:
    order = (
        "qdrant_unavailable",
        "postgres_unavailable",
        "embedding_unavailable",
        "reranker_unavailable",
        "retrieval_unavailable",
    )
    unique = {reason for reason in reasons if reason in order}
    return [reason for reason in order if reason in unique]


__all__ = [
    "HybridRetriever",
    "IndexedChunk",
    "KnowledgeIndexConfigurationError",
    "KnowledgeFilters",
    "KnowledgeIndex",
    "KnowledgeIndexNotPublishedError",
    "PostgresLexicalIndex",
    "RetrievalResult",
    "RetrievedEvidence",
]
