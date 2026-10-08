from __future__ import annotations

import pytest

from app.qa.provenance import NumericProvenanceValidator
from app.qa.tools.registry import ToolExecution, ToolResult


def _execution(name: str, value: dict) -> ToolExecution:
    return ToolExecution(
        name=name,
        result=ToolResult.ok(
            value=value,
            source=f"source:{name}",
            version="v1",
            parameters={"event_id": "event-1"},
        ),
    )


def _validator() -> NumericProvenanceValidator:
    return NumericProvenanceValidator()


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("最近断层约 999.9 公里。", "distance"),
        ("震中坐标为 121.7，31.9。", "coordinate"),
        ("历史地震最大震级为 9.9 级。", "magnitude"),
        ("影响人口约 120 万人。", "population"),
        ("影响面积约 999 平方公里。", "area"),
    ],
)
def test_rejects_unproven_key_numeric_claims(text: str, kind: str) -> None:
    validation = _validator().validate(
        text,
        executions=[_execution("fault.nearest", {"distance_km": 18.2})],
        model_evidence=[],
    )

    assert validation.safe is False
    assert kind in validation.unverified_claim_kinds
    assert "999" not in validation.clean_text
    assert "120" not in validation.clean_text
    assert validation.clean_text == (
        "无法确认：回答包含缺少工具或证据支撑的关键值。"
    )


def test_publishes_numbers_matching_deterministic_tool_results() -> None:
    validation = _validator().validate(
        "最近断层约 18.2 公里，震中为 121.5，31.2。",
        executions=[
            _execution(
                "fault.nearest",
                {"distance_km": 18.2, "fault_key": "f1"},
            ),
            _execution(
                "event.get_context",
                {"event_id": "event-1", "longitude": 121.5, "latitude": 31.2},
            ),
        ],
        model_evidence=[],
    )

    assert validation.safe is True
    assert validation.clean_text == (
        "最近断层约 18.2 公里，震中为 121.5，31.2。"
    )


def test_publishes_numbers_matching_exported_document_evidence() -> None:
    validation = _validator().validate(
        "预案规定影响面积为 125 平方公里。",
        executions=[],
        model_evidence=[{"text": "应急预案规定影响面积为 125 平方公里。"}],
    )

    assert validation.safe is True
    assert validation.clean_text == "预案规定影响面积为 125 平方公里。"


def test_ignores_citation_keys_dates_and_non_metric_numbers() -> None:
    validation = _validator().validate(
        "依据 [C1]，2026 年预案规定了 4 个响应等级。",
        executions=[],
        model_evidence=[{"text": "2026 年预案规定了 4 个响应等级。"}],
    )

    assert validation.safe is True
