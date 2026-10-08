import json

import pytest
from pydantic import BaseModel, Field

from app.qa.domain import ExecutionPlan, KnowledgeQuery, MapIntent, ToolCallPlan
from app.qa.planner import PlanValidationError, PlanValidator


class NearestFaultInput(BaseModel):
    event_id: str = Field(min_length=1)
    max_distance_km: float = Field(default=50.0, gt=0, le=500)


class FakeToolDefinition:
    def __init__(self, name: str, input_model: type[BaseModel]) -> None:
        self.name = name
        self.input_model = input_model


class EmptyRegistry:
    def get(self, name: str) -> None:
        return None


class FakeRegistry:
    def __init__(self) -> None:
        self._tools = {
            "fault.nearest": FakeToolDefinition("fault.nearest", NearestFaultInput)
        }

    def get(self, name: str) -> FakeToolDefinition | None:
        return self._tools.get(name)


def test_plan_validator_rejects_unknown_tool_and_sql() -> None:
    validator = PlanValidator()
    unknown = ExecutionPlan(
        intent="distance",
        tool_calls=[ToolCallPlan(name="db.raw_sql", arguments={"sql": "select 1"})],
        knowledge_queries=[],
        map_intents=[],
        clarification=None,
    )

    with pytest.raises(PlanValidationError, match="unknown tool"):
        validator.validate(unknown, EmptyRegistry())


def test_plan_validator_rejects_non_object_payload() -> None:
    with pytest.raises(PlanValidationError, match="object"):
        PlanValidator().validate("not-an-object", EmptyRegistry())  # type: ignore[arg-type]


def test_plan_validator_rejects_unknown_top_level_fields() -> None:
    with pytest.raises(PlanValidationError, match="unknown field"):
        PlanValidator().validate(
            {
                "intent": "distance",
                "tool_calls": [],
                "knowledge_queries": [],
                "map_intents": [],
                "clarification": None,
                "sql": "select 1",
            },
            EmptyRegistry(),
        )


def test_plan_validator_rejects_duplicate_tool_names() -> None:
    plan = {
        "intent": "distance",
        "tool_calls": [
            {"name": "fault.nearest", "arguments": {"event_id": "e1"}},
            {"name": "fault.nearest", "arguments": {"event_id": "e2"}},
        ],
        "knowledge_queries": [],
        "map_intents": [],
        "clarification": None,
    }

    with pytest.raises(PlanValidationError, match="duplicate tool"):
        PlanValidator().validate(plan, FakeRegistry())


def test_plan_validator_rejects_unknown_tool_argument_fields() -> None:
    plan = {
        "intent": "distance",
        "tool_calls": [
            {
                "name": "fault.nearest",
                "arguments": {"event_id": "e1", "sql": "select 1"},
            }
        ],
        "knowledge_queries": [],
        "map_intents": [],
        "clarification": None,
    }

    with pytest.raises(PlanValidationError, match="unknown field"):
        PlanValidator().validate(plan, FakeRegistry())


def test_plan_validator_rejects_invalid_tool_arguments() -> None:
    plan = {
        "intent": "distance",
        "tool_calls": [
            {
                "name": "fault.nearest",
                "arguments": {"event_id": ""},
            }
        ],
        "knowledge_queries": [],
        "map_intents": [],
        "clarification": None,
    }

    with pytest.raises(PlanValidationError, match="invalid arguments"):
        PlanValidator().validate(plan, FakeRegistry())


def test_plan_validator_rejects_too_many_knowledge_queries() -> None:
    plan = {
        "intent": "distance",
        "tool_calls": [],
        "knowledge_queries": [
            {"text": f"query {index}", "top_k": 5} for index in range(6)
        ],
        "map_intents": [],
        "clarification": None,
    }

    with pytest.raises(PlanValidationError, match="knowledge_queries"):
        PlanValidator().validate(plan, EmptyRegistry())


def test_plan_validator_rejects_top_k_above_cap() -> None:
    plan = {
        "intent": "distance",
        "tool_calls": [],
        "knowledge_queries": [{"text": "活动断层", "top_k": 21}],
        "map_intents": [],
        "clarification": None,
    }

    with pytest.raises(PlanValidationError, match="top_k"):
        PlanValidator().validate(plan, EmptyRegistry())


def test_plan_validator_rejects_too_many_tool_calls() -> None:
    plan = {
        "intent": "distance",
        "tool_calls": [
            {"name": "fault.nearest", "arguments": {"event_id": f"e{index}"}}
            for index in range(9)
        ],
        "knowledge_queries": [],
        "map_intents": [],
        "clarification": None,
    }

    with pytest.raises(PlanValidationError, match="tool_calls"):
        PlanValidator().validate(plan, FakeRegistry())


def test_plan_validator_rejects_payload_over_20000_chars() -> None:
    plan = {
        "intent": "distance",
        "tool_calls": [],
        "knowledge_queries": [{"text": "x" * 20001, "top_k": 5}],
        "map_intents": [],
        "clarification": None,
    }

    with pytest.raises(PlanValidationError, match="20000"):
        PlanValidator().validate(plan, EmptyRegistry())


def test_plan_validator_normalizes_tool_arguments() -> None:
    plan = {
        "intent": "distance",
        "tool_calls": [
            {
                "name": "fault.nearest",
                "arguments": {"event_id": "e1", "max_distance_km": "25.5"},
            }
        ],
        "knowledge_queries": [{"text": "活动断层", "top_k": 5}],
        "map_intents": [
            {
                "action_type": "buffer",
                "target_ref": "fault:nearest",
                "reason": "显示震中到最近断层距离",
            }
        ],
        "clarification": None,
    }

    validated = PlanValidator().validate(plan, FakeRegistry())

    assert validated == ExecutionPlan(
        intent="distance",
        tool_calls=[
            ToolCallPlan(
                name="fault.nearest",
                arguments={"event_id": "e1", "max_distance_km": 25.5},
            )
        ],
        knowledge_queries=[KnowledgeQuery(text="活动断层", top_k=5)],
        map_intents=[
            MapIntent(
                action_type="buffer",
                target_ref="fault:nearest",
                reason="显示震中到最近断层距离",
            )
        ],
        clarification=None,
    )


def test_plan_payload_serialization_stays_under_cap() -> None:
    plan = {
        "intent": "distance",
        "tool_calls": [],
        "knowledge_queries": [{"text": "活动断层数据说明", "top_k": 5}],
        "map_intents": [],
        "clarification": None,
    }

    validated = PlanValidator().validate(plan, EmptyRegistry())
    serialized = json.dumps(
        {
            "intent": validated.intent,
            "tool_calls": [
                {"name": call.name, "arguments": call.arguments}
                for call in validated.tool_calls
            ],
            "knowledge_queries": [
                {"text": query.text, "top_k": query.top_k}
                for query in validated.knowledge_queries
            ],
            "map_intents": [
                {
                    "action_type": action.action_type,
                    "target_ref": action.target_ref,
                    "reason": action.reason,
                }
                for action in validated.map_intents
            ],
            "clarification": validated.clarification,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert len(serialized) <= 20000
