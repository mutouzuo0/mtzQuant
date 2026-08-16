# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:09:30
# @description : 点时（Point-in-Time）查询协议（设计 3.13）

"""点时（Point-in-Time）查询协议（设计 3.13）。

仅 `timestamp <= cutoff` 不足以防未来数据——需要双时间校验:
    as_of           事件时间过滤: event_time  <= as_of
    knowledge_time  知识时间过滤: published_at <= knowledge_time（默认 = as_of）

行情类数据 event_time == published_at（盘中即时产生）→ 单参即可；
基本面/成分/规则/因子类数据两参语义不同，必须显式区分。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PitQuery:
    """一次点时查询的双时间边界。

    knowledge_time 缺省时等于 as_of（等价于"只按事件时间 cut off"，
    适用于 event_time == published_at 的行情类数据）。
    """

    as_of: datetime
    knowledge_time: datetime | None = None

    def __post_init__(self) -> None:
        if self.knowledge_time is None:
            object.__setattr__(self, "knowledge_time", self.as_of)

    def event_visible(self, event_time: datetime) -> bool:
        """该数据行所描述的时刻是否在 as_of 之前（含边界）。"""
        return event_time <= self.as_of

    def published_visible(self, published_at: datetime) -> bool:
        """该数据行的公布时刻是否在当前知识时间之前（含边界）——防未来数据。"""
        knowledge_time = self.knowledge_time or self.as_of
        return published_at <= knowledge_time

    def row_visible(self, event_time: datetime, published_at: datetime) -> bool:
        """两点同时满足才可见（基本面/成分/因子类数据的完整校验）。"""
        return self.event_visible(event_time) and self.published_visible(published_at)
