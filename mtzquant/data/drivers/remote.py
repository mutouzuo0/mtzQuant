# coding:utf-8
# @author      : 木头左
# @create_time        : 2026/08/16 14:55:00
# @update_time        : 2026/08/16 21:59:08
# @description : O4 远程源协议 + 注册表（3.2 下载侧投影）——RemoteKlineSource

"""远程源协议（设计 3.2 的下载侧投影）与注册表。

`RemoteKlineSource` 是数据获取层的统一落点（3.9 增量下载的「源」）:
- `fetch_kline(code, start, end, instrument_type)` → **tushare 源格式**原始列
  （ts_code/trade_date/open/high/low/close/vol/amount, 3.5）——落盘保持「归一前原始」,
  读时经 DataNormalizer 归一（3.12 raw 精神）;
- `fetch_master(instrument_type)` → 主数据原始列（stock_basic/fund_basic 字段, 3.11）。

实现: tushare（token 优先级 env > secrets.json）/ akshare。多源顺序 fallback 由
DataFetcher 编排（O5）; 每源限流走 RateLimitController（O4）。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class RemoteKlineSource(Protocol):
    """远程行情/主数据源协议（3.2 下载侧投影）。"""

    name: str

    def fetch_kline(
        self, code: str, start: date, end: date, *, instrument_type: str
    ) -> pd.DataFrame:
        """拉取日线; 返回 tushare 源格式原始列（3.5）。"""
        ...

    def fetch_master(self, instrument_type: str | None = None) -> pd.DataFrame:
        """拉取主数据（stock/fund 全量, 3.11）; 返回源原始列。"""
        ...


@runtime_checkable
class RemoteFundamentalSource(Protocol):
    """远程基本面/成分源协议（3.13 PIT 四时间, M3-R1 扩展）。

    实现约定: 返回**源原始列**（ts_code/ann_date/end_date/... 保持 tushare 命名）,
    落盘前由 DataFetcher 校验（R2）; 三字段 PIT 化由 FundamentalsStore 查询期完成:
      fina_indicator: event_time=end_date(报告期), published_at=ann_date(披露日)
      daily_basic:    event_time=trade_date,      published_at=trade_date
    """

    name: str

    def fetch_fina_indicator(self, code: str, start: date, end: date) -> pd.DataFrame:
        """拉取财务指标（净利润/营收同比等最小集, 按 ann_date 区间）。"""
        ...

    def fetch_daily_basic(self, code: str, start: date, end: date) -> pd.DataFrame:
        """拉取每日估值（pe/pb/市值, 按 trade_date 区间）。"""
        ...

    def fetch_index_constituents(self, index_code: str, trade_date: date) -> pd.DataFrame:
        """拉取指数成分快照（某交易日成分 + 权重 + 入/出日期区间, 3.13）。"""
        ...


# 注册表: name → 工厂（延迟 import, 可选依赖 download 组）
SOURCE_REGISTRY: dict[str, type] = {}


def register_source(name: str, factory: type) -> None:
    SOURCE_REGISTRY[name] = factory


def get_source(name: str, **kwargs: object) -> RemoteKlineSource:
    """按名构造源（延迟 import + 惰性注册; 未知源结构化报错）。

    注册惰性化避免循环 import: tushare_driver ↔ akshare_driver 互引本模块,
    若模块级 `_register_defaults()` 会形成 import 期环。
    """
    from mtzquant.core.errors import MtzQuantError

    _register_defaults()  # 幂等; 仅在未注册时补全默认两源
    if name not in SOURCE_REGISTRY:
        raise MtzQuantError(
            f"未知远程源 {name!r}", stage="remote", hint=f"可选: {sorted(SOURCE_REGISTRY)}"
        )
    obj = SOURCE_REGISTRY[name](**kwargs)
    if not isinstance(obj, RemoteKlineSource):
        raise MtzQuantError(f"源 {name!r} 未实现 RemoteKlineSource 协议", stage="remote")
    return obj


def _register_defaults() -> None:
    from mtzquant.data.drivers.akshare_driver import AkshareSource
    from mtzquant.data.drivers.tushare_driver import TushareSource

    SOURCE_REGISTRY.setdefault("tushare", TushareSource)
    SOURCE_REGISTRY.setdefault("akshare", AkshareSource)


# ---- 基本面/成分源注册表（M3-R1; 惰性注册, 同 kline 源解耦）----
FUNDAMENTAL_SOURCE_REGISTRY: dict[str, type] = {}


def register_fundamental_source(name: str, factory: type) -> None:
    FUNDAMENTAL_SOURCE_REGISTRY[name] = factory


def get_fundamental_source(name: str, **kwargs: object) -> RemoteFundamentalSource:
    """按名构造基本面源（延迟 import; 未知源结构化报错）。"""
    from mtzquant.core.errors import MtzQuantError

    if name not in FUNDAMENTAL_SOURCE_REGISTRY:
        raise MtzQuantError(
            f"未知基本面源 {name!r}",
            stage="remote",
            hint=f"可选: {sorted(FUNDAMENTAL_SOURCE_REGISTRY) or ['tushare']}",
        )
    obj = FUNDAMENTAL_SOURCE_REGISTRY[name](**kwargs)
    if not isinstance(obj, RemoteFundamentalSource):
        raise MtzQuantError(f"源 {name!r} 未实现 RemoteFundamentalSource 协议", stage="remote")
    return obj
