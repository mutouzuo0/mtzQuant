# coding:utf-8
# @author      : 木头左
# @date        : 2026/08/15 22:09:30
# @description : 核心类型与四时间模型（设计 3.3 / 3.13）

"""核心类型与四时间模型（设计 3.3 / 3.13）。

本模块为纯数据/枚举，无 I/O 依赖，是数据层与引擎层的公共词汇。

内部 K 线统一格式（设计 3.3 契约）:
    index = dt（UTC+8 毫秒时间戳）
    columns = KLINE_COLUMNS
时间约定: 日线时间戳 = 交易日 15:00 收盘时刻；分钟线 = bar 结束时刻。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum

from mtzquant.core.errors import MtzQuantError


class Frequency(StrEnum):
    """K 线频率（与 DataType 正交；3.3）。"""

    D1 = "1d"
    M1 = "1m"
    M5 = "5m"  # v1: 由 1m resample 得到并缓存
    # 预留: M3 = "3m" / M15 = "15m" / M30 = "30m" / M60 = "60m" / TICK = "tick"


class InstrumentType(StrEnum):
    """证券品种（决定 InstrumentProfile 交易规则；3.3）。"""

    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"  # 基准/对比用，不可交易
    CONVERTIBLE_BOND = "convertible_bond"  # 预留
    FUTURES = "futures"  # 预留
    OPTION = "option"  # 预留


class DataType(StrEnum):
    """数据类型（与 Frequency 正交；3.3）。"""

    KLINE = "kline"
    TICK = "tick"  # 预留
    TRADE = "trade"  # 预留（逐笔成交）
    ORDER = "order"  # 预留（逐笔委托）
    DEPTH = "depth"  # 预留（L2 盘口）


class AdjustMode(StrEnum):
    """复权模式（设计 3.14：复权价仅用于指标研究，绝不作为撮合/记账价格）。"""

    NONE = "none"
    FORWARD = "forward"  # 前复权
    BACKWARD = "backward"  # 后复权


# 内部 K 线列契约（设计 3.3；列序即契约，DataNormalizer 输出 / 引擎消费唯一认此格式）
KLINE_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "pre_close",
    "suspended",
    "limit_up",
    "limit_down",
)

# 时间约定常量（设计 3.3：从源头避免"开盘事件读到当日收盘价"的未来数据）
DAILY_BAR_TIME: time = time(15, 0)


@dataclass(frozen=True)
class TimeModel:
    """四时间模型（设计 3.13）——所有数据行/所有查询统一携带。

    字段:
        event_time    数据所描述的市场时刻（例: 2023Q3 财报 → 报告期 2023-09-30）
        published_at  数据正式公布时刻（例: 披露日 2023-10-28）
        available_at  本系统实际可取得时刻（>= published_at，如数据源次日同步；默认=published_at）
        ingested_at   数据进入本地时刻（下载时间，审计/排查用，可缺省）
    约束: published_at >= event_time；available_at >= published_at。
    行情类: event_time == published_at（盘中即时产生）→ 单参即可。
    """

    event_time: datetime
    published_at: datetime
    available_at: datetime | None = None
    ingested_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.published_at < self.event_time:
            raise MtzQuantError(
                "published_at 不能早于 event_time",
                stage="time_model",
                hint="基本面类数据：published_at 为披露日、event_time 为报告期",
            )
        available_at = self.available_at
        if available_at is None:
            available_at = self.published_at
            object.__setattr__(self, "available_at", available_at)
        if available_at < self.published_at:
            raise MtzQuantError(
                "available_at 不能早于 published_at",
                stage="time_model",
                hint="available_at 表示本系统实际可取得时刻，>= 正式公布时刻",
            )
