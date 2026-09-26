from __future__ import annotations

import asyncio
import json
import logging
import signal
from pathlib import Path

import httpx

from app.collector.coordinator import CollectorCoordinator
from app.collector.fan import FanCollector
from app.collector.service import CollectorService
from app.collector.spool import CollectorSpool
from app.collector.supervisor import CollectorSupervisor
from app.collector.wolfx import WolfxCollector
from app.config import Settings
from app.db import SessionFactory
from app.events.service import EventService
from app.regions.service import RegionContextResolver

logger = logging.getLogger(__name__)


class _HealthServer:
    def __init__(self, supervisor: CollectorSupervisor) -> None:
        self._supervisor = supervisor
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle,
            "0.0.0.0",
            8100,
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=3)
            path = request_line.decode("latin-1", errors="replace").split(" ", 2)
            if len(path) < 2 or path[1] != "/healthz":
                await self._respond(writer, 404, {"status": "not_found"})
                return

            states = self._supervisor.provider_states
            status_code = 200 if self._supervisor.health_is_available() else 503
            body = {
                "status": "ok" if status_code == 200 else "unavailable",
                "providers": states,
                "timestamp": _utc_now().isoformat(),
            }
            await self._respond(writer, status_code, body)
        except Exception:
            logger.exception("collector health handler failed")
            try:
                await self._respond(writer, 500, {"status": "error"})
            except Exception:
                pass

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        body: dict[str, object],
    ) -> None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        encoded = payload.encode("utf-8")
        writer.write(
            (
                f"HTTP/1.1 {status} {_REASON.get(status, 'Unknown')}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(encoded)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            + encoded
        )
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


_REASON = {
    200: "OK",
    404: "Not Found",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(signum, lambda _sig, _frame: stop_event.set())


async def _run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    )
    settings = Settings()
    if not settings.cenc_collector_enabled:
        logger.error("collector container started while CENC_COLLECTOR_ENABLED=false")
        return 2

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, stop_event)

    service = CollectorService(SessionFactory)
    spool = CollectorSpool(
        Path(settings.collector_spool_dir),
        settings.collector_max_spool_bytes,
    )
    coordinator = CollectorCoordinator(
        EventService(SessionFactory),
        RegionContextResolver(SessionFactory),
    )
    fan_collector = FanCollector(
        app_id=settings.resolved_fan_app_id,
        api_key=settings.fan_api_key.get_secret_value(),
        urls=(settings.fan_ws_primary_url, settings.fan_ws_backup_url),
        query_interval_seconds=settings.fan_query_interval_seconds,
    )

    async with httpx.AsyncClient() as client:
        wolfx_collector = WolfxCollector(
            url=settings.wolfx_cenc_url,
            poll_interval_seconds=settings.wolfx_poll_interval_seconds,
            client=client,
        )
        supervisor = CollectorSupervisor(
            settings=settings,
            service=service,
            coordinator=coordinator,
            spool=spool,
            fan_collector=fan_collector,
            wolfx_collector=wolfx_collector,
        )
        health_server = _HealthServer(supervisor)
        await health_server.start()
        supervisor_task = asyncio.create_task(supervisor.run(stop_event))
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            stop_event.set()
            raise
        finally:
            await supervisor_task
            await health_server.close()
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_run()))
    except KeyboardInterrupt:
        raise SystemExit(0)


if __name__ == "__main__":
    main()
