# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 10:50:00
# @update_time        : 2026/09/05 11:30:00
# @description : M3 jqdata 兼容模块：from jqdata import * 生效 + jq 类（滑点/费率/OrderStatus）

"""jqdata 兼容模块（M3）——聚宽策略顶部常写 `from jqdata import *`, 本地无 jqdata 包。

适配器 `load()` 在 exec 策略源码前, 把已注入的 API 命名空间 + jq 类挂到
`sys.modules["jqdata"]`, 使该 import 直接成功且与注入命名空间完全一致
（同名同对象, `from jqdata import *` 仅做一次命名空间合并）。

jq 类:
  - `FixedSlippage(value)`: 固定滑点——**总价差**口径, 买卖各加减一半
    （官方: `FixedSlippage(0.02)` 交易时加减 0.01 元, 见 JoinQuantAPI.md set_slippage）;
  - `PriceRelatedSlippage(value)`: 价格相关滑点——同为**总价差**比例
    （`PriceRelatedSlippage(0.002)` 交易时加减当时价格的 0.1%）;
  - `PerTrade(buy_cost, sell_cost, min_cost)`: set_commission 按笔费率
    （官方默认 `PerTrade(buy_cost=0.0003, sell_cost=0.0013, min_cost=5)`,
    sell_cost = 卖出佣金 + 印花税合计）;
  - `OrderStatus`: 订单状态 str 枚举（open/filled=部分成交/canceled/rejected/held=全部成交…,
    见 JoinQuantAPI.md OrderStatus）;
  - `OrderCost(...)`: set_order_cost 费用配置;
  - `datetime`: 标准库 datetime 模块（策略常 `datetime.timedelta(...)`, jq 命名空间自带）;
  - `log.set_level(...)`: no-op（注入 log 已限定级别, 过滤无意义）。

注意: `sys.modules["jqdata"]` 是进程级全局; 单进程单回测场景下安全, 并进程（serve/worker）
为 subprocess 隔离, 各自独立。
"""

from __future__ import annotations

import datetime as _datetime
import enum
import sys
import types
from typing import Any


class FixedSlippage:
    """聚宽固定滑点: `FixedSlippage(0.02)` → 总价差 0.02 元, 买卖各加减 0.01。"""

    def __init__(self, value: float) -> None:
        self.value = float(value)


class PriceRelatedSlippage:
    """聚宽价格相关滑点: `PriceRelatedSlippage(0.002)` → 总价差 0.2%, 买卖各加减一半。"""

    def __init__(self, value: float) -> None:
        self.value = float(value)


class PerTrade:
    """聚宽 set_commission 按笔费率: buy_cost/sell_cost 为比例, min_cost 为每笔下限（元）。

    官方语义（JoinQuantAPI.md set_commission）: 卖出费率含印花税——
    `PerTrade(0.0003, 0.0013, 5)` = 买入万3、卖出万3+千1印花、每笔最低 5 元。
    """

    def __init__(
        self, buy_cost: float = 0.0003, sell_cost: float = 0.0013, min_cost: float = 5.0
    ) -> None:
        self.buy_cost = float(buy_cost)
        self.sell_cost = float(sell_cost)
        self.min_cost = float(min_cost)


class OrderStatus(enum.StrEnum):
    """聚宽订单状态（官方枚举值见 JoinQuantAPI.md OrderStatus; str 语义可直接与字符串比较）。

    注意 `filled` 是**部分成交**、`held` 才是全部成交（Order.filled == Order.amount）。
    """

    new = "new"  # 订单新创建未委托（盘前/隔夜单）
    open = "open"  # 订单未完成, 无任何成交
    filled = "filled"  # 订单未完成, 部分成交
    canceled = "canceled"  # 订单完成, 已撤销（可能有成交）
    rejected = "rejected"  # 订单完成, 交易所已拒绝（可能有成交）
    held = "held"  # 订单完成, 全部成交（Order.filled == Order.amount）
    pending_cancel = "pending_cancel"  # 订单取消中（仅实盘）


class OrderCost:
    """聚宽订单费用配置: `OrderCost(close_tax=0.001, open_commission=0.0003, ...)`。"""

    def __init__(
        self,
        open_tax: float = 0.0,
        close_tax: float = 0.0,
        open_commission: float = 0.0,
        close_commission: float = 0.0,
        min_commission: float = 0.0,
        **kw: Any,
    ) -> None:
        self.open_tax = float(open_tax)
        self.close_tax = float(close_tax)
        self.open_commission = float(open_commission)
        self.close_commission = float(close_commission)
        self.min_commission = float(min_commission)
        self.__dict__.update(kw)  # 其余聚宽字段透传（如 type='stock'）


def install_jqdata(namespace: dict[str, Any]) -> types.ModuleType:
    """构造并注册 `sys.modules["jqdata"]`（幂等; 每次安装覆盖, 内容与当前命名空间一致）。"""
    mod = types.ModuleType("jqdata")
    mod.__dict__["FixedSlippage"] = FixedSlippage
    mod.__dict__["PriceRelatedSlippage"] = PriceRelatedSlippage
    mod.__dict__["PerTrade"] = PerTrade
    mod.__dict__["OrderStatus"] = OrderStatus
    mod.__dict__["OrderCost"] = OrderCost
    mod.__dict__["datetime"] = _datetime
    for key, value in namespace.items():
        if not key.startswith("_"):
            mod.__dict__[key] = value
    mod.__dict__["__all__"] = [k for k in mod.__dict__ if not k.startswith("_")]
    sys.modules["jqdata"] = mod
    _install_pandas_compat()
    return mod


def _install_pandas_compat() -> None:
    """旧式 pandas API 兼容（聚宽历史策略依赖, 现代 pandas 已移除）:
    `DataFrame.append`（行拼接, 与 pd.concat(axis=0) 等价）。仅缺省时补装, 不覆盖用户自定义。"""
    import pandas as pd

    if not hasattr(pd.DataFrame, "append"):

        def _append(
            self: pd.DataFrame,
            other: Any,
            ignore_index: bool = False,
            verify_integrity: bool = False,
            sort: bool = False,
        ) -> pd.DataFrame:
            del verify_integrity
            return pd.concat([self, other], axis=0, ignore_index=ignore_index, sort=sort)

        pd.DataFrame.append = _append  # type: ignore[attr-defined]
