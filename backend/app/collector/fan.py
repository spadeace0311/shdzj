from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect as websocket_connect

from app.collector.domain import (
    CollectorEnvelope,
    CollectorLane,
    CollectorProvider,
    ProviderHealthUpdate,
)

_STOPPED = object()

EnvelopeCallback = Callable[[CollectorEnvelope], Awaitable[None]]
HealthCallback = Callable[[ProviderHealthUpdate], Awaitable[None]]
ConnectCallback = Callable[[str], Any]
SleepCallback = Callable[[float], Awaitable[None]]
ClockCallback = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class FanParseResult:
    envelope: CollectorEnvelope | None = None
    auth_state: str | None = None
    heartbeat: bool = False
    error_text: str | None = None


class FanMessageParser:
    """Translate FAN Studio websocket frames into collector envelopes."""

    def parse(self, message: dict[str, object], received_at: datetime) -> FanParseResult:
        if not isinstance(message, dict):
            return FanParseResult()

        message_type = message.get("type")
        if message_type == "auth_success":
            return FanParseResult(auth_state="success")
        if message_type == "auth_fail":
            return FanParseResult(
                auth_state="failed",
                error_text="authentication failed",
            )
        if message_type in {"pong", "heartbeat"}:
            return FanParseResult(heartbeat=True)

        if message_type in {"initial_all", "query_response"}:
            data = _nested_data(message, "cenc", "Data")
            if isinstance(data, dict) and data:
                return FanParseResult(envelope=self._envelope(data, received_at))
            return FanParseResult()

        if message_type == "cenclist_response":
            history = message.get("Data")
            if not isinstance(history, dict) or not history:
                return FanParseResult()
            items = [item for item in history.values() if isinstance(item, dict)]
            items.sort(key=_history_sort_key)
            normalized = {f"No{index}": item for index, item in enumerate(items, start=1)}
            if not normalized:
                return FanParseResult()
            return FanParseResult(envelope=self._envelope(normalized, received_at))

        if message_type == "update":
            if message.get("source") != "cenc":
                return FanParseResult()
            data = message.get("Data")
            if isinstance(data, dict) and data:
                return FanParseResult(envelope=self._envelope({"No1": data}, received_at))

        return FanParseResult()

    def _envelope(self, payload: dict[str, object], received_at: datetime) -> CollectorEnvelope:
        return CollectorEnvelope(
            provider=CollectorProvider.FAN,
            lane=CollectorLane.WEBSOCKET,
            received_at=received_at,
            payload=payload,
        )


class _AuthenticationFailed(Exception):
    pass


def _nested_data(message: dict[str, object], *keys: str) -> object:
    value: object = message
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _history_sort_key(item: dict[str, object]) -> tuple[str, str]:
    shock_time = str(item.get("shockTime") or "")
    event_id = str(item.get("id") or item.get("eventId") or "")
    return (shock_time, event_id)


async def _default_connect(url: str) -> Any:
    return await websocket_connect(url)


class FanCollector:
    def __init__(
        self,
        *,
        app_id: str,
        api_key: str,
        urls: tuple[str, ...] | list[str],
        query_interval_seconds: int,
        parser: FanMessageParser | None = None,
        connect: ConnectCallback | None = None,
        sleep: SleepCallback | None = None,
        now: ClockCallback | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._app_id = app_id
        self._api_key = api_key
        self._urls = tuple(urls)
        if not self._urls:
            raise ValueError("at least one FAN websocket URL is required")
        self._query_interval_seconds = query_interval_seconds
        self._parser = parser or FanMessageParser()
        self._connect = connect or _default_connect
        self._sleep = sleep or asyncio.sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._rng = rng or random.Random()
        self._reset_state()

    def _reset_state(self) -> None:
        self._connected = False
        self._last_http_status: int | None = None
        self._last_connected_at: datetime | None = None
        self._last_message_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._consecutive_failures = 0
        self._reconnect_count = 0
        self._last_error: str | None = None

    async def run(
        self,
        on_envelope: EnvelopeCallback,
        on_health: HealthCallback,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop_event = stop_event or asyncio.Event()
        self._reset_state()
        await self._emit_health(on_health, "starting")

        url_index = 0
        try:
            while not stop_event.is_set():
                url = self._urls[url_index % len(self._urls)]
                try:
                    await self._run_connection(url, on_envelope, on_health, stop_event)
                except asyncio.CancelledError:
                    raise
                except _AuthenticationFailed:
                    self._record_failure("authentication failed")
                    await self._emit_health(on_health, "degraded")
                    await self._wait_for_stop(stop_event, self._next_delay())
                    url_index = (url_index + 1) % len(self._urls)
                except Exception:
                    if stop_event.is_set():
                        break
                    self._record_failure("connection failed")
                    await self._emit_health(on_health, "degraded")
                    await self._wait_for_stop(stop_event, self._next_delay())
                    url_index = (url_index + 1) % len(self._urls)
        finally:
            if stop_event.is_set():
                await self._emit_health(on_health, "stopped")

    async def _run_connection(
        self,
        url: str,
        on_envelope: EnvelopeCallback,
        on_health: HealthCallback,
        stop_event: asyncio.Event,
    ) -> None:
        websocket = await self._connect(url)
        try:
            self._connected = True
            self._last_connected_at = self._now()
            await websocket.send(
                json.dumps(
                    {"type": "auth", "appId": self._app_id, "key": self._api_key},
                    separators=(",", ":"),
                )
            )
            await websocket.send(json.dumps({"type": "cenclist"}, separators=(",", ":")))

            receive_task = asyncio.create_task(
                self._receive_loop(websocket, on_envelope, on_health, stop_event)
            )
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(websocket, stop_event)
            )
            done, pending = await asyncio.wait(
                {receive_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            for task in done:
                error = task.exception()
                if error is not None:
                    raise error
        finally:
            await websocket.close()

    async def _receive_loop(
        self,
        websocket: Any,
        on_envelope: EnvelopeCallback,
        on_health: HealthCallback,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            message = await self._recv_or_stop(websocket, stop_event)
            if message is _STOPPED:
                return

            parsed = self._parse_message(message)
            if parsed is None:
                continue
            if parsed.auth_state == "success":
                continue
            if parsed.auth_state == "failed":
                raise _AuthenticationFailed
            if parsed.heartbeat:
                continue
            if parsed.envelope is None:
                continue

            self._last_http_status = None
            self._last_message_at = parsed.envelope.received_at
            self._last_success_at = parsed.envelope.received_at
            self._consecutive_failures = 0
            self._last_error = None
            await on_envelope(parsed.envelope)
            await self._emit_health(on_health, "healthy")

    async def _heartbeat_loop(self, websocket: Any, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._wait_for_stop(stop_event, float(self._query_interval_seconds))
            if stop_event.is_set():
                return
            await websocket.send(json.dumps({"type": "query"}, separators=(",", ":")))

    async def _recv_or_stop(
        self,
        websocket: Any,
        stop_event: asyncio.Event,
    ) -> Any:
        if stop_event.is_set():
            return _STOPPED
        recv_task = asyncio.create_task(websocket.recv())
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {recv_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if stop_task in done:
            return _STOPPED
        return await recv_task

    def _parse_message(self, message: object) -> FanParseResult | None:
        if isinstance(message, dict):
            payload = message
        elif isinstance(message, (str, bytes, bytearray)):
            try:
                decoded = message.decode() if isinstance(message, (bytes, bytearray)) else message
            except UnicodeDecodeError:
                return None
            try:
                payload = json.loads(decoded)
            except (json.JSONDecodeError, TypeError):
                return None
        else:
            return None
        if not isinstance(payload, dict):
            return None
        return self._parser.parse(payload, self._now())

    def _record_failure(self, error_text: str) -> None:
        self._connected = False
        self._last_http_status = None
        self._consecutive_failures += 1
        self._reconnect_count += 1
        self._last_error = error_text

    def _next_delay(self) -> float:
        exponent = min(max(self._consecutive_failures - 1, 0), 30)
        base = min(30.0, float(2**exponent))
        jitter = self._rng.uniform(0.0, 0.25)
        return min(30.0, base + jitter)

    async def _wait_for_stop(self, stop_event: asyncio.Event, delay: float) -> None:
        if stop_event.is_set() or delay <= 0:
            return
        sleep_task = asyncio.create_task(self._sleep(delay))
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            {sleep_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _health(self, state: str) -> ProviderHealthUpdate:
        return ProviderHealthUpdate(
            provider=CollectorProvider.FAN,
            state=state,
            connected=self._connected,
            last_http_status=self._last_http_status,
            last_connected_at=self._last_connected_at,
            last_message_at=self._last_message_at,
            last_success_at=self._last_success_at,
            consecutive_failures=self._consecutive_failures,
            reconnect_count=self._reconnect_count,
            last_error=self._last_error,
            updated_at=self._now(),
        )

    async def _emit_health(self, on_health: HealthCallback, state: str) -> None:
        await on_health(self._health(state))
