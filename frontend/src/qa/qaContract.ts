import type {
  EventDetail,
  QaAnswer,
  QaAnswerAudit,
  QaMapAction,
  QaMapActionRecord,
  QaStreamEvent,
} from "../types";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function coordinatePair(value: unknown): [number, number] | null {
  if (
    !Array.isArray(value) ||
    value.length !== 2 ||
    !finiteNumber(value[0]) ||
    !finiteNumber(value[1])
  ) {
    return null;
  }
  const [longitude, latitude] = value as [number, number];
  if (
    longitude < -180 ||
    longitude > 180 ||
    latitude < -90 ||
    latitude > 90
  ) {
    return null;
  }
  return [longitude, latitude];
}

function boundsTuple(value: unknown): [number, number, number, number] | null {
  if (
    !Array.isArray(value) ||
    value.length !== 4 ||
    !value.every(finiteNumber)
  ) {
    return null;
  }
  const [west, south, east, north] = value as [
    number,
    number,
    number,
    number,
  ];
  if (
    west < -180 ||
    east > 180 ||
    south < -90 ||
    north > 90 ||
    west >= east ||
    south >= north
  ) {
    return null;
  }
  return [west, south, east, north];
}

function layerSelection(
  value: unknown,
): string[] | Record<string, boolean> | null {
  if (Array.isArray(value)) {
    if (
      value.length === 0 ||
      !value.every((layer) => typeof layer === "string" && layer.length > 0)
    ) {
      return null;
    }
    return value as string[];
  }
  if (!isRecord(value) || Object.keys(value).length === 0) {
    return null;
  }
  const visibility = visibilityRecord(value);
  return visibility ? { ...visibility } : null;
}

function visibilityRecord(
  value: unknown,
): Record<string, boolean> | null {
  if (!isRecord(value) || Object.keys(value).length === 0) {
    return null;
  }
  const normalized: Record<string, boolean> = {};
  for (const [key, visible] of Object.entries(value)) {
    if (typeof visible !== "boolean") {
      return null;
    }
    normalized[key] = visible;
  }
  return normalized;
}

function metadata(data: Record<string, unknown>): {
  provenance?: Record<string, unknown>;
  source_tool?: string | null;
} {
  return {
    ...(isRecord(data.provenance) ? { provenance: data.provenance } : {}),
    ...(typeof data.source_tool === "string" || data.source_tool === null
      ? { source_tool: data.source_tool }
      : {}),
  };
}

export function parseQaMapAction(
  data: Record<string, unknown>,
): QaMapAction | null {
  const actionType = stringValue(data.action_type);
  const reason = stringValue(data.reason) ?? "";
  const validUntil = stringValue(data.valid_until) ?? "";
  if (!actionType) {
    return null;
  }

  if (actionType === "locate") {
    const targetRef = stringValue(data.target_ref);
    const coordinates = data.coordinates
      ? coordinatePair(data.coordinates)
      : undefined;
    if (!targetRef || (data.coordinates !== undefined && !coordinates)) {
      return null;
    }
    return {
      action_type: "locate",
      target_ref: targetRef,
      reason,
      valid_until: validUntil,
      ...(coordinates ? { coordinates } : {}),
      ...(stringValue(data.feature_id)
        ? { feature_id: stringValue(data.feature_id)! }
        : {}),
      ...metadata(data),
    };
  }

  if (actionType === "fit_bounds") {
    const bounds = boundsTuple(data.bounds);
    if (!bounds) {
      return null;
    }
    return {
      action_type: "fit_bounds",
      bounds,
      reason,
      valid_until: validUntil,
      ...metadata(data),
    };
  }

  if (actionType === "buffer") {
    const targetRef = stringValue(data.target_ref);
    const center = data.center ? coordinatePair(data.center) : undefined;
    if (
      !targetRef ||
      !finiteNumber(data.radius_km) ||
      data.radius_km < 0.1 ||
      data.radius_km > 500 ||
      (data.center !== undefined && !center)
    ) {
      return null;
    }
    return {
      action_type: "buffer",
      target_ref: targetRef,
      radius_km: data.radius_km,
      reason,
      valid_until: validUntil,
      ...(center ? { center } : {}),
      ...(stringValue(data.feature_id)
        ? { feature_id: stringValue(data.feature_id)! }
        : {}),
      ...metadata(data),
    };
  }

  if (actionType === "highlight") {
    const targetRef = stringValue(data.target_ref);
    const layerId = stringValue(data.layer_id);
    const coordinates = data.coordinates
      ? coordinatePair(data.coordinates)
      : undefined;
    if (
      !targetRef ||
      !layerId ||
      (data.coordinates !== undefined && !coordinates)
    ) {
      return null;
    }
    return {
      action_type: "highlight",
      target_ref: targetRef,
      layer_id: layerId,
      reason,
      valid_until: validUntil,
      ...(stringValue(data.feature_id)
        ? { feature_id: stringValue(data.feature_id)! }
        : {}),
      ...(coordinates ? { coordinates } : {}),
      ...metadata(data),
    };
  }

  if (actionType === "set_layers") {
    const layers = layerSelection(data.layers);
    const visibility = data.visibility
      ? visibilityRecord(data.visibility)
      : undefined;
    if (!layers || (data.visibility !== undefined && !visibility)) {
      return null;
    }
    return {
      action_type: "set_layers",
      layers,
      reason,
      valid_until: validUntil,
      ...(visibility ? { visibility } : {}),
      ...(Array.isArray(data.source_tools) &&
      data.source_tools.every(
        (tool) => typeof tool === "string" && tool.length > 0,
      )
        ? { source_tools: data.source_tools as string[] }
        : {}),
      ...metadata(data),
    };
  }

  return null;
}

export function mapActionRecordToAction(
  record: QaMapActionRecord,
): QaMapAction | null {
  const payload = isRecord(record.payload) ? record.payload : {};
  const validUntil =
    record.valid_until ?? stringValue(payload.valid_until) ?? "";
  return parseQaMapAction({
    ...payload,
    action_type: record.action_type,
    valid_until: validUntil,
  });
}

export function answerIdFromEvents(events: QaStreamEvent[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const id = stringValue(events[index]!.data.answer_id);
    if (id) {
      return id;
    }
  }
  return null;
}

export function eventEpicenter(
  detail: EventDetail,
): [number, number] | undefined {
  if (
    detail.longitude === null ||
    detail.longitude === "" ||
    detail.latitude === null ||
    detail.latitude === ""
  ) {
    return undefined;
  }
  const longitude = Number(detail.longitude);
  const latitude = Number(detail.latitude);
  if (
    !Number.isFinite(longitude) ||
    !Number.isFinite(latitude) ||
    longitude < -180 ||
    longitude > 180 ||
    latitude < -90 ||
    latitude > 90
  ) {
    return undefined;
  }
  return [longitude, latitude];
}

export function qaAnswerAudit(
  answer: Pick<
    QaAnswer,
    | "model_name"
    | "model_version"
    | "prompt_version"
    | "execution_plan"
    | "tool_call_summary"
  >,
): QaAnswerAudit | null {
  const modelName =
    typeof answer.model_name === "string" ? answer.model_name : null;
  const modelVersion =
    typeof answer.model_version === "string" ? answer.model_version : null;
  const promptVersion =
    typeof answer.prompt_version === "string" ? answer.prompt_version : null;
  const executionPlan = isRecord(answer.execution_plan)
    ? answer.execution_plan
    : null;
  const toolCallSummary = Array.isArray(answer.tool_call_summary)
    ? answer.tool_call_summary.filter(isRecord)
    : [];
  if (
    modelName === null &&
    modelVersion === null &&
    promptVersion === null &&
    executionPlan === null &&
    toolCallSummary.length === 0
  ) {
    return null;
  }
  return {
    model_name: modelName,
    model_version: modelVersion,
    prompt_version: promptVersion,
    execution_plan: executionPlan,
    tool_call_summary: toolCallSummary,
  };
}
