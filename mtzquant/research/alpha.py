# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/17 00:10:00
# @update_time        : 2026/08/17 00:10:00
# @description : M3-S6 组合抽象最小实现：AlphaModel/AlphaCombiner/CapitalAllocator（设计 5.8.1）

"""组合研究层抽象（设计 5.8.1）——只定义接口与最小实现, 不引入 cvxpy。

- AlphaModel:      单策略 alpha 打分源（产出 scores 宽表, S3 消费）;
- StrategySleeve:  策略资金槽（某策略在组合中的权重份额）;
- AlphaCombiner:   alpha 合成（加权平均; IC 加权与 ML stacking 预留接口）;
- CapitalAllocator: 资本分配（固定分配; 风险预算预留接口）。

范围硬约束（§2.2）: 凸优化组合优化器 / 风险模型 M3 不做——接口占位即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from mtzquant.core.errors import MtzQuantError


@runtime_checkable
class AlphaModel(Protocol):
    """alpha 模型协议: 输入任意研究信号 → 逐期打分宽表（index=时点, columns=codes）。"""

    name: str

    def alpha(self, **kwargs: Any) -> pd.DataFrame: ...


@dataclass
class StrategySleeve:
    """策略资金槽: 某策略在组合中的权重份额（5.8.1 合成前归一）。"""

    name: str
    weight: float = 1.0  # 该策略在总组合中的分配比例（≤1）


class AlphaCombiner:
    """alpha 合成（5.8.1）——多策略打分 → 组合打分（加权平均）。

    预留: IC 加权（按历史 IC 归一权重）与 ML stacking（以策略 alpha 为特征再学习）。
    """

    def __init__(self, mode: str = "equal") -> None:
        if mode not in ("equal", "ic_weighted", "ml_stacking"):
            raise MtzQuantError(
                f"未知合成模式 {mode!r}", stage="alpha", hint="equal|ic_weighted|ml_stacking"
            )
        self.mode = mode

    def combine(
        self,
        alphas: dict[str, pd.DataFrame],
        sleeves: list[StrategySleeve] | None = None,
        *,
        ic_weights: dict[str, float] | None = None,
    ) -> pd.DataFrame:
        """加权合成各策略打分（对齐到公共日期轴; 缺失期 NaN → 剔除不参与）。"""
        if not alphas:
            raise MtzQuantError("alphas 为空", stage="alpha", hint="至少一个策略打分")
        if self.mode == "equal":
            w = {k: 1.0 / len(alphas) for k in alphas}
        elif self.mode == "ic_weighted":
            if not ic_weights:
                raise MtzQuantError(
                    "ic_weighted 需 ic_weights 参数",
                    stage="alpha",
                    hint="按各策略历史 IC 提供权重（预留）",
                )
            total = sum(abs(v) for v in ic_weights.values()) or 1.0
            w = {k: ic_weights.get(k, 0.0) / total for k in alphas}
        else:  # ml_stacking 预留: 空实现占位（§2.2 不做训练集成）
            raise MtzQuantError("ml_stacking 合成未实现（预留接口, §2.2 排除）", stage="alpha")
        frames = []
        for name, df in alphas.items():
            df2 = df.copy()
            df2.columns = pd.MultiIndex.from_product([[name], df.columns])
            frames.append(df2)
        combined = pd.concat(frames, axis=1)  # MultiIndex (name, code)
        # 按策略槽位加权（StrategySleeve.weight 归一）
        sleeve_map = {s.name: s.weight for s in (sleeves or [])}
        out = pd.DataFrame(
            index=combined.index,
            columns=sorted({c for _n, c in combined.columns}),
        )
        for name in alphas:
            sub = combined[name]
            factor = sleeve_map.get(name, 1.0) * w[name]
            for code in sub.columns:
                out[code] = out[code].add(sub[code] * factor, fill_value=0.0)
        return out


class CapitalAllocator:
    """资本分配（5.8.1）——固定分配最小实现; 风险预算预留接口。"""

    def __init__(self, mode: str = "fixed") -> None:
        if mode not in ("fixed", "risk_budget"):
            raise MtzQuantError(f"未知分配模式 {mode!r}", stage="alpha", hint="fixed|risk_budget")
        self.mode = mode

    def allocate(self, sleeves: list[StrategySleeve]) -> dict[str, float]:
        """按槽位固定分配（归一; 未归一输入自动归一）。"""
        if self.mode == "risk_budget":
            raise MtzQuantError(
                "risk_budget 未实现（预留接口, §2.2 排除; 需协方差风险模型）", stage="alpha"
            )
        total = sum(s.weight for s in sleeves) or 1.0
        return {s.name: s.weight / total for s in sleeves}
