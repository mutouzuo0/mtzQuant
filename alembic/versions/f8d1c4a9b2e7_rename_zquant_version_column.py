# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 16:10:00
# @update_time        : 2026/08/16 16:10:00
# @description : 品牌重命名迁移：backtest_run.zquant_version → mtzquant_version（SQLite 走 batch 模式）

"""rename zquant_version to mtzquant_version (brand rename)

Revision ID: f8d1c4a9b2e7
Revises: d6f2a1b9c0e0
Create Date: 2026-08-16 16:10:00.000000

品牌重命名 zQuant → mtzQuant：backtest_run 表列 zquant_version 改名
mtzquant_version（ORM/manifest/repo 已同步新名）。SQLite 需 batch 模式
（表重建方式实现 rename column）。初始迁移不改（已应用的库不重放）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f8d1c4a9b2e7"
down_revision: Union[str, Sequence[str], None] = "d6f2a1b9c0e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("backtest_run") as batch:
        batch.alter_column(
            "zquant_version",
            new_column_name="mtzquant_version",
            existing_type=sa.String(length=32),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("backtest_run") as batch:
        batch.alter_column(
            "mtzquant_version",
            new_column_name="zquant_version",
            existing_type=sa.String(length=32),
            existing_nullable=False,
        )
