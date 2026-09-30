"""Versioned professional artifact file naming."""

import math
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.artifacts.domain import ArtifactNameContext, ProductionMode

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ALLOWED_EXTENSIONS = frozenset({"jpg", "png", "docx", "pptx"})
_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_REPEATED_UNDERSCORES = re.compile(r"_+")
_MODE_MARKERS = {
    ProductionMode.LIVE: "",
    ProductionMode.MANUAL: "",
    ProductionMode.TEST: "【测试】",
    ProductionMode.DRILL: "【演练】",
    ProductionMode.REPLAY: "【测试回放】",
}


def _sanitize_component(value: str) -> str:
    sanitized = _WINDOWS_ILLEGAL.sub("_", value)
    sanitized = _REPEATED_UNDERSCORES.sub("_", sanitized)
    sanitized = sanitized.strip(" ._")
    if not sanitized:
        raise ValueError("artifact name component cannot be empty")
    return sanitized


def _shorten_component(value: str, limit: int) -> str:
    value = _sanitize_component(value)
    if len(value) <= limit:
        return value
    shortened = value[:limit].rstrip(" ._")
    if not shortened:
        raise ValueError("artifact name component is empty after truncation")
    return shortened


def _shanghai_datetime(value: str | datetime) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("generated_at must be an ISO-8601 timestamp") from error
    if not isinstance(value, datetime):
        raise TypeError("generated_at must be a string or datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=_SHANGHAI)
    return value.astimezone(_SHANGHAI)


def build_artifact_file_name(
    context: ArtifactNameContext,
    *,
    extension: str,
) -> str:
    if not isinstance(extension, str):
        raise TypeError("extension must be a string")
    normalized_extension = extension.lower()
    if normalized_extension not in _ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(_ALLOWED_EXTENSIONS))
        raise ValueError(f"unsupported artifact extension {extension!r}; expected {allowed}")

    if isinstance(context.version, bool) or not isinstance(context.version, int):
        raise TypeError("artifact version must be an integer")
    if not 1 <= context.version <= 999:
        raise ValueError("artifact version must be between 1 and 999")
    if not math.isfinite(context.magnitude):
        raise ValueError("earthquake magnitude must be finite")

    generated_at = _shanghai_datetime(context.generated_at)
    place = _shorten_component(context.place, 80)
    display_name = _shorten_component(context.display_name, 120)
    marker = _MODE_MARKERS[context.production_mode]

    stem = (
        f"{marker}{place}_{context.magnitude:.1f}级地震_{display_name}_"
        f"V{context.version:03d}_{generated_at:%Y%m%d-%H%M%S}"
    )
    stem = _REPEATED_UNDERSCORES.sub("_", stem).strip(" ._")
    file_name = f"{stem}.{normalized_extension}"
    if len(file_name) > 240:
        overflow = len(file_name) - 240
        display_name = display_name[: max(1, len(display_name) - overflow)].rstrip(" ._")
        stem = (
            f"{marker}{place}_{context.magnitude:.1f}级地震_{display_name}_"
            f"V{context.version:03d}_{generated_at:%Y%m%d-%H%M%S}"
        )
        stem = _REPEATED_UNDERSCORES.sub("_", stem).strip(" ._")
        file_name = f"{stem}.{normalized_extension}"
    if len(file_name) > 240:
        raise ValueError("artifact file name exceeds the supported length")
    if Path(file_name).name != file_name:
        raise ValueError("artifact file name must not contain path components")
    return file_name
