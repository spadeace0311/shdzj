from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
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
        self._last_health_updates: dict[
            CollectorProvider,
            ProviderHealthUpdate,
        ] = {}
        self._healthy_since: dict[CollectorProvider, datetime] = {}
        self._queue: asyncio.Queue[CollectorEnvelope] | None = None
        self._stop_event: asyncio.Event | None = None
        self._recovery_bounds: dict[CollectorProvider, datetime | None] = {
            CollectorProvider.FAN: None,
            CollectorProvider.WOLFX: None,
        }
        self._pending_spool = False

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

        watermarks = await self._load_watermarks()
        for provider in (CollectorProvider.FAN, CollectorProvider.WOLFX):
            bound = watermarks[provider]
            if bound is None:
                bound = self._now() - timedelta(
                    hours=self._settings.cenc_bootstrap_lookback_hours
                )
            self._recovery_bounds[provider] = bound
        if hasattr(self._fan_collector, "recovery_since"):
            self._fan_collector.recovery_since = self._recovery_bounds[
                CollectorProvider.FAN
            ]
        if hasattr(self._wolfx_collector, "recovery_since"):
            self._wolfx_collector.recovery_since = self._recovery_bounds[
                CollectorProvider.WOLFX
            ]

        if not await self._replay_spool():
            await self._mark_critical_and_stop()
            return
        self._pending_spool = False

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
                if self._pending_spool:
                    if not await self._replay_spool():
                        await self._mark_critical_and_stop()
                        break
                    self._pending_spool = False
                try:
                    envelope = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=0.5,
                    )
                except asyncio.TimeoutError:
                    await self._enforce_message_staleness()
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
        record_success: bool = True,
    ) -> str:
        try:
            result = await self._coordinator.ingest(envelope, trigger_reason)
        except SQLAlchemyError as exc:
            return await self._handle_storage_failure(
                envelope,
                trigger_reason,
                exc,
                allow_spool=allow_spool,
                record_success=record_success,
            )
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            return await self._handle_dead_letter(envelope, exc, allow_spool)

        if result is None:
            return "skipped"
        if record_success:
            await self._record_success(envelope)
        return "ingested"

    async def _handle_storage_failure(
        self,
        envelope: CollectorEnvelope,
        trigger_reason: str,
        _original_error: Exception,
        *,
        allow_spool: bool,
        record_success: bool,
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
                return await self._handle_dead_letter(envelope, exc, True)
            if result is None:
                return "skipped"
            if record_success:
                await self._record_success(envelope)
            return "ingested"

        try:
            return self._spool_envelope(envelope)
        except Exception:
            return await self._spool_failed()

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

    async def _persist_dead_letter(
        self,
        envelope: CollectorEnvelope,
        exc: Exception,
    ) -> None:
        category = classify_collector_error(exc)
        await self._service.record_dead_letter(
            provider=envelope.provider.value,
            lane=envelope.lane.value,
            raw_payload=_first_item(envelope.payload),
            received_at=envelope.received_at,
            error_category=category,
            error_message=self._safe_error_text(exc),
            source_message_id=self._source_message_id(envelope),
        )

    async def _handle_dead_letter(
        self,
        envelope: CollectorEnvelope,
        exc: Exception,
        allow_spool: bool,
    ) -> str:
        if not allow_spool:
            try:
                await self._persist_dead_letter(envelope, exc)
            except Exception:
                logger.exception("collector dead-letter persistence failed")
                await self._mark_critical_and_stop()
                return "critical"
            return "dead_letter"

        try:
            await self._persist_dead_letter(envelope, exc)
            return "dead_letter"
        except Exception:
            pass

        for delay in _RETRY_DELAYS:
            await self._sleep(delay)
            try:
                await self._persist_dead_letter(envelope, exc)
            except Exception:
                continue
            return "dead_letter"

        try:
            return self._spool_envelope(envelope)
        except Exception:
            return await self._spool_failed()

    def _spool_envelope(self, envelope: CollectorEnvelope) -> str:
        self._spool.append(envelope)
        self._pending_spool = True
        return "spooled"

    async def _spool_failed(self) -> str:
        logger.exception("collector spool append failed")
        await self._mark_critical_and_stop()
        return "critical"

    async def _process_live(self, envelope: CollectorEnvelope) -> None:
        expanded = self._coordinator.expand(envelope)
        all_settled = True
        for item in expanded:
            trigger_reason = item.trigger_reason
            bound = self._recovery_bounds[item.provider]
            source_time = self._source_time(item)
            if (
                bound is not None
                and source_time is not None
                and source_time < bound
            ):
                continue
            outcome = await self._process_expanded(
                item,
                trigger_reason,
                allow_spool=True,
            )
            if outcome in {"spooled", "critical", "dead_letter"}:
                all_settled = False
        if envelope.recovery_complete and all_settled:
            self._clear_recovery_bound(envelope.provider)

    async def _replay_spool(self) -> bool:
        pending = list(self._spool.iter_pending())
        # Startup/drain replay preserves durable received_at order. Source-time
        # ordering is reserved for explicit recovery batches in process_envelopes.
        pending.sort(key=lambda pair: pair[1].received_at)
        for path, envelope in pending:
            successful_items: list[CollectorEnvelope] = []
            all_settled = True
            for item in self._coordinator.expand(envelope):
                outcome = await self._process_expanded(
                    item,
                    _RECOVERY,
                    allow_spool=False,
                    record_success=False,
                )
                if outcome == "critical":
                    return False
                if outcome in {"spooled", "dead_letter"}:
                    all_settled = False
                if outcome == "ingested":
                    successful_items.append(item)
            try:
                self._spool.remove(path)
            except OSError:
                logger.exception(
                    "collector spool removal failed path=%s",
                    path,
                )
                return False
            for item in successful_items:
                await self._record_success(item)
            if envelope.recovery_complete and all_settled:
                self._clear_recovery_bound(envelope.provider)
        return True

    def _clear_recovery_bound(self, provider: CollectorProvider) -> None:
        self._recovery_bounds[provider] = None
        collector = (
            self._fan_collector
            if provider is CollectorProvider.FAN
            else self._wolfx_collector
        )
        if hasattr(collector, "recovery_since"):
            collector.recovery_since = None

    async def _mark_critical_and_stop(self) -> None:
        await self._set_provider_state(CollectorProvider.FAN, "critical")
        await self._set_provider_state(CollectorProvider.WOLFX, "critical")
        if self._stop_event is not None:
            self._stop_event.set()

    async def _load_watermarks(self) -> dict[CollectorProvider, datetime | None]:
        values: dict[CollectorProvider, datetime | None] = {
            CollectorProvider.FAN: None,
            CollectorProvider.WOLFX: None,
        }
        for provider in values:
            try:
                value = await self._service.get_last_processed_source_time(provider.value)
            except Exception:
                logger.exception("collector watermark load failed")
                continue
            values[provider] = value
        return values

    async def _enqueue_envelope(self, envelope: CollectorEnvelope) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            try:
                self._spool_envelope(envelope)
            except Exception:
                await self._spool_failed()

    async def _on_health(self, update: ProviderHealthUpdate) -> None:
        if self._health.get(update.provider) == "critical" and update.state != "critical":
            return
        update = self._effective_health_update(update)
        self._last_health_updates[update.provider] = update
        self._health[update.provider] = update.state
        try:
            await self._service.persist_health(update)
        except Exception:
            logger.exception("collector health persistence failed")

    async def _enforce_message_staleness(self) -> None:
        for update in list(self._last_health_updates.values()):
            if update.state != "healthy" or not update.connected:
                continue
            effective = self._effective_health_update(update)
            if effective.state != update.state:
                await self._on_health(effective)

    def _effective_health_update(
        self,
        update: ProviderHealthUpdate,
    ) -> ProviderHealthUpdate:
        if update.state != "healthy":
            if update.state in {"starting", "critical", "stopped"}:
                self._healthy_since.pop(update.provider, None)
            return update

        if update.last_message_at is not None:
            self._healthy_since[update.provider] = update.last_message_at
        else:
            self._healthy_since.setdefault(update.provider, self._now())

        anchor = self._healthy_since[update.provider]
        stale_after = timedelta(seconds=self._settings.collector_stale_after_seconds)
        if update.connected and self._now() - anchor > stale_after:
            return replace(
                update,
                state="degraded",
                last_error="no recent messages",
                updated_at=self._now(),
            )
        return update

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
