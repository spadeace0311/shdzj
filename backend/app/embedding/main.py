from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException

from app.embedding.runtime import (
    BgeRuntime,
    EmbeddingRuntimeError,
    get_embedding_runtime,
)
from app.embedding.schemas import (
    EmbedRequest,
    EmbedResponse,
    HealthResponse,
    RerankRequest,
    RerankResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = get_embedding_runtime()
    if isinstance(runtime, BgeRuntime):
        try:
            await runtime.load()
        except EmbeddingRuntimeError:
            pass
    yield


app = FastAPI(title="Local embedding service", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse, response_model_exclude_none=True)
async def health() -> HealthResponse:
    runtime = get_embedding_runtime()
    if runtime.is_loaded:
        return HealthResponse(status="ok")
    if getattr(runtime, "load_error", None) is not None:
        return HealthResponse(
            status="error",
            detail=getattr(runtime, "load_error"),
        )
    return HealthResponse(status="starting")


@app.post("/v1/embed", response_model=EmbedResponse)
async def embed(request: EmbedRequest) -> EmbedResponse:
    runtime = get_embedding_runtime()
    if not runtime.is_loaded:
        raise HTTPException(
            status_code=503,
            detail="embedding model is not loaded",
        )
    try:
        output = await runtime.embed(request.texts)
    except EmbeddingRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return EmbedResponse(
        dense=output["dense"],
        sparse=output["sparse"],
    )


@app.post("/v1/rerank", response_model=RerankResponse)
async def rerank(request: RerankRequest) -> RerankResponse:
    runtime = get_embedding_runtime()
    if not runtime.is_loaded:
        raise HTTPException(
            status_code=503,
            detail="reranker model is not loaded",
        )
    try:
        scores = await runtime.rerank(request.query, request.documents)
    except EmbeddingRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RerankResponse(scores=scores)
