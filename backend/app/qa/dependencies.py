from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.db import SessionFactory
from app.knowledge.adapters import (
    EMBEDDING_HTTP_TIMEOUT_SECONDS,
    EmbeddingAdapter,
    RerankerAdapter,
)
from app.knowledge.index import KnowledgeIndex
from app.knowledge.models import KnowledgeIndexVersion
from app.knowledge.retrieval import HybridRetriever, PostgresLexicalIndex
from app.qa.service import QuestionOrchestrator, StoredQaSession


class LockedKnowledgeRetrieverFactory:
    def __init__(
        self,
        *,
        embedding: EmbeddingAdapter,
        qdrant: KnowledgeIndex,
        postgres: PostgresLexicalIndex,
        reranker: RerankerAdapter,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    ) -> None:
        self._embedding = embedding
        self._qdrant = qdrant
        self._postgres = postgres
        self._reranker = reranker
        self._session_factory = session_factory

    async def __call__(
        self,
        stored: StoredQaSession,
    ) -> HybridRetriever:
        index_version_ids = stored.index_version_ids or (
            (stored.index_version_id,)
        )
        async with self._session_factory() as session:
            rows = list(
                (
                    await session.scalars(
                        select(KnowledgeIndexVersion).where(
                            KnowledgeIndexVersion.id.in_(index_version_ids)
                        )
                    )
                ).all()
            )
        by_id = {row.id: row for row in rows}
        snapshot_details = {
            UUID(str(item["index_version_id"])): item
            for item in (stored.manifest or {}).get(
                "knowledge_index_versions",
                [],
            )
            if isinstance(item, dict) and item.get("index_version_id")
        }
        locked_versions = []
        missing = [
            index_version_id
            for index_version_id in index_version_ids
            if index_version_id not in by_id
            and snapshot_details.get(index_version_id) is None
        ]
        if missing:
            raise LookupError("knowledge snapshot index versions are unavailable")
        for index_version_id in index_version_ids:
            row = by_id.get(index_version_id)
            detail = snapshot_details.get(index_version_id) or {}
            if row is None:
                if detail.get("status") != "published":
                    raise LookupError(
                        "knowledge snapshot index versions are unavailable"
                    )
                row = KnowledgeIndexVersion(
                    id=index_version_id,
                    source_version_id=UUID(
                        str(detail["source_version_id"])
                    ),
                    version=str(detail.get("version") or "locked"),
                    status="published",
                    collection_name=str(
                        detail.get("collection_name")
                        or detail.get("source_key")
                        or ""
                    ),
                    embedding_model=str(
                        detail.get("embedding_model") or "locked"
                    ),
                    reranker_model=str(
                        detail.get("reranker_model") or "locked"
                    ),
                    chunk_count=int(detail.get("chunk_count") or 0),
                    manifest={
                        key: value
                        for key, value in detail.items()
                        if key not in {"index_version_id", "source_version_id"}
                    },
                )
            elif detail.get("status") == "published":
                row.status = "published"
                if detail.get("collection_name"):
                    row.collection_name = str(detail["collection_name"])
            locked_versions.append(row)
        return HybridRetriever(
            embedding=self._embedding,
            qdrant=self._qdrant,
            postgres=self._postgres,
            reranker=self._reranker,
            index_versions=tuple(locked_versions),
        )


class QuestionOrchestratorRuntime:
    def __init__(
        self,
        *,
        deepseek: Any,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        qdrant: AsyncQdrantClient | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.qdrant = qdrant or AsyncQdrantClient(url=settings.qdrant_url)
        self.http_client = http_client or httpx.AsyncClient(
            timeout=EMBEDDING_HTTP_TIMEOUT_SECONDS
        )
        embedding = EmbeddingAdapter(client=self.http_client)
        reranker = RerankerAdapter(client=self.http_client)
        retriever_factory = LockedKnowledgeRetrieverFactory(
            embedding=embedding,
            qdrant=KnowledgeIndex(client=self.qdrant),
            postgres=PostgresLexicalIndex(session_factory),
            reranker=reranker,
            session_factory=session_factory,
        )
        self.orchestrator = QuestionOrchestrator(
            deepseek=deepseek,
            retriever_factory=retriever_factory,
            session_factory=session_factory,
        )

    async def close(self) -> None:
        await self.qdrant.close()
        await self.http_client.aclose()


__all__ = [
    "LockedKnowledgeRetrieverFactory",
    "QuestionOrchestratorRuntime",
]
