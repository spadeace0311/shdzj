import hashlib
import os
import sys
from pathlib import Path
from typing import Any, NamedTuple
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://earthquake:earthquake@postgres:5432/earthquake",
)
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-16-characters")
os.environ.setdefault(
    "SUPERADMIN_INITIAL_PASSWORD",
    "test-superadmin-password-at-least-16-characters",
)

from app.db import SessionFactory  # noqa: E402

from app.artifacts.retention import ArtifactRetentionService  # noqa: E402

from tests.artifact_helpers import (  # noqa: E402, F401
    artifact_acceptance_environment,
    artifact_repository,
    seeded_artifact_assessment,
    session,
)
from tests.data_asset_helpers import (  # noqa: E402, F401
    candidate_factory,
    data_asset_client,
    geojson_town_file,
    published_population_asset,
    seeded_assessment_run,
    seeded_imported_version,
    seeded_outbox,
)

_original_cenc_collector_enabled = os.environ.get("CENC_COLLECTOR_ENABLED")
os.environ["CENC_COLLECTOR_ENABLED"] = "false"

BENCHMARK_CHUNK_COUNT = 100_000
BENCHMARK_WRITE_BATCH_SIZE = 1_000
BENCHMARK_DENSE_DIMENSIONS = 1024
BENCHMARK_DENSE_VALUE = 0.05
BENCHMARK_SPARSE_INDEX = 7
BENCHMARK_SPARSE_WEIGHT = 0.75
BENCHMARK_SOURCE_KEY = "benchmark-100k"
BENCHMARK_ACTOR = "qa-performance-fixture"

_BENCHMARK_CJK_SEED = (
    "上海市地震应急预案震害评估响应分级活动断层监测预报综合协调应急技术服务保障后勤支持"
    "人员伤亡房屋破坏经济损失资源需求烈度影响街区乡镇人口建筑抗震设防重点目标生命线工程"
    "次生灾害滑坡地裂缝砂土液化海啸余震序列震中定位震源深度面波震级宏观烈度仪器烈度融合"
    "应急避难场所物资储备医疗救援消防救援道路抢修通信恢复电力保障供水保障燃气保障"
)


class SeededKnowledgeIndex(NamedTuple):
    chunk_count: int
    index_version: Any


def _benchmark_chunk_length(index: int) -> int:
    return 500 + (index % 501)


def _benchmark_chunk_text(index: int) -> str:
    length = _benchmark_chunk_length(index)
    seed_length = len(_BENCHMARK_CJK_SEED)
    offset = (index * 17) % seed_length
    return "".join(
        _BENCHMARK_CJK_SEED[(offset + position) % seed_length]
        for position in range(length)
    )


def _benchmark_embeddings(count: int):
    from app.embedding.schemas import EmbeddingBatch

    return EmbeddingBatch(
        dense=[
            [BENCHMARK_DENSE_VALUE] * BENCHMARK_DENSE_DIMENSIONS
            for _ in range(count)
        ],
        sparse=[
            {BENCHMARK_SPARSE_INDEX: BENCHMARK_SPARSE_WEIGHT}
            for _ in range(count)
        ],
    )


class _BenchmarkEmbeddingAdapter:
    async def embed(self, texts):
        return _benchmark_embeddings(len(texts))


class _BenchmarkReranker:
    async def rerank(self, query, documents):
        from app.embedding.schemas import RerankResult

        del query
        return [
            RerankResult(index=index, score=float(len(documents) - index))
            for index in range(len(documents))
        ]


@pytest.fixture
def session_factory():
    return SessionFactory


@pytest.fixture
def artifact_retention_service():
    return ArtifactRetentionService()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def seeded_100k_knowledge_index():
    from qdrant_client import AsyncQdrantClient
    from sqlalchemy import delete
    from sqlalchemy import insert as sql_insert

    from app.config import settings
    from app.db import engine
    from app.knowledge.index import IndexedChunk, KnowledgeIndex
    from app.knowledge.models import (
        KnowledgeChunk,
        KnowledgeIndexVersion,
        KnowledgeSource,
        KnowledgeSourceVersion,
    )

    collection_name = f"{settings.qdrant_collection_prefix}-{BENCHMARK_SOURCE_KEY}"

    await engine.dispose()
    async with SessionFactory() as db:
        async with db.begin():
            await db.execute(
                delete(KnowledgeSource).where(
                    KnowledgeSource.source_key == BENCHMARK_SOURCE_KEY
                )
            )

    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        if await qdrant.collection_exists(collection_name):
            await qdrant.delete_collection(collection_name)

        async with SessionFactory() as db:
            async with db.begin():
                source = KnowledgeSource(
                    source_key=BENCHMARK_SOURCE_KEY,
                    title="十万切片性能基准",
                    layer="local_authority",
                    source_type="benchmark",
                    access_level="internal",
                    created_by=BENCHMARK_ACTOR,
                )
                db.add(source)
                await db.flush()
                version = KnowledgeSourceVersion(
                    source_id=source.id,
                    version="v1",
                    status="published",
                    checksum="a" * 64,
                    created_by=BENCHMARK_ACTOR,
                )
                db.add(version)
                await db.flush()
                index_version = KnowledgeIndexVersion(
                    source_version_id=version.id,
                    version="v1",
                    status="published",
                    collection_name=collection_name,
                    embedding_model=settings.embedding_model_name,
                    reranker_model=settings.reranker_model_name,
                    chunk_count=BENCHMARK_CHUNK_COUNT,
                    manifest={"source_title": source.title},
                )
                db.add(index_version)
                await db.flush()
                source_id = source.id
                version_id = version.id

        index = KnowledgeIndex(client=qdrant)
        await index.ensure_collection(index_version)

        for batch_start in range(
            0,
            BENCHMARK_CHUNK_COUNT,
            BENCHMARK_WRITE_BATCH_SIZE,
        ):
            batch_end = min(
                batch_start + BENCHMARK_WRITE_BATCH_SIZE,
                BENCHMARK_CHUNK_COUNT,
            )
            rows: list[dict[str, Any]] = []
            indexed_chunks: list[IndexedChunk] = []
            for chunk_no in range(batch_start, batch_end):
                chunk_id = uuid4()
                text = _benchmark_chunk_text(chunk_no)
                checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
                rows.append(
                    {
                        "id": chunk_id,
                        "version_id": version_id,
                        "chunk_no": chunk_no + 1,
                        "section_path": ["基准测试"],
                        "checksum": checksum,
                        "qdrant_point_id": uuid5(NAMESPACE_URL, str(chunk_id)),
                        "search_text": text,
                        "text": text,
                    }
                )
                indexed_chunks.append(
                    IndexedChunk(
                        chunk_id=chunk_id,
                        version_id=version_id,
                        source_id=source_id,
                        source_key=BENCHMARK_SOURCE_KEY,
                        layer="local_authority",
                        access_level="internal",
                        text=text,
                        section_path=("基准测试",),
                        page_from=None,
                        page_to=None,
                        checksum=checksum,
                    )
                )

            async with SessionFactory() as db:
                async with db.begin():
                    await db.execute(sql_insert(KnowledgeChunk), rows)

            await index.upsert_chunks(
                index_version,
                indexed_chunks,
                _benchmark_embeddings(len(rows)),
            )

        yield SeededKnowledgeIndex(
            chunk_count=BENCHMARK_CHUNK_COUNT,
            index_version=index_version,
        )
    finally:
        try:
            if await qdrant.collection_exists(collection_name):
                await qdrant.delete_collection(collection_name)
        finally:
            await qdrant.close()

    await engine.dispose()
    async with SessionFactory() as db:
        async with db.begin():
            await db.execute(
                delete(KnowledgeSource).where(
                    KnowledgeSource.source_key == BENCHMARK_SOURCE_KEY
                )
            )
    await engine.dispose()


@pytest.fixture
async def hybrid_retriever(seeded_100k_knowledge_index):
    from qdrant_client import AsyncQdrantClient

    from app.config import settings
    from app.db import engine
    from app.knowledge.index import KnowledgeIndex
    from app.knowledge.retrieval import HybridRetriever, PostgresLexicalIndex

    await engine.dispose()
    client = AsyncQdrantClient(url=settings.qdrant_url)
    retriever = HybridRetriever(
        embedding=_BenchmarkEmbeddingAdapter(),
        qdrant=KnowledgeIndex(client=client),
        postgres=PostgresLexicalIndex(SessionFactory),
        reranker=_BenchmarkReranker(),
        index_version=seeded_100k_knowledge_index.index_version,
    )
    try:
        yield retriever
    finally:
        await client.close()
        await engine.dispose()


def pytest_unconfigure(config: object) -> None:
    if _original_cenc_collector_enabled is None:
        os.environ.pop("CENC_COLLECTOR_ENABLED", None)
    else:
        os.environ["CENC_COLLECTOR_ENABLED"] = _original_cenc_collector_enabled
