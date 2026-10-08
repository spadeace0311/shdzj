from app.qa.tools.artifacts import register_artifact_tools
from app.qa.tools.event import register_event_tools
from app.qa.tools.exposure import register_exposure_tools
from app.qa.tools.fault import register_fault_tools
from app.qa.tools.intensity import register_intensity_tools
from app.qa.tools.loss import register_loss_tools
from app.qa.tools.region import register_region_tools
from app.qa.tools.registry import (
    EmptyToolInput,
    ToolContext,
    ToolDefinition,
    ToolExecution,
    ToolRegistry,
    ToolResult,
    ToolStatus,
)
from app.qa.tools.seismicity import register_seismicity_tools


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_event_tools(registry)
    register_fault_tools(registry)
    register_seismicity_tools(registry)
    register_region_tools(registry)
    register_exposure_tools(registry)
    register_intensity_tools(registry)
    register_loss_tools(registry)
    register_artifact_tools(registry)
    return registry


__all__ = [
    "EmptyToolInput",
    "ToolContext",
    "ToolDefinition",
    "ToolExecution",
    "ToolRegistry",
    "ToolResult",
    "ToolStatus",
    "build_default_registry",
]
