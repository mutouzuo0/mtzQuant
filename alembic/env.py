# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 03:28:00
# @update_time        : 2026/08/16 21:59:08
# @description : Alembic env：绑定 mtzquant.store.models.Base 元数据（设计 8.3 迁移）

"""Alembic 迁移环境——绑定 mtzquant.store.models.Base 元数据。

数据库 URL 优先级: 环境变量 MTZQUANT_DB_URL > 默认 sqlite:///./mtzquant.db
（alembic.ini 的 sqlalchemy.url 作兜底）。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from mtzquant.store.models import Base  # noqa: F401  # 导入即注册全部表

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", os.environ.get("MTZQUANT_DB_URL", "sqlite:///./mtzquant.db"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
