# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:12:00
# @update_time        : 2026/08/17 00:12:00
# @description : M3-S6 PortfolioOptimizer 协议 + 等权 max_weight 实现（5.8.1, 凸优化版占位）

"""组合优化器（设计 5.8.1）——协议 + 最小实现。

- `PortfolioOptimizer` 协议: 输入期望收益/约束 → 最优权重;
- `EqualWeightOptimizer`: 等权 + max_weight 约束的闭式解（不引入 cvxpy）;
- 凸优化版接口占位（§2.2: cvxpy/风险模型 M3 不做, Research Pro 按需启用）。

范围硬约束: 不允许在 M3 引入 cvxpy——凸优化版只留协议签名。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

from mtzquant.core.errors import MtzQuantError
from mtzquant.research.portfolio import Constraints


@runtime_checkable
class PortfolioOptimizer(Protocol):
    """组合优化器协议（5.8.1）: 期望收益 + 约束 → 权重 DataFrame。"""

    def optimize(
        self, expected_returns: pd.DataFrame, constraints: Constraints
    ) -> pd.DataFrame: ...


class EqualWeightOptimizer:
    """等权 + max_weight 约束（闭式解; 凸优化版占位）。"""

    name = "equal_weight"

    def optimize(self, expected_returns: pd.DataFrame, constraints: Constraints) -> pd.DataFrame:
        """逐期: 等权分配给正期望收益标的, 命中 max_weight 则封顶并重归一。"""
        if expected_returns.empty:
            raise MtzQuantError("expected_returns 为空", stage="optimizer")
        out = expected_returns.copy()
        for idx in out.index:
            row = out.loc[idx].fillna(0.0)
            pos = row[row > 0]
            if pos.empty:
                out.loc[idx] = 0.0
                continue
            n = len(pos)
            w = pd.Series(0.0, index=row.index)
            w[pos.index] = 1.0 / n
            if constraints.max_weight < 1.0:
                w = w.clip(upper=constraints.max_weight)
                total = float(w.sum())
                if total > 1e-12:
                    w = w / total
            out.loc[idx] = w
        return out


class ConvexOptimizer:
    """凸优化版占位（§2.2 排除: cvxpy 不进 M3; Research Pro 按需启用）。"""

    name = "convex"

    def __init__(self) -> None:
        raise MtzQuantError(
            "凸优化组合优化器 M3 未实现（§2.2 排除; cvxpy/风险模型归 Research Pro）",
            stage="optimizer",
            hint="M3 用 EqualWeightOptimizer 或 PortfolioConstructor（等权/市值/打分加权）",
        )
