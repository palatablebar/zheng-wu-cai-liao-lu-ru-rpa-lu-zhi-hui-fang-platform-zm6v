"""录制导入服务：导入即可审阅候选，确认前正式运行不受影响。

流程：
1. import_recording：校验并归一化录制 → 与当前生效版本做差异分析 → 落 RecordingImport
   （pending_review）。相同内容指纹直接返回原批次，不新增版本；
2. 人工逐条审阅差异，suspected_misalign 必须逐条显式确认后才能 confirm；
3. confirm_recording：生成 pending_publish 版本快照（仍不影响正式运行），等待发布；
4. 发布时由 version_service 原子切换；驳回候选快照会同步撤回对应录制。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from src.constants.enums import ProcessStatus, ProcessVersionStatus, RecordingStatus, StepAction
from src.constants.limits import MAX_STEPS_PER_PROCESS
from src.models.process import Process
from src.models.process_version import ProcessVersion, RecordingImport
from src.models.step import Step
from src.services import version_service
from src.services.recording_diff import (
    build_recording_diff,
    content_fingerprint,
    summarize_diff,
)
from src.utils.errors import AppError

_VALID_ACTIONS = [item.value for item in StepAction]


def _get_process(db, process_id: int) -> Process:
    process = db.get(Process, process_id)
    if process is None:
        raise AppError(404, "PROCESS_NOT_FOUND", f"流程 {process_id} 不存在")
    return process


def normalize_recording_steps(steps: list[dict]) -> list[dict]:
    if not steps:
        raise AppError(422, "EMPTY_RECORDING", "录制 JSON 中没有步骤")
    if len(steps) > MAX_STEPS_PER_PROCESS:
        raise AppError(422, "TOO_MANY_STEPS", f"步骤数超过上限 {MAX_STEPS_PER_PROCESS}")
    normalized: list[dict] = []
    for index, raw in enumerate(steps, start=1):
        action = str(raw.get("action", "click"))
        if action not in _VALID_ACTIONS:
            raise AppError(422, "INVALID_ACTION", f"第 {index} 步动作不支持：{action}")
        candidates = raw.get("locator_candidates") or raw.get("locators") or []
        candidates = [dict(item) for item in candidates if isinstance(item, dict)][:6]
        if not candidates:
            candidates = [{"strategy": "css", "selector": f"#recorded-step-{index}",
                           "desc": "录制导入默认定位器"}]
        normalized.append({
            "action": action,
            "target_desc": str(raw.get("target_desc") or raw.get("target") or f"录制步骤 {index}")[:255],
            "sample_value": str(raw.get("sample_value") or raw.get("sample") or "")[:255],
            "locator_candidates": candidates,
            "timeout_ms": int(raw.get("timeout_ms", 15000)),
            "max_retries": int(raw.get("max_retries", 2)),
            "depends_on": [int(item) for item in raw.get("depends_on", [])],
            "assert_rule": raw.get("assert_rule") or None,
        })
    return normalized


def _baseline_steps(db, process: Process) -> list[dict]:
    """差异比对基准：优先当前已发布版本快照；从未发布则回退工作区步骤。"""
    published = version_service.get_published_version(db, process.id)
    if published is not None:
        return list(published.steps_snapshot or [])
    rows = db.scalars(select(Step).where(Step.process_id == process.id).order_by(Step.seq)).all()
    return [version_service.snapshot_from_step(row) for row in rows]


def import_recording(db, actor: dict, process_id: int, steps: list[dict],
                     replace: bool = False) -> dict:
    process = _get_process(db, process_id)
    if process.status == ProcessStatus.ARCHIVED.value:
        raise AppError(409, "PROCESS_ARCHIVED", "已归档流程不允许导入录制步骤")

    normalized = normalize_recording_steps(steps)
    fingerprint = content_fingerprint(normalized, replace)

    # 待发布版本存在时工作区处于冻结状态：任何新导入都先报门禁冲突
    pending = version_service.get_pending_version(db, process_id)
    if pending is not None:
        raise AppError(409, "PENDING_VERSION_EXISTS",
                       "该流程已有待发布版本，请先发布或驳回后再导入新录制")

    existing = db.scalars(
        select(RecordingImport).where(
            RecordingImport.process_id == process_id,
            RecordingImport.content_fingerprint == fingerprint,
            # 已驳回/已撤回的批次不参与去重：允许修正后重新导入同一份录制
            RecordingImport.status.in_([
                RecordingStatus.PENDING_REVIEW.value,
                RecordingStatus.CONFIRMED.value,
            ]),
        )
    ).first()
    if existing is not None:
        # 幂等：重复执行同一次录制不新增批次/版本
        return {
            "recording": _recording_out(existing),
            "deduplicated": True,
            "message": "与既有录制内容完全一致，已复用原候选批次，未新增版本",
        }

    diff_items = build_recording_diff(normalized, _baseline_steps(db, process))
    summary = summarize_diff(diff_items)
    recording = RecordingImport(
        process_id=process_id,
        status=RecordingStatus.PENDING_REVIEW.value,
        replace=bool(replace),
        content_fingerprint=fingerprint,
        raw_steps=normalized,
        diff_items=diff_items,
        diff_summary=summary,
        created_by=(actor or {}).get("email"),
    )
    db.add(recording)
    db.commit()
    db.refresh(recording)
    return {"recording": _recording_out(recording), "deduplicated": False,
            "message": "录制已生成可审阅候选，确认前正式运行仍使用当前发布版本"}


def list_recordings(db, process_id: int, status: str | None = None) -> list[dict]:
    _get_process(db, process_id)
    query = select(RecordingImport).where(RecordingImport.process_id == process_id)
    if status:
        query = query.where(RecordingImport.status == status)
    rows = db.scalars(query.order_by(RecordingImport.id.desc())).all()
    return [_recording_out(row) for row in rows]


def get_recording(db, recording_id: int) -> RecordingImport:
    recording = db.get(RecordingImport, recording_id)
    if recording is None:
        raise AppError(404, "RECORDING_NOT_FOUND", f"录制批次 {recording_id} 不存在")
    return recording


def recording_detail(db, recording_id: int) -> dict:
    return {"recording": _recording_out(get_recording(db, recording_id))}


def confirm_recording(db, actor: dict, recording_id: int,
                      confirmed_misalign_seqs: list[int], note: str = "") -> dict:
    recording = get_recording(db, recording_id)
    if recording.status != RecordingStatus.PENDING_REVIEW.value:
        raise AppError(409, "RECORDING_NOT_PENDING",
                       f"录制批次当前状态 {recording.status}，不能重复确认")
    process = _get_process(db, recording.process_id)

    flagged = [item for item in (recording.diff_items or [])
               if item["type"] == "suspected_misalign"]
    flagged_seqs = {int(item["seq"]) for item in flagged if item.get("seq") is not None}
    confirmed = {int(seq) for seq in confirmed_misalign_seqs}
    missing = sorted(flagged_seqs - confirmed)
    if missing:
        raise AppError(
            422, "MISALIGN_NOT_CONFIRMED",
            "存在未人工确认的疑似错位字段，正式运行前必须逐条确认",
            detail={"pending_seqs": missing},
        )
    unknown = sorted(confirmed - flagged_seqs)
    if unknown:
        raise AppError(422, "UNKNOWN_MISALIGN_SEQ", f"以下序号不属于疑似错位项：{unknown}")

    # 把人工确认结果回写进差异项，保留审计痕迹
    diff_items = list(recording.diff_items or [])
    for item in diff_items:
        if item.get("type") == "suspected_misalign" and int(item.get("seq", -1)) in confirmed:
            item["review_status"] = "misalign_confirmed"
            item["confirmed_by"] = (actor or {}).get("email")
    recording.diff_items = diff_items

    version = version_service.create_candidate_version(
        db, process, recording.raw_steps, recording.diff_summary,
        source_recording_id=recording.id,
        note=note or f"录制批次 #{recording.id} 人工确认生成",
    )
    recording.status = RecordingStatus.CONFIRMED.value
    recording.version_id = version.id
    recording.reviewed_by = (actor or {}).get("email")
    recording.reviewed_at = datetime.now()
    recording.note = (recording.note or "") + (f"\n{note}" if note else "")
    db.commit()
    db.refresh(recording)
    return {
        "recording": _recording_out(recording),
        "version": version_service_out(version),
        "message": "候选已确认并生成待发布版本；发布前正式运行仍使用原发布版本",
    }


def reject_recording(db, actor: dict, recording_id: int, note: str = "") -> dict:
    recording = get_recording(db, recording_id)
    if recording.status != RecordingStatus.PENDING_REVIEW.value:
        raise AppError(409, "RECORDING_NOT_PENDING",
                       f"录制批次当前状态 {recording.status}，不能驳回")
    recording.status = RecordingStatus.REJECTED.value
    recording.reviewed_by = (actor or {}).get("email")
    recording.reviewed_at = datetime.now()
    recording.note = ((recording.note or "") + f"\n驳回：{note}").strip()
    db.commit()
    db.refresh(recording)
    return {"recording": _recording_out(recording)}


def discard_confirmed_candidate(db, actor: dict, version: ProcessVersion, note: str = "") -> None:
    """驳回待发布版本快照时，同步把来源录制置为 discarded，可重新导入新录制。"""
    if version.status != ProcessVersionStatus.PENDING_PUBLISH.value:
        raise AppError(409, "VERSION_NOT_PENDING", f"版本 v{version.version} 不是待发布状态")
    version_service.reject_candidate_version(db, version)
    if version.source_recording_id:
        recording = db.get(RecordingImport, version.source_recording_id)
        if recording is not None and recording.status == RecordingStatus.CONFIRMED.value:
            recording.status = RecordingStatus.DISCARDED.value
            recording.reviewed_by = (actor or {}).get("email")
            recording.reviewed_at = datetime.now()
            recording.note = ((recording.note or "") + f"\n候选版本驳回：{note}").strip()
    db.commit()


def _recording_out(recording: RecordingImport) -> dict:
    return {
        "id": recording.id,
        "process_id": recording.process_id,
        "status": recording.status,
        "replace": bool(recording.replace),
        "content_fingerprint": recording.content_fingerprint,
        "raw_steps": recording.raw_steps or [],
        "diff_items": recording.diff_items or [],
        "diff_summary": recording.diff_summary or {},
        "version_id": recording.version_id,
        "note": recording.note or "",
        "created_by": recording.created_by,
        "reviewed_by": recording.reviewed_by,
        "reviewed_at": recording.reviewed_at.isoformat() if recording.reviewed_at else None,
        "created_at": recording.created_at.isoformat() if recording.created_at else None,
    }


def version_service_out(version: ProcessVersion) -> dict:
    return {
        "id": version.id,
        "process_id": version.process_id,
        "version": version.version,
        "status": version.status,
        "change_note": version.change_note,
        "source_recording_id": version.source_recording_id,
        "step_count": len(version.steps_snapshot or []),
        "review_summary": version.review_summary or {},
        "confirmed_by": version.confirmed_by,
        "confirmed_at": version.confirmed_at.isoformat() if version.confirmed_at else None,
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }
