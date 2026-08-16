# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 02:00:00
# @update_time        : 2026/08/17 02:00:00
# @description : M4-W3 迁移：run_event_journal 事件日志表（8.3.7, WS 补帧/回放数据源）

"""run_event_journal table (8.3.7 / M4-W3)

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-08-17 02:00:00.000000

回测事件日志（append-only, 与 ResultStore 信封同构）——WS 断线补帧/逐日回放页签
唯一数据源（8.3.7）; (run_id, event_seq) 唯一, 由 run_task flush 钩子批量写入（8.7）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "run_event_journal",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("committed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("ts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("run_id", "event_seq", name="uq_journal_run_seq"),
    )
    op.create_index("ix_journal_run", "run_event_journal", ["run_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("run_event_journal")
