from __future__ import annotations

import re
from decimal import Decimal
from typing import Protocol

from app.collector.domain import CollectorEnvelope, CollectorLane, CollectorProvider
from app.events.domain import EventKind, NormalizedEvent
from app.events.response_rules import ResponseInput
from app.events.sources.cenc import CencAdapter
from app.events.service import LifecycleIngestOutcome
from app.regions.domain import RegionContext


class CancellationIgnored(ValueError):
    """A business cancellation that must be ignored without dead-lettering."""


_NO_KEY = re.compile(r"^No\d+$")


class RegionContextResolverLike(Protocol):
    async def resolve(self, longitude: Decimal, latitude: Decimal) -> RegionContext:
        ...


class EventServiceLike(Protocol):
    async def ingest_collected(
        self,
        raw_payload: dict[str, object],
        event: NormalizedEvent,
        provider: str,
        lane: str,
        received_at: object,
        response_input: ResponseInput | None,
        region_context: RegionContext | None,
        trigger_reason: str = "live",
    ) -> LifecycleIngestOutcome:
        ...


class CollectorCoordinator:
    """Expand provider matrices into single-event envelopes and ingest them."""

    def __init__(
        self,
        event_service: EventServiceLike,
        region_resolver: RegionContextResolverLike,
        adapter: CencAdapter | None = None,
    ) -> None:
        self._event_service = event_service
        self._region_resolver = region_resolver
        self._adapter = adapter or CencAdapter()

    def expand(self, envelope: CollectorEnvelope) -> list[CollectorEnvelope]:
        _validate_provider_lane(envelope.provider, envelope.lane)
        payload = envelope.payload
        if not isinstance(payload, dict):
            return []

        if envelope.provider is CollectorProvider.FAN:
            items = _numeric_items(payload)
            return [
                CollectorEnvelope(
                    provider=envelope.provider,
                    lane=envelope.lane,
                    received_at=envelope.received_at,
                    payload={key: item},
                    trigger_reason=envelope.trigger_reason,
                    recovery_complete=envelope.recovery_complete,
                )
                for key, item in items
            ]

        if envelope.provider is CollectorProvider.WOLFX:
            return [envelope] if _numeric_items(payload) else []

        return []

    async def ingest(
        self,
        envelope: CollectorEnvelope,
        trigger_reason: str = "live",
    ) -> LifecycleIngestOutcome | None:
        _validate_provider_lane(envelope.provider, envelope.lane)
        item = _single_item(envelope.payload)
        if _is_cancellation(item):
            return None

        event = self._adapter.parse(item)
        if event.kind in {EventKind.TEST, EventKind.DRILL}:
            return None
        if event.source != "cenc":
            raise ValueError("collector coordinator only accepts CENC events")

        region_context = await self._region_resolver.resolve(
            event.longitude,
            event.latitude,
        )
        response_input = ResponseInput(
            magnitude=event.magnitude,
            depth_km=event.depth_km,
            inside_shanghai=region_context.inside_shanghai,
            distance_to_boundary_km=region_context.distance_to_boundary_km,
            deaths=None,
            max_intensity=None,
        )

        return await self._event_service.ingest_collected(
            raw_payload=item,
            event=event,
            provider=envelope.provider.value,
            lane=envelope.lane.value,
            received_at=envelope.received_at,
            response_input=response_input,
            region_context=region_context,
            trigger_reason=trigger_reason,
        )


def _validate_provider_lane(
    provider: CollectorProvider,
    lane: CollectorLane,
) -> None:
    if provider is CollectorProvider.FAN and lane is not CollectorLane.WEBSOCKET:
        raise ValueError("FAN envelopes must use the websocket lane")
    if provider is CollectorProvider.WOLFX and lane is not CollectorLane.HTTP:
        raise ValueError("Wolfx envelopes must use the http lane")


def _numeric_items(payload: dict[str, object]) -> list[tuple[str, dict[str, object]]]:
    items: list[tuple[str, dict[str, object]]] = []
    for key in payload:
        if not isinstance(key, str) or not _NO_KEY.fullmatch(key):
            continue
        item = payload[key]
        if isinstance(item, dict):
            items.append((key, item))
    items.sort(key=lambda pair: int(pair[0][2:]))
    return items


def _single_item(payload: dict[str, object]) -> dict[str, object]:
    items = _numeric_items(payload)
    if len(items) != 1:
        raise ValueError("collector coordinator expects a single-event envelope")
    return items[0][1]


def _is_cancellation(payload: dict[str, object]) -> bool:
    if any(payload.get(field) is True for field in ("cancel", "isCancel", "isCanceled")):
        return True
    for field in ("reportType", "type", "infoTypeName"):
        value = payload.get(field)
        if isinstance(value, str):
            normalized = value.casefold()
            if any(marker in normalized for marker in ("cancel", "取消")):
                return True
    return False
