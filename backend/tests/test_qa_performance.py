from __future__ import annotations

import time

import pytest

from app.knowledge.retrieval import KnowledgeFilters


@pytest.mark.performance
async def test_hybrid_retrieval_p95_under_one_second_on_100k_chunks(
    seeded_100k_knowledge_index,
    hybrid_retriever,
) -> None:
    latencies: list[float] = []
    for index in range(20):
        started = time.perf_counter()
        result = await hybrid_retriever.search(
            f"上海市活动断层距离测试 {index}",
            KnowledgeFilters(access_levels=("public", "internal")),
            limit=20,
        )
        latencies.append(time.perf_counter() - started)
        assert result.evidence
    p95 = sorted(latencies)[18]
    assert seeded_100k_knowledge_index.chunk_count >= 100_000
    assert p95 <= 1.0
