import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.auth.service import AuthUser
from app.events.models import EarthquakeEvent, RawMessage
from app.qa.tools import build_default_registry
from app.qa.tools.registry import (
    EmptyToolInput,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
)


@dataclass(frozen=True, slots=True)
class Arguments:
    text: str


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)


def make_context() -> ToolContext:
    return ToolContext(
        session=None,  # type: ignore[arg-type]
        user=AuthUser(
            username="qa-test",
            role="viewer",
            workgroup=None,
        ),
        event_id=uuid4(),
        revision_id=uuid4(),
        assessment_run_id=uuid4(),
        snapshot_id=uuid4(),
        index_version_id=uuid4(),
    )


@pytest.fixture(autouse=True)
async def dispose_engine_between_tests():
    from app.db import engine

    await engine.dispose()
    yield
    await engine.dispose()


async def test_tool_registry_validates_and_normalizes_arguments() -> None:
    registry = ToolRegistry()
    received: list[Arguments] = []

    async def handler(arguments, context):
        received.append(Arguments(arguments["text"]))
        return ToolResult.ok(
            value={"text": arguments["text"]},
            parameters={"derived_by_handler": True},
        )

    registry.register(
        ToolDefinition(
            name="test.echo",
            description="echo",
            input_model=EchoInput,
            handler=handler,
            timeout_seconds=1,
            parallel_safe=True,
        )
    )

    result = await registry.execute("test.echo", {"text": "ok"}, make_context())

    assert result.status == "ok"
    assert result.value == {"text": "ok"}
    assert result.parameters == {
        "derived_by_handler": True,
        "text": "ok",
    }
    assert received == [Arguments("ok")]


async def test_tool_registry_rejects_invalid_arguments_without_invoking_handler() -> None:
    registry = ToolRegistry()
    invoked = False

    async def handler(arguments, context):
        nonlocal invoked
        invoked = True
        return ToolResult.ok(value={})

    registry.register(
        ToolDefinition(
            name="test.echo",
            description="echo",
            input_model=EchoInput,
            handler=handler,
            timeout_seconds=1,
            parallel_safe=True,
        )
    )

    result = await registry.execute(
        "test.echo",
        {"text": "", "unexpected": True},
        make_context(),
    )

    assert result.status == "invalid"
    assert result.limitations == ("invalid_arguments",)
    assert invoked is False


async def test_tool_registry_times_out_without_leaking_error() -> None:
    registry = ToolRegistry()

    async def slow_handler(arguments, context):
        await asyncio.sleep(1)
        return ToolResult.ok(value={"answer": 42})

    registry.register(
        ToolDefinition(
            name="test.slow",
            description="slow",
            input_model=EmptyToolInput,
            handler=slow_handler,
            timeout_seconds=0.01,
            parallel_safe=True,
        )
    )

    result = await registry.execute("test.slow", {}, make_context())

    assert result.status == "unavailable"
    assert result.limitations == ("tool_timeout",)


async def test_tool_registry_redacts_exception_details() -> None:
    registry = ToolRegistry()
    secret = "restricted-payload-value"

    async def failing_handler(arguments, context):
        raise RuntimeError(secret)

    registry.register(
        ToolDefinition(
            name="test.failure",
            description="failure",
            input_model=EmptyToolInput,
            handler=failing_handler,
            timeout_seconds=1,
            parallel_safe=True,
        )
    )

    result = await registry.execute("test.failure", {}, make_context())

    assert result.status == "unavailable"
    assert result.value == {"error_type": "RuntimeError"}
    assert result.limitations == ("tool_error",)
    assert secret not in repr(result)


async def test_tool_registry_executes_safe_calls_in_parallel_and_keeps_plan_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    active = 0
    peak_active = 0
    started: list[str] = []

    async def handler(arguments, context):
        nonlocal active, peak_active
        started.append(arguments["text"])
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return ToolResult.ok(value={"text": arguments["text"]})

    registry.register(
        ToolDefinition(
            name="test.echo",
            description="echo",
            input_model=EchoInput,
            handler=handler,
            timeout_seconds=1,
            parallel_safe=True,
        )
    )
    monkeypatch.setattr(
        "app.qa.tools.registry.settings.qa_max_parallel_tools",
        2,
    )

    executions = await registry.execute_plan(
        [
            type("Call", (), {"name": "test.echo", "arguments": {"text": "first"}})(),
            type("Call", (), {"name": "test.echo", "arguments": {"text": "second"}})(),
            type("Call", (), {"name": "test.echo", "arguments": {"text": "third"}})(),
        ],
        make_context(),
    )

    assert peak_active == 2
    assert [execution.result.value["text"] for execution in executions] == [
        "first",
        "second",
        "third",
    ]
    assert started[:2] == ["first", "second"]


async def test_tool_registry_preserves_plan_order_around_unsafe_calls() -> None:
    registry = ToolRegistry()
    started: list[str] = []

    async def safe_handler(arguments, context):
        started.append("safe")
        await asyncio.sleep(0)
        return ToolResult.ok(value={})

    async def unsafe_handler(arguments, context):
        started.append("unsafe")
        return ToolResult.ok(value={})

    registry.register(
        ToolDefinition(
            name="test.safe",
            description="safe",
            input_model=EmptyToolInput,
            handler=safe_handler,
            timeout_seconds=1,
            parallel_safe=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="test.unsafe",
            description="unsafe",
            input_model=EmptyToolInput,
            handler=unsafe_handler,
            timeout_seconds=1,
            parallel_safe=False,
        )
    )

    executions = await registry.execute_plan(
        [
            type("Call", (), {"name": "test.unsafe", "arguments": {}})(),
            type("Call", (), {"name": "test.safe", "arguments": {}})(),
        ],
        make_context(),
    )

    assert started == ["unsafe", "safe"]
    assert [execution.name for execution in executions] == [
        "test.unsafe",
        "test.safe",
    ]


async def test_execute_plan_serializes_orm_calls_sharing_a_session(
    session_factory,
) -> None:
    registry = ToolRegistry()
    active = 0
    overlapped = False

    async def orm_handler(arguments, context):
        nonlocal active, overlapped
        active += 1
        overlapped = overlapped or active > 1
        try:
            await context.session.execute(select(EarthquakeEvent.id).limit(1))
            await asyncio.sleep(0.02)
            await context.session.execute(select(EarthquakeEvent.id).limit(1))
        finally:
            active -= 1
        return ToolResult.ok(value={})

    for name in ("test.orm_a", "test.orm_b"):
        registry.register(
            ToolDefinition(
                name=name,
                description="ORM-backed test tool",
                input_model=EmptyToolInput,
                handler=orm_handler,
                timeout_seconds=1,
                parallel_safe=True,
            )
        )

    async with session_factory() as session:
        context = ToolContext(
            session=session,
            user=AuthUser(
                username="qa-test",
                role="viewer",
                workgroup=None,
            ),
            event_id=None,
            revision_id=None,
            assessment_run_id=None,
            snapshot_id=uuid4(),
            index_version_id=uuid4(),
        )
        executions = await registry.execute_plan(
            [
                type(
                    "Call",
                    (),
                    {"name": "test.orm_a", "arguments": {}},
                )(),
                type(
                    "Call",
                    (),
                    {"name": "test.orm_b", "arguments": {}},
                )(),
            ],
            context,
        )

    assert overlapped is False
    assert [execution.result.status for execution in executions] == ["ok", "ok"]


async def test_tool_definition_marks_all_session_backed_tools_non_parallel() -> None:
    registry = build_default_registry()

    assert all(
        not registry.get(name).parallel_safe
        for name in (
            "event.get_context",
            "event.get_revision",
            "fault.nearest",
            "seismicity.within_radius",
            "seismicity.distance",
            "region.lookup",
        )
    )


async def test_tool_registry_savepoint_recovers_after_database_error(
    session_factory,
) -> None:
    registry = ToolRegistry()

    async def failing_db_handler(arguments, context):
        await context.session.execute(
            text("SELECT 1 FROM qa_task8_missing_table")
        )
        return ToolResult.ok(value={})

    registry.register(
        ToolDefinition(
            name="test.db_error",
            description="database error",
            input_model=EmptyToolInput,
            handler=failing_db_handler,
            timeout_seconds=1,
            parallel_safe=False,
        )
    )

    async with session_factory() as session:
        context = ToolContext(
            session=session,
            user=AuthUser(
                username="qa-test",
                role="viewer",
                workgroup=None,
            ),
            event_id=None,
            revision_id=None,
            assessment_run_id=None,
            snapshot_id=uuid4(),
            index_version_id=uuid4(),
        )
        await session.begin()
        marker = RawMessage(
            source="qa-savepoint-tool-test",
            source_message_id=str(uuid4()),
            message_kind="test",
            checksum=uuid4().hex + uuid4().hex,
            payload={},
            received_at=datetime.now(UTC),
        )
        session.add(marker)
        await session.flush()

        result = await registry.execute("test.db_error", {}, context)
        recovered = await session.scalar(text("SELECT PostGIS_Version()"))
        preserved = await session.scalar(
            select(RawMessage.id).where(RawMessage.id == marker.id)
        )
        await session.rollback()

    assert result.status == "unavailable"
    assert result.value == {"error_type": "ProgrammingError"}
    assert recovered is not None
    assert preserved == marker.id


async def test_tool_registry_savepoint_recovers_after_database_timeout(
    session_factory,
) -> None:
    registry = ToolRegistry()

    async def slow_db_handler(arguments, context):
        await context.session.execute(text("SELECT pg_sleep(1)"))
        return ToolResult.ok(value={})

    registry.register(
        ToolDefinition(
            name="test.db_timeout",
            description="database timeout",
            input_model=EmptyToolInput,
            handler=slow_db_handler,
            timeout_seconds=0.05,
            parallel_safe=False,
        )
    )

    async with session_factory() as session:
        context = ToolContext(
            session=session,
            user=AuthUser(
                username="qa-test",
                role="viewer",
                workgroup=None,
            ),
            event_id=None,
            revision_id=None,
            assessment_run_id=None,
            snapshot_id=uuid4(),
            index_version_id=uuid4(),
        )
        await session.begin()
        result = await registry.execute("test.db_timeout", {}, context)
        recovered = await session.scalar(text("SELECT PostGIS_Version()"))
        await session.rollback()

    assert result.status == "unavailable"
    assert result.limitations == ("tool_timeout",)
    assert recovered is not None


def test_tool_catalog_is_sorted_and_exposes_input_schema() -> None:
    registry = ToolRegistry()

    async def handler(arguments, context):
        return ToolResult.ok(value={})

    for name in ("test.z", "test.a"):
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                input_model=EmptyToolInput,
                handler=handler,
                timeout_seconds=1,
                parallel_safe=True,
            )
        )

    catalog = registry.catalog()

    assert [item["name"] for item in catalog] == ["test.a", "test.z"]
    assert catalog[0]["parameters"] == {
        "additionalProperties": False,
        "properties": {},
        "title": "EmptyToolInput",
        "type": "object",
    }
