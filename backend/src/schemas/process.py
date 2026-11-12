from typing import Any

from pydantic import BaseModel, Field


class LocatorCandidateIn(BaseModel):
    strategy: str = Field(default="css")
    selector: str
    desc: str = ""


class StepUpdateRequest(BaseModel):
    action: str | None = None
    target_desc: str | None = None
    locator_candidates: list[LocatorCandidateIn] | None = None
    timeout_ms: int | None = Field(default=None, ge=1000, le=120000)
    max_retries: int | None = Field(default=None, ge=0, le=5)
    assert_rule: str | None = None
    depends_on: list[int] | None = None


class StepImportRequest(BaseModel):
    steps: list[dict[str, Any]]
    replace: bool = False


class RecordingConfirmRequest(BaseModel):
    # 疑似错位项的序号（diff_items 中 type=suspected_misalign 的 seq），必须逐条人工确认
    confirmed_misalign_seqs: list[int] = Field(default_factory=list)
    note: str = ""


class RecordingRejectRequest(BaseModel):
    note: str = ""


class ProcessActionRequest(BaseModel):
    note: str = ""
