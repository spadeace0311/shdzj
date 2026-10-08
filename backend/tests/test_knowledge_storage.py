from io import BytesIO
from pathlib import Path

import pytest

from app.knowledge.storage import KnowledgeFileStore


class _ChunkedAsyncSource:
    def __init__(self, payload: bytes, *, chunk_size: int) -> None:
        self.payload = payload
        self.chunk_size = chunk_size
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if not self.payload:
            return b""
        chunk = self.payload[: min(size, self.chunk_size)]
        self.payload = self.payload[len(chunk) :]
        return chunk


async def test_store_upload_is_content_addressed_and_path_safe(
    tmp_path: Path,
) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=1024)
    stored = await store.store_upload(
        BytesIO(b"knowledge"),
        file_name="evidence.txt",
        source_id="src_1",
        version="v1",
    )
    assert stored.relative_path.endswith("-evidence.txt")
    assert store.resolve(stored.relative_path).read_bytes() == b"knowledge"

    with pytest.raises(ValueError, match="path separator"):
        await store.store_upload(
            BytesIO(b"bad"),
            file_name="../escape.txt",
            source_id="src_1",
            version="v2",
        )
    with pytest.raises(ValueError, match="path separator"):
        await store.store_upload(
            BytesIO(b"bad"),
            file_name="..\\escape.txt",
            source_id="src_1",
            version="v3",
        )


async def test_store_upload_enforces_maximum_size(tmp_path: Path) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=8)

    with pytest.raises(ValueError, match="upload exceeds configured maximum size"):
        await store.store_upload(
            BytesIO(b"more than eight bytes"),
            file_name="large.txt",
            source_id="src_1",
            version="v1",
        )


async def test_store_upload_uses_bounded_async_reads(tmp_path: Path) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=32)
    source = _ChunkedAsyncSource(b"0123456789", chunk_size=3)

    stored = await store.store_upload(
        source,
        file_name="chunked.txt",
        source_id="src_1",
        version="v1",
    )

    assert stored.size_bytes == 10
    assert source.read_sizes == [1024 * 1024] * 5
    assert store.resolve(stored.relative_path).read_bytes() == b"0123456789"


async def test_store_upload_cleans_temporary_file_after_oversize(
    tmp_path: Path,
) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=5)

    with pytest.raises(ValueError, match="upload exceeds configured maximum size"):
        await store.store_upload(
            BytesIO(b"0123456789"),
            file_name="too-large.txt",
            source_id="src_1",
            version="v1",
        )

    assert list(tmp_path.glob(".upload-*.tmp")) == []


async def test_write_text_uses_content_addressed_parsed_path(tmp_path: Path) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=1024)

    stored = store.write_text("ignored-name.txt", "parsed knowledge")

    assert stored.relative_path.startswith("parsed/")
    assert stored.relative_path.endswith(".txt")
    assert store.resolve(stored.relative_path).read_text(encoding="utf-8") == (
        "parsed knowledge"
    )


async def test_resolve_rejects_path_escape(tmp_path: Path) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=1024)

    with pytest.raises(ValueError, match="escape"):
        store.resolve("../outside.txt")
