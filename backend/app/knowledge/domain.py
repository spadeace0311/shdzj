from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SourceLayer(StrEnum):
    LOCAL_AUTHORITY = "local_authority"
    STRUCTURED_LIVE = "structured_live"
    PUBLIC_REFERENCE = "public_reference"


class KnowledgeVersionStatus(StrEnum):
    REGISTERED = "registered"
    UPLOADED = "uploaded"
    PARSING = "parsing"
    PARSED = "parsed"
    EMBEDDING = "embedding"
    INDEXED = "indexed"
    PUBLISHED = "published"
    FAILED = "failed"
    DISABLED = "disabled"


class KnowledgeJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class KnowledgeVersionDisabledError(ValueError):
    """Raised when a disabled knowledge version is used for live operations."""


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    text: str
    section_path: tuple[str, ...]
    page_from: int | None
    page_to: int | None
    table_range: tuple[int, int] | None
    metadata: dict[str, Any] = field(default_factory=dict)
