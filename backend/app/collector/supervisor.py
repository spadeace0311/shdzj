from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

from sqlalchemy.exc import SQLAlchemyError

from app.collector.domain import (
    CollectorEnvelope,
    CollectorProvider,
    ProviderHealthUpdate,
)
from app.collector.service import classify_collector_error
from app.events.domain import NormalizedEvent
from app.events.sources.cenc import CencAdapter

logger = logging.getLogger(__name__)

_LIVE = "live"
_RECOVERY = "recovery"
_RETRY_DELAYS = (1.0, 2.0, 4.0)


class CollectorSupervisor:
    def __init__(
        self,
        *,
        settings,
        service,
        coordinator,
        spool,
        fan_collector,
        wolfx_collector,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._service = service
        self._coordinator = coordinator
        self._spool = spool
        self._fan_collector = fan_collector
        self._wolfx_collector = wolfx_collector
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._adapter = CencAdapter()
        self._health: dict[CollectorProvider, str] = {
            CollectorProvider.FAN: "starting",
            CollectorProvider.WOLFX: "starting",
        }
        self._queue: asyncio.Queue[CollectorEnvelope] | None = None
        self._stop_event: asyncio.Event | None = None
        self._bootstrap_cutoff: datetime | None = None

    @property
    def provider_states(self) -> dict[str, str]:
        return {
            provider.value: state
            for provider, state in self._health.items()
        }

    def health_is_available(self) -> bool:
        states = set(self._health.values())
        return any(state in {"healthy", "degraded"} for state in states)

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        self._stop_event = stop_event or asyncio.Event()
        stop_event = self._stop_event
        self._queue = asyncio.Queue(maxsize=2_000)

        await self._set_provider_state(CollectorProvider.FAN, "starting")
        await self._set_provider_state(CollectorProvider.WOLFX, "starting")

        watermark = await self._load_watermark()
        if watermark is None:
            self._bootstrap_cutoff = self._now() - timedelta(
                hours=self._settings.cenc_bootstrap_lookback_hours
            )
        else:
            self._bootstrap_cutoff = None

        if not await self._replay_spool():
            await self._set_provider_state(CollectorProvider.FAN, "critical")
            await self._set_provider_state(CollectorProvider.WOLFX, "critical")
            return

        tasks = [
            asyncio.create_task(
                self._fan_collector.run(
                    on_envelope=self._enqueue_envelope,
                    on_health=self._on_health,
                    stop_event=stop_event,
                )
            ),
            asyncio.create_task(
                self._wolfx_collector.run(
                    on_envelope=self._enqueue_envelope,
                    on_health=self._on_health,
                    stop_event=stop_event,
                )
            ),
        ]

        try:
            while not stop_event.is_set():
                try:
                    envelope = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=0.5,
                    )
                except asyncio.TimeoutError:
                    continue
                await self._process_live(envelope)
        except asyncio.CancelledError:
            raise
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            for provider in (CollectorProvider.FAN, CollectorProvider.WOLFX):
                if self._health.get(provider) != "critical":
                    await self._set_provider_state(provider, "stopped")

    async def process_envelopes(
        self,
        envelopes: list[CollectorEnvelope],
        trigger_reason: str = _LIVE,
    ) -> None:
        expanded: list[CollectorEnvelope] = []
        for envelope in envelopes:
            expanded.extend(self._coordinator.expand(envelope))
        if trigger_reason == _RECOVERY:
            expanded.sort(key=self._sort_key)
        for envelope in expanded:
            await self._process_expanded(envelope, trigger_reason, allow_spool=True)

    async def process_one(
        self,
        envelope: CollectorEnvelope,
        trigger_reason: str = _LIVE,
    ) -> str:
        expanded = self._coordinator.expand(envelope)
        if not expanded:
            return "skipped"
        outcomes: list[str] = []
        for item in expanded:
            outcomes.append(
                await self._process_expanded(item, trigger_reason, allow_spool=True)
            )
        if "critical" in outcomes:
            return "critical"
        if "spooled" in outcomes:
            return "spooled"
        if "dead_letter" in outcomes:
            return "dead_letter"
        if outcomes and all(outcome == "skipped" for outcome in outcomes):
            return "skipped"
        return "ingested"

    async def _process_expanded(
        self,
        envelope: CollectorEnvelope,
        trigger_reason: str,
        *,
        allow_spool: bool,
    ) -> str:
        try:
            result = await self._coordinator.ingest(envelope, trigger_reason)
        except SQLAlchemyError as exc:
            return await self._handle_storage_failure(
                envelope,
                trigger_reason,
                exc,
                allow_spool=allow_spool,
            )
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            await self._record_dead_letter(envelope, exc)
            return "dead_letter"

        if result is None:
            return "skipped"
        await self._record_success(envelope)
        return "ingested"

    async def _handle_storage_failure(
        self,
        envelope: CollectorEnvelope,
        trigger_reason: str,
        _original_error: Exception,
        *,
        allow_spool: bool,
    ) -> str:
        if not allow_spool:
            return "critical"

        for delay in _RETRY_DELAYS:
            await self._sleep(delay)
            try:
                result = await self._coordinator.ingest(envelope, trigger_reason)
            except SQLAlchemyError:
                continue
            except Exception as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                await self._record_dead_letter(envelope, exc)
                return "dead_letter"
            if result is None:
                return "skipped"
            await self._record_success(envelope)
            return "ingested"

        try:
            self._spool.append(envelope)
        except Exception:
            logger.exception("collector spool append failed")
            await self._set_provider_state(CollectorProvider.FAN, "critical")
            await self._set_provider_state(CollectorProvider.WOLFX, "critical")
            if self._stop_event is not None:
                self._stop_event.set()
            return "critical"
        return "spooled"

    async def _record_success(self, envelope: CollectorEnvelope) -> None:
        source_time = self._source_time(envelope)
        try:
            await self._service.update_after_success(
                envelope.provider.value,
                self._now(),
                source_time,
            )
        except Exception:
            logger.exception("collector watermark update failed")

    async def _record_dead_letter(
        self,
        envelope: CollectorEnvelope,
        exc: Exception,
    ) -> None:
        category = classify_collector_error(exc)
        try:
            await self._service.record_dead_letter(
                provider=envelope.provider.value,
                lane=envelope.lane.value,
                raw_payload=_first_item(envelope.payload),
                received_at=envelope.received_at,
                error_category=category,
                error_message=self._safe_error_text(exc),
                source_message_id=self._source_message_id(envelope),
            )
        except Exception:
            logger.exception("collector dead-letter write failed")

    async def _process_live(self, envelope: CollectorEnvelope) -> None:
        for item in self._coordinator.expand(envelope):
            cutoff = self._bootstrap_cutoff
            source_time = self._source_time(item)
            if cutoff is not None and source_time is not None:
                if source_time < cutoff:
                    continue
                self._bootstrap_cutoff = None
            await self._process_expanded(item, _LIVE, allow_spool=True)

    async def _replay_spool(self) -> bool:
        pending = list(self._spool.iter_pending())
        pending.sort(key=lambda pair: pair[1].received_at)
        for path, envelope in pending:
            for item in self._coordinator.expand(envelope):
                outcome = await self._process_expanded(
                    item,
                    _RECOVERY,
                    allow_spool=False,
                )
                if outcome == "critical":
                    return False
            self._spool.remove(path)
        return True

    async def _load_watermark(self) -> datetime | None:
        values: list[datetime] = []
        for provider in (CollectorProvider.FAN, CollectorProvider.WOLFX):
            try:
                value = await self._service.get_last_processed_source_time(provider.value)
            except Exception:
                logger.exception("collector watermark load failed")
                continue
            if value is not None:
                values.append(value)
        return max(values) if values else None

    async def _enqueue_envelope(self, envelope: CollectorEnvelope) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            try:
                self._spool.append(envelope)
            except Exception:
                logger.exception("collector queue overflow spool write failed")
                await self._set_provider_state(CollectorProvider.FAN, "critical")
                await self._set_provider_state(CollectorProvider.WOLFX, "critical")
                if self._stop_event is not None:
                    self._stop_event.set()

    async def _on_health(self, update: ProviderHealthUpdate) -> None:
        self._health[update.provider] = update.state
        try:
            await self._service.persist_health(update)
        except Exception:
            logger.exception("collector health persistence failed")

    async def _set_provider_state(
        self,
        provider: CollectorProvider,
        state: str,
    ) -> None:
        self._health[provider] = state
        update = ProviderHealthUpdate(
            provider=provider,
            state=state,
            connected=False,
            last_http_status=None,
            last_connected_at=None,
            last_message_at=None,
            last_success_at=None,
            consecutive_failures=0,
            reconnect_count=0,
            last_error=None,
            updated_at=self._now(),
        )
        try:
            await self._service.persist_health(update)
        except Exception:
            logger.exception("collector health state persistence failed")

    def _source_time(self, envelope: CollectorEnvelope) -> datetime | None:
        event = self._try_parse(envelope)
        if event is None:
            return None
        return event.report_time or event.origin_time

    def _sort_key(self, envelope: CollectorEnvelope) -> tuple[datetime, datetime, int]:
        event = self._try_parse(envelope)
        if event is None:
            return (envelope.received_at, envelope.received_at, self._provider_rank(envelope))
        checkpoint = event.report_time or event.origin_time
        return (checkpoint, event.origin_time, self._provider_rank(envelope))

    def _try_parse(self, envelope: CollectorEnvelope) -> NormalizedEvent | None:
        try:
            return self._adapter.parse(_first_item(envelope.payload))
        except Exception:
            return None

    def _source_message_id(self, envelope: CollectorEnvelope) -> str | None:
        event = self._try_parse(envelope)
        if event is not None and event.source_event_id:
            return event.source_event_id
        item = _first_item(envelope.payload)
        for field in ("EventID", "eventId", "id"):
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _provider_rank(envelope: CollectorEnvelope) -> int:
        return 0 if envelope.provider is CollectorProvider.FAN else 1

    @staticmethod
    def _safe_error_text(exc: Exception) -> str:
        return f"{type(exc).__name__}: {exc}"[:2_000]


def _first_item(payload: dict[str, object]) -> dict[str, object]:
    for value in payload.values():
        if isinstance(value, dict):
            return value
    return {}
