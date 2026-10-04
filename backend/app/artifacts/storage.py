from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.config import settings


_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/][^\s\"']+|\\\\[^\s\"']+|/[^\s\"']+)"
)


@dataclass(frozen=True, slots=True)
class StoredArtifactFile:
    file_name: str
    relative_path: str
    managed_path: Path
    size_bytes: int
    checksum: str


class ArtifactStore:
    """Local content-addressed artifact store with an S3-compatible boundary."""

    def __init__(
        self,
        root: str | Path,
        *,
        namespace: str | None = None,
        max_override_bytes: int | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._storage_namespace = (
            _safe_storage_namespace(namespace)
            if namespace is not None
            else _root_storage_namespace(self._root)
        )
        self._staging = self._root / "staging"
        self._objects = self._root / "objects"
        self._max_override_bytes = (
            settings.artifact_max_override_bytes
            if max_override_bytes is None
            else max_override_bytes
        )
        if self._max_override_bytes < 1:
            raise ValueError("max_override_bytes must be positive")

    @property
    def storage_namespace(self) -> str:
        return self._storage_namespace

    def stage(self, source: BinaryIO, *, file_name: str) -> Path:
        safe_name = _safe_file_name(file_name)
        suffix = Path(safe_name).suffix
        self._staging.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix="stage-",
            suffix=suffix,
            dir=self._staging,
        )
        temp_path = Path(temp_name)
        try:
            total = 0
            with os.fdopen(descriptor, "wb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self._max_override_bytes:
                        raise ValueError("artifact upload exceeds maximum size")
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return temp_path

    def store_immutable(
        self,
        path: Path,
        *,
        file_name: str,
    ) -> StoredArtifactFile:
        source = Path(path)
        if not source.is_file():
            raise ValueError("artifact staging path is not a regular file")
        size_bytes = source.stat().st_size
        if size_bytes <= 0:
            raise ValueError("artifact file must not be empty")
        if size_bytes > self._max_override_bytes:
            raise ValueError("artifact upload exceeds maximum size")

        checksum = _sha256_path(source)
        safe_name = _safe_file_name(file_name)
        relative_path = f"objects/{checksum[:2]}/{checksum[2:4]}/" f"{checksum}-{safe_name}"
        object_path = self._root / relative_path
        object_path.parent.mkdir(parents=True, exist_ok=True)

        if object_path.exists():
            return StoredArtifactFile(
                file_name=safe_name,
                relative_path=relative_path,
                managed_path=object_path,
                size_bytes=size_bytes,
                checksum=checksum,
            )

        descriptor, temp_name = tempfile.mkstemp(
            prefix=".object-",
            dir=object_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with source.open("rb") as source_file:
                with os.fdopen(descriptor, "wb") as target_file:
                    shutil.copyfileobj(source_file, target_file, 1024 * 1024)
                    target_file.flush()
                    os.fsync(target_file.fileno())
            os.replace(temp_path, object_path)
            _fsync_directory(object_path.parent)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

        return StoredArtifactFile(
            file_name=safe_name,
            relative_path=relative_path,
            managed_path=object_path,
            size_bytes=size_bytes,
            checksum=checksum,
        )

    def store_immutable_stream(
        self,
        source: BinaryIO,
        *,
        file_name: str,
    ) -> StoredArtifactFile:
        staged = self.stage(source, file_name=file_name)
        try:
            return self.store_immutable(staged, file_name=file_name)
        finally:
            staged.unlink(missing_ok=True)

    def relative_path_for(self, checksum: str, *, file_name: str) -> str:
        if len(checksum) != 64:
            raise ValueError("checksum must contain 64 hexadecimal characters")
        try:
            int(checksum, 16)
        except ValueError as error:
            raise ValueError("checksum must contain 64 hexadecimal characters") from error
        safe_name = _safe_file_name(file_name)
        return f"objects/{checksum[:2]}/{checksum[2:4]}/" f"{checksum}-{safe_name}"

    def resolve(self, relative_path: str) -> Path:
        normalized = _safe_relative_path(relative_path)
        root = self._root
        candidate = (root / normalized).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ValueError("artifact path escapes storage root") from error
        return candidate

    def delete_unreferenced(self, stored: StoredArtifactFile) -> None:
        path = Path(stored.managed_path).resolve(strict=False)
        root = self._root
        try:
            path.relative_to(root)
        except ValueError as error:
            raise ValueError("artifact path escapes storage root") from error
        if path.exists():
            path.unlink()

    def redact_error(self, error: BaseException, *, relative_path: str | None = None) -> str:
        message = str(error)
        replacements: list[tuple[str, str]] = [(str(self._root), "<artifact-root>")]
        if relative_path:
            replacements.append((str(self.resolve(relative_path)), "<artifact-object>"))
        for value, replacement in sorted(
            replacements,
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            message = message.replace(value, replacement)
        return _ABSOLUTE_PATH_PATTERN.sub("<path>", message)[:2000]


def _safe_file_name(file_name: str) -> str:
    if not isinstance(file_name, str) or not file_name.strip():
        raise ValueError("file_name must not be empty")
    normalized = file_name.replace("\\", "/")
    if normalized != normalized.split("/")[-1]:
        raise ValueError("file_name must not contain a path separator")
    if normalized in {".", ".."}:
        raise ValueError("file_name must not be a path traversal")
    safe = normalized.strip()
    if "\x00" in safe:
        raise ValueError("file_name must not contain NUL bytes")
    return safe


def _safe_relative_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("relative_path must not be empty")
    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("relative_path must be relative")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative_path must not contain path traversal")
    return normalized


def _root_storage_namespace(root: Path) -> str:
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:40]
    return f"local-root-v1:{digest}"


def _safe_storage_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("storage namespace must not be empty")
    value = namespace.strip()
    if len(value) > 128:
        raise ValueError("storage namespace is too long")
    if any(character.isspace() for character in value):
        raise ValueError("storage namespace must not contain whitespace")
    return value


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
