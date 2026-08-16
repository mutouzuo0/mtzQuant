# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : MinimalBar：撮合模型共用的最小行情载体（不依赖数据层，阶段 D 再映射）

"""MinimalBar：撮合模型共用的最小行情载体（设计 5.3.2/5.3.3）。

数据层（阶段 D）提供完整 BarData 后，由 BrokerSim 适配为 MinimalBar 再喂给撮合模型；
本模块不 import 数据层，保持撮合模型纯计算、可独立单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MinimalBar:
    """撮合所需的最小行情字段（OHLCV + 状态标记）。"""

    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float  # 该 bar 总成量（容量约束基准之一）
    pre_close: float | None = None  # 昨收（一字板判定的分母）
    suspended: bool = False  # 停牌
    limit_up: bool = False  # 触及涨停
    limit_down: bool = False  # 触及跌停

    @property
    def is_one_word_limit(self) -> bool:
        """一字板：open==high==low==close 且触及停价（设计 5.3.2/5.3.3 涨跌停细化）。"""
        if self.suspended:
            return False
        if not (self.open == self.high == self.low == self.close):
            return False
        return self.limit_up or self.limit_down
