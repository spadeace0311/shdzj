export type EventKind =
  | "auto"
  | "formal"
  | "correction"
  | "manual"
  | "test"
  | "drill";

export interface EventSummary {
  id: string;
  source: string;
  event_kind: string;
  place: string;
  magnitude: string | number;
  depth_km: string | number;
  origin_time: string;
  longitude: string | number;
  latitude: string | number;
  institutional_level: string | null;
  service_level: number | null;
  revision_no: number;
}

export interface ResponseSuggestion {
  institutional_level?: string;
  service_level?: number | null;
  downgraded?: boolean;
  causes?: string[];
  rule_version?: string;
}

export interface EventDetail {
  id: string;
  source: string;
  place: string;
  magnitude: string | number;
  depth_km: string | number;
  origin_time: string;
  longitude: string | number;
  latitude: string | number;
  institutional_level: string | null;
  service_level: number | null;
  response_suggestion: ResponseSuggestion | null;
  response_rule_version: string | null;
  revision_no: number;
  event_kind: string | null;
}

export interface ManualEventInput {
  origin_time: string;
  longitude: string;
  latitude: string;
  magnitude: string;
  depth_km: string;
  source: string;
  source_event_id?: string;
  place?: string;
  event_kind: string;
}

export interface EventIngestResponse {
  event_id: string;
  revision_id?: string;
  revision_no?: number;
  event_kind?: string;
  institutional_level?: string | null;
  service_level?: number | null;
}

export interface TokenResponse {
  access_token: string;
  token_type?: string;
}

const INSTITUTIONAL_LEVEL_LABELS: Record<string, string> = {
  special_major: "特别重大响应",
  major: "重大响应",
  larger: "较大响应",
  general: "一般响应",
  none: "无需响应",
  pending: "待研判",
};

const SERVICE_LEVEL_LABELS: Record<number, string> = {
  1: "一级",
  2: "二级",
  3: "三级",
  4: "四级",
};

const EVENT_KIND_LABELS: Record<string, string> = {
  auto: "自动速报",
  formal: "正式报告",
  correction: "更正报告",
  manual: "人工事件",
  test: "测试",
  drill: "演练",
};

export function formatInstitutionalLevel(level: string | null | undefined): string {
  if (!level) {
    return "待研判";
  }
  return INSTITUTIONAL_LEVEL_LABELS[level] ?? level;
}

export function formatServiceLevel(level: number | null | undefined): string {
  if (level === null || level === undefined) {
    return "待研判";
  }
  const suffix = SERVICE_LEVEL_LABELS[level] ?? `${level}级`;
  return `服务响应${suffix}`;
}

export function formatEventKind(kind: string | null | undefined): string {
  if (!kind) {
    return "未知";
  }
  return EVENT_KIND_LABELS[kind] ?? kind;
}

export function isTestOrDrill(kind: string | null | undefined): boolean {
  return kind === "test" || kind === "drill";
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatMagnitude(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  return Number(value).toFixed(1);
}

export function formatDepth(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  return `${Number(value).toFixed(1)} km`;
}

export function formatCoordinate(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  return Number(value).toFixed(4);
}
