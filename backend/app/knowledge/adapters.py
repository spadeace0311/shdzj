from __future__ import annotations

import math
import os
from collections.abc import Sequence
from typing import Any

import httpx

from app.config import settings
from app.embedding.schemas import DENSE_DIMENSIONS, EmbeddingBatch, RerankResult


def _embedding_http_timeout_seconds() -> float:
    raw_value = os.getenv("EMBEDDING_HTTP_TIMEOUT_SECONDS", "30")
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(
            "EMBEDDING_HTTP_TIMEOUT_SECONDS must be a number"
        ) from exc
    if value <= 0:
        raise ValueError("EMBEDDING_HTTP_TIMEOUT_SECONDS must be positive")
    return value


EMBEDDING_HTTP_TIMEOUT_SECONDS = _embedding_http_timeout_seconds()
EMBEDDING_BATCH_SIZE = 16


class EmbeddingUnavailableError(RuntimeError):
    """Raised when the embedding service cannot produce valid vectors."""


class RerankerUnavailableError(RuntimeError):
    """Raised when the reranker service cannot produce valid scores."""


class _ModelServiceAdapter:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = EMBEDDING_HTTP_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or settings.embedding_service_url).rstrip("/")
        self._timeout = timeout
        self._client = client

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            if self._client is not None:
                return await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    json=payload,
                    timeout=self._timeout,
                )
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.request(
                    method,
                    f"{self._base_url}{path}",
                    json=payload,
                    timeout=self._timeout,
                )
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailableError(
                "embedding service request failed"
            ) from exc


class EmbeddingAdapter(_ModelServiceAdapter):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = EMBEDDING_HTTP_TIMEOUT_SECONDS,
        batch_size: int = EMBEDDING_BATCH_SIZE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url=base_url, timeout=timeout, client=client)
        self._batch_size = batch_size

    async def embed(self, texts: Sequence[str]) -> EmbeddingBatch:
        if not texts:
            raise ValueError("at least one text is required")
        dense: list[list[float]] = []
        sparse: list[dict[int, float]] = []
        for offset in range(0, len(texts), self._batch_size):
            batch = list(texts[offset : offset + self._batch_size])
            response = await self._request(
                "POST",
                "/v1/embed",
                {"texts": batch},
            )
            batch_dense, batch_sparse = _parse_embedding_response(
                response,
                expected_count=len(batch),
            )
            dense.extend(batch_dense)
            sparse.extend(batch_sparse)
        if len(dense) != len(texts) or len(sparse) != len(texts):
            raise EmbeddingUnavailableError(
                "embedding service returned an unexpected result count"
            )
        return EmbeddingBatch(dense=dense, sparse=sparse)

    async def health(self) -> bool:
        try:
            response = await self._request("GET", "/health")
            response.raise_for_status()
            payload = response.json()
            return isinstance(payload, dict) and payload.get("status") == "ok"
        except (
            EmbeddingUnavailableError,
            httpx.HTTPStatusError,
            ValueError,
            TypeError,
        ):
            return False


class RerankerAdapter(_ModelServiceAdapter):
    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
    ) -> list[RerankResult]:
        if not documents:
            return []
        try:
            response = await self._request(
                "POST",
                "/v1/rerank",
                {"query": query, "documents": list(documents)},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RerankerUnavailableError(
                    "reranker service response is not an object"
                )
            scores = payload.get("scores")
        except (
            EmbeddingUnavailableError,
            RerankerUnavailableError,
            httpx.HTTPStatusError,
            ValueError,
            TypeError,
        ) as exc:
            raise RerankerUnavailableError(
                "reranker service request failed"
            ) from exc
        if not isinstance(scores, list) or len(scores) != len(documents):
            raise RerankerUnavailableError(
                "reranker service returned an unexpected score count"
            )
        results: list[RerankResult] = []
        for index, score in enumerate(scores):
            if not _is_finite_number(score):
                raise RerankerUnavailableError(
                    "reranker service returned a non-finite score"
                )
            results.append(RerankResult(index=index, score=float(score)))
        return results


def _parse_embedding_response(
    response: httpx.Response,
    *,
    expected_count: int,
) -> tuple[list[list[float]], list[dict[int, float]]]:
    try:
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPStatusError, ValueError) as exc:
        raise EmbeddingUnavailableError(
            "embedding service returned an invalid response"
        ) from exc
    if not isinstance(payload, dict):
        raise EmbeddingUnavailableError("embedding service response is not an object")
    dense = payload.get("dense")
    sparse = payload.get("sparse")
    if not isinstance(dense, list) or not isinstance(sparse, list):
        raise EmbeddingUnavailableError("embedding service response is malformed")
    if len(dense) != expected_count or len(sparse) != expected_count:
        raise EmbeddingUnavailableError(
            "embedding service returned an unexpected result count"
        )
    normalized_dense: list[list[float]] = []
    normalized_sparse: list[dict[int, float]] = []
    for vector, weights in zip(dense, sparse, strict=True):
        if not isinstance(vector, list) or len(vector) != DENSE_DIMENSIONS:
            raise EmbeddingUnavailableError(
                f"embedding service returned {len(vector) if isinstance(vector, list) else 'non-list'} dense dimensions"
            )
        if not all(_is_finite_number(value) for value in vector):
            raise EmbeddingUnavailableError(
                "embedding service returned a non-finite vector"
            )
        normalized_dense.append([float(value) for value in vector])
        if not isinstance(weights, dict):
            raise EmbeddingUnavailableError("embedding sparse vector is not a mapping")
        normalized_weights: dict[int, float] = {}
        for token_id, value in weights.items():
            if not _is_finite_number(value):
                raise EmbeddingUnavailableError(
                    "embedding service returned a non-finite sparse weight"
                )
            try:
                parsed_token_id = int(str(token_id))
            except ValueError as exc:
                raise EmbeddingUnavailableError(
                    "embedding service returned an invalid sparse token id"
                ) from exc
            normalized_weights[parsed_token_id] = float(value)
        normalized_sparse.append(normalized_weights)
    return normalized_dense, normalized_sparse


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )
