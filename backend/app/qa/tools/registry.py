from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from time import perf_counter
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import config
from app.artifacts.models import ProductionInputSnapshotItem
from app.auth.service import AuthUser
from app.data_assets.models import DataAssetVersion
from app.data_assets.snapshot_service import DataAssetSnapshotService
from app.db import SessionFactory
from app.knowledge.models import KnowledgeSnapshot

settings = config.settings

_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class ToolStatus(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: ToolStatus
    value: Any = None
    unit: str | None = None
    source: str | None = None
    version: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ToolStatus(self.status))
        object.__setattr__(self, "parameters", dict(self.parameters))
        object.__setattr__(self, "limitations", tuple(self.limitations))

    @classmethod
    def ok(
        cls,
        *,
        value: Any,
        unit: str | None = None,
        source: str | None = None,
        version: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        limitations: Iterable[str] = (),
    ) -> ToolResult:
        return cls(
            status=ToolStatus.OK,
            value=value,
            unit=unit,
            source=source,
            version=version,
            parameters=dict(parameters or {}),
            limitations=tuple(limitations),
        )

    @classmethod
    def not_found(
        cls,
        *,
        source: str | None = None,
        version: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        limitations: Iterable[str] = (),
    ) -> ToolResult:
        return cls(
            status=ToolStatus.NOT_FOUND,
            value={},
            source=source,
            version=version,
            parameters=dict(parameters or {}),
            limitations=tuple(limitations),
        )

    @classmethod
    def unavailable(
        cls,
        *,
        limitations: Iterable[str],
        error_type: str | None = None,
        source: str | None = None,
        version: str | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        value = {"error_type": error_type} if error_type is not None else {}
        return cls(
            status=ToolStatus.UNAVAILABLE,
            value=value,
            source=source,
            version=version,
            parameters=dict(parameters or {}),
            limitations=tuple(limitations),
        )

    @classmethod
    def invalid(
        cls,
        *,
        limitations: Iterable[str] = ("invalid_arguments",),
        parameters: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return cls(
            status=ToolStatus.INVALID,
            value={},
            parameters=dict(parameters or {}),
            limitations=tuple(limitations),
        )


class ToolHandler(Protocol):
    async def __call__(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    session: AsyncSession
    user: AuthUser
    event_id: UUID | None
    revision_id: UUID | None
    assessment_run_id: UUID | None
    snapshot_id: UUID
    index_version_id: UUID


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler
    timeout_seconds: float
    parallel_safe: bool

    def __post_init__(self) -> None:
        if not _TOOL_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("tool name must contain two lowercase segments")
        if self.timeout_seconds <= 0:
            raise ValueError("tool timeout must be positive")
        if not issubclass(self.input_model, BaseModel):
            raise TypeError("tool input_model must be a Pydantic BaseModel")


@dataclass(frozen=True, slots=True)
class ToolExecution:
    name: str
    result: ToolResult
    duration_ms: float = 0.0

    @property
    def tool_name(self) -> str:
        return self.name

    @property
    def tool_result(self) -> ToolResult:
        return self.result


class EmptyToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[ToolDefinition] | None = None,
        *,
        settings: config.Settings | None = None,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    ) -> None:
        self._settings = settings or config.settings
        self._session_factory = session_factory
        self._tools: dict[str, ToolDefinition] = {}
        for definition in tools or ():
            self.register(definition)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"duplicate tool: {definition.name}")
        self._tools[definition.name] = definition

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.input_model.model_json_schema(),
                "timeout_seconds": definition.timeout_seconds,
                "parallel_safe": definition.parallel_safe,
            }
            for definition in sorted(
                self._tools.values(),
                key=lambda item: item.name,
            )
        ]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        definition = self.get(name)
        if definition is None:
            return ToolResult.not_found(limitations=("unknown_tool",))
        try:
            normalized = definition.input_model.model_validate(arguments).model_dump(
                mode="python"
            )
        except ValidationError:
            return ToolResult.invalid()

        timeout_seconds = min(
            definition.timeout_seconds,
            self._settings.qa_tool_timeout_seconds,
        )
        try:
            result = await self._execute_handler(
                definition.handler,
                normalized,
                context,
                timeout_seconds,
            )
        except TimeoutError:
            return ToolResult.unavailable(
                limitations=("tool_timeout",),
                error_type="TimeoutError",
                parameters=normalized,
            )
        except Exception as exc:
            return ToolResult.unavailable(
                limitations=("tool_error",),
                error_type=type(exc).__name__,
                parameters=normalized,
            )
        if not isinstance(result, ToolResult):
            return ToolResult.unavailable(
                limitations=("tool_error",),
                error_type="InvalidToolResult",
                parameters=normalized,
            )
        parameters = dict(result.parameters)
        parameters.update(normalized)
        return replace(result, parameters=parameters)

    async def _execute_handler(
        self,
        handler: ToolHandler,
        arguments: dict[str, Any],
        context: ToolContext,
        timeout_seconds: float,
    ) -> ToolResult:
        if context.session is None:
            return await _invoke_handler(
                handler,
                arguments,
                context,
                timeout_seconds,
            )

        # A cancelled asyncpg query can poison its connection; isolate tool I/O.
        tool_session = self._session_factory()
        tool_context = replace(context, session=tool_session)
        try:
            return await _invoke_handler(
                handler,
                arguments,
                tool_context,
                timeout_seconds,
            )
        finally:
            await _dispose_tool_session(tool_session)

    async def execute_plan(
        self,
        calls: Sequence[Any],
        context: ToolContext,
    ) -> list[ToolExecution]:
        executions: list[ToolExecution | None] = [None] * len(calls)
        limit = max(1, self._settings.qa_max_parallel_tools)
        semaphore = asyncio.Semaphore(limit)
        # AsyncSession is stateful; never overlap calls that share one.
        allow_parallel = context.session is None

        async def execute_safe(index: int) -> ToolExecution:
            call = calls[index]
            async with semaphore:
                started_at = perf_counter()
                result = await self.execute(
                    call.name,
                    dict(call.arguments),
                    context,
                )
                duration_ms = (perf_counter() - started_at) * 1000
            return ToolExecution(
                name=call.name,
                result=result,
                duration_ms=duration_ms,
            )

        index = 0
        while index < len(calls):
            definition = self.get(str(calls[index].name))
            if (
                definition is None
                or not definition.parallel_safe
                or not allow_parallel
            ):
                call = calls[index]
                started_at = perf_counter()
                result = await self.execute(
                    call.name,
                    dict(call.arguments),
                    context,
                )
                executions[index] = ToolExecution(
                    name=call.name,
                    result=result,
                    duration_ms=(perf_counter() - started_at) * 1000,
                )
                index += 1
                continue

            batch_end = index + 1
            while batch_end < len(calls):
                batch_definition = self.get(str(calls[batch_end].name))
                if (
                    batch_definition is None
                    or not batch_definition.parallel_safe
                    or not allow_parallel
                ):
                    break
                batch_end += 1
            batch_indices = range(index, batch_end)
            safe_executions = await asyncio.gather(
                *(execute_safe(batch_index) for batch_index in batch_indices)
            )
            for batch_index, execution in zip(
                batch_indices,
                safe_executions,
                strict=True,
            ):
                executions[batch_index] = execution
            index = batch_end

        return [execution for execution in executions if execution is not None]


async def _invoke_handler(
    handler: ToolHandler,
    arguments: dict[str, Any],
    context: ToolContext,
    timeout_seconds: float,
) -> ToolResult:
    async with asyncio.timeout(timeout_seconds):
        return await handler(arguments, context)


async def _dispose_tool_session(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        pass
    try:
        await session.close()
    except Exception:
        pass


async def resolve_locked_asset_version(
    context: ToolContext,
    asset_key: str,
) -> DataAssetVersion | None:
    version: DataAssetVersion | None = None
    if context.assessment_run_id is not None:
        version = await DataAssetSnapshotService().get_locked_version(
            context.session,
            run_id=context.assessment_run_id,
            asset_key=asset_key,
        )
    if version is not None:
        return version

    version_id = await _snapshot_asset_version_id(context, asset_key)
    if version_id is None:
        return None
    return await context.session.get(DataAssetVersion, version_id)


async def _snapshot_asset_version_id(
    context: ToolContext,
    asset_key: str,
) -> UUID | None:
    snapshot = await context.session.get(KnowledgeSnapshot, context.snapshot_id)
    if snapshot is not None:
        manifest = snapshot.manifest or {}
        for field_name in ("data_asset_versions", "assets"):
            items = manifest.get(field_name)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, Mapping) or item.get("asset_key") != asset_key:
                    continue
                version_id = item.get("asset_version_id") or item.get("version_id")
                if version_id is not None:
                    return UUID(str(version_id))

    if snapshot is None or snapshot.artifact_production_run_id is None:
        return None
    version_id = await context.session.scalar(
        select(ProductionInputSnapshotItem.asset_version_id)
        .join(
            ProductionInputSnapshotItem.snapshot,
        )
        .where(
            ProductionInputSnapshotItem.snapshot.has(
                production_run_id=snapshot.artifact_production_run_id
            ),
            ProductionInputSnapshotItem.asset_key == asset_key,
            ProductionInputSnapshotItem.asset_version_id.is_not(None),
        )
        .limit(1)
    )
    return version_id
