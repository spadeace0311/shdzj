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

export type WorkgroupTaskStatus =
  | "pending"
  | "in_progress"
  | "pending_review"
  | "completed"
  | "not_required"
  | "failed";

export type WorkgroupTaskTimelinessState =
  | "on_time"
  | "at_risk"
  | "overdue";

export interface WorkgroupTaskContributor {
  user_id: string;
  username: string;
  contribution_count: number;
  first_contributed_at: string;
  last_contributed_at: string;
}

export interface WorkgroupTask {
  id: string;
  event_id: string;
  workgroup_code: string;
  task_code: string;
  title: string;
  status: WorkgroupTaskStatus;
  timeliness_state: WorkgroupTaskTimelinessState;
  due_at: string | null;
  row_version: number;
  instruction: string;
  priority: number;
  phase_code: string | null;
  source_type: string;
  source_ref: string | null;
  activated_at: string | null;
  completed_at: string | null;
  closed_at: string | null;
  created_at: string;
  updated_at: string;
  contributors: WorkgroupTaskContributor[];
  can_work: boolean;
  can_confirm: boolean;
}

export interface TaskSubmitInput {
  result_text?: string | null;
}

export interface TemporaryTaskCreateInput {
  workgroup_code: string;
  title: string;
  instruction: string;
  priority: number;
  due_at?: string | null;
  continues_until_cancelled?: boolean;
  source_ref?: string | null;
}

export interface CommandHallActiveEvent {
  event_id: string | null;
}

export interface CommandHallTaskCounts {
  total: number;
  pending: number;
  in_progress: number;
  pending_review: number;
  completed: number;
  not_required: number;
  failed: number;
  overdue: number;
  at_risk: number;
  dual_version_count: number;
}

export interface CommandHallAlert {
  id: string;
  event_id: string;
  workgroup_code: string | null;
  task_id: string | null;
  alert_key: string;
  alert_type: string;
  severity: "info" | "warning" | "critical";
  status: string;
  title: string;
  detail: Record<string, unknown>;
  first_seen_at: string;
  resolved_at: string | null;
  updated_at: string;
}

export interface CommandHallGroup {
  [key: string]: unknown;
  event_id: string;
  workgroup_code: string;
  name: string;
  display_order: number;
  roster_version: number | null;
  roster_fingerprint: string | null;
  leader: Record<string, unknown> | null;
  deputies: Record<string, unknown>[];
  members: Record<string, unknown>[];
  attendance: Record<string, unknown>[];
  confirming_authority: Record<string, unknown> | null;
  tasks: Record<string, unknown>[];
  task_count: number;
  task_counts: CommandHallTaskCounts;
  latest_deliverable: Record<string, unknown> | null;
  alert_summary: Record<string, number>;
  projection_version: number;
  updated_at: string;
}

export type CommandHallArtifactProductionMode =
  | "live"
  | "manual"
  | "test"
  | "drill"
  | "replay";

export interface CommandHallArtifact {
  publication_id: string;
  artifact_id: string;
  artifact_key: string;
  output_profile: string;
  artifact_version: number;
  status: ArtifactStatus;
  quality_grade: string | null;
  production_mode: CommandHallArtifactProductionMode | string;
  publication_mode: ArtifactPublicationMode | string;
  is_forced: boolean;
  published_at: string;
  file_name: string;
  format: string;
}

export interface CommandHallArtifactSummary {
  published_count: number;
  status_counts: Record<string, number>;
  production_modes?: Record<string, number>;
  latest_artifacts: CommandHallArtifact[];
}

export interface CommandHallOverview {
  event_id: string;
  event: Record<string, unknown>;
  group_count: number;
  groups: CommandHallGroup[];
  alerts: CommandHallAlert[];
  task_counts: CommandHallTaskCounts;
  artifact_summary: CommandHallArtifactSummary;
  alert_summary: Record<string, number>;
  dual_version_count: number;
  projection_version: number;
  sync_status: "current" | "syncing";
  projection_lag_seconds: number;
  projection_source_updated_at: string | null;
  updated_at: string;
}

export interface CommandHallGroupDetail {
  event_id: string;
  workgroup_code: string;
  group: Record<string, unknown>;
  tasks: Record<string, unknown>[];
  alerts: CommandHallAlert[];
  alert_summary: Record<string, number>;
  task_counts: CommandHallTaskCounts;
  projection_version: number;
  updated_at: string;
}

export interface CommandHallTaskDetail {
  id: string;
  event_id: string;
  task: Record<string, unknown>;
  contributors: Record<string, unknown>[];
  deliverables: Record<string, unknown>[];
  task_events: Record<string, unknown>[];
  notifications: Record<string, unknown>[];
  projection_version: number | null;
}

export interface CommandHallStreamEvent {
  type: string;
  data: Record<string, unknown>;
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

export type ArtifactGroup = "map" | "background" | "core";

export type ArtifactStatus = "complete" | "degraded" | "failed";

export type ArtifactPublicationMode =
  | "automatic"
  | "rebuild"
  | "superadmin_override";

export type ProductionRunStatus =
  | "pending"
  | "running"
  | "completed"
  | "partial"
  | "failed"
  | "canceled";

export interface ArtifactSummary {
  artifact_id: string;
  artifact_key: string;
  output_profile: string;
  display_name: string;
  artifact_version: number;
  status: ArtifactStatus;
  quality_grade: string;
  needs_review: boolean;
  production_mode: string;
  publication_mode: ArtifactPublicationMode;
  file_name: string;
  format: string;
  size_bytes: number;
  generated_at: string;
  download_url: string;
  thumbnail_url: string | null;
}

export interface ProductionRun {
  production_run_id: string;
  assessment_run_id: string;
  status: ProductionRunStatus;
  production_mode: string;
  launch_mode: string;
  generation_seq: number;
  generation_scope: string;
  deadline_basis_at: string;
  deadline_at: string;
  required_output_count: number;
  complete_count: number;
  degraded_count: number;
  failed_count: number;
  timeout_count: number;
  needs_review_count: number;
  is_current: boolean;
  artifacts: ArtifactSummary[];
  context_fingerprint: string;
  catalog_version: string;
  template_versions: Record<string, string>;
  data_asset_versions: Record<string, string>;
  renderer_versions: Record<string, string>;
  marker: string | null;
}

export interface ProductionFilters {
  kind: "all" | ArtifactGroup;
  status: "all" | ArtifactStatus;
  quality_grade: "all" | string;
  publication_mode: "all" | ArtifactPublicationMode;
}

export interface ArtifactOverrideInput {
  file: File;
  revision_id: string;
  reason: string;
  expected_current_artifact_id?: string;
  idempotency_key: string;
}

export interface ArtifactOverrideResponse {
  artifact_id: string;
  production_run_id: string;
  production_task_id: string;
  artifact_publication_id: string;
  status: string;
  generation_seq: number;
  file_name: string;
  checksum: string;
  size_bytes: number;
  generated_at: string;
  superseded_artifact_id: string | null;
}

export interface ArtifactProgressSummary {
  productionRunId: string;
  status: string;
  completeCount: number;
  degradedCount: number;
  failedCount: number;
  timeoutCount: number;
  needsReviewCount: number;
  requiredOutputCount: number;
  mapCount: number;
  backgroundDocumentCount: number;
  coreDocumentCount: number;
  progressText: string;
}

export type ArtifactPreviewState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; url: string }
  | { status: "error"; message: string };

export const BACKGROUND_DOC_KEYS = [
  "doc.background",
  "doc.housing",
  "doc.economy",
  "doc.population",
  "doc.key_targets",
  "doc.spatial_distances",
  "doc.area_overview",
  "doc.historical_catalog",
] as const;

export function artifactGroup(artifactKey: string): ArtifactGroup {
  if (artifactKey.startsWith("map.")) {
    return "map";
  }
  if ((BACKGROUND_DOC_KEYS as readonly string[]).includes(artifactKey)) {
    return "background";
  }
  return "core";
}

export function matchesArtifactFilters(
  artifact: ArtifactSummary,
  filters: ProductionFilters,
): boolean {
  if (filters.kind !== "all" && artifactGroup(artifact.artifact_key) !== filters.kind) {
    return false;
  }
  if (filters.status !== "all" && artifact.status !== filters.status) {
    return false;
  }
  if (
    filters.quality_grade !== "all" &&
    artifact.quality_grade !== filters.quality_grade
  ) {
    return false;
  }
  if (
    filters.publication_mode !== "all" &&
    artifact.publication_mode !== filters.publication_mode
  ) {
    return false;
  }
  return true;
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

export interface IntensityProductSummary {
  product_id: string;
  product_type: "model" | "instrument" | "fusion";
  status: string;
  quality_grade: string | null;
  coverage_ratio: number;
  output_checksum: string | null;
  statistics: Record<string, unknown>;
}

export interface IntensityResult {
  run_id: string;
  event_id: string;
  revision_id: string;
  run_status: string;
  products: IntensityProductSummary[];
}

export interface IntensityGridBand {
  name: string;
  unit: string | null;
  precision: number | null;
}

export interface IntensityGridArtifact {
  product_id: string;
  checksum: string;
  width: number;
  height: number;
  srid: number;
  bbox: [number, number, number, number];
  coverage_ratio: number;
  bands: IntensityGridBand[];
  tile_template: string;
}

export interface AssessmentRunStatus {
  run_id: string;
  event_id: string;
  revision_id: string;
  run_no: number;
  status: AssessmentRunStatusValue;
  t1_at: string | null;
  deadline_at: string;
  completed_task_count: number;
  failed_task_count: number;
  total_task_count: number;
  tasks: AssessmentTaskStatus[];
  intensity?: IntensityResult | null;
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
