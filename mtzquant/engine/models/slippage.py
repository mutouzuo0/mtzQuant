# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : SlippageModel 滑点：比例/固定，买上调卖下调

"""SlippageModel 滑点（设计 5.3.3）。

v1 支持比例滑点与固定滑点（每股绝对值），买入上调、卖出下调：
  buy:  price×(1+ratio)+fixed   sell: price×(1-ratio)-fixed
M5+ 增强：随参与率增大的冲击模型（大单更高滑点）。
"""

from __future__ import annotations

from dataclasses import dataclass

from mtzquant.core.errors import MtzQuantError
from mtzquant.engine.orders import OrderDirection


@dataclass(frozen=True)
class SlippageModel:
    """v1 默认滑点实现（纯计算）。"""

    ratio: float = 0.001  # 比例滑点（默认 0.1%）
    fixed: float = 0.0  # 固定滑点（默认 0，单位与价格一致）

    def __post_init__(self) -> None:
        if self.ratio < 0 or self.fixed < 0:
            raise MtzQuantError(
                f"滑点参数不能为负: ratio={self.ratio} fixed={self.fixed}",
                stage="slippage",
            )

    def apply(self, price: float, side: OrderDirection) -> float:
        if price <= 0:
            raise MtzQuantError(f"成交基准价必须为正，得到 {price}", stage="slippage")
        buy = side in (OrderDirection.BUY, OrderDirection.OPEN_LONG, OrderDirection.CLOSE_SHORT)
        if buy:
            return price * (1 + self.ratio) + self.fixed
        return price * (1 - self.ratio) - self.fixed
