from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.qa.map_actions import MapActionBuilder
from app.qa.tools.registry import ToolExecution, ToolResult


NOW = datetime(2026, 10, 8, 2, 0, tzinfo=UTC)


def _builder() -> MapActionBuilder:
    return MapActionBuilder(clock=lambda: NOW)


def _tool_result(
    name: str,
    value: dict,
    *,
    parameters: dict | None = None,
) -> ToolExecution:
    return ToolExecution(
        name=name,
        result=ToolResult.ok(
            value=value,
            parameters=parameters or {},
        ),
    )


def test_builds_only_the_five_whitelisted_actions_with_ten_minute_ttl() -> None:
    actions = _builder().build(
        map_intents=[
            {
                "action_type": "locate",
                "target_ref": "fault:f1",
                "reason": "定位最近断层",
            },
            {
                "action_type": "fit_bounds",
                "bounds": [121.0, 30.8, 122.0, 31.5],
                "reason": "显示影响范围",
            },
            {
                "action_type": "buffer",
                "target_ref": "event:epicenter",
                "radius_km": 50,
                "reason": "绘制五十公里范围",
            },
            {
                "action_type": "highlight",
                "target_ref": "historical:event-1",
                "layer_id": "historical_earthquakes",
                "reason": "突出历史地震",
            },
            {
                "action_type": "set_layers",
                "layers": ["epicenter", "faults"],
                "reason": "显示相关图层",
            },
        ],
        tool_results=[
            _tool_result(
                "event.get_context",
                {
                    "event_id": "event-1",
                    "longitude": 121.5,
                    "latitude": 31.2,
                },
            ),
            _tool_result(
                "fault.nearest",
                {"fault_key": "f1", "distance_km": 8.2},
            ),
            _tool_result(
                "seismicity.within_radius",
                {
                    "event_id": "event-1",
                    "bounds": [121.0, 30.8, 122.0, 31.5],
                    "radius_km": 50,
                },
                parameters={"event_id": "event-1", "radius_km": 50},
            ),
            _tool_result(
                "seismicity.within_radius",
                {
                    "event_id": "event-1",
                    "events": [{"event_id": "historical-1"}],
                    "radius_km": 50,
                },
            ),
        ],
    )

    assert [action.action_type for action in actions] == [
        "locate",
        "fit_bounds",
        "buffer",
        "highlight",
        "set_layers",
    ]
    assert all(action.valid_until == NOW + timedelta(minutes=10) for action in actions)
    assert actions[1].bounds == [121.0, 30.8, 122.0, 31.5]
    assert actions[2].radius_km == 50
    assert actions[3].layer_id == "historical_earthquakes"
    assert actions[4].layers == ["epicenter", "faults"]


def test_fit_bounds_and_buffer_require_deterministic_tool_values() -> None:
    actions = _builder().build(
        map_intents=[
            {
                "action_type": "fit_bounds",
                "bounds": [121.0, 30.8, 122.0, 31.5],
                "reason": "显示影响范围",
            },
            {
                "action_type": "buffer",
                "target_ref": "event:epicenter",
                "radius_km": 50,
                "reason": "绘制五十公里范围",
            },
        ],
        tool_results=[],
    )

    assert actions == []


def test_model_values_are_replaced_by_deterministic_tool_values() -> None:
    actions = _builder().build(
        map_intents=[
            {
                "action_type": "fit_bounds",
                "bounds": [121.0, 30.8, 122.0, 31.5],
                "reason": "显示影响范围",
            },
            {
                "action_type": "buffer",
                "target_ref": "event:epicenter",
                "radius_km": 50,
                "reason": "绘制五十公里范围",
            },
        ],
        tool_results=[
            _tool_result(
                "seismicity.within_radius",
                {
                    "event_id": "event-1",
                    "bounds": [121.1, 30.9, 122.1, 31.6],
                    "radius_km": 50,
                },
                parameters={"event_id": "event-1", "radius_km": 50},
            )
        ],
    )

    assert len(actions) == 2
    assert actions[0].bounds == [121.1, 30.9, 122.1, 31.6]
    assert actions[1].radius_km == 50
    assert all(action.source_tool == "seismicity.within_radius" for action in actions)


def test_mismatched_model_values_are_ignored_in_favor_of_tool_values() -> None:
    actions = _builder().build(
        map_intents=[
            {
                "action_type": "fit_bounds",
                "bounds": [120.0, 30.0, 121.0, 31.0],
                "reason": "伪造范围",
            },
            {
                "action_type": "buffer",
                "target_ref": "event:epicenter",
                "radius_km": 80,
                "reason": "伪造半径",
            },
        ],
        tool_results=[
            _tool_result(
                "seismicity.within_radius",
                {
                    "event_id": "event-1",
                    "bounds": [121.0, 30.8, 122.0, 31.5],
                    "radius_km": 50,
                },
                parameters={"event_id": "event-1", "radius_km": 50},
            )
        ],
    )

    assert len(actions) == 2
    assert actions[0].bounds == [121.0, 30.8, 122.0, 31.5]
    assert actions[1].radius_km == 50


def test_rejects_html_script_unknown_fields_external_urls_and_unknown_layers() -> None:
    actions = _builder().build(
        map_intents=[
            {
                "action_type": "locate",
                "target_ref": "fault:f1",
                "reason": "定位断层",
                "script": "alert(1)",
            },
            {
                "action_type": "locate",
                "target_ref": "https://evil.invalid/map",
                "reason": "外部跳转",
            },
            {
                "action_type": "locate",
                "target_ref": "fault:f2",
                "reason": "<script>alert(1)</script>",
            },
            {
                "action_type": "highlight",
                "target_ref": "fault:f2",
                "layer_id": "arbitrary",
                "reason": "未知图层",
            },
            {
                "action_type": "set_layers",
                "layers": ["epicenter", "unknown"],
                "reason": "混合图层",
            },
            {"action_type": "run_script", "reason": "执行脚本"},
        ],
        tool_results=[],
    )

    assert actions == []


def test_rejects_invalid_bounds_and_buffer_radii() -> None:
    actions = _builder().build(
        map_intents=[
            {
                "action_type": "fit_bounds",
                "bounds": [200, 10, 201, 11],
                "reason": "越界",
            },
            {
                "action_type": "fit_bounds",
                "bounds": [122, 31, 121, 30],
                "reason": "边界颠倒",
            },
            {
                "action_type": "buffer",
                "target_ref": "event:epicenter",
                "radius_km": 0,
                "reason": "半径过小",
            },
            {
                "action_type": "buffer",
                "target_ref": "event:epicenter",
                "radius_km": float("nan"),
                "reason": "非法数值",
            },
        ],
        tool_results=[
            _tool_result(
                "event.get_context",
                {
                    "event_id": "event-1",
                    "bounds": [200, 10, 201, 11],
                },
            ),
            _tool_result(
                "event.get_context",
                {
                    "event_id": "event-1",
                    "bounds": [122, 31, 121, 30],
                },
            ),
            _tool_result(
                "seismicity.within_radius",
                {"event_id": "event-1", "radius_km": 0},
            ),
            _tool_result(
                "seismicity.within_radius",
                {"event_id": "event-1", "radius_km": float("nan")},
            ),
        ],
    )

    assert actions == []


def test_set_layers_accepts_only_explicit_predefined_visibility_mapping() -> None:
    actions = _builder().build(
        map_intents=[
            {
                "action_type": "set_layers",
                "layers": {
                    "epicenter": True,
                    "faults": False,
                    "unknown": True,
                },
                "reason": "切换图层",
            }
        ],
        tool_results=[],
    )

    assert actions == []
