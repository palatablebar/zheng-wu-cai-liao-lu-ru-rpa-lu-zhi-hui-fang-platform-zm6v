from enum import Enum


class UserRole(str, Enum):
    ADMIN = "admin"
    ENGINEER = "engineer"
    VIEWER = "viewer"


class MatterType(str, Enum):
    ANNUAL_REPORT = "annual_report"
    QUALIFICATION = "qualification"
    SUBSIDY = "subsidy"
    CHANGE = "change"


class ProcessStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ProcessVersionStatus(str, Enum):
    """流程版本快照状态：待发布 → 已发布；被新版本替换的已发布快照标记 superseded。"""

    PENDING_REVIEW = "pending_review"
    PENDING_PUBLISH = "pending_publish"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class RecordingStatus(str, Enum):
    """录制批次状态：候选待审 → 已确认（生成待发布版本）/ 已驳回 / 已撤回。"""

    PENDING_REVIEW = "pending_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    DISCARDED = "discarded"


class DiffType(str, Enum):
    """录制相对当前生效版本的差异分类。"""

    ADDED = "added"
    MISSING = "missing"
    SUSPECTED_MISALIGN = "suspected_misalign"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class StepAction(str, Enum):
    CLICK = "click"
    INPUT = "input"
    UPLOAD = "upload"
    ASSERT = "assert"
    WAIT = "wait"


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TAKEOVER = "takeover"


class RunTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    DRY_RUN = "dry_run"


class RunStepStatus(str, Enum):
    OK = "ok"
    RETRIED = "retried"
    FAILED = "failed"
    SKIPPED = "skipped"


class InterventionReason(str, Enum):
    ASSERT_FAILED = "assert_failed"
    LOCATOR_DRIFT = "locator_drift"
    TIMEOUT = "timeout"
    MANUAL = "manual"


class InterventionStatus(str, Enum):
    OPEN = "open"
    RETRYING = "retrying"
    SKIPPED = "skipped"
    ABORTED = "aborted"
    RESOLVED = "resolved"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ErrorCode(str, Enum):
    E_TIMEOUT = "E_TIMEOUT"
    E_ASSERT_FAILED = "E_ASSERT_FAILED"
    E_LOCATOR_MISS = "E_LOCATOR_MISS"
    E_CREDIT_INVALID = "E_CREDIT_INVALID"
    E_CONCURRENCY_CONFLICT = "E_CONCURRENCY_CONFLICT"
    E_STEP_FAILED = "E_STEP_FAILED"


ACTIVE_RUN_STATUSES = (RunStatus.QUEUED.value, RunStatus.RUNNING.value, RunStatus.TAKEOVER.value)
