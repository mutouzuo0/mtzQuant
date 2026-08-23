# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 10:50:00
# @update_time        : 2026/08/17 10:50:00
# @description : M3 jqdata 兼容模块：让聚宽策略 `from jqdata import *` 本地生效 + jq 类

"""jqdata 兼容模块（M3）——聚宽策略顶部常写 `from jqdata import *`, 本地无 jqdata 包。

适配器 `load()` 在 exec 策略源码前, 把已注入的 API 命名空间 + jq 类挂到
`sys.modules["jqdata"]`, 使该 import 直接成功且与注入命名空间完全一致
（同名同对象, `from jqdata import *` 仅做一次命名空间合并）。

jq 类:
  - `PriceRelatedSlippage(value)`: 价格相关滑点（按成交价比例; set_slippage 取 .value）;
  - `OrderCost(open_tax, close_tax, open_commission, close_commission, min_commission, **kw)`:
    订单费用配置（set_order_cost 取字段）;
  - `datetime`: 标准库 datetime 模块（策略常 `datetime.timedelta(...)`, jq 命名空间自带）;
  - `log.set_level(...)`: no-op（注入 log 已限 info/warn/error, 级别过滤无意义）。

注意: `sys.modules["jqdata"]` 是进程级全局; 单进程单回测场景下安全, 并进程（serve/worker）
为 subprocess 隔离, 各自独立。
"""

from __future__ import annotations

import datetime as _datetime
import sys
import types
from typing import Any


class PriceRelatedSlippage:
    """聚宽价格相关滑点: `PriceRelatedSlippage(0.01)` → 按成交价 1% 滑点。"""

    def __init__(self, value: float) -> None:
        self.value = float(value)


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
    mod.__dict__["PriceRelatedSlippage"] = PriceRelatedSlippage
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
