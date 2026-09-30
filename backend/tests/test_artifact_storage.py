from __future__ import annotations

from io import BytesIO

import pytest

from app.artifacts.domain import ArtifactDefinition, ArtifactKind, ProductionMode
from app.artifacts.storage import ArtifactStore
from app.artifacts.validation import ArtifactValidator


def _map_definition(*, file_format: str = "jpg") -> ArtifactDefinition:
    return ArtifactDefinition(
        artifact_key="map.epicenter",
        display_name="震中位置分布图",
        kind=ArtifactKind.MAP,
        output_profile="a3v-professional",
        format=file_format,
        legacy_code="M26",
        priority=80,
        depends_on=(),
        optional_depends_on=(),
        required_assets=(),
        optional_assets=(),
        template_key="map.epicenter",
        marker_policy="mode",
        failure_policy="block",
        quality_policy="A",
    )


def test_store_is_content_addressed_and_rejects_path_escape(tmp_path) -> None:
    store = ArtifactStore(tmp_path, max_override_bytes=1024)
    stored = store.store_immutable_stream(
        BytesIO(b"artifact-bytes"),
        file_name="map.jpg",
    )

    assert (
        stored.checksum
        == "6521df166eb07efaf36eba5b6bedefd9d6a252e9c80bab1c99653700ec71473c"
    )
    assert stored.relative_path.endswith("-map.jpg")
    with pytest.raises(ValueError):
        store.resolve("../outside.jpg")


def test_store_immutable_stream_cleans_staging(tmp_path) -> None:
    store = ArtifactStore(tmp_path, max_override_bytes=1024)
    store.store_immutable_stream(BytesIO(b"artifact-bytes"), file_name="map.jpg")

    staging = tmp_path / "staging"
    assert staging.exists()
    assert list(staging.iterdir()) == []


def test_delete_unreferenced_removes_object(tmp_path) -> None:
    store = ArtifactStore(tmp_path, max_override_bytes=1024)
    stored = store.store_immutable_stream(
        BytesIO(b"artifact-bytes"),
        file_name="map.jpg",
    )

    assert stored.managed_path.exists()
    store.delete_unreferenced(stored)
    assert not stored.managed_path.exists()


def test_store_rejects_oversized_stream_and_cleans_staging(tmp_path) -> None:
    store = ArtifactStore(tmp_path, max_override_bytes=4)

    with pytest.raises(ValueError):
        store.store_immutable_stream(
            BytesIO(b"too-large"),
            file_name="map.jpg",
        )

    assert list((tmp_path / "staging").iterdir()) == []


def test_resolve_rejects_absolute_and_backslash_escapes(tmp_path) -> None:
    store = ArtifactStore(tmp_path, max_override_bytes=1024)

    for candidate in ("/etc/passwd", "..\\outside.jpg", "C:\\outside.jpg"):
        with pytest.raises(ValueError):
            store.resolve(candidate)


def test_validator_rejects_extension_mismatch(tmp_path) -> None:
    path = tmp_path / "fake.jpg"
    path.write_bytes(b"not-an-image")

    result = ArtifactValidator().validate(
        path,
        _map_definition(),
        production_mode=ProductionMode.LIVE,
    )

    assert result.valid is False
    assert result.error_category == "format_mismatch"


def test_validator_rejects_map_with_wrong_dimensions(tmp_path) -> None:
    path = tmp_path / "small.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")

    result = ArtifactValidator().validate(
        path,
        _map_definition(),
        production_mode=ProductionMode.LIVE,
    )

    assert result.valid is False
    assert result.error_category in {"format_mismatch", "geometry_mismatch"}
