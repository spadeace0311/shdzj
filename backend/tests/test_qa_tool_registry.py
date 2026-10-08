import asyncio
from dataclasses import dataclass
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app.auth.service import AuthUser
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
