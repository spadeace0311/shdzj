from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

from app.embedding.schemas import DENSE_DIMENSIONS


class EmbeddingRuntimeError(RuntimeError):
    """Raised when the local model runtime cannot produce vectors or scores."""


@dataclass(frozen=True, slots=True)
class EmbeddingRuntimeConfig:
    embedding_model_name: str
    reranker_model_name: str
    device: str
    batch_size: int
    max_length: int

    @classmethod
    def from_env(cls) -> "EmbeddingRuntimeConfig":
        return cls(
            embedding_model_name=os.getenv(
                "EMBEDDING_MODEL_NAME",
                "BAAI/bge-m3",
            ),
            reranker_model_name=os.getenv(
                "RERANKER_MODEL_NAME",
                "BAAI/bge-reranker-v2-m3",
            ),
            device=os.getenv("EMBEDDING_DEVICE", "cpu"),
            batch_size=_positive_int_from_env("EMBEDDING_BATCH_SIZE", 16),
            max_length=_positive_int_from_env("EMBEDDING_MAX_LENGTH", 8192),
        )


class BgeRuntime:
    """Lazy local BGE-M3 embedding and reranker runtime."""

    def __init__(
        self,
        config: EmbeddingRuntimeConfig | None = None,
    ) -> None:
        self._config = config or EmbeddingRuntimeConfig.from_env()
        self._embedding = None
        self._reranker = None
        self._loaded = False
        self._load_error: str | None = None
        self._load_cause: BaseException | None = None

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def load_error(self) -> str | None:
        return self._load_error

    @property
    def load_cause(self) -> BaseException | None:
        return self._load_cause

    async def load(self) -> None:
        if self._loaded:
            return
        try:
            self._embedding = self._load_embedding_model()
            self._reranker = self._load_reranker_model()
        except Exception as exc:
            self._loaded = False
            self._load_cause = exc
            self._load_error = "local embedding models failed to load"
            raise EmbeddingRuntimeError("local embedding models failed to load") from exc
        self._loaded = True
        self._load_error = None
        self._load_cause = None

    async def embed(self, texts: list[str]) -> dict[str, Any]:
        if not self._loaded:
            raise EmbeddingRuntimeError("embedding model is not loaded")
        try:
            output = self._embedding.encode(
                texts,
                batch_size=self._config.batch_size,
                max_length=self._config.max_length,
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )
        except Exception as exc:
            raise EmbeddingRuntimeError("embedding inference failed") from exc
        return {
            "dense": self._normalize_dense(output.get("dense_vecs"), len(texts)),
            "sparse": self._normalize_sparse(output.get("lexical_weights"), len(texts)),
        }

    async def rerank(
        self,
        query: str,
        documents: list[str],
    ) -> list[float]:
        if not self._loaded:
            raise EmbeddingRuntimeError("reranker model is not loaded")
        try:
            scores = self._reranker.compute_score(
                [[query, document] for document in documents],
                normalize=True,
            )
        except Exception as exc:
            raise EmbeddingRuntimeError("reranker inference failed") from exc
        normalized: list[float] = []
        for score in scores:
            normalized.append(
                _coerce_finite_float(
                    score,
                    error_type=EmbeddingRuntimeError,
                    message="reranker returned a non-finite score",
                )
            )
        return normalized

    def _load_embedding_model(self) -> Any:
        from FlagEmbedding import BGEM3FlagModel

        return BGEM3FlagModel(
            self._config.embedding_model_name,
            use_fp16=self._config.device != "cpu",
            device=self._config.device,
        )

    def _load_reranker_model(self) -> Any:
        from FlagEmbedding import FlagReranker

        return FlagReranker(
            self._config.reranker_model_name,
            use_fp16=self._config.device != "cpu",
            device=self._config.device,
        )

    def _normalize_dense(
        self,
        dense: Any,
        expected_count: int,
    ) -> list[list[float]]:
        if dense is None:
            raise EmbeddingRuntimeError("embedding model returned no dense vectors")
        if hasattr(dense, "tolist"):
            dense = dense.tolist()
        if not isinstance(dense, list):
            raise EmbeddingRuntimeError("embedding dense vectors are not a list")
        if dense and not isinstance(dense[0], list):
            dense = [dense]
        if len(dense) != expected_count:
            raise EmbeddingRuntimeError("embedding model returned an unexpected vector count")
        normalized: list[list[float]] = []
        for vector in dense:
            if len(vector) != DENSE_DIMENSIONS:
                raise EmbeddingRuntimeError(
                    f"embedding model returned {len(vector)} dense dimensions"
                )
            normalized.append(
                [
                    _coerce_finite_float(
                        value,
                        error_type=EmbeddingRuntimeError,
                        message="embedding model returned a non-finite vector",
                    )
                    for value in vector
                ]
            )
        return normalized

    def _normalize_sparse(
        self,
        sparse: Any,
        expected_count: int,
    ) -> list[dict[str, float]]:
        if sparse is None:
            raise EmbeddingRuntimeError("embedding model returned no sparse vectors")
        if not isinstance(sparse, list) or len(sparse) != expected_count:
            raise EmbeddingRuntimeError("embedding model returned an unexpected sparse count")
        normalized: list[dict[str, float]] = []
        for weights in sparse:
            if not isinstance(weights, dict):
                raise EmbeddingRuntimeError("embedding sparse vector is not a mapping")
            normalized_weights: dict[str, float] = {}
            for token_id, value in weights.items():
                normalized_weights[str(token_id)] = _coerce_finite_float(
                    value,
                    error_type=EmbeddingRuntimeError,
                    message="embedding model returned a non-finite sparse weight",
                )
            normalized.append(normalized_weights)
        return normalized


def _positive_int_from_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise EmbeddingRuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise EmbeddingRuntimeError(f"{name} must be positive")
    return value


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _coerce_finite_float(
    value: Any,
    *,
    error_type: type[RuntimeError],
    message: str,
) -> float:
    if not _is_finite_number(value):
        raise error_type(message)
    try:
        return float(value)
    except (OverflowError, ValueError) as exc:
        raise error_type(message) from exc


_runtime: BgeRuntime | None = None


def set_embedding_runtime(runtime: BgeRuntime) -> None:
    global _runtime
    _runtime = runtime


def get_embedding_runtime() -> BgeRuntime:
    global _runtime
    if _runtime is None:
        _runtime = BgeRuntime()
    return _runtime
