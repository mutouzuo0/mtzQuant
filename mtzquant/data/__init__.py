# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 00:44:00
# @update_time        : 2026/08/16 21:59:08
# @description : 数据层统一出口：SourceDriver→DataNormalizer→Provider 三段式管道（设计 3）

"""数据层统一出口（设计 3）：三段式管道 SourceDriver→DataNormalizer→MarketDataProvider。

导入本包即注册 data.driver，数据/缓存/日历/主数据等组件均可从本包直接引用。
import-linter 契约: data 层禁止 import mtzquant.engine（品种档案由调用侧注入）。
"""

from __future__ import annotations

from mtzquant.data.cache import CacheStats, DataCache, cache_key
from mtzquant.data.calendar import TradeCalendar
from mtzquant.data.drivers import CsvSourceDriver, SourceDriver, create_driver, register_driver
from mtzquant.data.master import InstrumentRow, MasterStore
from mtzquant.data.normalizer import DataNormalizer, LimitMapProvider, to_ms_index
from mtzquant.data.provider import BAR_DTYPE, Bar, MarketDataProvider

__all__ = [
    "BAR_DTYPE",
    "Bar",
    "CacheStats",
    "CsvSourceDriver",
    "DataCache",
    "DataNormalizer",
    "InstrumentRow",
    "LimitMapProvider",
    "MarketDataProvider",
    "MasterStore",
    "SourceDriver",
    "TradeCalendar",
    "cache_key",
    "create_driver",
    "register_driver",
    "to_ms_index",
]
