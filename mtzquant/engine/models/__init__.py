# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 21:33:45
# @description : 可插拔撮合模型（设计 5.3.3）：Fill/Slippage/Fee/Liquidity/Latency 五模型汇总

"""可插拔撮合模型（设计 5.3.3）：Fill/Slippage/Fee/Liquidity/Latency 五模型汇总。

v1 即定义 Protocol 接口 + 默认实现，先简后繁；撮合/记账层只依赖默认实现的组合
`MatchingModels` 快照作为任务默认，不引用各模型内部细节（解耦，M5+ 可替换增强实现）。
"""

from __future__ import annotations

from mtzquant.engine.models.fee import FeeBreakdown, FeeModel
from mtzquant.engine.models.fill_price import FillModel, PriceBasis
from mtzquant.engine.models.latency import LatencyModel
from mtzquant.engine.models.liquidity import LiquidityBasis, LiquidityModel
from mtzquant.engine.models.slippage import SlippageModel

__all__ = [
    "FeeBreakdown",
    "FeeModel",
    "FillModel",
    "PriceBasis",
    "LatencyModel",
    "LiquidityBasis",
    "LiquidityModel",
    "SlippageModel",
]
