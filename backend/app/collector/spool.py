from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.collector.domain import (
    CollectorEnvelope,
    CollectorLane,
    CollectorProvider,
)


class CollectorSpool:
    """Durable one-envelope-per-file JSON spool with bounded capacity."""

    def __init__(self, directory: str | Path, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._directory = Path(directory)
        self._max_bytes = max_bytes
        self._directory.mkdir(parents=True, exist_ok=True)

    def append(self, envelope: CollectorEnvelope) -> Path:
        if not isinstance(envelope, CollectorEnvelope):
            raise TypeError("envelope must be a CollectorEnvelope")

        data = json.dumps(
            {
                "provider": envelope.provider.value,
                "lane": envelope.lane.value,
                "received_at": envelope.received_at.isoformat(),
                "payload": envelope.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

        if self._usage_bytes() + len(data) > self._max_bytes:
            raise OverflowError("collector spool capacity exceeded")

        filename = self._filename(envelope.received_at)
        path = self._directory / filename
        temp_path = self._directory / f".{filename}.{uuid.uuid4().hex}.tmp"
        try:
            with temp_path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            self._fsync_directory()
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return path

    def iter_pending(self) -> Iterator[tuple[Path, CollectorEnvelope]]:
        pending: list[tuple[Path, CollectorEnvelope]] = []
        for path in self._directory.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                envelope = self._deserialize(payload)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            pending.append((path, envelope))
        pending.sort(key=lambda pair: pair[1].received_at)
        yield from pending

    def remove(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
            self._fsync_directory()
        except OSError:
            pass

    def _usage_bytes(self) -> int:
        total = 0
        for path in self._directory.glob("*.json"):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def _filename(self, received_at: datetime) -> str:
        stamp = received_at.isoformat().replace(":", "").replace("+", "")
        return f"envelope-{stamp}-{uuid.uuid4().hex}.json"

    @staticmethod
    def _deserialize(payload: dict[str, object]) -> CollectorEnvelope:
        received_at = datetime.fromisoformat(str(payload["received_at"]))
        return CollectorEnvelope(
            provider=CollectorProvider(str(payload["provider"])),
            lane=CollectorLane(str(payload["lane"])),
            received_at=received_at,
            payload=payload["payload"],
        )

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            fd = os.open(self._directory, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
