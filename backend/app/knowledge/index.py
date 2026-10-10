from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    CreateAlias,
    CreateAliasOperation,
    DatetimeRange,
    DeleteAlias,
    DeleteAliasOperation,
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    IsNullCondition,
    MatchAny,
    MatchValue,
    PayloadField,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app.config import settings
from app.embedding.schemas import DENSE_DIMENSIONS, EmbeddingBatch
from app.knowledge.models import KnowledgeIndexVersion


class KnowledgeIndexConfigurationError(ValueError):
    """Raised when retrieval is missing the active index version."""


class KnowledgeIndexNotPublishedError(ValueError):
    """Raised when a non-published index version is used for vector search."""


@dataclass(frozen=True, slots=True)
class KnowledgeIndexRebuild:
    logical_name: str
    alias_name: str
    staging_name: str
    previous_name: str | None
    previous_alias_exists: bool


@dataclass(frozen=True, slots=True)
class KnowledgeFilters:
    source_ids: tuple[UUID, ...] = ()
    source_version_ids: tuple[UUID, ...] = ()
    layers: tuple[str, ...] = ()
    access_levels: tuple[str, ...] = ()
    event_id: UUID | None = None
    global_only: bool = False
    published_before: datetime | None = None
    snapshot_locked: bool = False

    def __post_init__(self) -> None:
        if self.global_only and self.event_id is not None:
            raise ValueError("global_only cannot be combined with event_id")


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    chunk_id: UUID
    version_id: UUID
    source_id: UUID
    source_key: str
    layer: str
    access_level: str
    text: str
    section_path: tuple[str, ...]
    page_from: int | None
    page_to: int | None
    checksum: str


@dataclass(frozen=True, slots=True)
class RetrievedEvidence:
    chunk_id: UUID
    version_id: UUID
    source_title: str
    layer: str
    access_level: str
    text: str
    section_path: tuple[str, ...]
    page_from: int | None
    page_to: int | None
    source_uri: str | None
    checksum: str
    scores: dict[str, float] = field(default_factory=dict)


class KnowledgeIndex:
    def __init__(
        self,
        *,
        client: AsyncQdrantClient | None = None,
        url: str | None = None,
    ) -> None:
        self._client = client or AsyncQdrantClient(url=url or settings.qdrant_url)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def ensure_collection(
        self,
        index_version: KnowledgeIndexVersion,
        *,
        collection_name: str | None = None,
    ) -> None:
        collection_name = collection_name or _logical_collection_name(
            index_version
        )
        if await self._client.collection_exists(collection_name):
            await self.validate_collection(
                index_version,
                collection_name=collection_name,
            )
            return
        await self._create_collection(collection_name)

    async def validate_collection(
        self,
        index_version: KnowledgeIndexVersion,
        *,
        collection_name: str | None = None,
    ) -> None:
        collection_name = collection_name or _logical_collection_name(
            index_version
        )
        info = await self._client.get_collection(collection_name)
        config = getattr(info, "config", None)
        params = getattr(config, "params", None)
        vectors = getattr(params, "vectors", None)
        dense = vectors.get("dense") if isinstance(vectors, dict) else vectors
        if (
            dense is None
            or int(getattr(dense, "size", 0)) != DENSE_DIMENSIONS
            or getattr(dense, "distance", None) != Distance.COSINE
        ):
            raise KnowledgeIndexConfigurationError(
                "Qdrant dense vector configuration is incompatible"
            )
        sparse_config = getattr(params, "sparse_vectors", None) or {}
        if sparse_config.get("sparse") is None:
            raise KnowledgeIndexConfigurationError(
                "Qdrant sparse vector configuration is incompatible"
            )
        payload_schema = getattr(info, "payload_schema", None) or {}
        required_payload_fields = {
            "chunk_id",
            "version_id",
            "source_id",
            "source_key",
            "source_title",
            "layer",
            "access_level",
            "section_path",
            "checksum",
            "text",
        }
        if payload_schema and not required_payload_fields <= set(payload_schema):
            raise KnowledgeIndexConfigurationError(
                "Qdrant payload schema is incompatible"
            )
        points, _offset = await self._client.scroll(
            collection_name=collection_name,
            limit=1,
            with_payload=True,
        )
        if points:
            payload = dict(getattr(points[0], "payload", None) or {})
            if not required_payload_fields <= set(payload):
                raise KnowledgeIndexConfigurationError(
                    "Qdrant point payload schema is incompatible"
                )
            _validate_payload_types(payload)

    async def upsert_chunks(
        self,
        index_version: KnowledgeIndexVersion,
        chunks: list[IndexedChunk],
        embeddings: EmbeddingBatch,
        *,
        collection_name: str | None = None,
    ) -> None:
        if not chunks:
            return
        if (
            len(chunks) != len(embeddings.dense)
            or len(chunks) != len(embeddings.sparse)
        ):
            raise ValueError("chunk and embedding counts do not match")

        manifest = dict(index_version.manifest or {})
        source_title = str(manifest.get("source_title") or chunks[0].source_key)
        source_uri = manifest.get("source_uri")
        event_id = manifest.get("event_id")
        published_at = manifest.get("published_at")
        if published_at is None and index_version.activated_at is not None:
            published_at = index_version.activated_at.isoformat()

        points: list[PointStruct] = []
        for chunk, dense, sparse in zip(
            chunks,
            embeddings.dense,
            embeddings.sparse,
            strict=True,
        ):
            if len(dense) != DENSE_DIMENSIONS:
                raise ValueError(
                    f"dense vector must contain {DENSE_DIMENSIONS} dimensions"
                )
            indices = sorted(sparse)
            point = PointStruct(
                id=str(uuid5(NAMESPACE_URL, str(chunk.chunk_id))),
                vector={
                    "dense": dense,
                    "sparse": SparseVector(
                        indices=indices,
                        values=[float(sparse[index]) for index in indices],
                    ),
                },
                payload={
                    "chunk_id": str(chunk.chunk_id),
                    "version_id": str(chunk.version_id),
                    "source_id": str(chunk.source_id),
                    "source_key": chunk.source_key,
                    "source_title": source_title,
                    "layer": chunk.layer,
                    "access_level": chunk.access_level,
                    "event_id": str(event_id) if event_id is not None else None,
                    "section_path": list(chunk.section_path),
                    "page_from": chunk.page_from,
                    "page_to": chunk.page_to,
                    "checksum": chunk.checksum,
                    "source_uri": source_uri,
                    "published_at": published_at,
                    "text": chunk.text,
                },
            )
            points.append(point)

        await self._client.upsert(
            collection_name=collection_name
            or _logical_collection_name(index_version),
            points=points,
        )

    async def prepare_rebuild(
        self,
        index_version: KnowledgeIndexVersion,
    ) -> KnowledgeIndexRebuild:
        logical_name = _logical_collection_name(index_version)
        await self.validate_collection(index_version)
        alias_name = _rebuild_alias_name(logical_name)
        previous_name = await self._alias_target(alias_name)
        previous_alias_exists = previous_name is not None
        if previous_name is None and await self._client.collection_exists(
            logical_name
        ):
            previous_name = logical_name
        staging_name = (
            f"{logical_name[:255 - 13]}-{uuid4().hex[:12]}"
        )
        await self._create_collection(staging_name)
        return KnowledgeIndexRebuild(
            logical_name=logical_name,
            alias_name=alias_name,
            staging_name=staging_name,
            previous_name=previous_name,
            previous_alias_exists=previous_alias_exists,
        )

    async def commit_rebuild(self, rebuild: KnowledgeIndexRebuild) -> None:
        operations = []
        if rebuild.previous_alias_exists:
            operations.append(
                DeleteAliasOperation(
                    delete_alias=DeleteAlias(
                        alias_name=rebuild.alias_name,
                    )
                )
            )
        operations.append(
            CreateAliasOperation(
                create_alias=CreateAlias(
                    alias_name=rebuild.alias_name,
                    collection_name=rebuild.staging_name,
                )
            )
        )
        await self._client.update_collection_aliases(operations)

    async def compensate_rebuild(self, rebuild: KnowledgeIndexRebuild) -> None:
        if rebuild.previous_alias_exists:
            previous_name = rebuild.previous_name or rebuild.logical_name
            operations = [
                CreateAliasOperation(
                    create_alias=CreateAlias(
                        alias_name=rebuild.alias_name,
                        collection_name=previous_name,
                    )
                )
            ]
        else:
            operations = [
                DeleteAliasOperation(
                    delete_alias=DeleteAlias(alias_name=rebuild.alias_name)
                )
            ]
        await self._client.update_collection_aliases(operations)

    async def abort_rebuild(self, rebuild: KnowledgeIndexRebuild) -> None:
        if await self._client.collection_exists(rebuild.staging_name):
            await self._client.delete_collection(rebuild.staging_name)

    async def _alias_target(self, alias_name: str) -> str | None:
        response = await self._client.get_aliases()
        for alias in getattr(response, "aliases", ()):
            if getattr(alias, "alias_name", None) == alias_name:
                return str(getattr(alias, "collection_name", "")) or None
        return None

    async def _create_collection(self, collection_name: str) -> None:
        await self._client.create_collection(
            collection_name=collection_name,
            vectors_config={
                "dense": VectorParams(
                    size=DENSE_DIMENSIONS,
                    distance=Distance.COSINE,
                )
            },
            sparse_vectors_config={"sparse": SparseVectorParams()},
        )

    async def delete_version(
        self,
        index_version: KnowledgeIndexVersion,
        version_id: UUID,
    ) -> None:
        await self._client.delete(
            collection_name=_collection_name(index_version),
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(
                            key="version_id",
                            match=MatchValue(value=str(version_id)),
                        )
                    ]
                )
            ),
        )

    async def search_dense(
        self,
        index_version: KnowledgeIndexVersion,
        vector: list[float],
        filters: KnowledgeFilters,
        limit: int,
    ) -> list[RetrievedEvidence]:
        _require_published_index_version(index_version)
        if len(vector) != DENSE_DIMENSIONS:
            raise ValueError(
                f"dense vector must contain {DENSE_DIMENSIONS} dimensions"
            )
        response = await self._client.query_points(
            collection_name=_collection_name(index_version),
            query=vector,
            using="dense",
            query_filter=_qdrant_filter(filters, index_version),
            limit=limit,
            with_payload=True,
        )
        return [
            _evidence_from_point(point, score_name="dense")
            for point in response.points
        ]

    async def search_sparse(
        self,
        index_version: KnowledgeIndexVersion,
        sparse: dict[int, float],
        filters: KnowledgeFilters,
        limit: int,
    ) -> list[RetrievedEvidence]:
        _require_published_index_version(index_version)
        indices = sorted(sparse)
        response = await self._client.query_points(
            collection_name=_collection_name(index_version),
            query=SparseVector(
                indices=indices,
                values=[float(sparse[index]) for index in indices],
            ),
            using="sparse",
            query_filter=_qdrant_filter(filters, index_version),
            limit=limit,
            with_payload=True,
        )
        return [
            _evidence_from_point(point, score_name="sparse")
            for point in response.points
        ]


def _collection_name(index_version: KnowledgeIndexVersion) -> str:
    collection_name = index_version.collection_name.strip()
    if not collection_name:
        raise KnowledgeIndexConfigurationError(
            "knowledge index version is missing a collection name"
        )
    return collection_name


def _logical_collection_name(index_version: KnowledgeIndexVersion) -> str:
    manifest_alias = (index_version.manifest or {}).get("collection_alias")
    if manifest_alias:
        return str(manifest_alias).strip()
    return _collection_name(index_version)


def _rebuild_alias_name(logical_name: str) -> str:
    suffix = "-active"
    return f"{logical_name[:255 - len(suffix)]}{suffix}"


def _validate_payload_types(payload: dict[str, object]) -> None:
    string_fields = {
        "chunk_id",
        "version_id",
        "source_id",
        "source_key",
        "source_title",
        "layer",
        "access_level",
        "checksum",
        "text",
    }
    if any(not isinstance(payload.get(field), str) for field in string_fields):
        raise KnowledgeIndexConfigurationError(
            "Qdrant point payload field types are incompatible"
        )
    if not isinstance(payload.get("section_path"), list):
        raise KnowledgeIndexConfigurationError(
            "Qdrant point section_path type is incompatible"
        )
    for field_name in ("event_id", "source_uri"):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, str):
            raise KnowledgeIndexConfigurationError(
                "Qdrant point nullable field types are incompatible"
            )
    for field_name in ("page_from", "page_to"):
        value = payload.get(field_name)
        if value is not None and isinstance(value, bool):
            raise KnowledgeIndexConfigurationError(
                "Qdrant point page fields are incompatible"
            )


def _qdrant_filter(
    filters: KnowledgeFilters,
    index_version: KnowledgeIndexVersion,
) -> Filter:
    conditions: list[FieldCondition] = [
        FieldCondition(
            key="version_id",
            match=MatchValue(value=str(index_version.source_version_id)),
        )
    ]
    if filters.source_ids:
        conditions.append(
            _match_any("source_id", [str(value) for value in filters.source_ids])
        )
    if filters.source_version_ids:
        conditions.append(
            _match_any(
                "version_id",
                [str(value) for value in filters.source_version_ids],
            )
        )
    if filters.layers:
        conditions.append(_match_any("layer", list(filters.layers)))
    if filters.access_levels:
        conditions.append(_match_any("access_level", list(filters.access_levels)))
    if filters.event_id is not None:
        conditions.append(
            FieldCondition(
                key="event_id",
                match=MatchValue(value=str(filters.event_id)),
            )
        )
    if filters.global_only:
        conditions.append(
            IsNullCondition(
                is_null=PayloadField(key="event_id"),
            )
        )
    if filters.published_before is not None:
        conditions.append(
            FieldCondition(
                key="published_at",
                range=DatetimeRange(lte=filters.published_before),
            )
        )
    return Filter(must=conditions)


def _match_any(key: str, values: list[str]) -> FieldCondition:
    return FieldCondition(key=key, match=MatchAny(any=values))


def _require_published_index_version(
    index_version: KnowledgeIndexVersion,
) -> None:
    if index_version.status != "published":
        raise KnowledgeIndexNotPublishedError(
            "only published knowledge index versions can be searched"
        )


def _evidence_from_point(
    point: object,
    *,
    score_name: str,
) -> RetrievedEvidence:
    payload = dict(getattr(point, "payload", None) or {})
    section_path = payload.get("section_path") or []
    if not isinstance(section_path, list):
        section_path = list(section_path)
    source_uri = payload.get("source_uri")
    return RetrievedEvidence(
        chunk_id=_uuid_value(payload.get("chunk_id")),
        version_id=_uuid_value(payload.get("version_id")),
        source_title=str(payload.get("source_title") or ""),
        layer=str(payload.get("layer") or ""),
        access_level=str(payload.get("access_level") or ""),
        text=str(payload.get("text") or ""),
        section_path=tuple(str(value) for value in section_path),
        page_from=_optional_int(payload.get("page_from")),
        page_to=_optional_int(payload.get("page_to")),
        source_uri=str(source_uri) if source_uri is not None else None,
        checksum=str(payload.get("checksum") or ""),
        scores={score_name: float(getattr(point, "score"))},
    )


def _uuid_value(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if value is None:
        raise ValueError("Qdrant point is missing a UUID payload field")
    return UUID(str(value))


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
