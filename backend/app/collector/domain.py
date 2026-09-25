from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class CollectorProvider(StrEnum):
    FAN = "fan"
    WOLFX = "wolfx"


class CollectorLane(StrEnum):
    WEBSOCKET = "websocket"
    HTTP = "http"


@dataclass(frozen=True, slots=True)
class CollectorEnvelope:
    provider: CollectorProvider
    lane: CollectorLane
    received_at: datetime
    payload: dict[str, object] = field(repr=False)
    trigger_reason: str = "live"
    recovery_complete: bool = False

    def __post_init__(self) -> None:
        if self.received_at.tzinfo is None or self.received_at.utcoffset() is None:
            raise ValueError("received_at must include timezone information")
        if self.trigger_reason not in {"live", "recovery"}:
            raise ValueError("trigger_reason must be either 'live' or 'recovery'")
        object.__setattr__(self, "received_at", self.received_at.astimezone(UTC))
        object.__setattr__(self, "payload", dict(self.payload))


@dataclass(frozen=True, slots=True)
class ProviderHealthUpdate:
    provider: CollectorProvider
    state: str
    connected: bool
    last_http_status: int | None
    last_connected_at: datetime | None
    last_message_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    reconnect_count: int
    last_error: str | None
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.state not in {"starting", "healthy", "degraded", "critical", "stopped"}:
            raise ValueError("invalid collector state")
        if self.consecutive_failures < 0 or self.reconnect_count < 0:
            raise ValueError("collector counters must not be negative")
