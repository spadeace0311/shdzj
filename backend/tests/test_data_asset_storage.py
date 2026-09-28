import hashlib
from io import BytesIO
from pathlib import Path

import pytest

from app.data_assets.storage import ManagedFileStore


def test_store_upload_writes_checksum_addressed_file(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_upload_bytes=1024)
    payload = b'{"type":"FeatureCollection","features":[]}'

    stored = store.store_upload(
        BytesIO(payload),
        file_name="boundary.geojson",
        expected_checksum=hashlib.sha256(payload).hexdigest(),
    )

    assert stored.checksum == hashlib.sha256(payload).hexdigest()
    assert stored.size_bytes == len(payload)
    assert Path(stored.managed_path).read_bytes() == payload
    assert store.resolve(stored.relative_path) == Path(stored.managed_path)


def test_store_upload_rejects_traversal_and_oversize(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_upload_bytes=4)

    with pytest.raises(ValueError, match="file name"):
        store.store_upload(BytesIO(b"data"), file_name="../escape.geojson")
    with pytest.raises(ValueError, match="maximum"):
        store.store_upload(BytesIO(b"12345"), file_name="large.geojson")


def test_store_upload_rejects_checksum_mismatch(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_upload_bytes=1024)
    payload = b'{"type":"FeatureCollection","features":[]}'

    with pytest.raises(ValueError, match="checksum"):
        store.store_upload(
            BytesIO(payload),
            file_name="boundary.geojson",
            expected_checksum="0" * 64,
        )


def test_resolve_rejects_path_that_escapes_root(tmp_path: Path) -> None:
    store = ManagedFileStore(tmp_path, max_upload_bytes=1024)

    with pytest.raises(ValueError, match="escapes"):
        store.resolve("../outside.geojson")
