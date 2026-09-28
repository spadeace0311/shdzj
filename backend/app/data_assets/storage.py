from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class StoredFile:
    file_name: str
    relative_path: str
    managed_path: str
    size_bytes: int
    checksum: str


class ManagedFileStore:
    def __init__(self, root: str | Path, *, max_upload_bytes: int) -> None:
        self._root = Path(root).resolve()
        self._max_upload_bytes = max_upload_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    def store_upload(
        self,
        source: BinaryIO,
        *,
        file_name: str,
        expected_checksum: str | None = None,
    ) -> StoredFile:
        safe_name = self._safe_name(file_name)
        digest = hashlib.sha256()
        size = 0
        temporary = self._root / f".upload-{os.getpid()}-{id(source)}.tmp"
        try:
            with temporary.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > self._max_upload_bytes:
                        raise ValueError("upload exceeds configured maximum size")
                    digest.update(chunk)
                    target.write(chunk)
            checksum = digest.hexdigest()
            if expected_checksum is not None and checksum != expected_checksum.lower():
                raise ValueError("upload checksum does not match")
            relative = Path(checksum[:2]) / checksum[2:4] / f"{checksum}-{safe_name}"
            destination = self._root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                temporary.unlink()
            else:
                temporary.replace(destination)
            return StoredFile(
                file_name=safe_name,
                relative_path=relative.as_posix(),
                managed_path=str(destination),
                size_bytes=size,
                checksum=checksum,
            )
        finally:
            if temporary.exists():
                temporary.unlink()

    def resolve(self, relative_path: str) -> Path:
        candidate = (self._root / relative_path).resolve()
        if self._root not in candidate.parents:
            raise ValueError("managed path escapes storage root")
        return candidate

    @staticmethod
    def _safe_name(file_name: str) -> str:
        if not file_name or Path(file_name).name != file_name:
            raise ValueError("file name must not contain path segments")
        if any(character in file_name for character in ("\x00", "\r", "\n")):
            raise ValueError("file name contains invalid characters")
        return file_name[:255]


def copy_host_file(
    store: ManagedFileStore,
    source_path: str | Path,
    *,
    file_name: str,
) -> StoredFile:
    with Path(source_path).open("rb") as source:
        return store.store_upload(source, file_name=file_name)
