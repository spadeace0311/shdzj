from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence

from app.collector.coordinator import CollectorCoordinator
from app.collector.domain import CollectorEnvelope, CollectorLane, CollectorProvider
from app.collector.service import CollectorService
from app.db import SessionFactory
from app.events.service import EventService
from app.regions.service import RegionContextResolver


async def replay_dead_letter(
    dead_letter_id: str,
    service: CollectorService,
    coordinator: CollectorCoordinator,
) -> None:
    record = await service.load_dead_letter(dead_letter_id)
    if record is None:
        raise LookupError(f"dead letter not found: {dead_letter_id}")

    try:
        raw_payload = record["raw_payload"]
        if not isinstance(raw_payload, dict):
            raise ValueError("dead letter payload must be an object")
        envelope = CollectorEnvelope(
            provider=CollectorProvider(str(record["provider"])),
            lane=CollectorLane(str(record["lane"])),
            received_at=record["received_at"],
            # The supervisor persists the expanded single item, not the NoN envelope.
            payload={"No1": raw_payload},
        )
        await coordinator.ingest(envelope, trigger_reason="recovery")
    except Exception as exc:
        await service.mark_dead_letter(dead_letter_id, "open", error=exc)
        raise

    status = "resolved" if record.get("status") in {"retried", "resolved"} else "retried"
    await service.mark_dead_letter(dead_letter_id, status)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.collector.replay")
    parser.add_argument("--dead-letter", required=True, type=uuid.UUID)
    return parser


async def _replay(dead_letter_id: uuid.UUID) -> int:
    service = CollectorService(SessionFactory)
    coordinator = CollectorCoordinator(
        EventService(SessionFactory),
        RegionContextResolver(SessionFactory),
    )
    try:
        await replay_dead_letter(str(dead_letter_id), service, coordinator)
    except Exception:
        print("dead-letter replay failed", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_replay(args.dead_letter))


if __name__ == "__main__":
    raise SystemExit(main())
