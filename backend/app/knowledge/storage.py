from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class StoredKnowledgeFile:
    file_name: str
    relative_path: str
    managed_path: str
    size_bytes: int
    checksum: str


class KnowledgeFileStore:
    def __init__(self, root: str | Path, *, max_upload_bytes: int) -> None:
        self._root = Path(root).resolve()
        self._max_upload_bytes = max_upload_bytes
        self._root.mkdir(parents=True, exist_ok=True)

    async def store_upload(
        self,
        source: BinaryIO,
        *,
        file_name: str,
        source_id: str,
        version: str,
    ) -> StoredKnowledgeFile:
        del source_id, version
        safe_name = self._safe_name(file_name)
        digest = hashlib.sha256()
        size = 0
        temporary = self._root / f".upload-{os.getpid()}-{id(source)}.tmp"
        try:
            with temporary.open("wb") as target:
                while chunk := await _read_upload_chunk(source, 1024 * 1024):
                    size += len(chunk)
                    if size > self._max_upload_bytes:
                        raise ValueError("upload exceeds configured maximum size")
                    digest.update(chunk)
                    await asyncio.to_thread(target.write, chunk)

            checksum = digest.hexdigest()
            relative = (
                Path("objects")
                / checksum[:2]
                / checksum[2:4]
                / f"{checksum}-{safe_name}"
            )
            destination = self._root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                await asyncio.to_thread(temporary.unlink)
            else:
                await asyncio.to_thread(temporary.replace, destination)
            return StoredKnowledgeFile(
                file_name=safe_name,
                relative_path=relative.as_posix(),
                managed_path=str(destination),
                size_bytes=size,
                checksum=checksum,
            )
        finally:
            if temporary.exists():
                await asyncio.to_thread(temporary.unlink)

    def write_text(self, relative_path: str, text: str) -> StoredKnowledgeFile:
        # The supplied path is only validated; parsed text is always stored by
        # content address under parsed/ so later versions cannot overwrite it.
        self.resolve(relative_path)
        encoded = text.encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        relative = Path("parsed") / f"{checksum}.txt"
        destination = self._root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)
        return StoredKnowledgeFile(
            file_name=f"{checksum}.txt",
            relative_path=relative.as_posix(),
            managed_path=str(destination),
            size_bytes=len(encoded),
            checksum=checksum,
        )

    def resolve(self, relative_path: str) -> Path:
        candidate = (self._root / relative_path).resolve()
        try:
            candidate.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("managed path escapes storage root") from exc
        return candidate

    @staticmethod
    def _safe_name(file_name: str) -> str:
        if (
            not file_name
            or "/" in file_name
            or "\\" in file_name
            or Path(file_name).name != file_name
        ):
            raise ValueError("file name must not contain path separator")
        if any(character in file_name for character in ("\x00", "\r", "\n")):
            raise ValueError("file name contains invalid characters")
        return file_name[:255]


async def _read_upload_chunk(source: BinaryIO, size: int) -> bytes:
    read = source.read
    if inspect.iscoroutinefunction(read):
        return await read(size)
    return await asyncio.to_thread(read, size)
