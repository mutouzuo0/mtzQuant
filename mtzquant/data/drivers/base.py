# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:12:00
# @update_time        : 2026/08/16 21:59:08
# @description : D1 数据源驱动：SourceDriver 协议 + DriverRegistry + InstrumentRef（设计 3.2）

"""数据源驱动层（设计 3.2）——三段式管道的第 ① 段。

SourceDriver 只负责「读取与列举」，不做业务归一（归一是 DataNormalizer 的职责）。
新增数据源 = 一个模块 + 一行 register_driver()；引擎/UI 通过配置项 data.driver 选用：
    register_driver("local_csv", CsvSourceDriver)          # v1 实现（drivers/csv_driver.py）
    register_driver("tushare", TushareSourceDriver)        # 预留
v1 不实现的负载口（tick/trades/orders/depth）在协议中留签名——接口让位、实现交给 M5。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from mtzquant.core.errors import MtzQuantError
from mtzquant.core.types import AdjustMode, Frequency, InstrumentType


@dataclass(frozen=True)
class InstrumentRef:
    """标的最小引用（设计 3.2/3.11：有限字段，与 master/instruments.csv 一一映射）。"""

    code: str  # 内部归一代码（如 510300.SH）
    name: str = ""
    instrument_type: InstrumentType = InstrumentType.STOCK
    list_date: datetime | None = None
    delist_date: datetime | None = None
    exchange: str = ""  # SH / SZ / BJ（可由 code 后缀推导，显式给出便于过滤）


@runtime_checkable
class SourceDriver(Protocol):
    """数据源驱动协议：只负责读取与列举，不做业务归一。

    实现约定: load_kline 返回的 DataFrame 保持来源原始列名与类型
    （不做列名映射/代码归一/时间戳归一——那些是 DataNormalizer 的职责）。
    """

    name: str  # 驱动名: "local_csv" / "tushare" / ...

    def list_instruments(
        self, instrument_type: InstrumentType | None = None
    ) -> list[InstrumentRef]: ...

    def load_kline(
        self,
        code: str,
        frequency: Frequency,
        start: datetime,
        end: datetime,
        fields: list[str] | None = None,  # 列裁剪, None=全部
        adjusted: AdjustMode = AdjustMode.NONE,  # 仅指标研究用途(3.14); 撮合用 raw
    ) -> pd.DataFrame: ...

    # ---- 以下接口 v1 不实现, 协议预留（M5）----
    def load_tick(self, code: str, start: datetime, end: datetime) -> pd.DataFrame: ...
    def load_trades(self, code: str, start: datetime, end: datetime) -> pd.DataFrame: ...
    def load_orders(self, code: str, start: datetime, end: datetime) -> pd.DataFrame: ...
    def load_depth(self, code: str, start: datetime, end: datetime) -> pd.DataFrame: ...


class DriverRegistry:
    """驱动注册表（设计 3.2：新增数据源 = 一个模块 + 一行注册）。"""

    def __init__(self) -> None:
        self._drivers: dict[str, type[SourceDriver]] = {}

    def register(self, name: str, driver_cls: type[SourceDriver]) -> None:
        if not name or not isinstance(name, str):
            raise MtzQuantError(
                f"驱动名必须为非空字符串，得到 {name!r}",
                stage="driver_registry",
                hint="register_driver('local_csv', CsvSourceDriver)",
            )
        if name in self._drivers and self._drivers[name] is not driver_cls:
            raise MtzQuantError(
                f"驱动名 {name!r} 已被注册为 {self._drivers[name].__name__}",
                stage="driver_registry",
                hint="更换驱动实现前先覆盖注册，或换用新驱动名",
            )
        self._drivers[name] = driver_cls

    def create(self, name: str, **config: Any) -> SourceDriver:
        cls = self._drivers.get(name)
        if cls is None:
            known = ", ".join(sorted(self._drivers)) or "（空）"
            raise MtzQuantError(
                f"未知数据驱动: {name!r}",
                stage="driver_registry",
                hint=f"已注册驱动: {known}；配置项 data.driver 检查 1.1 对应注册行",
            )
        return cls(**config)  # type: ignore[call-arg]

    def names(self) -> list[str]:
        return sorted(self._drivers)

    def __contains__(self, name: str) -> bool:
        return name in self._drivers


# 模块级默认注册表（全局唯一；测试可用独立实例隔离注册状态）
_default_registry = DriverRegistry()


def register_driver(name: str, driver_cls: type[SourceDriver]) -> None:
    """注册新数据源（设计 3.2：一个模块 + 一行注册）。"""
    _default_registry.register(name, driver_cls)


def create_driver(name: str, **config: Any) -> SourceDriver:
    """按配置创建驱动实例。"""
    return _default_registry.create(name, **config)
