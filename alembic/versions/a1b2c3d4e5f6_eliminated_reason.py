# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 01:10:00
# @update_time        : 2026/08/17 01:10:00
# @description : M3-U3 淘汰留痕迁移：backtest_run 增列 eliminated_reason（5.8.2, 8.3.1 回写）

"""eliminated_reason column (5.8.2 / M3-U3)

Revision ID: a1b2c3d4e5f6
Revises: f8d1c4a9b2e7
Create Date: 2026-08-17 01:10:00.000000

防过拟合淘汰留痕: backtest_run 增列 eliminated_reason TEXT NULL——
被淘汰候选 run 保留 + 淘汰原因（5.8.2; 设计 8.3.1 回写）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f8d1c4a9b2e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("backtest_run", sa.Column("eliminated_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("backtest_run", "eliminated_reason")
