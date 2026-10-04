from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Awaitable, Callable

import httpx

from app.collector.domain import (
    CollectorEnvelope,
    CollectorLane,
    CollectorProvider,
    ProviderHealthUpdate,
)
from app.events.sources.cenc import CencAdapter


EnvelopeCallback = Callable[[CollectorEnvelope], Awaitable[None]]
HealthCallback = Callable[[ProviderHealthUpdate], Awaitable[None]]
SleepCallback = Callable[[float], Awaitable[None]]
ClockCallback = Callable[[], datetime]

_NO_KEY = re.compile(r"^No\d+$")


@dataclass(frozen=True, slots=True)
class WolfxParseResult:
    envelopes: list[CollectorEnvelope]
    complete: bool


class WolfxMessageParser:
    """Translate the Wolfx CENC eqlist payload into collector envelopes."""

    def parse(
        self,
        payload: dict[str, object],
        received_at: datetime,
        recovery_since: datetime | None = None,
    ) -> list[CollectorEnvelope]:
        return self.parse_result(payload, received_at, recovery_since).envelopes

    def parse_result(
        self,
        payload: dict[str, object],
        received_at: datetime,
        recovery_since: datetime | None = None,
    ) -> WolfxParseResult:
        if not isinstance(payload, dict):
            return WolfxParseResult(envelopes=[], complete=False)

        keys = [key for key in payload if isinstance(key, str) and _NO_KEY.fullmatch(key)]
        keys.sort(key=lambda key: int(key[2:]))

        envelopes: list[CollectorEnvelope] = []
        complete = True
        for key in keys:
            item = payload[key]
            if not isinstance(item, dict):
                complete = False
                continue
            if item.get("type") not in {"automatic", "reviewed"}:
                continue
            source_time = _source_time(item)
            if source_time is None:
                complete = False
            if recovery_since is not None:
                if source_time is not None and source_time < recovery_since:
                    continue
            envelopes.append(
                CollectorEnvelope(
                    provider=CollectorProvider.WOLFX,
                    lane=CollectorLane.HTTP,
                    received_at=received_at,
                    payload={key: item},
                    trigger_reason=(
                        "recovery" if recovery_since is not None else "live"
                    ),
                )
            )
        if recovery_since is not None and complete:
            if envelopes:
                envelopes[-1] = replace(envelopes[-1], recovery_complete=True)
        return WolfxParseResult(envelopes=envelopes, complete=complete)


def _source_time(item: dict[str, object]) -> datetime | None:
    try:
        event = CencAdapter().parse(item)
        return event.report_time or event.origin_time
    except Exception:
        return None


class WolfxCollector:
    def __init__(
        self,
        *,
        url: str,
        poll_interval_seconds: int,
        client: httpx.AsyncClient,
        parser: WolfxMessageParser | None = None,
        sleep: SleepCallback | None = None,
        now: ClockCallback | None = None,
        rng: random.Random | None = None,
        recovery_since: datetime | None = None,
    ) -> None:
        if not url:
            raise ValueError("a Wolfx CENC URL is required")
        self._url = url
        self._poll_interval_seconds = poll_interval_seconds
        self._client = client
        self._parser = parser or WolfxMessageParser()
        self._sleep = sleep or asyncio.sleep
        self._now = now or (lambda: datetime.now(UTC))
        self._rng = rng or random.Random()
        self._recovery_since = recovery_since
        self._reset_state()

    @property
    def recovery_since(self) -> datetime | None:
        return self._recovery_since

    @recovery_since.setter
    def recovery_since(self, value: datetime | None) -> None:
        self._recovery_since = value

    def _reset_state(self) -> None:
        self._connected = False
        self._last_http_status: int | None = None
        self._last_connected_at: datetime | None = None
        self._last_message_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_transport_at: datetime | None = None
        self._consecutive_failures = 0
        self._reconnect_count = 0
        self._last_error: str | None = None

    async def poll_once(self) -> list[CollectorEnvelope]:
        envelopes, _, _ = await self._poll_once()
        return envelopes

    async def _poll_once(
        self,
        recovery_since: datetime | None = None,
    ) -> tuple[list[CollectorEnvelope], int, datetime]:
        response = await self._client.get(self._url, timeout=10.0)
        response.raise_for_status()
        received_at = self._now()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Wolfx response must be a JSON object")
        result = self._parser.parse_result(
            payload,
            received_at,
            recovery_since,
        )
        envelopes = result.envelopes
        if recovery_since is not None and result.complete and not envelopes:
            envelopes.append(
                CollectorEnvelope(
                    provider=CollectorProvider.WOLFX,
                    lane=CollectorLane.HTTP,
                    received_at=received_at,
                    payload={},
                    trigger_reason="recovery",
                    recovery_complete=True,
                )
            )
        return envelopes, response.status_code, received_at

    async def run(
        self,
        on_envelope: EnvelopeCallback,
        on_health: HealthCallback,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        stop_event = stop_event or asyncio.Event()
        self._reset_state()
        await self._emit_health(on_health, "starting")

        try:
            while not stop_event.is_set():
                try:
                    envelopes, status, received_at = await self._poll_once(
                        self._recovery_since
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if stop_event.is_set():
                        break
                    self._record_failure(self._classify_error(exc))
                    await self._emit_health(on_health, "degraded")
                    await self._wait_for_stop(stop_event, self._next_delay())
                    continue

                self._record_success(envelopes, status, received_at)
                for envelope in envelopes:
                    await on_envelope(envelope)
                await self._emit_health(on_health, "healthy")
                await self._wait_for_stop(
                    stop_event,
                    float(self._poll_interval_seconds),
                )
        finally:
            if stop_event.is_set():
                self._connected = False
                await self._emit_health(on_health, "stopped")

    def _record_success(
        self,
        envelopes: list[CollectorEnvelope],
        status: int,
        received_at: datetime,
    ) -> None:
        self._connected = True
        self._last_http_status = status
        self._last_connected_at = received_at
        self._last_success_at = received_at
        self._last_transport_at = received_at
        if any(envelope.payload for envelope in envelopes):
            self._last_message_at = received_at
        self._consecutive_failures = 0
        self._last_error = None

    def _record_failure(self, error_text: str) -> None:
        self._connected = False
        self._last_http_status = None
        self._consecutive_failures += 1
        self._reconnect_count += 1
        self._last_error = error_text

    def _classify_error(self, exc: Exception) -> str:
        if isinstance(exc, httpx.TimeoutException):
            return "http timeout"
        if isinstance(exc, httpx.HTTPStatusError):
            return f"http status {exc.response.status_code}"
        if isinstance(exc, json.JSONDecodeError):
            return "invalid JSON response"
        if isinstance(exc, httpx.HTTPError):
            return "http request failed"
        return "unexpected response shape"

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
        try:
            await asyncio.wait(
                {sleep_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (sleep_task, stop_task):
                task.cancel()
            for task in (sleep_task, stop_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    def _health(self, state: str) -> ProviderHealthUpdate:
        return ProviderHealthUpdate(
            provider=CollectorProvider.WOLFX,
            state=state,
            connected=self._connected,
            last_http_status=self._last_http_status,
            last_connected_at=self._last_connected_at,
            last_message_at=self._last_message_at,
            last_success_at=self._last_success_at,
            last_transport_at=self._last_transport_at,
            consecutive_failures=self._consecutive_failures,
            reconnect_count=self._reconnect_count,
            last_error=self._last_error,
            updated_at=self._now(),
        )

    async def _emit_health(self, on_health: HealthCallback, state: str) -> None:
        await on_health(self._health(state))
