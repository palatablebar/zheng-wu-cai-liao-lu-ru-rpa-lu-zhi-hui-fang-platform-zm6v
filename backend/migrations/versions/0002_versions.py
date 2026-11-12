"""process versions and recording imports

Revision ID: 0002_versions
Revises: 0001_initial
Create Date: 2026-11-12

录制导入可审阅化：
- process_versions：流程不可变版本快照（pending_publish/published/superseded/rejected），
  运行绑定版本、发布原子切换；
- recording_imports：录制候选批次与差异清单，内容指纹去重，疑似错位人工确认门禁。
"""
from alembic import op

from src.config.database import Base
import src.models  # noqa: F401  (register metadata)

revision = "0002_versions"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=[
        Base.metadata.tables["process_versions"],
        Base.metadata.tables["recording_imports"],
    ])


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, tables=[
        Base.metadata.tables["recording_imports"],
        Base.metadata.tables["process_versions"],
    ])
