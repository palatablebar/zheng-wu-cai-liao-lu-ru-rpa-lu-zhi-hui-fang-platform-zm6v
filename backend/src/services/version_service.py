"""流程版本快照服务：不可变快照 + 发布原子切换 + 运行绑定版本。

关键约束：
1. 录制导入只生成 pending_publish 候选快照，不动正式流程；正式运行只引用 published 快照；
2. 发布是一次原子切换：候选快照转 published，原 published 转 superseded，
   同时把候选步骤覆盖到工作区 steps 表（供编辑器/评分使用），整条流程一次切换；
3. 运行创建时固化 process_version，执行器始终按该版本读取 steps_snapshot，
   新发布不影响已经启动的运行；
4. 重复导入同一份录制（指纹相同）返回原批次，不新增版本。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from src.constants.enums import ProcessStatus, ProcessVersionStatus
from src.models.process import Process
from src.models.process_version import ProcessVersion
from src.models.step import Step
from src.utils.errors import AppError

# 每个流程最多保留的不可变快照数（超出后最旧的 superseded 快照清理）
MAX_VERSION_SNAPSHOTS = 20


class SnapshotStepView:
    """版本快照步骤的只读视图，字段接口与 Step 模型对齐，供序列化/看板/接管复用。"""

    def __init__(self, snapshot: dict):
        self.seq = int(snapshot["seq"])
        self.id = self.seq
        self.action = snapshot.get("action", "")
        self.target_desc = snapshot.get("target_desc", "")
        self.assert_rule = snapshot.get("assert_rule")
        self.locator_candidates = snapshot.get("locator_candidates") or []
        self.locator_scores = snapshot.get("locator_scores") or []


def step_view_map_for_run(db, run) -> dict[int, object]:
    """运行步骤视图索引：版本化运行按快照 seq，历史运行回退工作区 Step 主键。"""
    version = load_version_for_run(db, run)
    if version is not None:
        return {int(item["seq"]): SnapshotStepView(item) for item in (version.steps_snapshot or [])}
    return {step.id: step for step in db.scalars(
        select(Step).where(Step.process_id == run.process_id)).all()}


def snapshot_step_from_raw(raw: dict, seq: int) -> dict:
    """把录制/工作区步骤规范化为不可变快照里的一步。"""
    candidates = raw.get("locator_candidates") or raw.get("locators") or []
    return {
        "seq": int(seq),
        "action": str(raw.get("action", "click")),
        "target_desc": str(raw.get("target_desc") or raw.get("target") or f"录制步骤 {seq}")[:255],
        "sample_value": str(raw.get("sample_value") or raw.get("sample") or "")[:255],
        "locator_candidates": [dict(item) for item in candidates if isinstance(item, dict)][:6],
        "locator_scores": list(raw.get("locator_scores") or []),
        "robustness_score": float(raw.get("robustness_score", 50.0) or 50.0),
        "risk_level": str(raw.get("risk_level", "medium")),
        "risk_reason": str(raw.get("risk_reason", "no_history")),
        "timeout_ms": int(raw.get("timeout_ms", 15000)),
        "max_retries": int(raw.get("max_retries", 2)),
        "depends_on": [int(item) for item in (raw.get("depends_on") or [])],
        "assert_rule": raw.get("assert_rule") or None,
    }


def snapshot_from_step(step: Step) -> dict:
    return snapshot_step_from_raw(
        {
            "seq": step.seq,
            "action": step.action,
            "target_desc": step.target_desc,
            "sample_value": getattr(step, "sample_value", "") or "",
            "locator_candidates": step.locator_candidates or [],
            "locator_scores": step.locator_scores or [],
            "robustness_score": step.robustness_score,
            "risk_level": step.risk_level,
            "risk_reason": step.risk_reason,
            "timeout_ms": step.timeout_ms,
            "max_retries": step.max_retries,
            "depends_on": step.depends_on or [],
            "assert_rule": step.assert_rule,
        },
        step.seq,
    )


def next_version_number(db, process_id: int) -> int:
    last = db.scalars(
        select(ProcessVersion.version)
        .where(ProcessVersion.process_id == process_id)
        .order_by(ProcessVersion.version.desc())
        .limit(1)
    ).first()
    return int(last or 0) + 1


def get_published_version(db, process_id: int) -> ProcessVersion | None:
    return db.scalars(
        select(ProcessVersion)
        .where(ProcessVersion.process_id == process_id,
               ProcessVersion.status == ProcessVersionStatus.PUBLISHED.value)
        .order_by(ProcessVersion.version.desc())
    ).first()


def get_pending_version(db, process_id: int) -> ProcessVersion | None:
    return db.scalars(
        select(ProcessVersion)
        .where(ProcessVersion.process_id == process_id,
               ProcessVersion.status == ProcessVersionStatus.PENDING_PUBLISH.value)
        .order_by(ProcessVersion.version.desc())
    ).first()


def ensure_baseline_snapshot(db, process: Process) -> ProcessVersion | None:
    """历史流程（版本表上线前已发布）惰性补齐当前版本快照，保证运行可按版本取步骤。"""
    published = get_published_version(db, process.id)
    if published is not None:
        return published
    if process.status != ProcessStatus.PUBLISHED.value:
        return None
    steps = db.scalars(select(Step).where(Step.process_id == process.id).order_by(Step.seq)).all()
    if not steps:
        return None
    snapshot = ProcessVersion(
        process_id=process.id,
        version=int(process.version),
        status=ProcessVersionStatus.PUBLISHED.value,
        change_note="版本表上线前已发布流程的基线快照",
        steps_snapshot=[snapshot_from_step(step) for step in steps],
        review_summary={},
        published_at=process.published_at or datetime.now(),
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def load_version_for_run(db, run) -> ProcessVersion | None:
    """运行启动时绑定的版本；新模型运行必然命中，历史运行回退到当前 published/基线。"""
    version = db.scalars(
        select(ProcessVersion).where(
            ProcessVersion.process_id == run.process_id,
            ProcessVersion.version == run.process_version,
            ProcessVersion.status.in_([
                ProcessVersionStatus.PUBLISHED.value,
                ProcessVersionStatus.SUPERSEDED.value,
            ]),
        )
    ).first()
    if version is not None:
        return version
    process = db.get(Process, run.process_id)
    if process is not None and process.status == ProcessStatus.PUBLISHED.value:
        return ensure_baseline_snapshot(db, process)
    return None


def list_versions(db, process_id: int) -> list[ProcessVersion]:
    return list(db.scalars(
        select(ProcessVersion)
        .where(ProcessVersion.process_id == process_id)
        .order_by(ProcessVersion.version.desc())
    ).all())


def create_candidate_version(db, process: Process, steps_raw: list[dict], review_summary: dict,
                             source_recording_id: int | None, note: str) -> ProcessVersion:
    pending = get_pending_version(db, process.id)
    if pending is not None:
        raise AppError(
            409, "PENDING_VERSION_EXISTS",
            "该流程已有待发布版本，请先发布或驳回后再导入新录制",
        )
    version = ProcessVersion(
        process_id=process.id,
        version=next_version_number(db, process.id),
        status=ProcessVersionStatus.PENDING_PUBLISH.value,
        change_note=note[:255],
        source_recording_id=source_recording_id,
        steps_snapshot=[
            snapshot_step_from_raw(raw, index)
            for index, raw in enumerate(steps_raw, start=1)
        ],
        review_summary=review_summary,
    )
    db.add(version)
    db.flush()
    return version


_COMPARED_KEYS = (
    "seq", "action", "target_desc", "locator_candidates",
    "timeout_ms", "max_retries", "depends_on", "assert_rule",
)


def _working_snapshot_payload(db, process_id: int) -> list[dict]:
    steps = db.scalars(
        select(Step).where(Step.process_id == process_id).order_by(Step.seq)
    ).all()
    return [snapshot_from_step(step) for step in steps]


def working_matches_version(db, process_id: int, version: ProcessVersion) -> bool:
    """工作区步骤与指定版本快照内容是否一致（用于判断手工发布是否需要新版本）。"""
    current = _working_snapshot_payload(db, process_id)
    baseline = list(version.steps_snapshot or [])
    if len(current) != len(baseline):
        return False
    for left, right in zip(current, baseline):
        if any(left.get(key) != right.get(key) for key in _COMPARED_KEYS):
            return False
    return True


def create_candidate_from_workspace(db, process: Process, note: str) -> ProcessVersion:
    """把当前工作区步骤打包为待发布候选（草稿首发 / 编辑器手工调整后的发布入口）。"""
    payload = _working_snapshot_payload(db, process.id)
    if not payload:
        raise AppError(422, "EMPTY_PROCESS", "流程没有步骤，不能发布")
    return create_candidate_version(db, process, payload, {}, None, note or "工作区步骤发布")


def _replace_working_steps(db, process_id: int, snapshot_steps: list[dict]) -> None:
    """发布时把候选快照覆盖到工作区 steps 表，保证编辑器/评分与生效版本一致。"""
    old_steps = db.scalars(select(Step).where(Step.process_id == process_id)).all()
    for step in old_steps:
        db.delete(step)
    db.flush()
    # 旧对象已从 identity map 移除，同序号重新 insert 不再触发覆盖警告
    for item in snapshot_steps:
        db.add(Step(
            process_id=process_id,
            seq=int(item["seq"]),
            action=item["action"],
            target_desc=item["target_desc"],
            locator_candidates=list(item.get("locator_candidates") or []),
            locator_scores=list(item.get("locator_scores") or []),
            robustness_score=float(item.get("robustness_score", 50.0)),
            risk_level=item.get("risk_level", "medium"),
            risk_reason=item.get("risk_reason", "no_history"),
            timeout_ms=int(item.get("timeout_ms", 15000)),
            max_retries=int(item.get("max_retries", 2)),
            depends_on=list(item.get("depends_on") or []),
            assert_rule=item.get("assert_rule"),
        ))
    db.flush()


def switch_published_version(db, process: Process, candidate: ProcessVersion) -> ProcessVersion:
    """整条流程一次切换：候选转 published、旧 published 转 superseded、工作区同步。"""
    current = get_published_version(db, process.id)
    candidate.status = ProcessVersionStatus.PUBLISHED.value
    candidate.published_at = datetime.now()
    if current is not None and current.id != candidate.id:
        current.status = ProcessVersionStatus.SUPERSEDED.value

    _replace_working_steps(db, process.id, candidate.steps_snapshot)

    process.version = candidate.version
    process.status = ProcessStatus.PUBLISHED.value
    process.step_count = len(candidate.steps_snapshot)
    process.published_at = candidate.published_at
    archived = list(process.archived_versions or [])
    if current is not None and f"v{current.version}" not in archived:
        archived.append(f"v{current.version}")
    process.archived_versions = archived[-10:]

    _prune_old_snapshots(db, process.id)
    db.flush()
    return candidate


def _prune_old_snapshots(db, process_id: int) -> None:
    """仅当超上限时清理最旧的 superseded 快照；至少保留最近一条用于历史运行回放。"""
    versions = list_versions(db, process_id)
    removable = [item for item in versions
                 if item.status == ProcessVersionStatus.SUPERSEDED.value]
    overflow = len(versions) - MAX_VERSION_SNAPSHOTS
    if overflow <= 0:
        return
    for item in removable[:overflow]:
        db.delete(item)


def reject_candidate_version(db, version: ProcessVersion) -> None:
    version.status = ProcessVersionStatus.REJECTED.value
    db.flush()
