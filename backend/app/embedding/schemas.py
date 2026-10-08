from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator


DENSE_DIMENSIONS = 1024


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    dense: list[list[float]]
    sparse: list[dict[int, float]]


@dataclass(frozen=True, slots=True)
class RerankResult:
    index: int
    score: float


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1)

    @field_validator("texts")
    @classmethod
    def validate_texts(cls, texts: list[str]) -> list[str]:
        if any(not text.strip() for text in texts):
            raise ValueError("embedding texts must not be blank")
        return texts


class EmbedResponse(BaseModel):
    dense: list[list[float]]
    sparse: list[dict[str, float]]


class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    documents: list[str] = Field(min_length=1)

    @field_validator("query", "documents")
    @classmethod
    def validate_non_blank_text(
        cls,
        value: str | list[str],
    ) -> str | list[str]:
        values = value if isinstance(value, list) else [value]
        if any(not text.strip() for text in values):
            raise ValueError("reranker inputs must not be blank")
        return value


class RerankResponse(BaseModel):
    scores: list[float]


class HealthResponse(BaseModel):
    status: Literal["starting", "ok", "error"]
    detail: str | None = None
