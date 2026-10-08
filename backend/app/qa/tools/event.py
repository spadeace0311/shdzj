from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.events.models import EarthquakeEvent, EarthquakeRevision
from app.qa.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)


class EventContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID | None = None


class EventRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: UUID


class GetEventContextTool:
    async def handle(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        requested_event_id = arguments.get("event_id")
        if (
            requested_event_id is not None
            and context.event_id is not None
            and requested_event_id != context.event_id
        ):
            return ToolResult.invalid(
                limitations=("event_context_mismatch",),
                parameters={"event_id": str(requested_event_id)},
            )
        event_id = _context_event_id(arguments, context)
        if event_id is None:
            return ToolResult.not_found(limitations=("event_context_missing",))
        if context.revision_id is None:
            return ToolResult.not_found(
                limitations=("revision_context_missing",),
            )
        event = await context.session.get(EarthquakeEvent, event_id)
        if event is None:
            return ToolResult.not_found(limitations=("event_not_found",))
        revision = await context.session.get(
            EarthquakeRevision,
            context.revision_id,
        )
        if revision is None or revision.event_id != event.id:
            return ToolResult.invalid(limitations=("revision_not_in_event",))
        return ToolResult.ok(
            value=_event_payload(event, revision),
            source="earthquake_event",
            version=str(revision.id),
            parameters={"event_id": str(event.id), "revision_id": str(revision.id)},
        )


class GetEventRevisionTool:
    async def handle(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        if context.event_id is None:
            return ToolResult.not_found(limitations=("event_context_missing",))
        event = await context.session.get(EarthquakeEvent, context.event_id)
        if event is None:
            return ToolResult.not_found(limitations=("event_not_found",))
        revision_id = arguments["revision_id"]
        revision = await context.session.get(EarthquakeRevision, revision_id)
        if revision is None:
            return ToolResult.not_found(
                source="earthquake_event",
                version=str(revision_id),
                limitations=("revision_not_found",),
            )
        if revision.event_id != event.id:
            return ToolResult.invalid(
                limitations=("revision_not_in_event",),
                parameters={"revision_id": str(revision_id)},
            )
        return ToolResult.ok(
            value=_event_payload(event, revision),
            source="earthquake_event",
            version=str(revision.id),
            parameters={"event_id": str(event.id), "revision_id": str(revision.id)},
        )


async def load_context_event_and_revision(
    context: ToolContext,
    requested_event_id: UUID | None = None,
) -> tuple[EarthquakeEvent | None, EarthquakeRevision | None, ToolResult | None]:
    event_id = _context_event_id({"event_id": requested_event_id}, context)
    if event_id is None:
        return None, None, ToolResult.not_found(
            limitations=("event_required",),
        )
    if requested_event_id is not None and requested_event_id != event_id:
        return None, None, ToolResult.invalid(
            limitations=("event_context_mismatch",),
            parameters={"event_id": str(requested_event_id)},
        )
    if context.revision_id is None:
        return None, None, ToolResult.not_found(
            limitations=("revision_context_missing",),
        )
    event = await context.session.get(EarthquakeEvent, event_id)
    if event is None:
        return None, None, ToolResult.not_found(
            limitations=("event_not_found",),
        )
    revision = await context.session.get(
        EarthquakeRevision,
        context.revision_id,
    )
    if revision is None or revision.event_id != event.id:
        return None, None, ToolResult.invalid(
            limitations=("revision_not_in_event",),
        )
    return event, revision, None


def register_event_tools(registry: ToolRegistry) -> None:
    context_tool = GetEventContextTool()
    revision_tool = GetEventRevisionTool()
    registry.register(
        ToolDefinition(
            name="event.get_context",
            description="获取当前事件及快照锁定修订报文",
            input_model=EventContextInput,
            handler=context_tool.handle,
            timeout_seconds=1.5,
            parallel_safe=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="event.get_revision",
            description="获取当前事件内指定修订报文",
            input_model=EventRevisionInput,
            handler=revision_tool.handle,
            timeout_seconds=1.5,
            parallel_safe=False,
        )
    )


def _context_event_id(
    arguments: dict[str, Any],
    context: ToolContext,
) -> UUID | None:
    requested = arguments.get("event_id")
    return context.event_id or requested


def _event_payload(
    event: EarthquakeEvent,
    revision: EarthquakeRevision,
) -> dict[str, Any]:
    return {
        "event_id": str(event.id),
        "revision_id": str(revision.id),
        "revision_no": revision.revision_no,
        "event_kind": revision.revision_kind,
        "origin_time": _isoformat(revision.origin_time),
        "longitude": _float(revision.longitude),
        "latitude": _float(revision.latitude),
        "depth_km": _float(revision.depth_km),
        "magnitude": _float(revision.magnitude),
        "place": revision.place,
        "institutional_level": (
            revision.institutional_level
            if revision.institutional_level is not None
            else event.institutional_level
        ),
        "service_level": (
            revision.service_level
            if revision.service_level is not None
            else event.service_level
        ),
        "t1_at": _isoformat(event.t1_at),
    }


def _float(value: Decimal) -> float:
    return float(value)


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
