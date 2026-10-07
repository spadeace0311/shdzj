from io import BytesIO
from pathlib import Path

import pytest

from app.knowledge.storage import KnowledgeFileStore


def test_store_upload_is_content_addressed_and_path_safe(tmp_path: Path) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=1024)
    stored = store.store_upload(
        BytesIO(b"knowledge"),
        file_name="evidence.txt",
        source_id="src_1",
        version="v1",
    )
    assert stored.relative_path.endswith("-evidence.txt")
    assert store.resolve(stored.relative_path).read_bytes() == b"knowledge"

    with pytest.raises(ValueError, match="path separator"):
        store.store_upload(
            BytesIO(b"bad"),
            file_name="../escape.txt",
            source_id="src_1",
            version="v2",
        )
    with pytest.raises(ValueError, match="path separator"):
        store.store_upload(
            BytesIO(b"bad"),
            file_name="..\\escape.txt",
            source_id="src_1",
            version="v3",
        )


def test_store_upload_enforces_maximum_size(tmp_path: Path) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=8)

    with pytest.raises(ValueError, match="upload exceeds configured maximum size"):
        store.store_upload(
            BytesIO(b"more than eight bytes"),
            file_name="large.txt",
            source_id="src_1",
            version="v1",
        )


def test_write_text_uses_content_addressed_parsed_path(tmp_path: Path) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=1024)

    stored = store.write_text("ignored-name.txt", "parsed knowledge")

    assert stored.relative_path.startswith("parsed/")
    assert stored.relative_path.endswith(".txt")
    assert store.resolve(stored.relative_path).read_text(encoding="utf-8") == (
        "parsed knowledge"
    )


def test_resolve_rejects_path_escape(tmp_path: Path) -> None:
    store = KnowledgeFileStore(tmp_path, max_upload_bytes=1024)

    with pytest.raises(ValueError, match="escape"):
        store.resolve("../outside.txt")
