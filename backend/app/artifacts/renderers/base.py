from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any


class RemoteAssetForbiddenError(ValueError):
    """Raised when a render spec attempts to load a remote asset."""


class LocalAssetMissingError(FileNotFoundError):
    """Raised when a required local render asset cannot be resolved."""


@dataclass(frozen=True, slots=True)
class RenderQuality:
    grade: str | None = None
    needs_review: bool = False
    missing_assets: tuple[str, ...] = ()
    degradation_reasons: tuple[str, ...] = ()
    spatialized_estimate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "needs_review": self.needs_review,
            "missing_assets": list(self.missing_assets),
            "degradation_reasons": list(self.degradation_reasons),
            "spatialized_estimate": self.spatialized_estimate,
        }


@dataclass(frozen=True, slots=True)
class RenderResult:
    path: Path
    format: str
    width: int
    height: int
    dpi: int
    checksum: str
    quality: RenderQuality
    task_status: str
    file_name: str
    render_manifest: dict[str, Any]
    non_empty_ratio: float = 0.0
    size_bytes: int = 0
    generated_at: datetime | None = field(default=None)
    page_count: int | None = None
    control_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.width <= 0:
            raise ValueError("render width must be positive")
        if self.height <= 0:
            raise ValueError("render height must be positive")
        if self.dpi <= 0:
            raise ValueError("render dpi must be positive")
        if len(self.checksum) != 64:
            raise ValueError("render checksum must be a SHA-256 hex digest")
        if self.task_status not in {"succeeded", "degraded"}:
            raise ValueError("render task_status must be succeeded or degraded")
        if not 0.0 <= self.non_empty_ratio <= 1.0:
            raise ValueError("non_empty_ratio must be between zero and one")
        if self.size_bytes < 0:
            raise ValueError("render size_bytes must not be negative")
        if self.page_count is not None and self.page_count <= 0:
            raise ValueError("render page_count must be positive when provided")
