from src.services import process_service, recording_service, run_service


def list_processes(db, status=None, matter_type=None) -> dict:
    items = process_service.list_processes(db, status, matter_type)
    return {"total": len(items), "items": items}


def get_process(db, process_id: int) -> dict:
    return process_service.process_detail(db, process_id)


def update_step(db, user, process_id: int, step_id: int, payload: dict) -> dict:
    return process_service.update_step(db, process_id, step_id, payload)


def rescore(db, process_id: int) -> dict:
    return process_service.rescore_process(db, process_id)


def publish(db, process_id: int, note: str = "") -> dict:
    return process_service.publish_process(db, process_id, note)


def reject_pending(db, user, process_id: int, note: str = "") -> dict:
    return process_service.reject_pending_version(db, user, process_id, note)


def archive(db, process_id: int, note: str = "") -> dict:
    return process_service.archive_process(db, process_id, note)


def import_steps(db, user, process_id: int, steps: list, replace: bool) -> dict:
    return process_service.import_steps(db, user, process_id, steps, replace)


def list_recordings(db, process_id: int, status: str | None) -> dict:
    items = recording_service.list_recordings(db, process_id, status)
    return {"total": len(items), "items": items}


def get_recording(db, recording_id: int) -> dict:
    return recording_service.recording_detail(db, recording_id)


def confirm_recording(db, user, recording_id: int, confirmed_seqs: list[int], note: str) -> dict:
    return recording_service.confirm_recording(db, user, recording_id, confirmed_seqs, note)


def reject_recording(db, user, recording_id: int, note: str) -> dict:
    return recording_service.reject_recording(db, user, recording_id, note)


def dry_run(db, user, process_id: int, material_id: int) -> dict:
    return run_service.create_run(db, user, process_id, material_id, "dry_run")
