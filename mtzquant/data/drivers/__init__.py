# coding:utf-8
# @author            : 木头左
# @create_time       : 2026/08/16 00:44:00
# @update_time       : 2026/08/16 21:59:08
# @description       : D1/D2 数据源驱动包：协议/注册表 + CSV 驱动（导入即注册 local_csv）

"""数据源驱动包（设计 3.2/3.5）：协议/注册表 + CSV 驱动。

导入本包即完成 register_driver('local_csv', CsvSourceDriver)——
新增数据源 = 一个模块 + 一行 register_driver（3.2 不加任何框架改动）。
"""

from __future__ import annotations

from mtzquant.data.drivers.base import (
    DriverRegistry,
    InstrumentRef,
    SourceDriver,
    create_driver,
    register_driver,
)
from mtzquant.data.drivers.csv_driver import CsvSourceDriver

# 注册 v1 唯一驱动（引擎/UI 通过 settings.data.driver 选用, 3.6）
register_driver("local_csv", CsvSourceDriver)

__all__ = [
    "CsvSourceDriver",
    "DriverRegistry",
    "InstrumentRef",
    "SourceDriver",
    "create_driver",
    "register_driver",
]
