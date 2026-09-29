export type EventKind =
  | "auto"
  | "formal"
  | "correction"
  | "manual"
  | "test"
  | "drill";

export type CollectorState =
  | "starting"
  | "healthy"
  | "degraded"
  | "critical"
  | "stopped";

export interface CollectorProviderStatus {
  provider: "fan" | "wolfx";
  state: CollectorState;
  connected: boolean;
  last_http_status: number | null;
  last_connected_at: string | null;
  last_message_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  reconnect_count: number;
  last_error: string | null;
  updated_at: string;
}

export interface CollectorStatus {
  overall_state: CollectorState;
  providers: CollectorProviderStatus[];
  open_dead_letter_count: number;
  boundary_version: string | null;
  last_ingested_event_id: string | null;
}

export type EventLifecycleState =
  | "auto_pending"
  | "formal_triggered"
  | "correction_triggered"
  | "not_applicable";

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
  lifecycle_state: EventLifecycleState;
  t1_at: string | null;
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
  lifecycle_state: EventLifecycleState;
  t1_at: string | null;
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
  lifecycle_state?: EventLifecycleState | null;
  t1_at?: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type?: string;
}

export interface CurrentUser {
  username: string;
  role: string;
  workgroup: string | null;
}

export type DataAssetType = "vector" | "table" | "raster" | "parameter";

export type DataAssetVersionStatus =
  | "imported"
  | "validated"
  | "published"
  | "retired"
  | "rejected";

export interface DataAssetSummary {
  asset_key: string;
  region_id: string;
  name: string;
  data_type: DataAssetType;
  spatial_granularity: string;
  responsibility_unit: string;
  update_interval_days: number;
  is_core: boolean;
  published_version: string | null;
  published_at: string | null;
  update_due_at: string | null;
  is_update_overdue: boolean;
}

export interface ValidationIssue {
  severity: string;
  code: string;
  message: string;
  row_number: number | null;
  field_name: string | null;
}

export interface DataAssetVersion {
  id: string;
  asset_key: string;
  region_id: string;
  version: string;
  status: DataAssetVersionStatus;
  source_uri: string;
  source_crs: string;
  license_name: string | null;
  acquired_at: string | null;
  valid_from: string | null;
  valid_to: string | null;
  quality_grade: string | null;
  change_note: string | null;
  schema_summary: Record<string, unknown>;
  record_count: number;
  checksum: string;
  imported_by: string;
  reviewed_by: string | null;
  imported_at: string;
  validated_at: string | null;
  published_at: string | null;
  retired_at: string | null;
  validation_errors: ValidationIssue[];
  validation_warnings: ValidationIssue[];
  statistics: Record<string, unknown>;
}

export interface DataAssetImportInput {
  version: string;
  source_uri: string;
  license_name?: string;
  change_note: string;
  file: File;
}

export interface DataAssetImportAccepted {
  job_id: string;
  version_id: string;
  asset_key: string;
  version: string;
  status: "queued" | "running" | "completed" | "rejected" | "failed";
}

export interface DataAssetImportJob {
  job_id: string;
  asset_key: string;
  version: string;
  version_id: string;
  status: "queued" | "running" | "completed" | "rejected" | "failed";
  error_summary: string | null;
  validation_errors: ValidationIssue[];
  validation_warnings: ValidationIssue[];
  statistics: Record<string, unknown>;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface ValidationReport {
  version_id: string;
  status: string;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
  statistics: Record<string, unknown>;
  checked_at: string;
}

export type AssessmentRunStatusValue =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "canceled";

export type AssessmentTaskStatusValue =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "skipped"
  | "canceled";

export interface AssessmentTaskStatus {
  id: string;
  task_key: string;
  task_type: string;
  component: string;
  priority: number;
  sequence: number;
  status: AssessmentTaskStatusValue;
  deadline_at: string;
  started_at: string | null;
  completed_at: string | null;
  attempt_count: number;
  max_attempts: number;
  last_error: string | null;
}

export interface AssessmentRunStatus {
  run_id: string;
  event_id: string;
  revision_id: string;
  run_no: number;
  status: AssessmentRunStatusValue;
  t1_at: string;
  deadline_at: string;
  completed_task_count: number;
  failed_task_count: number;
  total_task_count: number;
  tasks: AssessmentTaskStatus[];
}

export type LossProductType =
  | "building_damage"
  | "population_impact"
  | "casualties"
  | "economic_loss"
  | "resource_demand"
  | "validation";

export type LossProductStatus =
  | "complete"
  | "partial"
  | "unavailable"
  | "invalid";

export type LossQualityGrade = "L1" | "L2" | "L3" | "L0";

export type LossCalibrationStatus =
  | "calibrated"
  | "reference_uncalibrated"
  | "uncalibrated";

export type LossValueType = "low" | "central" | "high";

export type LossValueStatus =
  | "available"
  | "zero"
  | "rounded_to_zero"
  | "unavailable"
  | "not_applicable";

export interface LossValue {
  area_scope: string;
  area_code: string;
  area_name: string | null;
  metric_key: string;
  value_type: LossValueType;
  value_status: LossValueStatus;
  numeric_value: number | null;
  unit: string;
  precision: number | null;
  quality_grade: LossQualityGrade;
  note: string | null;
}

export interface LossProductSummary {
  product_id: string;
  product_type: LossProductType;
  status: LossProductStatus;
  quality_grade: LossQualityGrade;
  calibration_status: LossCalibrationStatus;
  coverage_ratio: number;
  partial_scope: boolean;
  needs_review: boolean;
  spatialized_estimate: boolean;
  algorithm_version: string;
  parameter_version: string;
  region_profile_version: string;
  output_checksum: string | null;
  statistics: Record<string, unknown>;
  metrics: LossValue[];
  reason: string | null;
}

export interface LossResult {
  run_id: string;
  event_id: string;
  revision_id: string;
  effective_run_id: string | null;
  is_fallback: boolean;
  products: LossProductSummary[];
}

export type LossAreaScope = "city" | "county" | "town";

export interface LossAreaFeature {
  area_scope: LossAreaScope;
  area_code: string;
  area_name: string;
  geometry: Record<string, unknown>;
  metrics: LossValue[];
}

export interface LossAreaResponse {
  run_id: string;
  scope: LossAreaScope;
  features: LossAreaFeature[];
}

export interface LossGridArtifact {
  product_id: string;
  checksum: string;
  width: number;
  height: number;
  srid: number;
  bbox: [number, number, number, number];
  spatial_allocation_rule: string;
  coverage_ratio: number;
  bands: Array<{
    name: string;
    unit: string;
    precision: number | null;
  }>;
  spatialized_estimate: boolean;
  tile_template: string;
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

export function formatLifecycleState(value: string | null | undefined): string {
  if (value === "auto_pending") {
    return "自动待定";
  }
  if (value === "formal_triggered") {
    return "正式报已触发评估";
  }
  if (value === "correction_triggered") {
    return "修订已触发评估";
  }
  if (value === "not_applicable") {
    return "不适用";
  }
  return "未进入生命周期";
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
