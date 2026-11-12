from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin

class ProcessVersion(Base, TimestampMixin):
    """流程版本快照（不可变）。

    - status=pending_review/pending_publish 的快照只是候选，正式运行不会引用；
    - 每次发布只把一条快照原子切换为 published，原 published 快照随即失效；
    - 运行在创建时固化 process_version，执行器按该版本读取 steps_snapshot，
      因此发布切换不影响已经启动的运行。
    """

    __tablename__ = "process_versions"
    __table_args__ = (
        UniqueConstraint("process_id", "version", name="uq_process_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    process_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending_review", nullable=False, index=True)
    change_note: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    source_recording_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    steps_snapshot: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    review_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RecordingImport(Base, TimestampMixin):
    """一次录制导入：导入即生成可审阅候选，不直接改动正式流程。

    - content_fingerprint 对录制步骤内容做确定性哈希，重复导入同一份录制
      （replace 标志相同）直接返回原批次，不新增批次/版本；
    - diff_items 按字段名称（target_desc）、动作、样例值列出
      新增 / 缺失 / 疑似错位 / 更新 / 未变；
    - 疑似错位（suspected_misalign）全部人工确认前，批次不能确认，
      正式运行继续引用当前已发布版本。
    """

    __tablename__ = "recording_imports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    process_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending_review", nullable=False, index=True)
    replace: Mapped[bool] = mapped_column(default=False, nullable=False)
    # 内容指纹仅用于应用层幂等（配合状态过滤），不加唯一约束：
    # 驳回/撤回后允许重新导入同一份录制形成新批次。
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_steps: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    diff_items: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    diff_summary: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
