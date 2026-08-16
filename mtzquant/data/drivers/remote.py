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
