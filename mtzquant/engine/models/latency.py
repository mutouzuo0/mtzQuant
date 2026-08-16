# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:30:00
# @description : LatencyModel 生效延迟：v1 固定 0（eligible_fill_at=下一 bar）

"""LatencyModel 生效延迟（设计 5.3.3）。

v1 默认 bars_delay=1：订单 submitted_at 后最早可撮合事件 = 下一 bar
（日线模式 = 次日开盘撮合事件，由 BrokerSim 按日历解析，模型只提供延迟拍数）。
"""

from __future__ import annotations

from dataclasses import dataclass

from mtzquant.core.errors import MtzQuantError


@dataclass(frozen=True)
class LatencyModel:
    """v1 默认延迟实现：下单后隔 bars_delay 个 bar 才 eligible_fill。"""

    bars_delay: int = 1  # 默认 1：下一 bar（0 表示同 bar 即时生效，v1 不承诺）

    def __post_init__(self) -> None:
        if self.bars_delay < 0:
            raise MtzQuantError(
                f"bars_delay 不能为负，得到 {self.bars_delay}",
                stage="latency",
            )

    def effective_sequence(self, submitted_sequence: int) -> int:
        """撮合事件序号下的最早可撮合序号（submitted + delay，供 BrokerSim 对照日历）。"""
        return submitted_sequence + self.bars_delay
