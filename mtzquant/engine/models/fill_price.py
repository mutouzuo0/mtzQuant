# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/15 22:30:00
# @update_time        : 2026/09/05 15:30:00
# @description : FillModel 基准价：same_close(默认)/same_open/next_open/next_close，零价差（5.3.3）

"""FillModel 基准价选择（设计 5.3.3）。

四种基准价：same_close（默认，决策当日收盘，5.3.3 前视警示）/ same_open（决策当日开盘——
聚宽 run_daily 早盘槽位折叠用, 忠实"低开早盘买入"类策略 9:31 开盘成交语义）/ next_open
（次日开盘，保真基线）/ next_close（次日收盘）。默认 half_spread=0（成交价=基准价,
对齐 PTrade 精确收盘, 5.3.3）; half_spread>0 时买入取 ask 侧代理（基准价×(1+half_spread)）、
卖出取 bid 侧代理（基准价×(1-half_spread)）。成交成本默认由 SlippageModel（策略 set_slippage）承担。

`fill_price(bar, side, basis=None)` 支持**按单基准覆盖**（basis 参数优先于 self.basis）——
平台适配器对 run_daily 早盘槽位订单打 same_open 标, 晚盘槽位打 same_close, 实现
"早盘折叠开盘、尾盘折叠收盘"的日线近似（M2 调度语义增强）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mtzquant.engine.models.bar import MinimalBar
from mtzquant.engine.orders import OrderDirection


class PriceBasis(StrEnum):
    NEXT_OPEN = "next_open"  # 次日开盘（保真基线, 设计 5.3.3 无前视）
    SAME_CLOSE = "same_close"  # 默认: 决策当日收盘（5.3.3 same-bar 口径, 前视警示）
    NEXT_CLOSE = "next_close"  # 次日收盘
    SAME_OPEN = "same_open"  # 决策当日开盘（聚宽早盘 run_daily 槽位折叠成交价）


_BUY_SIDES = frozenset({OrderDirection.BUY, OrderDirection.OPEN_LONG, OrderDirection.CLOSE_SHORT})
_SELL_SIDES = frozenset({OrderDirection.SELL, OrderDirection.CLOSE_LONG, OrderDirection.OPEN_SHORT})


@dataclass(frozen=True)
class FillModel:
    """基准价实现（无 I/O、纯计算）。默认 basis/half_spread 与引擎默认一致（5.3.3）。"""

    basis: PriceBasis = PriceBasis.SAME_CLOSE
    half_spread: float = 0.0  # 买卖侧代理价差（默认 0; >0 时买入上浮/卖出下浮）

    def fill_price(
        self, bar: MinimalBar, side: OrderDirection, basis: PriceBasis | None = None
    ) -> float:
        ref = self._reference(bar, basis)
        if side in _BUY_SIDES:
            return ref * (1 + self.half_spread)
        return ref * (1 - self.half_spread)

    def _reference(self, bar: MinimalBar, basis: PriceBasis | None = None) -> float:
        eff = basis or self.basis
        if eff is PriceBasis.NEXT_OPEN or eff is PriceBasis.SAME_OPEN:
            return bar.open
        return bar.close  # same_close / next_close 共用收盘语义（由触发时点区分）
