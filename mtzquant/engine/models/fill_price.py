# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : FillModel 基准价选择：next_open(默认)/same_close/next_close，买卖现货侧代理价差

"""FillModel 基准价选择（设计 5.3.3）。

v1 三种基准价：next_open（默认）/ same_close / next_close。
买入取 ask 侧代理（基准价×(1+half_spread)）、卖出取 bid 侧代理（基准价×(1-half_spread)），
价差以比例给出（默认 half_spread=0.001，即每股一档面对的中间价代理）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mtzquant.engine.models.bar import MinimalBar
from mtzquant.engine.orders import OrderDirection


class PriceBasis(StrEnum):
    NEXT_OPEN = "next_open"  # 默认：下一 bar 开盘价（日线=次日开盘）
    SAME_CLOSE = "same_close"  # 同 bar 收盘价
    NEXT_CLOSE = "next_close"  # 下一 bar 收盘价


_BUY_SIDES = frozenset({OrderDirection.BUY, OrderDirection.OPEN_LONG, OrderDirection.CLOSE_SHORT})
_SELL_SIDES = frozenset({OrderDirection.SELL, OrderDirection.CLOSE_LONG, OrderDirection.OPEN_SHORT})


@dataclass(frozen=True)
class FillModel:
    """v1 默认基准价实现（无 I/O、纯计算）。"""

    basis: PriceBasis = PriceBasis.NEXT_OPEN
    half_spread: float = 0.001  # 买卖侧代理价差（比例，0.1%）

    def fill_price(self, bar: MinimalBar, side: OrderDirection) -> float:
        ref = self._reference(bar)
        if side in _BUY_SIDES:
            return ref * (1 + self.half_spread)
        return ref * (1 - self.half_spread)

    def _reference(self, bar: MinimalBar) -> float:
        if self.basis is PriceBasis.NEXT_OPEN:
            return bar.open
        return bar.close  # same_close / next_close 共用收盘语义（由触发时点区分）
