from sqlalchemy import select

from src.constants.enums import RunStatus
from src.models.material import Material
from src.models.process import Process
from src.models.run import Run
from src.models.run_step import RunStep
from src.services import version_service
from src.services.serializers import run_out


def board_snapshot(db) -> dict:
    runs = db.scalars(select(Run).order_by(Run.id.desc()).limit(60)).all()
    process_map = {p.id: p for p in db.scalars(select(Process)).all()}
    material_map = {m.id: m for m in db.scalars(select(Material)).all()}
    # 每个运行按其启动时绑定的版本解析步骤（版本化运行取快照，历史运行回退工作区表）
    step_maps_by_run = {run.id: version_service.step_view_map_for_run(db, run) for run in runs}

    active_runs = [run for run in runs if run.status in (RunStatus.RUNNING.value, RunStatus.TAKEOVER.value)]
    run_steps = []
    if active_runs:
        run_steps = db.scalars(
            select(RunStep).where(RunStep.run_id.in_([run.id for run in active_runs]))
            .order_by(RunStep.seq)
        ).all()
    steps_by_run: dict[int, list] = {}
    for item in run_steps:
        steps_by_run.setdefault(item.run_id, []).append(item)

    def enrich(run: Run) -> dict:
        data = run_out(run)
        process = process_map.get(run.process_id)
        material = material_map.get(run.material_id)
        data["process_name"] = process.name if process else ""
        data["matter_type"] = process.matter_type if process else ""
        data["company_name"] = material.company_name if material else ""
        data["credit_code"] = material.credit_code if material else ""
        items = steps_by_run.get(run.id, [])
        step_map = step_maps_by_run.get(run.id, {})

        def _step(run_step: RunStep):
            return step_map.get(run_step.seq) or step_map.get(run_step.step_id)

        data["progress"] = {
            "total": run.total_steps or (process.step_count if process else 0),
            "done": sum(1 for item in items if item.status in ("ok", "retried", "skipped")),
            "steps": [
                {
                    "seq": item.seq,
                    "status": item.status,
                    "attempt": item.attempt,
                    "duration_ms": item.duration_ms,
                    "target_desc": (step.target_desc if (step := _step(item)) else ""),
                    "action": (step.action if (step := _step(item)) else ""),
                    "locator_used": item.locator_used,
                    "error_code": item.error_code,
                }
                for item in items
            ],
        }
        return data

    queue = [enrich(run) for run in runs if run.status == RunStatus.QUEUED.value]
    active = [enrich(run) for run in active_runs]
    failures = [enrich(run) for run in runs if run.status == RunStatus.FAILED.value][:5]
    stats = {
        "total": len(runs),
        "succeeded": sum(1 for run in runs if run.status == RunStatus.SUCCEEDED.value),
        "failed": sum(1 for run in runs if run.status == RunStatus.FAILED.value),
        "takeover": sum(1 for run in runs if run.status == RunStatus.TAKEOVER.value),
        "running": sum(1 for run in runs if run.status == RunStatus.RUNNING.value),
        "queued": len(queue),
    }
    return {
        "robot": {"name": "SU-BOT-01", "station": "苏州政务外包值守台 A2", "poll_seconds": 2},
        "stats": stats,
        "queue": queue,
        "active": active,
        "recent_failures": failures,
    }
