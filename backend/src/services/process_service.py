from datetime import datetime

from sqlalchemy import func, select

from src.constants.enums import ProcessStatus, RiskLevel, StepAction
from src.models.process import Process
from src.models.run import Run
from src.models.step import Step
from src.services import version_service
from src.services.executor import load_history
from src.services.locator_score import score_step
from src.services.serializers import process_out, step_out
from src.utils.errors import AppError


def success_rate_map(db) -> dict[int, float]:
    rows = db.execute(
        select(Run.process_id, Run.status, func.count(Run.id))
        .where(Run.trigger != "dry_run")
        .group_by(Run.process_id, Run.status)
    ).all()
    totals: dict[int, int] = {}
    succeeded: dict[int, int] = {}
    for process_id, status, count in rows:
        totals[process_id] = totals.get(process_id, 0) + count
        if status == "succeeded":
            succeeded[process_id] = succeeded.get(process_id, 0) + count
    return {pid: (succeeded.get(pid, 0) / total if total else 0.0) for pid, total in totals.items()}


def list_processes(db, status: str | None = None, matter_type: str | None = None) -> list[dict]:
    from src.services import recording_service
    from src.models.process_version import ProcessVersion, RecordingImport
    from src.constants.enums import ProcessVersionStatus, RecordingStatus

    query = select(Process).order_by(Process.id)
    if status:
        query = query.where(Process.status == status)
    if matter_type:
        query = query.where(Process.matter_type == matter_type)
    processes = db.scalars(query).all()
    rates = success_rate_map(db)

    pending_versions = db.scalars(select(ProcessVersion).where(
        ProcessVersion.status == ProcessVersionStatus.PENDING_PUBLISH.value)).all()
    pending_by_process = {item.process_id: item for item in pending_versions}
    pending_counts = dict(db.execute(
        select(RecordingImport.process_id, func.count(RecordingImport.id))
        .where(RecordingImport.status == RecordingStatus.PENDING_REVIEW.value)
        .group_by(RecordingImport.process_id)
    ).all())

    result = []
    for process in processes:
        item = process_out(process, rates.get(process.id, process.success_rate))
        pending = pending_by_process.get(process.id)
        item["pending_version"] = (
            recording_service.version_service_out(pending) if pending else None)
        item["pending_recording_count"] = pending_counts.get(process.id, 0)
        result.append(item)
    return result


def get_process(db, process_id: int) -> Process:
    process = db.get(Process, process_id)
    if process is None:
        raise AppError(404, "PROCESS_NOT_FOUND", f"流程 {process_id} 不存在")
    return process


def process_detail(db, process_id: int) -> dict:
    process = get_process(db, process_id)
    steps = db.scalars(select(Step).where(Step.process_id == process_id).order_by(Step.seq)).all()
    data = process_out(process, success_rate_map(db).get(process.id, process.success_rate))
    data["steps"] = [step_out(step) for step in steps]
    data["risk_summary"] = {
        "high": sum(1 for step in steps if step.risk_level == RiskLevel.HIGH.value),
        "medium": sum(1 for step in steps if step.risk_level == RiskLevel.MEDIUM.value),
        "low": sum(1 for step in steps if step.risk_level == RiskLevel.LOW.value),
    }
    published = version_service.get_published_version(db, process_id)
    pending = version_service.get_pending_version(db, process_id)
    from src.services import recording_service
    data["active_version"] = recording_service.version_service_out(published) if published else None
    data["pending_version"] = recording_service.version_service_out(pending) if pending else None
    data["versions"] = [recording_service.version_service_out(item)
                        for item in version_service.list_versions(db, process_id)]
    data["pending_recordings"] = recording_service.list_recordings(
        db, process_id, status="pending_review")
    return data


def build_step_scores(db, step: Step) -> dict:
    history = load_history(db, step.id)
    return score_step({
        "id": step.id,
        "seq": step.seq,
        "target_desc": step.target_desc,
        "locator_candidates": step.locator_candidates or [],
    }, history)


def rescore_process(db, process_id: int) -> dict:
    process = get_process(db, process_id)
    if version_service.get_pending_version(db, process_id) is not None:
        raise AppError(409, "PENDING_VERSION_EXISTS",
                       "存在待发布版本，工作区步骤已冻结；请先发布或驳回该版本后再评分")
    steps = db.scalars(select(Step).where(Step.process_id == process_id).order_by(Step.seq)).all()
    results = []
    for step in steps:
        result = build_step_scores(db, step)
        step.locator_scores = result["candidates"]
        step.robustness_score = float(result["robustness_score"])
        step.risk_level = result["risk_level"]
        step.risk_reason = result["risk_reason"]
        results.append(result)
    process.step_count = len(steps)
    db.commit()
    return {
        "process_id": process.id,
        "process_name": process.name,
        "steps": results,
        "high_risk_steps": [item["seq"] for item in results if item["risk_level"] == "high"],
    }


def update_step(db, process_id: int, step_id: int, payload: dict) -> dict:
    process = get_process(db, process_id)
    step = db.get(Step, step_id)
    if step is None or step.process_id != process_id:
        raise AppError(404, "STEP_NOT_FOUND", f"步骤 {step_id} 不属于流程 {process_id}")
    if process.status == ProcessStatus.ARCHIVED.value:
        raise AppError(409, "PROCESS_ARCHIVED", "已归档流程不允许编辑")
    if version_service.get_pending_version(db, process_id) is not None:
        raise AppError(409, "PENDING_VERSION_EXISTS",
                       "存在待发布版本，工作区步骤已冻结；请先发布或驳回该版本后再编辑")
    if payload.get("action") is not None:
        if payload["action"] not in [item.value for item in StepAction]:
            raise AppError(422, "INVALID_ACTION", f"不支持的步骤动作：{payload['action']}")
        step.action = payload["action"]
    if payload.get("target_desc") is not None:
        step.target_desc = payload["target_desc"][:255]
    if payload.get("locator_candidates") is not None:
        candidates = [dict(item) for item in payload["locator_candidates"]][:6]
        if not candidates:
            raise AppError(422, "LOCATOR_REQUIRED", "至少保留一个定位器候选")
        step.locator_candidates = candidates
    if payload.get("timeout_ms") is not None:
        step.timeout_ms = int(payload["timeout_ms"])
    if payload.get("max_retries") is not None:
        step.max_retries = int(payload["max_retries"])
    if payload.get("assert_rule") is not None:
        step.assert_rule = payload["assert_rule"] or None
    if payload.get("depends_on") is not None:
        step.depends_on = sorted({int(item) for item in payload["depends_on"] if int(item) != step.seq})
    db.flush()
    result = build_step_scores(db, step)
    step.locator_scores = result["candidates"]
    step.robustness_score = float(result["robustness_score"])
    step.risk_level = result["risk_level"]
    step.risk_reason = result["risk_reason"]
    db.commit()
    return {"step": step_out(step), "score": result, "process_id": process_id}


def import_steps(db, actor, process_id: int, steps: list[dict], replace: bool = False) -> dict:
    """录制导入不再直接改步骤：生成可审阅候选（新增/缺失/疑似错位），确认+发布后才生效。"""
    from src.services import recording_service
    return recording_service.import_recording(db, actor, process_id, steps, replace)


def publish_process(db, process_id: int, note: str = "") -> dict:
    from src.services import recording_service
    process = get_process(db, process_id)
    if process.status == ProcessStatus.ARCHIVED.value:
        raise AppError(409, "PROCESS_ARCHIVED", "已归档流程不能发布，请先恢复或新建版本")

    step_count = db.scalar(select(func.count(Step.id)).where(Step.process_id == process_id))
    if not step_count:
        raise AppError(422, "EMPTY_PROCESS", "流程没有步骤，不能发布")

    candidate = version_service.get_pending_version(db, process_id)
    published = version_service.get_published_version(db, process_id)

    if candidate is None and published is None:
        # 草稿首发：为当前工作区生成首个版本快照
        candidate = version_service.create_candidate_from_workspace(
            db, process, note or "草稿首发")
    elif candidate is None and published is not None \
            and not version_service.working_matches_version(db, process_id, published):
        # 已发布流程在编辑器手工调整过步骤：生成新版本快照，保证运行版本与工作区一致
        candidate = version_service.create_candidate_from_workspace(
            db, process, note or "编辑器手工调整发布")

    if candidate is not None:
        # 整条流程一次原子切换：候选转 published，工作区同步，旧版本转 superseded
        switched = version_service.switch_published_version(db, process, candidate)
        db.commit()
        return {"process": process_out(process),
                "version": recording_service.version_service_out(switched),
                "note": note, "switched": True}

    # 工作区与生效快照完全一致：仅刷新发布状态，不新增版本
    process.status = ProcessStatus.PUBLISHED.value
    process.step_count = int(step_count)
    process.published_at = datetime.now()
    db.commit()
    return {"process": process_out(process),
            "version": recording_service.version_service_out(published) if published else None,
            "note": note, "switched": False}


def reject_pending_version(db, actor, process_id: int, note: str = "") -> dict:
    from src.services import recording_service
    process = get_process(db, process_id)
    candidate = version_service.get_pending_version(db, process_id)
    if candidate is None:
        raise AppError(404, "PENDING_VERSION_NOT_FOUND", "该流程没有待发布版本")
    recording_service.discard_confirmed_candidate(db, actor, candidate, note)
    db.refresh(process)
    return {"process": process_out(process),
            "version": recording_service.version_service_out(candidate), "note": note}


def archive_process(db, process_id: int, note: str = "") -> dict:
    process = get_process(db, process_id)
    if process.status == ProcessStatus.ARCHIVED.value:
        return {"process": process_out(process), "note": note}
    process.status = ProcessStatus.ARCHIVED.value
    db.commit()
    return {"process": process_out(process), "note": note}


def step_for_run(db, message: str) -> None:
    raise AppError(422, "INVALID_STEP", message)
