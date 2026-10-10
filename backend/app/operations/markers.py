from __future__ import annotations

TEST_MARKER = "test"
DRILL_MARKER = "drill"
TEST_FILENAME_PREFIX = "【测试】"
DRILL_FILENAME_PREFIX = "【演练】"


def operation_marker(event_type: str | None) -> str | None:
    if event_type == "test":
        return TEST_MARKER
    if event_type == "drill":
        return DRILL_MARKER
    return None


def production_mode_marker(production_mode: str | None) -> str | None:
    if production_mode == "test":
        return TEST_MARKER
    if production_mode == "drill":
        return DRILL_MARKER
    return None


def marker_filename_prefix(marker: str | None) -> str:
    if marker == TEST_MARKER:
        return TEST_FILENAME_PREFIX
    if marker == DRILL_MARKER:
        return DRILL_FILENAME_PREFIX
    return ""


def enforce_marker_file_name(file_name: str, marker: str | None) -> str:
    normalized = str(file_name).replace("\\", "/")
    if (
        not normalized
        or normalized != normalized.rsplit("/", 1)[-1]
        or normalized in {".", ".."}
        or normalized.startswith("/")
        or (len(normalized) >= 2 and normalized[1] == ":")
    ):
        raise ValueError("file_name must not be empty")
    prefix = marker_filename_prefix(marker)
    if not prefix:
        return normalized
    return f"{prefix}{normalized.removeprefix(prefix)}"


def enforce_marker_text(
    text_result: dict | None,
    marker: str | None,
) -> dict | None:
    if text_result is None:
        return None
    marked = dict(text_result)
    if marker is None:
        marked.pop("_operation_marker", None)
    else:
        marked["_operation_marker"] = marker
    return marked
