from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.qa.domain import ExecutionPlan, KnowledgeQuery, MapIntent, ToolCallPlan


class PlanValidationError(ValueError):
    pass


class ToolDefinitionProtocol(Protocol):
    name: str
    input_model: type[BaseModel]


class ToolRegistryProtocol(Protocol):
    def get(self, name: str) -> ToolDefinitionProtocol | None: ...


class ToolCallPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class KnowledgeQueryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    top_k: int = Field(ge=1, le=20)


class MapIntentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str = Field(min_length=1)
    target_ref: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class PlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1)
    tool_calls: list[ToolCallPayload] = Field(default_factory=list, max_length=8)
    knowledge_queries: list[KnowledgeQueryPayload] = Field(
        default_factory=list,
        max_length=5,
    )
    map_intents: list[MapIntentPayload] = Field(default_factory=list)
    clarification: str | None = None


def _validation_message(exc: ValidationError) -> str:
    messages = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "plan"
        messages.append(f"{location}: {error['msg']}")
    return "; ".join(messages)


class PlanValidator:
    def validate(
        self,
        plan: ExecutionPlan | dict[str, Any],
        registry: ToolRegistryProtocol,
    ) -> ExecutionPlan:
        if isinstance(plan, ExecutionPlan):
            payload = asdict(plan)
        elif isinstance(plan, dict):
            payload = plan
        else:
            raise PlanValidationError("plan must be a JSON object")
        return self.validate_payload(payload, registry)

    def validate_payload(
        self,
        payload: dict[str, Any],
        registry: ToolRegistryProtocol,
    ) -> ExecutionPlan:
        self._check_size(payload)
        try:
            parsed = PlanPayload.model_validate(payload)
        except ValidationError as exc:
            raise PlanValidationError(f"unknown field: {_validation_message(exc)}") from None

        names = [call.name for call in parsed.tool_calls]
        if len(names) != len(set(names)):
            duplicate = next(name for name in names if names.count(name) > 1)
            raise PlanValidationError(f"duplicate tool: {duplicate}")

        normalized_calls = [
            self._normalize_tool_call(call, registry) for call in parsed.tool_calls
        ]
        return ExecutionPlan(
            intent=parsed.intent,
            tool_calls=normalized_calls,
            knowledge_queries=[
                KnowledgeQuery(text=query.text, top_k=query.top_k)
                for query in parsed.knowledge_queries
            ],
            map_intents=[
                MapIntent(
                    action_type=action.action_type,
                    target_ref=action.target_ref,
                    reason=action.reason,
                )
                for action in parsed.map_intents
            ],
            clarification=parsed.clarification,
        )

    def _normalize_tool_call(
        self,
        call: ToolCallPayload,
        registry: ToolRegistryProtocol,
    ) -> ToolCallPlan:
        definition = registry.get(call.name)
        if definition is None:
            raise PlanValidationError(f"unknown tool: {call.name}")

        input_model = definition.input_model
        allowed_fields = set(input_model.model_fields)
        unknown_fields = sorted(set(call.arguments) - allowed_fields)
        if unknown_fields:
            fields = ", ".join(unknown_fields)
            raise PlanValidationError(
                f"unknown field in tool {call.name}: {fields}"
            )

        try:
            normalized = input_model.model_validate(call.arguments).model_dump(
                mode="python"
            )
        except ValidationError:
            raise PlanValidationError(
                f"invalid arguments for tool {call.name}"
            ) from None
        return ToolCallPlan(name=call.name, arguments=normalized)

    def _check_size(self, payload: dict[str, Any]) -> None:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(serialized) > 20000:
            raise PlanValidationError("plan JSON exceeds 20000 characters")
