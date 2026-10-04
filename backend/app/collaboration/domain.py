from enum import StrEnum


class WorkgroupCode(StrEnum):
    NEWS_INFORMATION = "news_information"
    MONITORING_FORECAST = "monitoring_forecast"
    COMPREHENSIVE_COORDINATION = "comprehensive_coordination"
    DAMAGE_ASSESSMENT = "damage_assessment"
    EMERGENCY_TECHNOLOGY = "emergency_technology"
    LOGISTICS = "logistics"
    CENTER_STATION = "center_station"


class DutyRole(StrEnum):
    LEADER = "leader"
    DEPUTY = "deputy"
    MEMBER = "member"
    VIEWER = "viewer"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PENDING_REVIEW = "pending_review"
    COMPLETED = "completed"
    NOT_REQUIRED = "not_required"
    FAILED = "failed"


class TimelinessState(StrEnum):
    ON_TIME = "on_time"
    AT_RISK = "at_risk"
    OVERDUE = "overdue"


class TaskSourceType(StrEnum):
    PREPLAN = "preplan"
    CORRECTION = "correction"
    AD_HOC = "ad_hoc"
    SYSTEM_REVIEW = "system_review"


class DeliverableRequirementKind(StrEnum):
    AUTOMATIC_ARTIFACT = "automatic_artifact"
    MANUAL_FILE = "manual_file"
    MANUAL_TEXT = "manual_text"
    MANUAL_FILE_OR_TEXT = "manual_file_or_text"


class DeliverableSourceKind(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    SUPERADMIN_OVERRIDE = "superadmin_override"
